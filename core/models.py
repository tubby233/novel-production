"""数据模型：章节、对话、消息、思考参数。

这里全部是纯 dataclass，不含任何 Qt 与网络依赖，方便单测与序列化。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


# --------------------------------------------------------------------------- #
# 章节状态机
# --------------------------------------------------------------------------- #
class ChapterState(StrEnum):
    """章节状态。StrEnum 的 value 直接可用于 JSON 持久化与信号传递。"""

    PENDING = "pending"                        # 待生成
    QUEUED = "queued"                          # 排队中（已提交，等待并发闸门）
    GENERATING = "generating"                  # 生成中
    TO_CHECK = "to_check"                      # 待检查
    CHECKING = "checking"                      # 检查中
    AWAITING_DECISION = "awaiting_decision"    # 待决策
    PASSED = "passed"                          # 合格
    FAILED = "failed"                          # 出错（可重试）

    @property
    def busy(self) -> bool:
        """是否处于"忙"状态：忙时所有会改写数据的操作都被互斥规则挡住。

        注意：**排队中不算忙**——任务还没开始跑，用户随时可以改主意或停止它；
        界面把 "queued" 单独当作"已提交、等待开始"来处理。
        """
        return self in (ChapterState.GENERATING, ChapterState.CHECKING)

    @property
    def queued_or_busy(self) -> bool:
        """已提交（排队中）或正在跑：此时不应重复提交同一章的任务。"""
        return self in (
            ChapterState.QUEUED,
            ChapterState.GENERATING,
            ChapterState.CHECKING,
        )


STATE_LABELS: dict[ChapterState, str] = {
    ChapterState.PENDING: "待生成",
    ChapterState.QUEUED: "排队中",
    ChapterState.GENERATING: "生成中",
    ChapterState.TO_CHECK: "待检查",
    ChapterState.CHECKING: "检查中",
    ChapterState.AWAITING_DECISION: "待决策",
    ChapterState.PASSED: "合格",
    ChapterState.FAILED: "出错",
}

#: 排队的任务被"停止全部/停止本章"取消时，需要回退到的状态由此推导：
#: queued 前记录在 ``Chapter.pre_queue_state`` 里，取不到时按下表兜底。
QUEUE_FALLBACK_STATE: dict[ChapterState, ChapterState] = {
    ChapterState.GENERATING: ChapterState.TO_CHECK,
    ChapterState.CHECKING: ChapterState.TO_CHECK,
    ChapterState.TO_CHECK: ChapterState.TO_CHECK,
    ChapterState.PENDING: ChapterState.PENDING,
    ChapterState.FAILED: ChapterState.FAILED,
    ChapterState.AWAITING_DECISION: ChapterState.AWAITING_DECISION,
    ChapterState.PASSED: ChapterState.PASSED,
    ChapterState.QUEUED: ChapterState.TO_CHECK,
}

#: 允许的状态迁移表。engine 是唯一执行者，任何不在表内的迁移都会被拒绝并记日志。
ALLOWED_TRANSITIONS: dict[ChapterState, frozenset[ChapterState]] = {
    ChapterState.PENDING: frozenset(
        {ChapterState.GENERATING, ChapterState.QUEUED, ChapterState.PENDING}
    ),
    ChapterState.QUEUED: frozenset(
        {
            ChapterState.GENERATING,
            ChapterState.CHECKING,
            ChapterState.TO_CHECK,
            ChapterState.PENDING,
            ChapterState.FAILED,
            ChapterState.AWAITING_DECISION,
            ChapterState.PASSED,
            ChapterState.QUEUED,
        }
    ),
    ChapterState.GENERATING: frozenset(
        {
            ChapterState.TO_CHECK,
            ChapterState.FAILED,
            ChapterState.PENDING,
            ChapterState.GENERATING,
        }
    ),
    ChapterState.TO_CHECK: frozenset(
        {
            ChapterState.CHECKING,
            ChapterState.GENERATING,
            ChapterState.QUEUED,
            ChapterState.FAILED,
        }
    ),
    ChapterState.CHECKING: frozenset(
        {ChapterState.AWAITING_DECISION, ChapterState.TO_CHECK, ChapterState.FAILED}
    ),
    ChapterState.AWAITING_DECISION: frozenset(
        {
            ChapterState.PASSED,
            ChapterState.GENERATING,
            ChapterState.CHECKING,
            ChapterState.QUEUED,
            ChapterState.AWAITING_DECISION,
        }
    ),
    ChapterState.PASSED: frozenset(
        {
            ChapterState.GENERATING,
            ChapterState.CHECKING,
            ChapterState.AWAITING_DECISION,
            ChapterState.QUEUED,
            ChapterState.PASSED,
        }
    ),
    ChapterState.FAILED: frozenset(
        {
            ChapterState.GENERATING,
            ChapterState.CHECKING,
            ChapterState.TO_CHECK,
            ChapterState.PENDING,
            ChapterState.QUEUED,
        }
    ),
}


def can_transition(src: ChapterState, dst: ChapterState) -> bool:
    """判断 src -> dst 是否是合法迁移。同状态视为合法（幂等信号）。"""
    if src == dst:
        return True
    return dst in ALLOWED_TRANSITIONS.get(src, frozenset())


# --------------------------------------------------------------------------- #
# 思考参数
# --------------------------------------------------------------------------- #
Effort = Literal["low", "high", "max"]
EFFORT_CHOICES: tuple[Effort, ...] = ("low", "high", "max")

#: DeepSeek 文档给出的 effort 归一化映射：
#: minimal->low, low->low, medium->high, high->high, xhigh->high, max->max, ultra->max
EFFORT_ALIASES: dict[str, str] = {
    "minimal": "low",
    "low": "low",
    "medium": "high",
    "high": "high",
    "xhigh": "high",
    "max": "max",
    "ultra": "max",
}


def normalize_effort(value: str) -> str:
    """把用户/配置里可能出现的别名归一化成 low|high|max。非法值回落 high。"""
    return EFFORT_ALIASES.get(str(value).strip().lower(), "high")


@dataclass
class ThinkingConfig:
    """单个对话的深度思考参数。

    enabled        思考模式开关（映射 extra_body={"thinking": {"type": ...}}）
    effort         思考强度（映射 reasoning_effort，仅 enabled 时生效）
    show_reasoning 是否在界面展示 reasoning_content（仅全局开关关闭时生效）
    """

    enabled: bool = True
    effort: str = "high"
    show_reasoning: bool = True

    def normalized(self) -> "ThinkingConfig":
        return ThinkingConfig(
            enabled=bool(self.enabled),
            effort=normalize_effort(self.effort),
            show_reasoning=bool(self.show_reasoning),
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "effort": self.effort,
            "show_reasoning": self.show_reasoning,
        }

    @classmethod
    def from_dict(cls, data: dict | None, fallback: "ThinkingConfig | None" = None) -> "ThinkingConfig":
        base = fallback or cls()
        if not isinstance(data, dict):
            return base.normalized()
        return cls(
            enabled=bool(data.get("enabled", base.enabled)),
            effort=normalize_effort(data.get("effort", base.effort)),
            show_reasoning=bool(data.get("show_reasoning", base.show_reasoning)),
        ).normalized()


# --------------------------------------------------------------------------- #
# 消息 / 用量 / 对话
# --------------------------------------------------------------------------- #
@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0

    def add(self, other: "Usage") -> "Usage":
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
        )

    def to_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "cache_hit_tokens": self.cache_hit_tokens,
        }


MessageSource = Literal[
    "first_generate",   # 首次生成模板
    "check",            # 检查模板
    "regen_by_check",   # 根据检查结果重新生成模板
    "regen_manual",     # 手动重新生成模板
    "user",             # 用户手动输入（不经模板）
    "system",           # 提示词
]

SOURCE_LABELS: dict[str, str] = {
    "first_generate": "模板：首次生成",
    "check": "模板：检查",
    "regen_by_check": "模板：根据检查结果重新生成",
    "regen_manual": "模板：手动重新生成",
    "user": "用户手动输入",
    "system": "提示词",
}


@dataclass
class Message:
    role: Literal["system", "user", "assistant"]
    content: str
    reasoning: str = ""
    source: str = "user"
    created_at: float = field(default_factory=time.time)
    usage: Usage | None = None
    interrupted: bool = False   # 流式被用户中断：内容可能不完整

    def to_dict(self) -> dict:
        data = {
            "role": self.role,
            "content": self.content,
            "reasoning": self.reasoning,
            "source": self.source,
            "created_at": self.created_at,
            "interrupted": self.interrupted,
        }
        if self.usage:
            data["usage"] = self.usage.to_dict()
        return data


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


ConversationKind = Literal["generate", "check"]
ConversationStatus = Literal["idle", "running", "done", "error", "cancelled"]

CONVERSATION_LABELS: dict[str, str] = {
    "generate": "生成对话",
    "check": "检查对话",
}


@dataclass
class Conversation:
    """一个独立的对话（message history + 该对话自己的思考参数）。"""

    conv_id: str
    chapter_id: str
    kind: ConversationKind
    messages: list[Message] = field(default_factory=list)
    thinking: ThinkingConfig = field(default_factory=ThinkingConfig)
    status: ConversationStatus = "idle"
    last_error: str = ""
    created_at: float = field(default_factory=time.time)

    # ---- 流式过程中的临时缓冲（只用于界面渲染，不参与请求）----
    stream_content: str = ""
    stream_reasoning: str = ""

    @staticmethod
    def create(chapter_id: str, kind: ConversationKind, thinking: ThinkingConfig) -> "Conversation":
        return Conversation(
            conv_id=new_id(kind),
            chapter_id=chapter_id,
            kind=kind,
            thinking=thinking.normalized(),
        )

    # ---- 消息历史维护 ----
    def append(self, message: Message) -> Message:
        self.messages.append(message)
        return message

    def api_messages(self) -> list[tuple[str, str]]:
        """返回 (role, content) 列表。

        注意：**绝不包含 reasoning_content**。本项目不携带 tools 参数，
        DeepSeek 文档明确说明此场景下 reasoning_content 无需回传，传入也会被忽略。
        """
        return [(m.role, m.content) for m in self.messages]

    def last_assistant(self) -> Message | None:
        for msg in reversed(self.messages):
            if msg.role == "assistant":
                return msg
        return None

    def reset_stream(self) -> None:
        self.stream_content = ""
        self.stream_reasoning = ""

    def clear(self) -> None:
        """清空对话消息（保留对话本身与思考参数）。"""
        self.messages.clear()
        self.reset_stream()
        self.last_error = ""
        self.status = "idle"


# --------------------------------------------------------------------------- #
# 章节
# --------------------------------------------------------------------------- #
@dataclass
class Chapter:
    chapter_id: str
    index: int
    title: str
    outline: str
    state: ChapterState = ChapterState.PENDING
    generated_content: str = ""
    check_result: str = ""
    generate_conv: Conversation | None = None
    check_conv: Conversation | None = None
    check_history: list[str] = field(default_factory=list)
    regen_count: int = 0
    last_error: str = ""
    note: str = ""
    #: 生成正文时使用的大纲快照。
    #: 生成对话一旦被"清空对话"，对话里的第一条 user 消息就没了；
    #: 此时若还想"重新生成"，必须有一份独立于对话之外的大纲。
    #: 因此每次成功提交生成任务时都把当时的大纲存到这里（见 engine.submit_generate）。
    outline_snapshot: str = ""
    #: 置为"排队中"之前的状态，用于"停止全部/停止本章"把排队任务回退到原状态。
    pre_queue_state: ChapterState | None = None
    #: 运行序号：每次开始一次生成/检查就 +1。
    #: 引擎把它随信号一起发出，界面用它丢弃"上一轮任务遗留的陈旧信号"
    #: （信号在 UI 线程是排队投递的，停止/重启后旧信号可能晚于新任务到达）。
    run_seq: int = 0

    @staticmethod
    def create(index: int, title: str, outline: str) -> "Chapter":
        return Chapter(
            chapter_id=new_id("ch"),
            index=index,
            title=title,
            outline=outline,
        )

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, str(self.state))

    @property
    def busy(self) -> bool:
        return self.state.busy

    @property
    def queued(self) -> bool:
        """是否已提交任务、正在队列里等待开始。"""
        return self.state == ChapterState.QUEUED

    @property
    def queued_or_busy(self) -> bool:
        """已提交（排队中）或正在跑：此时不应重复提交同一章的任务。"""
        return self.state.queued_or_busy

    @property
    def has_content(self) -> bool:
        return bool(self.generated_content.strip())

    def ensure_generate_conv(self, thinking: ThinkingConfig) -> Conversation:
        if self.generate_conv is None:
            self.generate_conv = Conversation.create(self.chapter_id, "generate", thinking)
        return self.generate_conv

    def ensure_check_conv(self, thinking: ThinkingConfig) -> Conversation:
        if self.check_conv is None:
            self.check_conv = Conversation.create(self.chapter_id, "check", thinking)
        return self.check_conv

    def new_check_conv(self, thinking: ThinkingConfig) -> Conversation:
        """每次检查都开一个全新对话（与旧的检查对话完全隔离）。"""
        self.check_conv = Conversation.create(self.chapter_id, "check", thinking)
        return self.check_conv

    def outline_for_generate(self) -> str:
        """生成正文时使用的大纲：优先用快照，其次用当前章节大纲。"""
        snapshot = (self.outline_snapshot or "").strip()
        if snapshot:
            return self.outline_snapshot
        return self.outline

    def snapshot_outline(self) -> str:
        """把当前章节大纲存为快照（在下发生成任务时调用）。

        Python 字符串是不可变对象，这里只是保存引用，不存在"后续编辑影响快照"的问题。
        """
        current = self.outline or ""
        if current.strip():
            self.outline_snapshot = current
        return self.outline_snapshot

    def summary(self) -> dict:
        """导出/持久化用的轻量摘要。"""
        return {
            "index": self.index,
            "title": self.title,
            "state": str(self.state),
            "chars": len(self.generated_content),
            "regen_count": self.regen_count,
        }


# --------------------------------------------------------------------------- #
# 流式增量（api_client -> engine -> UI 的统一载荷）
# --------------------------------------------------------------------------- #
@dataclass
class StreamDelta:
    """一条流式增量。

    kind = "reasoning" 思维链增量
           "content"   正文增量
           "usage"     用量统计（不产生文本）
           "done"      流结束
    """

    kind: Literal["reasoning", "content", "usage", "done"]
    text: str = ""
    usage: Usage | None = None


@dataclass
class CompletionResult:
    content: str
    reasoning: str
    usage: Usage
    finish_reason: str = ""

    @property
    def truncated(self) -> bool:
        """finish_reason == "length" 表示被 max_tokens 或上下文截断。"""
        return self.finish_reason == "length"


# --------------------------------------------------------------------------- #
# 结果类型
# --------------------------------------------------------------------------- #
GenReason = Literal["first", "regen_by_check", "regen_manual"]

GEN_REASON_LABELS: dict[str, str] = {
    "first": "首次生成",
    "regen_by_check": "根据检查结果重新生成",
    "regen_manual": "手动重新生成",
}
