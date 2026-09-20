"""任务编排引擎。

线程模型
--------
* UI 主线程：只跑 Qt，绝不 await、绝不阻塞。
* 引擎线程（QThread）：内部运行一个 asyncio 事件循环，所有 HTTP 请求都在这里。
* 通信：引擎线程 ``emit`` Qt 信号（PySide6 会以 QueuedConnection 投递到 UI 线程），
  UI 线程通过 ``asyncio.run_coroutine_threadsafe`` / ``call_soon_threadsafe`` 反向提交任务。

并发调度
--------
* 一个全局作业队列 + 一个 asyncio.Semaphore（值 = 配置的并发数）。
* 同一章节同时只会有一个作业在跑（队列串行 + 忙状态校验）。
* "停止本章/停止全部" = 取消对应的 asyncio.Task；已被取消的任务在 done 回调里收尾。

状态机
------
状态迁移只在 ``transition()`` 中执行，并校验 :data:`ALLOWED_TRANSITIONS`。
界面从不自行改状态，只响应 ``chapter_state_changed`` 信号。
"""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Literal

from PySide6.QtCore import QObject, QThread, Signal

from . import paths  # noqa: F401  (保留：便于将来把任务日志落盘)
from .api_client import DeepSeekClient
from .checker import ChapterChecker, CheckOutcome
from .config import AppConfig, thinking_defaults_for
from .generator import ChapterGenerator, GenerateOutcome
from .models import (
    GEN_REASON_LABELS,
    QUEUE_FALLBACK_STATE,
    Chapter,
    ChapterState,
    Conversation,
    Message,
    STATE_LABELS,
    StreamDelta,
    ThinkingConfig,
    Usage,
    can_transition,
)

JobKind = Literal[
    "generate",
    "check",
    "chat_generate",
    "chat_check",
]

JOB_LABELS: dict[str, str] = {
    "generate": "生成",
    "check": "检查",
    "chat_generate": "生成对话手动消息",
    "chat_check": "检查对话手动消息",
}

#: 槽位满时调度器的轮询间隔（秒）
IDLE_POLL_SECONDS = 0.05


@dataclass
class Job:
    chapter_id: str
    kind: JobKind
    reason: str = "first"      # first | regen_by_check | regen_manual
    text: str = ""             # 手动消息内容
    created_at: float = field(default_factory=time.time)
    job_id: str = ""           # 队列面板用它做拖动排序的稳定标识


@dataclass(frozen=True)
class QueueEntry:
    """给界面看的队列条目（不可变快照，跨线程读取安全）。"""

    position: int
    job_id: str
    chapter_id: str
    title: str
    kind: JobKind
    kind_label: str
    reason_label: str
    created_at: float


class JobQueue:
    """可重排的作业队列。

    相比 ``asyncio.Queue`` 多出来的能力：**按优先级重排**（界面拖动排序）、
    **按章删除**、**随时快照**（给队列面板显示）。

    设计要点：只保存"等待开始"的任务。调度器**只在有空闲并发槽位时才取走任务**，
    因此队列里看到的就是"还没轮到"的任务——界面拖动排序始终有效，
    不会出现"刚拖完却发现它已经被取走了"的情况。
    """

    def __init__(self) -> None:
        self._items: deque[Job] = deque()
        self._event = asyncio.Event()
        self.inflight: set[str] = set()

    # ---- 生产者 / 消费者（引擎线程）---- #
    def put(self, job: Job) -> None:
        if not job.job_id:
            job.job_id = f"job_{uuid.uuid4().hex[:10]}"
        self._items.append(job)
        self._event.set()

    def get_nowait(self) -> Job | None:
        if not self._items:
            return None
        job = self._items.popleft()
        self.inflight.add(job.job_id)
        if not self._items:
            self._event.clear()
        return job

    async def get(self) -> Job:
        while True:
            if self._items:
                # 派发出去的同时把事件清掉：下次 put 会重新唤醒
                job = self._items.popleft()
                self.inflight.add(job.job_id)
                if not self._items:
                    self._event.clear()
                return job
            self._event.clear()
            await self._event.wait()

    def task_done(self, job: Job | None = None) -> None:
        """把一个已结束的任务移出 in-flight 集合。"""
        if job is not None:
            self.inflight.discard(job.job_id)

    # ---- 查询 / 重排（引擎线程）---- #
    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> list[Job]:
        dropped = list(self._items)
        self._items.clear()
        self._event.clear()
        return dropped

    def remove_chapter(self, chapter_id: str) -> int:
        before = len(self._items)
        self._items = deque(job for job in self._items if job.chapter_id != chapter_id)
        if not self._items:
            self._event.clear()
        return before - len(self._items)

    def reorder(self, job_ids: list[str]) -> None:
        """把指定任务按给定顺序移到队首（拖动排序的结果）。

        未在 ``job_ids`` 中出现的任务保持原有相对顺序排在后面。
        """
        if not job_ids:
            return
        wanted = {job_id: index for index, job_id in enumerate(job_ids)}
        picked = sorted(
            (job for job in self._items if job.job_id in wanted),
            key=lambda job: wanted[job.job_id],
        )
        rest = [job for job in self._items if job.job_id not in wanted]
        self._items = deque(picked + rest)
        if self._items:
            self._event.set()

    def items(self) -> list[Job]:
        return list(self._items)


