"""异步 API 封装（OpenAI 兼容 / DeepSeek）。

职责：
1. 把"思考开关 + 思考强度 + 采样参数"规范化成真实请求参数（含文档规定的约束处理）。
2. 流式调用，并把 ``reasoning_content`` 与 ``content`` 拆成统一增量事件。
3. 把 SDK 抛出的异常翻译成带中文说明的 :class:`ApiError`。

文档依据：
  思考模式          https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
  Chat Completions  https://api-docs.deepseek.com/zh-cn/api/create-chat-completion
  错误码            https://api-docs.deepseek.com/zh-cn/quick_start/error_codes
  限速与隔离        https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit

重要约定（全项目）
------------------
* 请求**绝不携带 tools**，因此历史轮次的 ``reasoning_content`` 无需回传，
  客户端只发送 role + content（见 :class:`ApiMessage` 的类型约束）。
* 程序不解析模型输出的语义；这里只负责把增量拼成字符串。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Literal, Sequence

from .config import (
    CHARS_PER_TOKEN,
    ApiConfig,
    SamplingConfig,
    estimate_tokens,
    model_limits,
)
from .errors import ApiError, ApiErrorKind, TaskCancelled
from .models import CompletionResult, StreamDelta, ThinkingConfig, Usage, normalize_effort

#: 思考模式下被 API 忽略的参数（DeepSeek 文档：设置不报错，但不会生效）
#: 注意：frequency_penalty / presence_penalty 已被官方标记为 deprecated，程序完全不提供、也不发送。
IGNORED_IN_THINKING = ("temperature",)

#: 思考强度为 max 时，服务端默认输出上限（未显式设置 max_tokens 时）
DEFAULT_MAX_TOKENS_BY_EFFORT = {"max": 128_000, "default": 64_000}

#: 预算保护：为模型输出预留的最小空间（token）
MIN_OUTPUT_BUDGET = 512


@dataclass(frozen=True)
class ApiMessage:
    """发往 API 的消息。**类型上就无法携带 reasoning_content。**

    本项目不带 tools 参数，DeepSeek 文档明确说明此场景下思维链无需回传、
    即使传入也会被忽略，因此历史消息只保留 role + content。
    """

    role: Literal["system", "user", "assistant"]
    content: str

    def to_payload(self) -> dict:
        return {"role": self.role, "content": self.content}


@dataclass
class NormalizedCall:
    """规范化后的真实入参。"""

    payload: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        """给"本次生效参数预览"用的一行描述。"""
        return "；".join(self.notes) if self.notes else "参数无调整，按配置原样发送。"


@dataclass
class StreamEvent:
    """引擎/界面可订阅的额外事件（重试提示等）。"""

    kind: Literal["info", "retry", "note"]
    text: str = ""


# --------------------------------------------------------------------------- #
# 异常翻译
# --------------------------------------------------------------------------- #
def classify_exception(exc: BaseException) -> ApiError:
    """把任意异常翻译成 ApiError。"""
    if isinstance(exc, ApiError):
        return exc
    if isinstance(exc, asyncio.CancelledError):
        return ApiError(ApiErrorKind.CANCELLED, "任务已被用户停止。", retryable=False)

    # openai SDK 的异常类型（延迟导入，避免没有安装时 import 失败影响其他模块测试）
    try:  # pragma: no cover - 依赖具体 SDK 版本
        import openai
        import httpx
    except Exception:  # pragma: no cover
        openai = None  # type: ignore[assignment]
        httpx = None  # type: ignore[assignment]

    if openai is not None:
        if isinstance(exc, openai.AuthenticationError):
            return ApiError(ApiErrorKind.AUTH, "API 密钥无效或没有访问权限（401/403）。", status_code=401, raw=str(exc))
        if isinstance(exc, openai.PermissionDeniedError):
            return ApiError(ApiErrorKind.AUTH, "当前密钥没有访问该模型的权限（403）。", status_code=403, raw=str(exc))
        if isinstance(exc, openai.RateLimitError):
            return ApiError(ApiErrorKind.RATE_LIMIT, "触发限速或并发上限（429）。", status_code=429, raw=str(exc))
        if isinstance(exc, openai.APITimeoutError):
            return ApiError(ApiErrorKind.TIMEOUT, "请求超时。", raw=str(exc))
        if isinstance(exc, openai.APIConnectionError):
            return ApiError(ApiErrorKind.CONNECTION, "无法连接 API 服务。", raw=str(exc))
        if isinstance(exc, openai.BadRequestError):
            return ApiError(ApiErrorKind.BAD_REQUEST, "请求参数被服务端拒绝（400）。", status_code=400, raw=str(exc), retryable=False)
        if isinstance(exc, openai.NotFoundError):
            return ApiError(ApiErrorKind.BAD_REQUEST, "接口或模型不存在（404）。", status_code=404, raw=str(exc), retryable=False)
        if isinstance(exc, openai.InternalServerError):
            return ApiError(ApiErrorKind.SERVER, "服务端内部错误（5xx）。", status_code=500, raw=str(exc))
        if isinstance(exc, openai.APIStatusError):
            status = getattr(exc, "status_code", None)
            kind = ApiErrorKind.SERVER if (status or 0) >= 500 else ApiErrorKind.BAD_REQUEST
            return ApiError(kind, f"服务端返回状态码 {status}。", status_code=status, raw=str(exc))

    if httpx is not None:
        if isinstance(exc, httpx.TimeoutException):
            return ApiError(ApiErrorKind.TIMEOUT, "请求超时。", raw=str(exc))
        if isinstance(exc, httpx.TransportError):
            return ApiError(ApiErrorKind.CONNECTION, "网络连接失败。", raw=str(exc))

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return ApiError(ApiErrorKind.CONNECTION, "网络或系统 IO 异常。", raw=str(exc))

    return ApiError(ApiErrorKind.UNKNOWN, f"未预期的错误：{type(exc).__name__}", raw=str(exc))


# --------------------------------------------------------------------------- #
# 客户端
# --------------------------------------------------------------------------- #
class DeepSeekClient:
    """异步客户端。一个实例对应一份 API 配置，配置变更时重建。"""

    def __init__(
        self,
        api: ApiConfig,
        sampling: SamplingConfig,
        *,
        max_retries: int = 2,
        stream_include_usage: bool = True,
    ) -> None:
        self.api = api
        self.sampling = sampling
        self.max_retries = max(0, int(max_retries))
        self.stream_include_usage = stream_include_usage
        self.calls = 0                    # 已发起的请求计数（用于状态栏/统计）
        self.total_usage = Usage()
        self._client: Any = None

    # ---------------- 生命周期 ---------------- #
    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if not self.api.api_key.strip():
            raise ApiError(ApiErrorKind.AUTH, "尚未配置 API 密钥。", retryable=False)
        try:
            import httpx
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover
            raise ApiError(
                ApiErrorKind.UNKNOWN,
                "缺少依赖：请先执行 pip install -r requirements.txt",
                raw=str(exc),
                retryable=False,
            ) from exc

        timeout = httpx.Timeout(
            connect=30.0,
            read=float(max(30, self.api.timeout_seconds)),
            write=60.0,
            pool=30.0,
        )
        self._client = AsyncOpenAI(
            api_key=self.api.api_key,
            base_url=self.api.base_url.rstrip("/"),
            timeout=timeout,
            max_retries=0,   # 重试由本模块控制，便于把重试状态显示到界面
        )
        return self._client

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.close()

    async def __aenter__(self) -> "DeepSeekClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.aclose()

    def apply_config(self, api: ApiConfig, sampling: SamplingConfig) -> None:
        """热更新配置。地址/密钥变化时关闭旧连接。"""
        changed = (api.api_key != self.api.api_key) or (api.base_url != self.api.base_url)
        self.api = api
        self.sampling = sampling
        if changed and self._client is not None:
            old, self._client = self._client, None
            with contextlib.suppress(Exception):
                asyncio.get_event_loop().create_task(old.close())

    # ---------------- 参数规范化 ---------------- #
    def normalize_params(
        self,
        thinking: ThinkingConfig,
        sampling: SamplingConfig | None = None,
        *,
        prompt_chars: int = 0,
    ) -> NormalizedCall:
        """把用户配置变成真实请求参数，并记录所有被程序自动调整的地方。

        文档规则（程序自动遵守，界面上只做提示）：
          * 思考模式启用：temperature 不生效；不带 tools 时 reasoning_content 无需回传。
          * top_p 在思考模式生效但下限 0.95；非思考模式恒为 1.0。
          * extra_body={"thinking": {"type": "enabled"|"disabled"}}。
          * reasoning_effort: low / high / max，仅思考模式生效。
          * frequency_penalty / presence_penalty 已被官方弃用（传入无任何效果），本程序不发送。
        """
        sampling = sampling or self.sampling
        thinking = thinking.normalized()
        notes: list[str] = []
        limits = model_limits(self.api.model)

        payload: dict[str, Any] = {
            "model": self.api.model,
            "stream": True,
        }

        # ---- thinking 开关与强度 ---- #
        if thinking.enabled:
            payload["extra_body"] = {"thinking": {"type": "enabled"}}
            payload["reasoning_effort"] = normalize_effort(thinking.effort)
            notes.append(f"thinking=enabled，reasoning_effort={payload['reasoning_effort']}")
        else:
            payload["extra_body"] = {"thinking": {"type": "disabled"}}
            notes.append("thinking=disabled（不发送 reasoning_effort）")

        # ---- user_id（可选，放在 extra_body）---- #
        if self.api.user_id.strip():
            payload["extra_body"]["user_id"] = self.api.user_id.strip()

        # ---- top_p ---- #
        if thinking.enabled:
            effective_top_p = max(0.95, float(sampling.top_p))
            if effective_top_p != float(sampling.top_p):
                notes.append(f"top_p 已由 {sampling.top_p:g} 抬升至 0.95（思考模式下限）")
            else:
                notes.append(f"top_p={effective_top_p:g}（思考模式生效）")
        else:
            effective_top_p = 1.0
            if abs(float(sampling.top_p) - 1.0) > 1e-9:
                notes.append(f"top_p 被忽略并固定为 1.0（非思考模式），你的设置 {sampling.top_p:g} 未生效")
            else:
                notes.append("top_p=1.0（非思考模式固定值）")
        payload["top_p"] = effective_top_p

        # ---- 采样参数 ---- #
        if thinking.enabled:
            ignored = "、".join(IGNORED_IN_THINKING)
            notes.append(f"{ignored} 在思考模式下不生效，已从请求中剔除")
        else:
            payload["temperature"] = float(sampling.temperature)
            notes.append(f"temperature={sampling.temperature:g}")
        notes.append("frequency_penalty / presence_penalty 已被 DeepSeek 弃用，程序不发送")

        # ---- max_tokens 与上下文预算保护 ---- #
        max_tokens = int(sampling.max_tokens)
        max_tokens = max(1, min(max_tokens, limits.max_output))
        if max_tokens != int(sampling.max_tokens):
            notes.append(f"max_tokens 已收敛到模型上限 {limits.max_output}")

        if prompt_chars > 0:
            prompt_tokens = max(1, int(prompt_chars / CHARS_PER_TOKEN))
            budget = limits.context_window - prompt_tokens
            if budget < MIN_OUTPUT_BUDGET:
                raise ApiError(
                    ApiErrorKind.BAD_REQUEST,
                    f"当前对话的上下文已接近模型窗口上限（约 {prompt_tokens} tokens 输入，"
                    f"窗口 {limits.context_window}）。请清空对话或减少手动追加的消息后重试。",
                    retryable=False,
                )
            if max_tokens > budget:
                notes.append(
                    f"max_tokens 已由 {max_tokens} 下调至 {budget}，以满足 "
                    f"{limits.context_window:,} tokens 上下文窗口（输入 + 输出总长受限）"
                )
                max_tokens = budget

        payload["max_tokens"] = max_tokens
        notes.append(f"max_tokens={max_tokens}")

        if self.stream_include_usage:
            payload["stream_options"] = {"include_usage": True}

        return NormalizedCall(payload=payload, notes=notes)

    # ---------------- 请求 ---------------- #
    async def stream_chat(
        self,
        messages: Sequence[ApiMessage],
        *,
        thinking: ThinkingConfig,
        sampling: SamplingConfig | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        cancel_event: "asyncio.Event | None" = None,
    ) -> AsyncIterator[StreamDelta]:
        """流式调用。逐条 yield :class:`StreamDelta`。

        * ``delta.reasoning_content`` -> StreamDelta(kind="reasoning")
        * ``delta.content``           -> StreamDelta(kind="content")
        * 最后一个块的 usage          -> StreamDelta(kind="usage")
        * 结束                        -> StreamDelta(kind="done")

        单个 chunk 的解析异常只记录并跳过，绝不中断整个流。
        """
        prompt_chars = sum(len(m.content) for m in messages)
        call = self.normalize_params(thinking, sampling, prompt_chars=prompt_chars)
        payload = dict(call.payload)
        payload["messages"] = [m.to_payload() for m in messages]

        def emit_event(kind: str, text: str) -> None:
            if on_event is not None:
                with contextlib.suppress(Exception):
                    on_event(StreamEvent(kind=kind, text=text))  # type: ignore[arg-type]

        for note in call.notes:
            emit_event("note", note)

        attempt = 0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise TaskCancelled()
            stream = None
            try:
                client = self._ensure_client()
                self.calls += 1
                stream = await client.chat.completions.create(**payload)
                async for delta in self._iterate_stream(stream):
                    if cancel_event is not None and cancel_event.is_set():
                        raise TaskCancelled()
                    if delta.kind == "usage" and delta.usage is not None:
                        self.total_usage = self.total_usage.add(delta.usage)
                    yield delta
                yield StreamDelta(kind="done")
                return
            except asyncio.CancelledError:
                raise TaskCancelled()
            except BaseException as exc:  # noqa: BLE001 - 统一翻译
                error = classify_exception(exc)
                if error.kind == ApiErrorKind.CANCELLED:
                    raise TaskCancelled() from exc
                if error.retryable and attempt < self.max_retries:
                    attempt += 1
                    delay = min(30.0, (2 ** attempt) + random.uniform(0, 1.5))
                    emit_event(
                        "retry",
                        f"{error.message} 正在第 {attempt}/{self.max_retries} 次重试（{delay:.1f} 秒后）…",
                    )
                    await asyncio.sleep(delay)
                    continue
                raise error from exc
            finally:
                if stream is not None:
                    with contextlib.suppress(Exception):
                        await stream.close()

    async def _iterate_stream(self, stream: Any) -> AsyncIterator[StreamDelta]:
        """把一个 SDK 流对象拆成增量事件。"""
        async for chunk in stream:
            usage = self._extract_usage(chunk)
            if usage is not None:
                yield StreamDelta(kind="usage", usage=usage)

            choices = getattr(chunk, "choices", None)
            if not choices:
                continue
            for choice in choices:
                delta = getattr(choice, "delta", None)
                if delta is None:
                    continue
                reasoning = self._safe_text(delta, "reasoning_content")
                if reasoning:
                    yield StreamDelta(kind="reasoning", text=reasoning)
                content = self._safe_text(delta, "content")
                if content:
                    yield StreamDelta(kind="content", text=content)

    @staticmethod
    def _safe_text(container: Any, attribute: str) -> str:
        """容错读取一个可能不存在的字符串字段。"""
        try:
            value = getattr(container, attribute, None)
            if value is None and isinstance(container, dict):
                value = container.get(attribute)
            if value is None:
                return ""
            return value if isinstance(value, str) else str(value)
        except Exception:
            return ""

    @staticmethod
    def _extract_usage(chunk: Any) -> Usage | None:
        raw = getattr(chunk, "usage", None)
        if raw is None:
            return None
        try:
            details = getattr(raw, "completion_tokens_details", None)
            prompt_details = getattr(raw, "prompt_tokens_details", None)
            return Usage(
                prompt_tokens=int(getattr(raw, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(raw, "completion_tokens", 0) or 0),
                reasoning_tokens=int(getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
                total_tokens=int(getattr(raw, "total_tokens", 0) or 0),
                cache_hit_tokens=int(getattr(prompt_details, "prompt_cache_hit_tokens", 0) or 0)
                if prompt_details
                else int(getattr(raw, "prompt_cache_hit_tokens", 0) or 0),
            )
        except Exception:
            return None

    async def complete(
        self,
        messages: Sequence[ApiMessage],
        *,
        thinking: ThinkingConfig,
        sampling: SamplingConfig | None = None,
        on_delta: Callable[[StreamDelta], None] | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        cancel_event: "asyncio.Event | None" = None,
    ) -> CompletionResult:
        """把流式调用收集成完整结果。

        **注意**：即使调用方只想要完整结果，也一律走流式路径——
        这样"流式/非流式两条路径解析逻辑一致"，避免出现只在某一条路径上的
        reasoning_content 解析差异。
        """
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = Usage()
        finish_reason = ""

        async for delta in self.stream_chat(
            messages,
            thinking=thinking,
            sampling=sampling,
            on_event=on_event,
            cancel_event=cancel_event,
        ):
            if on_delta is not None:
                with contextlib.suppress(Exception):
                    on_delta(delta)
            if delta.kind == "content":
                content_parts.append(delta.text)
            elif delta.kind == "reasoning":
                reasoning_parts.append(delta.text)
            elif delta.kind == "usage" and delta.usage is not None:
                usage = delta.usage

        return CompletionResult(
            content="".join(content_parts),
            reasoning="".join(reasoning_parts),
            usage=usage,
            finish_reason=finish_reason,
        )

    # ---------------- 连接测试 ---------------- #
    async def test_connection(self) -> tuple[bool, str]:
        """发一条极短请求验证配置。返回 (是否成功, 说明文本)。"""
        limits = model_limits(self.api.model)
        try:
            client = self._ensure_client()
        except ApiError as exc:
            return False, exc.user_text()

        # 优先用 /models 快速验证密钥与连通性
        try:
            models = await client.models.list()
            ids = [getattr(m, "id", "") for m in getattr(models, "data", []) or []]
            if ids and self.api.model not in ids:
                return True, (
                    f"连接正常，但模型列表中未出现 “{self.api.model}”。\n"
                    f"服务端可用模型：{', '.join(ids)}\n"
                    "如确认名称正确可忽略此提示。"
                )
        except Exception:
            pass  # 部分中转服务不提供 /models，回落到聊天接口测试

        messages = [
            ApiMessage(role="system", content="你是一个连通性测试助手，请只回复两个字：正常。"),
            ApiMessage(role="user", content="请回复：正常"),
        ]
        thinking = ThinkingConfig(enabled=False, effort="low", show_reasoning=False)
        sampling = SamplingConfig(
            temperature=0.0, top_p=1.0, max_tokens=64, concurrency=1,
        )
        try:
            result = await self.complete(messages, thinking=thinking, sampling=sampling)
        except ApiError as exc:
            return False, exc.user_text()
        except Exception as exc:  # pragma: no cover
            return False, classify_exception(exc).user_text()

        return True, (
            "连接成功。\n"
            f"模型：{self.api.model}（{limits.display_name}）\n"
            f"上下文窗口：{limits.context_window:,} tokens｜单次输出上限：{limits.max_output:,} tokens\n"
            f"账号并发上限：{limits.concurrency}（官方文档值）\n"
            f"返回内容：{result.content.strip()[:80]}"
        )
