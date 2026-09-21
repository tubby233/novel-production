"""检查对话逻辑。

每次检查都使用**全新**的独立对话：
  system = 检查提示词（用户提供，默认为空）
  首条 user = 检查消息模板填充本章正文后的文本

检查对话与生成对话完全隔离，message history 互不影响。
程序**不判断**检查结果是好是坏，只把返回文本原样交给界面展示与用户编辑。
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
    Conversation,
    Message,
    StreamDelta,
    ThinkingConfig,
    Usage,
)


@dataclass
class CheckOutcome:
    ok: bool = False
    text: str = ""
    reasoning: str = ""
    usage: Usage = field(default_factory=Usage)
    error_text: str = ""
    error_kind: str = ""
    #: True = 用户主动停止（不是错误，界面不当作报错处理）
    cancelled: bool = False


class ChapterChecker:
    def __init__(self, client: DeepSeekClient, cfg: AppConfig) -> None:
        self.client = client
        self.cfg = cfg

    def apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------ #
    def new_conversation(self, chapter: Chapter, thinking: ThinkingConfig) -> Conversation:
        """创建一个全新的检查对话（替换旧的），并写入检查提示词。"""
        conv = Conversation.create(chapter.chapter_id, "check", thinking)
        system_prompt = self.cfg.prompts.system_check
        if system_prompt.strip():
            conv.append(Message(role="system", content=system_prompt, source="system"))
        chapter.check_conv = conv
        return conv

    def build_check_message(self, content: str, *, word_count: int | None = None) -> str:
        """构造检查消息。字数由**程序**统计后填入 ``{word_count}``。

        需求：字数在程序内计算，作为参数发给检查对话，不再由检查对话统计。
        """
        if word_count is None:
            word_count = MT.count_content_chars(content)
        return MT.build_check_user_msg(
            content,
            word_count=word_count,
            text=self.cfg.template(MT.TemplateKind.CHECK),
        )

    # ------------------------------------------------------------------ #
    async def run(
        self,
        chapter: Chapter,
        content: str,
        *,
        thinking: ThinkingConfig,
        on_delta: Callable[[StreamDelta], None] | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> CheckOutcome:
        """新建检查对话并执行一次检查。"""
        conv = self.new_conversation(chapter, thinking)
        # 字数在程序里算好，随消息一起发给检查对话（AI 不需要也不会去数）
        word_count = MT.count_content_chars(content)
        conv.append(
            Message(
                role="user",
                content=self.build_check_message(content, word_count=word_count),
                source="check",
            )
        )
        conv.reset_stream()
        conv.status = "running"
        conv.last_error = ""

        payload = [ApiMessage(role=m.role, content=m.content) for m in conv.messages]  # type: ignore[arg-type]

        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = Usage()
        outcome = CheckOutcome()

        try:
            async for delta in self.client.stream_chat(
                payload,
                thinking=conv.thinking,
                sampling=self.cfg.sampling,
                on_event=on_event,
                cancel_event=cancel_event,
            ):
                if delta.kind == "content":
                    text_parts.append(delta.text)
                    conv.stream_content += delta.text
                elif delta.kind == "reasoning":
                    reasoning_parts.append(delta.text)
                    conv.stream_reasoning += delta.text
                elif delta.kind == "usage" and delta.usage is not None:
                    usage = delta.usage
                if on_delta is not None:
                    on_delta(delta)

            text = "".join(text_parts)
            conv.append(
                Message(
                    role="assistant",
                    content=text,
                    reasoning="".join(reasoning_parts),
                    source="assistant",
                    usage=usage,
                )
            )
            conv.status = "done"
            outcome.ok = True
            outcome.text = text
            outcome.reasoning = "".join(reasoning_parts)
            outcome.usage = usage
            return outcome

        except TaskCancelled as exc:
            partial = "".join(text_parts)
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
            outcome.text = partial
            outcome.error_text = str(exc)
            return outcome

        except ApiError as exc:
            partial = "".join(text_parts)
            conv.status = "error"
            conv.last_error = exc.user_text()
            outcome.error_text = exc.user_text()
            outcome.error_kind = str(exc.kind)
            outcome.text = partial
            return outcome

        except asyncio.CancelledError:
            # 与 TaskCancelled 一致：保留已收到的部分内容，标记为"用户停止"而非错误
            partial = "".join(text_parts)
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
            outcome.text = partial
            outcome.reasoning = "".join(reasoning_parts)
            outcome.usage = usage
            outcome.error_text = "任务已被用户停止。"
            return outcome

        except Exception as exc:  # pragma: no cover - 兜底
            conv.status = "error"
            conv.last_error = f"未预期的错误：{type(exc).__name__}: {exc}"
            outcome.error_text = conv.last_error
            return outcome
        finally:
            conv.reset_stream()

    # ------------------------------------------------------------------ #
    async def continue_manual_chat(
        self,
        chapter: Chapter,
        user_message: str,
        *,
        thinking: ThinkingConfig,
        on_delta: Callable[[StreamDelta], None] | None = None,
        on_event: Callable[[StreamEvent], None] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> CheckOutcome:
        """在**已有的**检查对话里继续手动对话（不走模板）。"""
        conv = chapter.check_conv
        if conv is None:
            return CheckOutcome(ok=False, error_text="当前章节还没有检查对话。")

        conv.append(Message(role="user", content=user_message, source="user"))
        conv.reset_stream()
        conv.status = "running"

        payload = [ApiMessage(role=m.role, content=m.content) for m in conv.messages]  # type: ignore[arg-type]
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage = Usage()
        outcome = CheckOutcome()

        try:
            async for delta in self.client.stream_chat(
                payload,
                thinking=conv.thinking,
                sampling=self.cfg.sampling,
                on_event=on_event,
                cancel_event=cancel_event,
            ):
                if delta.kind == "content":
                    text_parts.append(delta.text)
                    conv.stream_content += delta.text
                elif delta.kind == "reasoning":
                    reasoning_parts.append(delta.text)
                    conv.stream_reasoning += delta.text
                elif delta.kind == "usage" and delta.usage is not None:
                    usage = delta.usage
                if on_delta is not None:
                    on_delta(delta)

            text = "".join(text_parts)
            conv.append(
                Message(role="assistant", content=text, reasoning="".join(reasoning_parts),
                        source="assistant", usage=usage)
            )
            conv.status = "done"
            outcome.ok = True
            outcome.text = text
            outcome.usage = usage
            return outcome
        except TaskCancelled as exc:
            partial = "".join(text_parts)
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
            outcome.text = partial
            outcome.reasoning = "".join(reasoning_parts)
            outcome.usage = usage
            outcome.error_text = str(exc)
            return outcome
        except ApiError as exc:
            conv.status = "error"
            conv.last_error = exc.user_text()
            outcome.error_text = exc.user_text()
            outcome.text = "".join(text_parts)
            return outcome
        except asyncio.CancelledError:
            partial = "".join(text_parts)
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
            outcome.text = partial
            outcome.reasoning = "".join(reasoning_parts)
            outcome.usage = usage
            outcome.error_text = "任务已被用户停止。"
            return outcome
        except Exception as exc:  # pragma: no cover
            conv.status = "error"
            conv.last_error = f"未预期的错误：{type(exc).__name__}: {exc}"
            outcome.error_text = conv.last_error
            return outcome
        finally:
            conv.reset_stream()