class EngineSignals(QObject):
    """所有跨线程通信都通过这里。

    流式相关信号都带 ``run_seq``（章节的运行序号）：界面可用它判断
    "这个信号是不是当前这一轮的"，避免上一轮任务遗留的排队信号干扰新一轮。
    """

    chapter_state_changed = Signal(str, str)                    # chapter_id, state
    chapter_updated = Signal(str)                                # 正文/检查结果等内容变化
    delta_received = Signal(str, str, str, str, int)             # chapter_id, conv_kind, delta_kind, text, run_seq
    conversation_started = Signal(str, str, int)                 # chapter_id, conv_kind, run_seq
    conversation_finished = Signal(str, str, bool, str, int)     # chapter_id, conv_kind, ok, error, run_seq
    phase = Signal(str, str, str)                                # chapter_id, phase, detail
    usage_received = Signal(str, str, object)                    # chapter_id, conv_kind, Usage
    status = Signal(int, int, object)                            # queue_len, running, total_usage
    log = Signal(str, str)                                       # level, message
    all_finished = Signal()


class _EngineThread(QThread):
    """运行 asyncio 事件循环的线程。"""

    def __init__(self, engine: "Engine") -> None:
        super().__init__()
        self._engine = engine
        self.loop: asyncio.AbstractEventLoop | None = None
        self._ready = threading.Event()
        self._stop_event: asyncio.Event | None = None

    def run(self) -> None:  # pragma: no cover - 线程入口
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.loop = loop
        self._stop_event = asyncio.Event()
        self._ready.set()
        try:
            loop.create_task(self._engine._scheduler())
            loop.run_forever()
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.run_until_complete(loop.shutdown_asyncgens())
                if self._engine._client is not None:
                    loop.run_until_complete(self._engine._client.aclose())
            except Exception:
                pass
            loop.close()

    def wait_ready(self, timeout: float = 10.0) -> bool:
        return self._ready.wait(timeout)

    def request_stop(self) -> None:
        if self.loop and self._stop_event is not None:
            self.loop.call_soon_threadsafe(self._stop_event.set)
        if self.loop:
            self.loop.call_soon_threadsafe(self.loop.stop)


