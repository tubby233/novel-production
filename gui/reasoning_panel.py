"""思维链展示组件（默认折叠，可展开，超长自动收起）。

规则
----
* 默认折叠，提供"展开思考过程"按钮。
* 灰色/小字样式，与正文明确区分。
* 估算 token 数超过阈值（默认 2000）时自动收起，并提供"展示全部"。
* 思考阶段显示"正在思考…"动画，思考结束后切换为"回答中…"。
* 全局隐藏思维链开启时，整个面板变成一行灰字提示（内容仍在后台记录）。
* 只负责**展示**：从不把 reasoning_content 回传给 API，也不参与导出。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.config import CHARS_PER_TOKEN
from .help_icon import HelpIcon, attach_help

#: 流式渲染时最多保留的字符数（内存与重排保护；完整内容始终在对话对象里）
STREAM_RENDER_LIMIT = 20000

THINKING_FRAMES = ("正在思考", "正在思考.", "正在思考..", "正在思考...")


class ReasoningPanel(QWidget):
    """思维链面板。"""

    expandedChanged = Signal(bool)

    def __init__(
        self,
        *,
        collapse_tokens: int = 2000,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.collapse_tokens = max(100, int(collapse_tokens))
        self._buffer = ""
        self._expanded = False
        self._show_all = False
        self._globally_hidden = False
        self._phase = "idle"          # idle | thinking | answering | done
        self._frame = 0

        self._build_ui()

        self._anim = QTimer(self)
        self._anim.setInterval(320)
        self._anim.timeout.connect(self._tick)

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 4)
        root.setSpacing(2)

        header = QHBoxLayout()
        header.setSpacing(4)
        self.toggle_button = QPushButton(self)
        self.toggle_button.setFlat(True)
        self.toggle_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.toggle_button.setStyleSheet(
            "QPushButton { text-align: left; color: #57606a; font-size: 11px;"
            " border: none; padding: 0 2px; }"
            "QPushButton:hover { color: #0969da; }"
        )
        self.toggle_button.clicked.connect(self.toggle)
        header.addWidget(self.toggle_button)

        self.phase_label = QLabel(self)
        self.phase_label.setStyleSheet("color:#8250df; font-size:11px;")
        header.addWidget(self.phase_label)

        self.show_all_button = QPushButton("展示全部", self)
        self.show_all_button.setFlat(True)
        self.show_all_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.show_all_button.setStyleSheet(
            "QPushButton { color:#0969da; font-size:11px; border:none; padding:0 4px; }"
        )
        self.show_all_button.clicked.connect(self._on_show_all)
        self.show_all_button.setVisible(False)
        header.addWidget(self.show_all_button)

        header.addWidget(HelpIcon("show_reasoning", self, size=12))
        header.addStretch(1)
        root.addLayout(header)

        self.body = QPlainTextEdit(self)
        self.body.setReadOnly(True)
        self.body.setMaximumHeight(240)
        self.body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(max(8, self.font().pointSize() - 1))
        self.body.setFont(font)
        self.body.setStyleSheet(
            "QPlainTextEdit { background: #f6f8fa; color: #6b7280;"
            " border: 1px solid #d0d7de; border-left: 3px solid #8250df;"
            " border-radius: 4px; padding: 4px 6px; font-size: 11px; }"
        )
        attach_help(self.body, "show_reasoning")
        self.body.setVisible(False)
        root.addWidget(self.body)

        self.hidden_label = QLabel("思维链已在设置中全局隐藏（后台仍记录，可在消息历史中查看）。", self)
        self.hidden_label.setStyleSheet("color:#8a8f98; font-size:11px;")
        self.hidden_label.setVisible(False)
        root.addWidget(self.hidden_label)

    # ------------------------------------------------------------------ #
    # 状态
    # ------------------------------------------------------------------ #
    @property
    def buffer(self) -> str:
        return self._buffer

    @property
    def estimated_tokens(self) -> int:
        return max(0, int(len(self._buffer) / CHARS_PER_TOKEN))

    @property
    def is_expanded(self) -> bool:
        """思维链内容当前是否处于展开显示状态。"""
        return bool(self._expanded)

    def apply_global_hide(self, hidden: bool) -> None:
        self._globally_hidden = bool(hidden)
        self._refresh_visibility()

    def set_collapse_tokens(self, tokens: int) -> None:
        self.collapse_tokens = max(100, int(tokens))

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        """开始一次新的思考阶段。"""
        self._buffer = ""
        self._show_all = False
        self._phase = "thinking"
        self._frame = 0
        self.phase_label.setVisible(True)
        self.phase_label.setText(THINKING_FRAMES[0])
        self._anim.start()
        self._expanded = True
        self._refresh_visibility()
        self.expandedChanged.emit(True)

    def append(self, text: str) -> None:
        """追加思维链增量。"""
        if not text:
            return
        self._buffer += text
        if self._phase != "thinking":
            self._phase = "thinking"
            self._anim.start()
        self._refresh_body()
        self._refresh_header()

    def answering(self) -> None:
        """思考阶段结束、开始输出正文。"""
        if self._phase in ("done", "idle"):
            return
        self._phase = "answering"
        self._anim.stop()
        self.phase_label.setText("回答中…")
        self._refresh_visibility()

    def finish(self) -> None:
        """整个响应结束。"""
        self._phase = "done"
        self._anim.stop()
        self.phase_label.setText("")
        self.phase_label.setVisible(False)
        if self.estimated_tokens > self.collapse_tokens:
            self._expanded = False
        self._refresh_visibility()

    def clear(self) -> None:
        self._buffer = ""
        self._phase = "idle"
        self._show_all = False
        self._anim.stop()
        self.phase_label.setVisible(False)
        self._expanded = False
        self._refresh_visibility()

    # ------------------------------------------------------------------ #
    # 交互
    # ------------------------------------------------------------------ #
    def toggle(self) -> None:
        if not self._buffer:
            return
        self._expanded = not self._expanded
        self._refresh_visibility()
        self.expandedChanged.emit(self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        self._expanded = bool(expanded)
        self._refresh_visibility()

    def _on_show_all(self) -> None:
        self._show_all = not self._show_all
        self.show_all_button.setText("只看末尾" if self._show_all else "展示全部")
        self._refresh_body()

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(THINKING_FRAMES)
        self.phase_label.setText(THINKING_FRAMES[self._frame])

    def _refresh_header(self) -> None:
        tokens = self.estimated_tokens
        arrow = "▾" if self._expanded else "▸"
        action = "收起思考过程" if self._expanded else "展开思考过程"
        self.toggle_button.setText(f"{arrow} {action}（约 {tokens:,} tokens）")
        self.toggle_button.setVisible(bool(self._buffer) and not self._globally_hidden)
        self.show_all_button.setVisible(
            bool(self._buffer)
            and self._expanded
            and not self._globally_hidden
            and tokens > self.collapse_tokens
        )

    def _refresh_body(self) -> None:
        text = self._buffer
        if not self._show_all and len(text) > STREAM_RENDER_LIMIT:
            head = "……（前文较长，仅显示末尾部分，点“展示全部”查看完整思考过程）\n"
            text = head + text[-STREAM_RENDER_LIMIT:]
        if self.body.toPlainText() != text:
            self.body.setPlainText(text)
            self.body.verticalScrollBar().setValue(
                self.body.verticalScrollBar().maximum()
            )

    def _refresh_visibility(self) -> None:
        self._refresh_header()
        if self._globally_hidden:
            self.body.setVisible(False)
            self.hidden_label.setVisible(bool(self._buffer))
            self.toggle_button.setVisible(False)
            self.phase_label.setVisible(self._phase in ("thinking", "answering"))
            return

        self.hidden_label.setVisible(False)
        self.body.setVisible(self._expanded and bool(self._buffer))
        if self._expanded:
            self._refresh_body()
