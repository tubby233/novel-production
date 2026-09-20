"""生成对话逻辑。

一个章节对应**一个**生成对话；多次重新生成会把新消息追加到同一对话中，
从而保留该章的多轮上下文（message history）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Callable

from . import message_templates as MT
from .api_client import ApiMessage, DeepSeekClient, StreamEvent
from .config import AppConfig
from .errors import ApiError, TaskCancelled
from .models import (
    Chapter,
    CompletionResult,
    Conversation,
    Message,
    StreamDelta,
    ThinkingConfig,
    Usage,
)


@dataclass
class GenerateOutcome:
    """一次生成任务的结果。状态变更由 engine 统一执行。

    注意：``cancelled=True`` 表示**用户主动停止**，这不是错误。
    界面据此不弹日志窗口、不写错误日志（见 engine._run_job / MainWindow 的信号处理）。
    """

    ok: bool = False
    content: str = ""
    reasoning: str = ""
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = ""
    error_text: str = ""
    error_kind: str = ""
    cancelled: bool = False
    interrupted: bool = False      # 被停止：内容可能不完整

    @property
    def truncated(self) -> bool:
        return self.finish_reason == "length"


class ChapterGenerator:
    def __init__(self, client: DeepSeekClient, cfg: AppConfig) -> None:
        self.client = client
        self.cfg = cfg

    def apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def ensure_conversation(self, chapter: Chapter, thinking: ThinkingConfig) -> Conversation:
        """取（或创建）该章的生成对话，并把 system 提示词写入新对话。"""
        if chapter.generate_conv is None:
            conv = Conversation.create(chapter.chapter_id, "generate", thinking)
            system_prompt = self.cfg.prompts.system_generate
            if system_prompt.strip():
                conv.append(
                    Message(role="system", content=system_prompt, source="system")
                )
            chapter.generate_conv = conv
        return chapter.generate_conv

    # ------------------------------------------------------------------ #
    async def run(
        self,
        chapter: Chapter,
        user_message: str,
        *,
        source: str,
        thinking: ThinkingConfig,
        on_delta: Callable[[StreamDelta], None] | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> GenerateOutcome:
        """发送一条 user 消息并等待完整正文。

        * user_message 由 message_templates 生成（或用户手动输入，此时 source="user"）。
        * 这里**不做**任何内容解析；返回的 content 原样交给 engine 保存。
        """
        conv = self.ensure_conversation(chapter, thinking)
        conv.thinking = ThinkingConfig(
            enabled=thinking.enabled,
            effort=thinking.effort,
            show_reasoning=thinking.show_reasoning,
        )
        conv.append(Message(role="user", content=user_message, source=source))
        conv.reset_stream()
        conv.status = "running"
        conv.last_error = ""

        payload = [ApiMessage(role=m.role, content=m.content) for m in conv.messages]  # type: ignore[arg-type]

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = Usage()
        finish_reason = ""
        outcome = GenerateOutcome()

        try:
            async for delta in self.client.stream_chat(
                payload,
                thinking=conv.thinking,
                sampling=self.cfg.sampling,
                on_event=on_event,
                cancel_event=cancel_event,
            ):
                if delta.kind == "content":
                    content_parts.append(delta.text)
                    conv.stream_content += delta.text
                elif delta.kind == "reasoning":
                    reasoning_parts.append(delta.text)
                    conv.stream_reasoning += delta.text
                elif delta.kind == "usage" and delta.usage is not None:
                    usage = delta.usage
                if on_delta is not None:
                    on_delta(delta)

            content = "".join(content_parts)
            reasoning = "".join(reasoning_parts)
            conv.append(
                Message(
                    role="assistant",
                    content=content,
                    reasoning=reasoning,
                    source="assistant",
                    usage=usage,
                )
            )
            conv.status = "done"
            outcome.ok = True
            outcome.content = content
            outcome.reasoning = reasoning
            outcome.usage = usage
            outcome.finish_reason = finish_reason
            return outcome

        except TaskCancelled as exc:
            partial = "".join(content_parts)
            if partial:
                conv.append(
                    Message(
                        role="assistant",
                        content=partial,
                        reasoning="".join(reasoning_parts),
                        source="assistant",
                        usage=usage,
                        interrupted=True,
                    )
                )
            conv.status = "cancelled"
            outcome.cancelled = True
            outcome.interrupted = True
            outcome.content = partial
            outcome.reasoning = "".join(reasoning_parts)
            outcome.usage = usage
            outcome.error_text = str(exc)
            return outcome

        except ApiError as exc:
            partial = "".join(content_parts)
            conv.status = "error"
            conv.last_error = exc.user_text()
            outcome.error_text = exc.user_text()
            outcome.error_kind = str(exc.kind)
            outcome.content = partial
            outcome.reasoning = "".join(reasoning_parts)
            outcome.usage = usage
            outcome.interrupted = bool(partial)
            return outcome

        except asyncio.CancelledError:
            # asyncio 层直接取消（stop_all 走的就是这条路径）：同样属于"用户主动停止"
            partial = "".join(content_parts)
            if partial:
                conv.append(
                    Message(
                        role="assistant",
                        content=partial,
                        reasoning="".join(reasoning_parts),
                        source="assistant",
                        usage=usage,
                        interrupted=True,
                    )
                )
            conv.status = "cancelled"
            outcome.cancelled = True
            outcome.interrupted = True
            outcome.content = partial
            outcome.reasoning = "".join(reasoning_parts)
            outcome.usage = usage
            outcome.error_text = "任务已被用户停止。"
            return outcome

        except Exception as exc:  # pragma: no cover - 兜底
            conv.status = "error"
            conv.last_error = f"未预期的错误：{type(exc).__name__}: {exc}"
            outcome.error_text = conv.last_error
            outcome.error_kind = "unknown"
            return outcome
        finally:
            conv.reset_stream()

    # ------------------------------------------------------------------ #
    def build_first_message(self, chapter: Chapter) -> str:
        """首次生成 / 重新生成用的模板消息。

        大纲取自 :meth:`Chapter.outline_for_generate`：优先用**独立保存的大纲快照**。
        这样即使用户在"生成对话"里点了"清空对话"（第一条 user 消息随之消失），
        再点"重新生成"也能用同一份大纲正常生成，而不是拿不到大纲。
        """
        return MT.build_first_generate_user_msg(
            chapter.outline_for_generate(),
            text=self.cfg.template(MT.TemplateKind.FIRST_GENERATE),
        )

    def build_regen_by_check_message(self, check_result: str) -> str:
        return MT.build_regen_by_check_user_msg(
            check_result, text=self.cfg.template(MT.TemplateKind.REGEN_BY_CHECK)
        )

    def build_manual_message(self, user_input: str) -> str:
        return MT.build_regen_manual_user_msg(
            user_input, text=self.cfg.template(MT.TemplateKind.REGEN_MANUAL)
        )

    # ------------------------------------------------------------------ #
    async def run_manual_chat(
        self,
        chapter: Chapter,
        user_message: str,
        *,
        thinking: ThinkingConfig,
        on_delta: Callable[[StreamDelta], None] | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> GenerateOutcome:
        """用户手动发送的消息：**不经模板**，直接作为 user message 发送。"""
        return await self.run(
            chapter,
            user_message,
            source="user",
            thinking=thinking,
            on_delta=on_delta,
            on_event=on_event,
            cancel_event=cancel_event,
        )