class Engine(QObject):
    """对界面暴露的门面。所有方法都可从 UI 线程直接调用，立即返回。"""

    def __init__(self, cfg: AppConfig, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.signals = EngineSignals()
        self.cfg = cfg
        self.chapters: dict[str, Chapter] = {}

        self._thread = _EngineThread(self)
        self._queue = JobQueue()
        #: 与 UI 线程共享的并发数：改并发数时在 UI 线程改这里，调度器读到即生效
        self._control = threading.Lock()
        self._concurrency = max(1, int(cfg.sampling.concurrency))
        self._desired_concurrency = self._concurrency
        self._running: dict[str, asyncio.Task] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._stop_requested: set[str] = set()
        self._client: DeepSeekClient | None = None
        self._generator: ChapterGenerator | None = None
        self._checker: ChapterChecker | None = None
        self._total_usage = Usage()
        self._started = False
        self._bootstrapped = False
        #: 对话内容发生变化（AI 回复完成 / 清空对话 / 手动消息等），
        #: 由 MainWindow 轮询后写入项目的 conversations.json。
        self._conversations_dirty = False

    # ================================================================== #
    # 生命周期
    # ================================================================== #
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()
        if not self._thread.wait_ready():
            self.signals.log.emit("error", "引擎线程启动超时。")
            return
        # 在引擎线程里准备好客户端与业务对象
        self._call(self._bootstrap())

    def shutdown(self, timeout_ms: int = 5000) -> None:
        if not self._started:
            return
        self._started = False
        loop = self._thread.loop
        if loop is not None:
            loop.call_soon_threadsafe(self._cancel_all_now)
        self._thread.request_stop()
        self._thread.wait(timeout_ms)

    def _call(self, coro) -> None:
        """把协程提交到引擎线程。"""
        loop = self._thread.loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            asyncio.run_coroutine_threadsafe(coro, loop)
        except RuntimeError:  # pragma: no cover - 事件循环刚好在这一刻关闭
            coro.close()

    def _call_soon(self, func: Callable[[], None]) -> None:
        loop = self._thread.loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(func)
        except RuntimeError:  # pragma: no cover - 事件循环刚好在这一刻关闭
            pass

    async def _bootstrap(self) -> None:
        if self._bootstrapped:
            return
        self._client = DeepSeekClient(self.cfg.api, self.cfg.sampling)
        self._generator = ChapterGenerator(self._client, self.cfg)
        self._checker = ChapterChecker(self._client, self.cfg)
        self._bootstrapped = True
        self.signals.log.emit(
            "info", f"引擎已就绪（并发 {max(1, int(self._desired_concurrency))}）。"
        )

    async def _wait_ready(self) -> None:
        """等待引擎初始化完成（避免启动瞬间提交的任务被丢弃）。"""
        for _ in range(200):
            if self._bootstrapped:
                return
            await asyncio.sleep(0.05)

    # ================================================================== #
    # 配置热更新
    # ================================================================== #
    def apply_config(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        desired = max(1, int(cfg.sampling.concurrency))
        with self._control:
            changed = desired != self._desired_concurrency
            self._desired_concurrency = desired
        if changed:
            self.signals.log.emit("info", f"并发数已调整为 {desired}。")

        async def _apply() -> None:
            if self._client is not None:
                self._client.apply_config(cfg.api, cfg.sampling)
            if self._generator is not None:
                self._generator.apply_config(cfg)
            if self._checker is not None:
                self._checker.apply_config(cfg)

        self._call(_apply())

    # ================================================================== #
    # 章节管理
    # ================================================================== #
    def set_chapters(self, chapters: list[Chapter]) -> None:
        self.chapters = {c.chapter_id: c for c in chapters}

    def add_chapter(self, chapter: Chapter) -> None:
        self.chapters[chapter.chapter_id] = chapter

    def remove_chapter(self, chapter_id: str) -> None:
        self.stop_chapter(chapter_id)
        self.chapters.pop(chapter_id, None)

    def get(self, chapter_id: str) -> Chapter | None:
        return self.chapters.get(chapter_id)

    def queue_length(self) -> int:
        """当前排队中（尚未开始）的任务数。只用于界面提示，允许轻微误差。"""
        return len(self._queue)

    # ------------------------------------------------------------------ #
    # 队列查看与优先级（拖动排序）
    # ------------------------------------------------------------------ #
    def queue_snapshot(self) -> tuple[list[QueueEntry], list[QueueEntry]]:
        """返回 (排队中的任务, 正在运行的任务) 两份快照，供队列面板显示。"""
        pending: list[QueueEntry] = []
        for index, job in enumerate(self._queue.items(), start=1):
            pending.append(self._queue_entry(index, job))
        running: list[QueueEntry] = []
        for index, chapter_id in enumerate(sorted(self._running), start=1):
            chapter = self.chapters.get(chapter_id)
            running.append(
                QueueEntry(
                    position=index,
                    job_id=f"running_{chapter_id}",
                    chapter_id=chapter_id,
                    title=chapter.title if chapter else chapter_id,
                    kind="generate",
                    kind_label="正在运行",
                    reason_label=(
                        STATE_LABELS.get(chapter.state, str(chapter.state)) if chapter else ""
                    ),
                    created_at=0.0,
                )
            )
        return pending, running

    def _queue_entry(self, position: int, job: Job) -> QueueEntry:
        chapter = self.chapters.get(job.chapter_id)
        reason_label = (
            GEN_REASON_LABELS.get(job.reason, job.reason) if job.kind == "generate" else ""
        )
        return QueueEntry(
            position=position,
            job_id=job.job_id,
            chapter_id=job.chapter_id,
            title=chapter.title if chapter else job.chapter_id,
            kind=job.kind,
            kind_label=JOB_LABELS.get(job.kind, job.kind),
            reason_label=reason_label,
            created_at=job.created_at,
        )

    def reorder_queue(self, job_ids: list[str]) -> None:
        """把指定任务按给定顺序提到队首（队列面板拖动后调用）。

        只影响**还没开始**的任务；正在跑的任务不受影响。
        """
        if not job_ids:
            return

        def _apply() -> None:
            self._queue.reorder(list(job_ids))
            self._emit_status()

        self._call_soon(_apply)

    def drop_queued_chapter(self, chapter_id: str) -> None:
        """把某章还没开始的任务从队列里撤掉（队列面板的"移出队列"）。"""
        def _apply() -> None:
            removed = self._queue.remove_chapter(chapter_id)
            chapter = self.chapters.get(chapter_id)
            if chapter is not None:
                self._restore_queued_state(chapter)
            if removed:
                self.signals.log.emit(
                    "info",
                    f"已把 {chapter.title if chapter else chapter_id} 移出队列。",
                )
            self._emit_status()

        self._call_soon(_apply)

    # ------------------------------------------------------------------ #
    # 对话存档标记
    # ------------------------------------------------------------------ #
    def conversations_dirty(self) -> bool:
        """自上次存档以来，是否有对话内容发生变化（供 UI 侧触发落盘）。"""
        return self._conversations_dirty

    def mark_conversations_saved(self) -> None:
        """UI 侧把对话写入 conversations.json 成功后调用，清除脏标记。"""
        self._conversations_dirty = False

    def transition(self, chapter: Chapter, new_state: ChapterState, *, force: bool = False) -> bool:
        """唯一的状态迁移入口。非法迁移会被拒绝并记日志。"""
        if chapter.state == new_state:
            self.signals.chapter_state_changed.emit(chapter.chapter_id, str(new_state))
            return True
        if not force and not can_transition(chapter.state, new_state):
            self.signals.log.emit(
                "warn",
                f"{chapter.title}：拒绝非法状态迁移 "
                f"{STATE_LABELS.get(chapter.state, chapter.state)} → "
                f"{STATE_LABELS.get(new_state, new_state)}",
            )
            return False
        chapter.state = new_state
        self.signals.chapter_state_changed.emit(chapter.chapter_id, str(new_state))
        return True

    # ================================================================== #
    # 提交任务（全部立即返回）
    # ================================================================== #
    def submit_generate(self, chapter_id: str, reason: str = "first", text: str = "") -> None:
        """提交生成任务。

        reason = "first"           使用首次生成模板（{outline}）
                 "regen_by_check"  使用根据检查结果重新生成的模板（{check_result}，文本取自 text）
                 "regen_manual"    使用手动重新生成的模板（{user_input}，文本取自 text）

        提交后立即把章节置为**排队中**：任务一定排在队列末尾（asyncio.Queue 是 FIFO），
        界面因此能马上告诉用户"已排在队尾、还没开始跑"。
        """
        chapter = self.chapters.get(chapter_id)
        if chapter is not None:
            # ① 先存大纲快照：清空生成对话后仍能"重新生成"（快照与对话无关）
            chapter.snapshot_outline()
            # ② 标记排队中（任务真正开始时由 _do_generate 迁到"生成中"）
            self._mark_queued(chapter)
        self._enqueue(Job(chapter_id=chapter_id, kind="generate", reason=reason, text=text))

    def submit_check(self, chapter_id: str) -> None:
        chapter = self.chapters.get(chapter_id)
        if chapter is not None:
            self._mark_queued(chapter)
        self._enqueue(Job(chapter_id=chapter_id, kind="check"))

    def submit_manual_chat(self, chapter_id: str, conv_kind: str, text: str) -> None:
        """手动消息：不改章节状态，因此不置"排队中"（只追加到对话末尾排队发送）。"""
        kind: JobKind = "chat_check" if conv_kind == "check" else "chat_generate"
        self._enqueue(Job(chapter_id=chapter_id, kind=kind, text=text))

    # ------------------------------------------------------------------ #
    def _mark_queued(self, chapter: Chapter) -> None:
        """把章节置为"排队中"（在 UI 线程同步执行，界面可立即刷新）。

        重复提交时保留最初的 pre_queue_state，避免"排队中 -> 排队中"把回退目标冲掉。
        """
        if not chapter.state.queued_or_busy:
            chapter.pre_queue_state = chapter.state
        self.transition(chapter, ChapterState.QUEUED)

    def _restore_queued_state(self, chapter: Chapter) -> None:
        """排队中的任务被取消：回退到提交前的状态（任务从未开始，不应留下排队中）。"""
        if chapter.state != ChapterState.QUEUED:
            return
        previous = chapter.pre_queue_state or QUEUE_FALLBACK_STATE.get(
            chapter.state, ChapterState.PENDING
        )
        chapter.pre_queue_state = None
        self.transition(chapter, previous, force=True)

    def _enqueue(self, job: Job) -> None:
        self._call_soon(lambda: self._put_job(job))
        self._emit_status()

    def _put_job(self, job: Job) -> None:
        self._queue.put(job)
        self._emit_status()

    def stop_chapter(self, chapter_id: str) -> None:
        def _stop() -> None:
            self._stop_requested.add(chapter_id)
            event = self._cancel_events.get(chapter_id)
            if event is not None:
                event.set()
            task = self._running.get(chapter_id)
            if task is not None and not task.done():
                task.cancel()
            chapter = self.chapters.get(chapter_id)
            if chapter is not None:
                # 还没轮到执行的排队任务不会经过 _run_job，这里直接收尾
                self._restore_queued_state(chapter)
                self._drop_queued_jobs(chapter_id)

        self._call_soon(_stop)

    def stop_all(self) -> None:
        self._call_soon(self._cancel_all_now)

    def _cancel_all_now(self) -> None:
        for chapter_id, event in list(self._cancel_events.items()):
            self._stop_requested.add(chapter_id)
            event.set()
        for task in list(self._running.values()):
            if not task.done():
                task.cancel()
        # 清空尚未开始的排队任务
        self._queue.clear()
        # 排队中被取消的章节要回到"提交前"的状态，否则会一直卡在"排队中"
        for chapter in list(self.chapters.values()):
            self._restore_queued_state(chapter)
        self._reap_stuck_queued()
        self._emit_status()

    def _drop_queued_jobs(self, chapter_id: str) -> None:
        """把队列里属于该章的未开始任务丢弃（保留其它章的任务与相对顺序）。"""
        self._queue.remove_chapter(chapter_id)
        self._emit_status()

    def _reap_stuck_queued(self) -> None:
        """兜底修复：状态是"排队中"但队列与运行集合里都没有它的章节。

        正常流程不会出现这种情况；一旦因为异常/竞态出现（用户反馈过"其它章都到
        待决策了，只有一章永远排队中"），这里立刻把它恢复成可继续操作的状态，
        界面就不会卡住。
        """
        pending_ids = {job.chapter_id for job in self._queue.items()}
        running_ids = set(self._running)
        for chapter in list(self.chapters.values()):
            if chapter.state != ChapterState.QUEUED:
                continue
            if chapter.chapter_id in pending_ids or chapter.chapter_id in running_ids:
                continue
            self.signals.log.emit(
                "warn",
                f"{chapter.title}：排队中但没有对应任务，已自动恢复为可继续操作的状态。",
            )
            self._restore_queued_state(chapter)

    # ================================================================== #
    # 纯本地操作（同样在引擎线程执行，避免与任务竞争）
    # ================================================================== #
    def accept_chapter(self, chapter_id: str) -> None:
        def _accept() -> None:
            chapter = self.chapters.get(chapter_id)
            if chapter is None or chapter.busy:
                return
            self.transition(chapter, ChapterState.PASSED)
            self.signals.log.emit("info", f"{chapter.title} 已标记为合格。")

        self._call_soon(_accept)

    def postpone_chapter(self, chapter_id: str) -> None:
        def _postpone() -> None:
            chapter = self.chapters.get(chapter_id)
            if chapter is None or chapter.busy:
                return
            if chapter.state == ChapterState.AWAITING_DECISION:
                self.transition(chapter, ChapterState.AWAITING_DECISION)
            self.signals.log.emit("info", f"{chapter.title} 暂不处理。")

        self._call_soon(_postpone)

    def set_chapter_content(self, chapter_id: str, content: str) -> None:
        def _set() -> None:
            chapter = self.chapters.get(chapter_id)
            if chapter is None:
                return
            chapter.generated_content = content
            self.signals.chapter_updated.emit(chapter_id)

        self._call_soon(_set)

    def set_check_result(self, chapter_id: str, text: str) -> None:
        def _set() -> None:
            chapter = self.chapters.get(chapter_id)
            if chapter is None:
                return
            chapter.check_result = text

        self._call_soon(_set)

    def clear_conversation(self, chapter_id: str, conv_kind: str) -> None:
        """清空某个对话（消息历史）。

        清空**生成对话**时把章节状态改为"待生成"（除非本章已标记合格）：
        这样再点"重新生成"就会走"重新发送大纲"的路径，而不是"按修改建议重新生成"。
        """
        def _clear() -> None:
            chapter = self.chapters.get(chapter_id)
            if chapter is None or chapter.busy:
                return
            conv = chapter.check_conv if conv_kind == "check" else chapter.generate_conv
            if conv is None:
                return
            conv.clear()
            if conv_kind == "generate" and chapter.state != ChapterState.PASSED:
                self.transition(chapter, ChapterState.PENDING, force=True)
            self._conversations_dirty = True
            self.signals.log.emit("info", f"{chapter.title} 的{conv_kind}对话已清空。")
            self.signals.chapter_updated.emit(chapter_id)

        self._call_soon(_clear)

    def set_thinking(self, chapter_id: str, conv_kind: str, thinking: ThinkingConfig) -> None:
        def _set() -> None:
            chapter = self.chapters.get(chapter_id)
            if chapter is None:
                return
            conv = chapter.check_conv if conv_kind == "check" else chapter.generate_conv
            if conv is None:
                # 对话尚未创建：先记到临时槽位，创建时会使用
                if conv_kind == "check":
                    chapter.check_conv = Conversation.create(
                        chapter.chapter_id, "check", thinking
                    )
                else:
                    chapter.ensure_generate_conv(thinking)
                return
            conv.thinking = ThinkingConfig(
                enabled=thinking.enabled,
                effort=thinking.effort,
                show_reasoning=thinking.show_reasoning,
            )

        self._call_soon(_set)

    # ================================================================== #
    # 调度器（运行在引擎线程）
    # ================================================================== #
    async def _scheduler(self) -> None:
        """作业调度器。

        * 并发闸门 = "当前运行数 < 并发数"：**没有空闲槽位时绝不把任务从队列里取走**，
          因此队列面板显示的就是"还没轮到"的任务，界面拖动排序随时有效
          （不会出现"刚拖完却发现它已经被取走"的情况）。
        * 空闲时绝不占用许可（不预扣），所以把并发数改大后立即生效。
        * 并发数调小时等待在跑的任务自然结束（不打断正在进行的 API 请求）。
        """
        while True:
            try:
                if self._running_count() >= self._concurrency_limit():
                    # 槽位满了：让任务留在队列里（可被界面重排），稍后再看
                    await asyncio.sleep(IDLE_POLL_SECONDS)
                    continue
                job = await self._queue.get()
                self._launch(job)
            except asyncio.CancelledError:
                break
            except Exception as exc:  # pragma: no cover - 调度器自我保护
                self.signals.log.emit("error", f"调度器异常：{type(exc).__name__}: {exc}")
                await asyncio.sleep(0.2)

    # ------------------------------------------------------------------ #
    # 并发数（可在 UI 线程改，调度器随时读到新值）
    # ------------------------------------------------------------------ #
    def _concurrency_limit(self) -> int:
        with self._control:
            return max(1, int(self._desired_concurrency))

    def _running_count(self) -> int:
        return len(self._running)

    def _launch(self, job: Job) -> None:
        chapter = self.chapters.get(job.chapter_id)
        if job.chapter_id in self._stop_requested:
            self._stop_requested.discard(job.chapter_id)
            self._queue.task_done(job)
            if chapter is not None:
                self._restore_queued_state(chapter)
            self._reap_stuck_queued()
            self._emit_status()
            return

        cancel_event = asyncio.Event()
        self._cancel_events[job.chapter_id] = cancel_event
        task = asyncio.create_task(self._run_job(job, cancel_event))
        self._running[job.chapter_id] = task

        def _done(_task: asyncio.Task) -> None:
            self._queue.task_done(job)
            self._running.pop(job.chapter_id, None)
            self._cancel_events.pop(job.chapter_id, None)
            self._stop_requested.discard(job.chapter_id)
            # 双保险：队列与运行集合里都没有该章了，就不该再显示"排队中"
            self._reap_stuck_queued()
            self._emit_status()
            if not self._running and not len(self._queue):
                self.signals.all_finished.emit()

        task.add_done_callback(_done)
        self._emit_status()

    async def _run_job(self, job: Job, cancel_event: asyncio.Event) -> None:
        chapter = self.chapters.get(job.chapter_id)
        if chapter is None:
            return
        if self._generator is None or self._checker is None or self._client is None:
            await self._wait_ready()
        if self._generator is None or self._checker is None or self._client is None:
            self.signals.log.emit("error", "引擎初始化失败，任务被丢弃。")
            return
        try:
            if job.kind == "generate":
                await self._do_generate(chapter, job, cancel_event)
            elif job.kind == "check":
                await self._do_check(chapter, cancel_event)
            elif job.kind == "chat_generate":
                await self._do_chat_generate(chapter, job, cancel_event)
            elif job.kind == "chat_check":
                await self._do_chat_check(chapter, job, cancel_event)
        except asyncio.CancelledError:
            # 用户主动停止：这是**正常操作**，只记一条 info，不要当报错吓人
            self.signals.log.emit("info", f"{chapter.title}：{JOB_LABELS[job.kind]}已按你的要求停止。")
            if chapter.state in (ChapterState.GENERATING, ChapterState.CHECKING):
                self._settle_after_cancel(chapter)
            raise
        except Exception as exc:  # pragma: no cover - 兜底
            self.signals.log.emit("error", f"{chapter.title} 任务异常：{type(exc).__name__}: {exc}")
            if chapter.state in (ChapterState.GENERATING, ChapterState.CHECKING):
                chapter.last_error = f"{type(exc).__name__}: {exc}"
                self.transition(chapter, ChapterState.FAILED, force=True)

    def _settle_after_cancel(self, chapter: Chapter) -> None:
        """用户停止后把章节收敛到一个"可以继续操作"的状态。

        注意：如果停止时章节已经是"待决策/合格"（例如用户点了"按要求重新生成"之后
        立刻又按了停止），**保持原状态不动**——否则会把用户的决策结果冲掉。
        """
        if chapter.state in (ChapterState.AWAITING_DECISION, ChapterState.PASSED):
            return
        if chapter.state == ChapterState.GENERATING:
            if chapter.has_content:
                self.transition(chapter, ChapterState.TO_CHECK, force=True)
            else:
                self.transition(chapter, ChapterState.PENDING, force=True)
        elif chapter.state == ChapterState.CHECKING:
            if chapter.check_result.strip():
                self.transition(chapter, ChapterState.AWAITING_DECISION, force=True)
            else:
                self.transition(chapter, ChapterState.TO_CHECK, force=True)

    # ================================================================== #
    # 具体作业
    # ================================================================== #
    def _thinking_for(self, chapter: Chapter, conv_kind: str) -> ThinkingConfig:
        conv = chapter.check_conv if conv_kind == "check" else chapter.generate_conv
        if conv is not None:
            return conv.thinking
        return thinking_defaults_for(self.cfg, conv_kind)

    async def _do_generate(self, chapter: Chapter, job: Job, cancel_event: asyncio.Event) -> None:
        assert self._generator is not None
        if not chapter.outline_for_generate().strip():
            self.signals.log.emit("warn", f"{chapter.title} 的大纲为空，已跳过。")
            self.transition(chapter, ChapterState.PENDING, force=True)
            return

        thinking = self._thinking_for(chapter, "generate")
        chapter.run_seq += 1
        run_seq = chapter.run_seq
        chapter.pre_queue_state = None      # 任务真正开始：排队阶段结束
        self.transition(chapter, ChapterState.GENERATING, force=True)
        self.signals.conversation_started.emit(chapter.chapter_id, "generate", run_seq)
        self.signals.phase.emit(
            chapter.chapter_id, "generate", f"{JOB_LABELS['generate']}中…"
        )

        if job.reason == "regen_by_check":
            user_message = self._generator.build_regen_by_check_message(job.text)
            source = "regen_by_check"
        elif job.reason == "regen_manual":
            user_message = self._generator.build_manual_message(job.text)
            source = "regen_manual"
        else:
            user_message = self._generator.build_first_message(chapter)
            source = "first_generate"

        outcome = await self._generator.run(
            chapter,
            user_message,
            source=source,
            thinking=thinking,
            on_delta=lambda d: self._forward_delta(chapter, "generate", d, run_seq),
            on_event=lambda e: self._forward_event(chapter, e.kind, e.text),
            cancel_event=cancel_event,
        )
        self._finish_generate(chapter, outcome, job, run_seq)

    def _finish_generate(
        self, chapter: Chapter, outcome: GenerateOutcome, job: Job, run_seq: int
    ) -> None:
        if outcome.content:
            chapter.generated_content = outcome.content.rstrip()
            self.signals.chapter_updated.emit(chapter.chapter_id)
        if job.reason in ("regen_by_check", "regen_manual"):
            chapter.regen_count += 1

        if outcome.usage.total_tokens or outcome.usage.completion_tokens:
            self._total_usage = self._total_usage.add(outcome.usage)
            self.signals.usage_received.emit(chapter.chapter_id, "generate", outcome.usage)

        if outcome.ok:
            if outcome.truncated:
                chapter.last_error = (
                    "本次生成的 finish_reason=length：正文可能因 max_tokens 或上下文上限被截断。"
                    "可调大 max_tokens 后重新生成。"
                )
                self.signals.log.emit("warn", f"{chapter.title}：{chapter.last_error}")
            self._conversations_dirty = True
            self.signals.conversation_finished.emit(
                chapter.chapter_id, "generate", True, "", run_seq
            )
            if self.cfg.ui.auto_check_after_generate:
                self.signals.phase.emit(chapter.chapter_id, "check", "等待检查…")
                self.transition(chapter, ChapterState.TO_CHECK, force=True)
                self.submit_check(chapter.chapter_id)
            else:
                self.transition(chapter, ChapterState.TO_CHECK, force=True)
            return

        if outcome.cancelled:
            # 用户主动停止：不是错误。用 ok=True（第 3 个参数）发结束信号，
            # 界面就不会把它当失败处理、也就不会自动弹出日志窗口。
            self.signals.log.emit(
                "info",
                f"{chapter.title}：生成已按你的要求停止（保留了已收到的 "
                f"{len(outcome.content)} 字符正文）。",
            )
            self.signals.conversation_finished.emit(
                chapter.chapter_id,
                "generate",
                True,
                f"已按你的要求停止生成（保留了已收到的 {len(outcome.content)} 字符正文）。",
                run_seq,
            )
            self._settle_after_cancel(chapter)
            self._conversations_dirty = True
            return

        self.signals.conversation_finished.emit(
            chapter.chapter_id, "generate", False, outcome.error_text, run_seq
        )
        chapter.last_error = outcome.error_text or "生成失败。"
        self.transition(chapter, ChapterState.FAILED, force=True)

    async def _do_check(self, chapter: Chapter, cancel_event: asyncio.Event) -> None:
        assert self._checker is not None
        if not chapter.has_content:
            self.signals.log.emit("warn", f"{chapter.title} 还没有正文，无法检查。")
            self.transition(chapter, ChapterState.TO_CHECK, force=True)
            return

        thinking = self._thinking_for(chapter, "check")
        chapter.run_seq += 1
        run_seq = chapter.run_seq
        chapter.pre_queue_state = None      # 任务真正开始：排队阶段结束
        self.transition(chapter, ChapterState.CHECKING, force=True)
        self.signals.conversation_started.emit(chapter.chapter_id, "check", run_seq)
        self.signals.phase.emit(chapter.chapter_id, "check", "检查中…")

        outcome = await self._checker.run(
            chapter,
            chapter.generated_content,
            thinking=thinking,
            on_delta=lambda d: self._forward_delta(chapter, "check", d, run_seq),
            on_event=lambda e: self._forward_event(chapter, e.kind, e.text),
            cancel_event=cancel_event,
        )
        self._finish_check(chapter, outcome, run_seq)

    def _finish_check(self, chapter: Chapter, outcome: CheckOutcome, run_seq: int) -> None:
        if outcome.text.strip():
            chapter.check_result = outcome.text.strip()
            chapter.check_history.append(outcome.text.strip())
            self.signals.chapter_updated.emit(chapter.chapter_id)
        if outcome.usage.total_tokens or outcome.usage.completion_tokens:
            self._total_usage = self._total_usage.add(outcome.usage)
            self.signals.usage_received.emit(chapter.chapter_id, "check", outcome.usage)

        if outcome.ok:
            self._conversations_dirty = True
            self.signals.conversation_finished.emit(
                chapter.chapter_id, "check", True, "", run_seq
            )
            self.transition(chapter, ChapterState.AWAITING_DECISION, force=True)
            self.signals.phase.emit(chapter.chapter_id, "decision", "等待你的决策。")
            return

        if outcome.cancelled:
            # 用户主动停止：不是错误，也不改变决策状态，只保留已收到的检查内容
            self.signals.log.emit("info", f"{chapter.title}：检查已按你的要求停止。")
            self.signals.conversation_finished.emit(
                chapter.chapter_id,
                "check",
                True,
                "已按你的要求停止检查。",
                run_seq,
            )
            self._settle_after_cancel(chapter)
            self._conversations_dirty = True
            return

        self.signals.conversation_finished.emit(
            chapter.chapter_id, "check", False, outcome.error_text, run_seq
        )
        chapter.last_error = outcome.error_text or "检查失败。"
        if chapter.check_result.strip():
            self.transition(chapter, ChapterState.AWAITING_DECISION, force=True)
        else:
            self.transition(chapter, ChapterState.FAILED, force=True)

    async def _do_chat_generate(
        self, chapter: Chapter, job: Job, cancel_event: asyncio.Event
    ) -> None:
        """生成对话中的手动消息：**不改章节状态**，只追加消息并流式输出。"""
        assert self._generator is not None
        if chapter.state in (ChapterState.GENERATING, ChapterState.CHECKING):
            self.signals.log.emit("warn", f"{chapter.title} 正忙，手动消息未发送。")
            return
        thinking = self._thinking_for(chapter, "generate")
        chapter.run_seq += 1
        run_seq = chapter.run_seq
        self.signals.conversation_started.emit(chapter.chapter_id, "generate", run_seq)
        outcome = await self._generator.run_manual_chat(
            chapter,
            job.text,
            thinking=thinking,
            on_delta=lambda d: self._forward_delta(chapter, "generate", d, run_seq),
            on_event=lambda e: self._forward_event(chapter, e.kind, e.text),
            cancel_event=cancel_event,
        )
        if outcome.usage.completion_tokens:
            self._total_usage = self._total_usage.add(outcome.usage)
            self.signals.usage_received.emit(chapter.chapter_id, "generate", outcome.usage)
        # 用户停止：ok=True，只是"停止"而不是"失败"
        self.signals.conversation_finished.emit(
            chapter.chapter_id,
            "generate",
            True if outcome.cancelled else outcome.ok,
            "已按你的要求停止。" if outcome.cancelled else outcome.error_text,
            run_seq,
        )
        self._conversations_dirty = True
        self.signals.chapter_updated.emit(chapter.chapter_id)

    async def _do_chat_check(
        self, chapter: Chapter, job: Job, cancel_event: asyncio.Event
    ) -> None:
        assert self._checker is not None
        if chapter.state in (ChapterState.GENERATING, ChapterState.CHECKING):
            self.signals.log.emit("warn", f"{chapter.title} 正忙，手动消息未发送。")
            return
        thinking = self._thinking_for(chapter, "check")
        chapter.run_seq += 1
        run_seq = chapter.run_seq
        self.signals.conversation_started.emit(chapter.chapter_id, "check", run_seq)
        outcome = await self._checker.continue_manual_chat(
            chapter,
            job.text,
            thinking=thinking,
            on_delta=lambda d: self._forward_delta(chapter, "check", d, run_seq),
            on_event=lambda e: self._forward_event(chapter, e.kind, e.text),
            cancel_event=cancel_event,
        )
        if outcome.usage.completion_tokens:
            self._total_usage = self._total_usage.add(outcome.usage)
            self.signals.usage_received.emit(chapter.chapter_id, "check", outcome.usage)
        self.signals.conversation_finished.emit(
            chapter.chapter_id,
            "check",
            True if outcome.cancelled else outcome.ok,
            "已按你的要求停止。" if outcome.cancelled else outcome.error_text,
            run_seq,
        )
        self._conversations_dirty = True
        self.signals.chapter_updated.emit(chapter.chapter_id)

    # ================================================================== #
    def _forward_delta(
        self, chapter: Chapter, conv_kind: str, delta: StreamDelta, run_seq: int
    ) -> None:
        if delta.kind in ("content", "reasoning") and delta.text:
            self.signals.delta_received.emit(
                chapter.chapter_id, conv_kind, delta.kind, delta.text, run_seq
            )

    def _forward_event(self, chapter: Chapter, kind: str, text: str) -> None:
        if not text:
            return
        if kind == "retry":
            self.signals.log.emit("warn", f"{chapter.title}：{text}")
        elif kind == "info":
            self.signals.log.emit("info", f"{chapter.title}：{text}")
        elif kind == "note":
            self.signals.log.emit("debug", f"{chapter.title}｜{text}")

    def _emit_status(self) -> None:
        queue_len = len(self._queue) if self._thread.loop else 0
        self.signals.status.emit(queue_len, len(self._running), self._total_usage)
