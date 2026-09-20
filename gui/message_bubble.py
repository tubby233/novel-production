"""对话中的单条消息气泡（支持流式追加）。

* 正文与思维链分开放置：思维链在 :class:`ReasoningPanel` 中（灰色小字、可折叠），
  正文在下方文本区（正常字号）。
* 流式追加走"缓冲 + 定时节流刷新"，避免每个 token 都重排导致卡顿。
* 气泡只做展示，不做任何内容解析。
"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.models import SOURCE_LABELS, Message, Usage
from .reasoning_panel import ReasoningPanel

#: 流式刷新间隔（毫秒）
FLUSH_INTERVAL_MS = 90

#: 单条消息渲染的最大字符数（超出只显示末尾，完整内容仍在对话对象里）
RENDER_LIMIT = 40000

DOTS = ("", ".", "..", "...")


class MessageBubble(QWidget):
    """一条消息。流式时用 :meth:`begin_stream` / :meth:`append` / :meth:`end_stream`。"""

    requestCopy = Signal(str)

    def __init__(
        self,
        role: str,
        *,
        source: str = "user",
        text: str = "",
        reasoning: str = "",
        created_at: float | None = None,
        usage: Usage | None = None,
        interrupted: bool = False,
        collapse_tokens: int = 2000,
        global_hide_reasoning: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.role = role
        self.source = source
        self._content_buffer = text or ""
        self._usage = usage
        self._created_at = created_at or time.time()
        self._streaming = False
        self._frame = 0
        self._pending = False
        self._global_hide = global_hide_reasoning
        self.setObjectName("MessageBubble")

        # 定时器需在 _build_ui 之前建好：构造过程中就会调用 _refresh_content
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(FLUSH_INTERVAL_MS)
        self._flush_timer.timeout.connect(self._flush)

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(400)
        self._anim_timer.timeout.connect(self._tick)

        # 用成员 QTimer（而非 QTimer.singleShot）延后调整高度：
        # 气泡在列表重建时会被 deleteLater 销毁，成员定时器随对象一起销毁，
        # 不会在 C++ 对象已删除后再回调，避免 RuntimeError。
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.setInterval(0)
        self._height_timer.timeout.connect(self._adjust_height)

        self._build_ui(collapse_tokens)

        self.reasoning_panel.apply_global_hide(global_hide_reasoning)
        if reasoning:
            self.reasoning_panel.begin()
            self.reasoning_panel.append(reasoning)
            self.reasoning_panel.finish()
        if self._content_buffer:
            self._refresh_content()

    # ------------------------------------------------------------------ #
    def _build_ui(self, collapse_tokens: int) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(6)
        role_name = {"user": "你", "assistant": "AI", "system": "系统提示词"}.get(self.role, self.role)
        self.role_label = QLabel(f"<b>{role_name}</b>", self)
        header.addWidget(self.role_label)

        source_text = SOURCE_LABELS.get(self.source, self.source)
        self.source_label = QLabel(source_text, self)
        self.source_label.setStyleSheet("color:#8a8f98; font-size:11px;")
        header.addWidget(self.source_label)

        stamp = time.strftime("%H:%M:%S", time.localtime(self._created_at))
        self.time_label = QLabel(stamp, self)
        self.time_label.setStyleSheet("color:#8a8f98; font-size:11px;")
        header.addWidget(self.time_label)

        header.addStretch(1)
        self.usage_label = QLabel(self)
        self.usage_label.setStyleSheet("color:#8a8f98; font-size:11px;")
        header.addWidget(self.usage_label)
        self._refresh_usage()

        self.copy_button = QPushButton("复制", self)
        self.copy_button.setFlat(True)
        self.copy_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.copy_button.setStyleSheet("QPushButton { color:#0969da; font-size:11px; border:none; }")
        self.copy_button.clicked.connect(lambda: self.requestCopy.emit(self._content_buffer))
        header.addWidget(self.copy_button)

        self.status_label = QLabel(self)
        self.status_label.setStyleSheet("color:#8250df; font-size:11px;")
        header.addWidget(self.status_label)
        root.addLayout(header)

        # system 提示词：默认折叠显示
        self.reasoning_panel = ReasoningPanel(collapse_tokens=collapse_tokens, parent=self)
        self.reasoning_panel.setVisible(self.role == "assistant")
        root.addWidget(self.reasoning_panel)

        self.content_edit = QPlainTextEdit(self)
        self.content_edit.setReadOnly(True)
        self.content_edit.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.content_edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.content_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.content_edit.setWordWrapMode(self.content_edit.wordWrapMode())
        self.content_edit.setStyleSheet(
            "QPlainTextEdit { background: transparent; border: none; padding: 0; }"
        )
        self.content_edit.setVisible(False)
        root.addWidget(self.content_edit)

        self.interrupted_label = QLabel("⚠ 该回复被中断，内容可能不完整。", self)
        self.interrupted_label.setStyleSheet("color:#9a6700; font-size:11px;")
        self.interrupted_label.setVisible(False)
        root.addWidget(self.interrupted_label)

        if self.role == "user":
            self.setStyleSheet("QWidget#MessageBubble { background: #f6f8fa; border-radius: 6px; }")
        elif self.role == "system":
            self.setStyleSheet(
                "QWidget#MessageBubble { background: #fff8c5; border-radius: 6px; }"
            )
        else:
            self.setStyleSheet(
                "QWidget#MessageBubble { background: #ffffff; border: 1px solid #eaeef2;"
                " border-radius: 6px; }"
            )

    # ------------------------------------------------------------------ #
    # 流式接口
    # ------------------------------------------------------------------ #
    def begin_stream(self) -> None:
        self._streaming = True
        self._content_buffer = ""
        self.content_edit.setPlainText("")
        self.content_edit.setVisible(False)
        self.interrupted_label.setVisible(False)
        self.status_label.setText("正在思考…")
        self._anim_timer.start()
        if self.role == "assistant":
            self.reasoning_panel.begin()

    def append(self, kind: str, text: str) -> None:
        """追加流式增量。kind ∈ {"content", "reasoning"}。"""
        if not text:
            return
        if kind == "reasoning":
            if self.reasoning_panel.isHidden():
                self.reasoning_panel.setVisible(True)
                self.reasoning_panel.begin()
            self.reasoning_panel.append(text)
            return
        # 正文开始 -> 思考阶段结束
        if self.reasoning_panel.buffer and self.status_label.text().startswith("正在思考"):
            self.reasoning_panel.answering()
            self.status_label.setText("回答中…")
        self._content_buffer += text
        if len(self._content_buffer) > RENDER_LIMIT:
            overflow_note = "……（前文过长，仅显示末尾部分）\n"
            self._content_buffer = overflow_note + self._content_buffer[-RENDER_LIMIT:]
        self._pending = True
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def end_stream(self, *, final_text: str = "", interrupted: bool = False) -> None:
        """流结束。final_text 非空时用它覆盖缓冲（权威内容以引擎侧为准）。"""
        self._streaming = False
        self._anim_timer.stop()
        self._flush_timer.stop()
        if final_text:
            self._content_buffer = final_text
        self.status_label.setText("")
        self.reasoning_panel.finish()
        self._refresh_content()
        self.interrupted_label.setVisible(interrupted)

    def set_usage(self, usage: Usage | None) -> None:
        self._usage = usage
        self._refresh_usage()

    def set_content(self, text: str) -> None:
        self._content_buffer = text or ""
        self._refresh_content()

    @property
    def content(self) -> str:
        return self._content_buffer

    @property
    def is_streaming(self) -> bool:
        """是否正在流式输出（气泡内已有未结束的流）。"""
        return self._streaming

    def set_global_hide_reasoning(self, hidden: bool) -> None:
        self._global_hide = hidden
        self.reasoning_panel.apply_global_hide(hidden)

    def set_collapse_tokens(self, tokens: int) -> None:
        self.reasoning_panel.set_collapse_tokens(tokens)

    # ---- 一键折叠 / 展开（供 ConversationView 批量调用）---- #
    def collapse(self) -> None:
        """收起该气泡的思维链（正文始终保持可见）。"""
        self.reasoning_panel.set_expanded(False)

    def expand(self) -> None:
        """展开该气泡的思维链。"""
        self.reasoning_panel.set_expanded(True)

    @property
    def is_collapsed(self) -> bool:
        """思维链是否处于收起状态（没有思维链时视为"无处可收"）。"""
        if not self.reasoning_panel.buffer:
            return False
        return not self.reasoning_panel.is_expanded

    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(DOTS)
        if self.status_label.text().startswith("正在思考"):
            self.status_label.setText("正在思考" + DOTS[self._frame])
        elif self.status_label.text().startswith("回答中"):
            self.status_label.setText("回答中" + DOTS[self._frame])

    def _flush(self) -> None:
        if not self._pending:
            return
        self._pending = False
        self._refresh_content()

    def _refresh_usage(self) -> None:
        if not self._usage:
            self.usage_label.setText("")
            return
        parts = [f"输入 {self._usage.prompt_tokens:,}", f"输出 {self._usage.completion_tokens:,}"]
        if self._usage.reasoning_tokens:
            parts.append(f"其中思考 {self._usage.reasoning_tokens:,}")
        self.usage_label.setText("｜".join(parts))

    def _refresh_content(self) -> None:
        text = self._content_buffer
        if self.content_edit.toPlainText() != text:
            self.content_edit.setPlainText(text)
        self.content_edit.setVisible(bool(text))
        self._height_timer.start()

    def _adjust_height(self) -> None:
        try:
            doc = self.content_edit.document()
            doc.setTextWidth(max(200, self.content_edit.viewport().width()))
            height = int(doc.size().height()) + 10
            self.content_edit.setFixedHeight(max(28, min(height, 3000)))
        except RuntimeError:
            # 控件已被销毁（列表重建时）：忽略即可
            pass
