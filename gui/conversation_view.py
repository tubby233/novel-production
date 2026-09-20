"""单个对话的视图。

默认**只显示最后一次 assistant 输出的正文**（正文正在生成时，直接实时流式展示），
只有点击"查看详细对话"后才展示完整的 message history。
每个对话的深度思考参数默认折叠，点击"对话设置"才展开。

界面里的"生成对话"和"检查视图"各用一个实例，两者完全独立。
"""

from __future__ import annotations

import json
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.models import (
    CONVERSATION_LABELS,
    Chapter,
    Conversation,
    Message,
    ThinkingConfig,
    Usage,
)

from .help_icon import HelpIcon, attach_help
from .message_bubble import MessageBubble
from .thinking_panel import ThinkingPanel
from .widgets import confirm, info

BTN_SHOW_DETAIL = "查看详细对话"
BTN_SHOW_LAST = "只看最后正文"


class ConversationView(QWidget):
    """一个对话的界面：最后正文 / 完整对话 双模式 + 可折叠的对话设置。"""

    manualMessageSent = Signal(str)       # 用户手动输入的消息文本
    clearRequested = Signal()
    stopRequested = Signal()
    thinkingChanged = Signal(object)      # ThinkingConfig

    def __init__(
        self,
        conv_kind: str,
        *,
        global_hide_reasoning: bool = False,
        collapse_tokens: int = 2000,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.conv_kind = conv_kind
        self._global_hide = global_hide_reasoning
        self._collapse_tokens = collapse_tokens
        self._chapter: Chapter | None = None
        self._bubbles: list[MessageBubble] = []
        self._detail_bubble: MessageBubble | None = None
        self._detail_rendered_count = 0
        self._last_bubble: MessageBubble | None = None
        self._detail = False              # 默认只看最后正文
        self._busy = False

        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(0)
        self._scroll_timer.timeout.connect(self._do_scroll_to_bottom)

        self._build_ui()
        self._refresh_mode_button()
        self._set_busy(False)

    # ================================================================== #
    # 界面
    # ================================================================== #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        # ---------------- 标题栏 ---------------- #
        header = QHBoxLayout()
        header.setSpacing(6)
        self.title_label = QLabel(self)
        header.addWidget(self.title_label)
        header.addWidget(HelpIcon("act_view_history", self, size=12))
        header.addStretch(1)

        self.mode_button = QPushButton(BTN_SHOW_DETAIL, self)
        self.mode_button.setCheckable(True)
        self.mode_button.setToolTip(
            "<div style='max-width:440px; white-space:normal; font-size:9pt'>"
            "默认只显示<b>最后一次生成的正文</b>；正文正在生成时这里是实时流式输出。<br>"
            "点击“查看详细对话”可展示完整的 message history（含每条消息的来源与 token 用量）。</div>"
        )
        self.mode_button.toggled.connect(self._on_mode_toggled)
        header.addWidget(self.mode_button)

        # 一键折叠 / 展开本对话的全部消息（AI 回复 + 软件发给 AI 的消息都在同一个列表里）
        self.collapse_all_button = QPushButton("折叠全部", self)
        self.collapse_all_button.setToolTip(
            "一键收起本对话里所有消息的思维链（消息条目本身仍保留）。"
        )
        self.collapse_all_button.clicked.connect(self.collapse_all)
        header.addWidget(self.collapse_all_button)

        self.expand_all_button = QPushButton("展开全部", self)
        self.expand_all_button.setToolTip("一键展开本对话里所有消息的思维链。")
        self.expand_all_button.clicked.connect(self.expand_all)
        header.addWidget(self.expand_all_button)

        self.settings_button = QPushButton("对话设置", self)
        self.settings_button.setCheckable(True)
        self.settings_button.setChecked(False)
        self.settings_button.setToolTip("在**新窗口**中打开该对话的深度思考参数设置。")
        attach_help(self.settings_button, "thinking_enabled")
        self.settings_button.toggled.connect(self._on_settings_toggled)
        header.addWidget(self.settings_button)

        self.history_button = QPushButton("消息历史(JSON)", self)
        self.history_button.setCheckable(True)
        attach_help(self.history_button, "act_view_history")
        self.history_button.toggled.connect(self._on_history_toggled)
        header.addWidget(self.history_button)

        self.clear_button = QPushButton("清空对话", self)
        attach_help(self.clear_button, "act_clear_conversation")
        self.clear_button.clicked.connect(self._on_clear)
        header.addWidget(self.clear_button)

        # 注意：这里不再放"停止"按钮——章节视图右上角已有全局的"停止本章"，
        # 按钮栏太挤会影响"折叠全部 / 展开全部"的可见性。
        root.addLayout(header)

        # ---------------- 对话设置（放在**新窗口**里，按需求不在主界面内展开） ---------------- #
        self.thinking_panel = ThinkingPanel(
            f"{CONVERSATION_LABELS.get(self.conv_kind, self.conv_kind)}的深度思考参数",
            ThinkingConfig(),
            global_hide_reasoning=self._global_hide,
            parent=self,
        )
        self.thinking_panel.thinkingChanged.connect(self._on_thinking_changed)
        self.thinking_panel.hide()      # 只有"对话设置"窗口里才显示它
        self._settings_window: QDialog | None = None

        # ---------------- 内容区 ---------------- #
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll_container = QWidget(self.scroll)
        self.messages_layout = QVBoxLayout(self.scroll_container)
        self.messages_layout.setContentsMargins(2, 2, 2, 2)
        self.messages_layout.setSpacing(6)
        self.messages_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_container)
        self.splitter.addWidget(self.scroll)

        self.history_browser = QTextBrowser(self)
        self.history_browser.setVisible(False)
        self.history_browser.setMinimumWidth(260)
        attach_help(self.history_browser, "act_view_history")
        self.splitter.addWidget(self.history_browser)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)
        root.addWidget(self.splitter, 1)

        # ---------------- 输入区 ---------------- #
        composer = QHBoxLayout()
        composer.setSpacing(6)
        self.input_edit = QPlainTextEdit(self)
        self.input_edit.setPlaceholderText(
            "手动消息：不经过任何模板，直接作为 user message 发送（Ctrl+Enter 发送）"
        )
        self.input_edit.setMaximumHeight(84)
        attach_help(self.input_edit, "act_send_manual")
        composer.addWidget(self.input_edit, 1)

        self.send_button = QPushButton("发送", self)
        attach_help(self.send_button, "act_send_manual")
        self.send_button.clicked.connect(self._on_send)
        composer.addWidget(self.send_button)
        root.addLayout(composer)

        self.input_edit.installEventFilter(self)

    # ================================================================== #
    # 事件过滤：Ctrl+Enter 发送
    # ================================================================== #
    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.input_edit and event.type() == event.Type.KeyPress:
            if (
                event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier
            ):
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    # ================================================================== #
    # 数据绑定
    # ================================================================== #
    def set_chapter(self, chapter: Chapter | None) -> None:
        """切换章节：重置为默认视图（只看最后正文 + 折叠对话设置）。"""
        self._chapter = chapter
        self._reset_modes()
        self.render()

    def _reset_modes(self) -> None:
        for button, checked in (
            (self.mode_button, False),
            (self.settings_button, False),
            (self.history_button, False),
        ):
            blocked = button.blockSignals(True)
            button.setChecked(checked)
            button.blockSignals(blocked)
        self._detail = False
        self._close_settings_window()
        self.history_browser.setVisible(False)
        self._refresh_mode_button()

    def conversation(self) -> Conversation | None:
        if self._chapter is None:
            return None
        return (
            self._chapter.check_conv
            if self.conv_kind == "check"
            else self._chapter.generate_conv
        )

    def apply_global_settings(self, *, hide_reasoning: bool, collapse_tokens: int) -> None:
        self._global_hide = hide_reasoning
        self._collapse_tokens = collapse_tokens
        self.thinking_panel.set_global_hide(hide_reasoning)
        for bubble in self._bubbles:
            bubble.set_global_hide_reasoning(hide_reasoning)
            bubble.set_collapse_tokens(collapse_tokens)
        self._sync_collapse_buttons()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.thinking_panel.set_running(busy)
        self._set_busy(busy)

    def _set_busy(self, busy: bool) -> None:
        self.send_button.setEnabled(not busy)
        self.send_button.setToolTip(
            "请等待当前生成/检查完成或先停止。"
            if busy
            else "把输入框内容作为 user message 直接发送（不经过模板）。"
        )
        self.clear_button.setStyleSheet("color:#8a8f98;" if busy else "")

    # ================================================================== #
    # 模式切换
    # ================================================================== #
    def _on_mode_toggled(self, checked: bool) -> None:
        self._detail = bool(checked)
        self._refresh_mode_button()
        self.render()

    def _refresh_mode_button(self) -> None:
        self.mode_button.setText(BTN_SHOW_LAST if self._detail else BTN_SHOW_DETAIL)

    def show_detail(self, detail: bool = True) -> None:
        if self.mode_button.isChecked() != detail:
            self.mode_button.setChecked(detail)
        else:
            self._on_mode_toggled(detail)

    def show_settings(self, visible: bool = True) -> None:
        self.settings_button.setChecked(visible)
        if visible:
            self._open_settings_window()
        else:
            self._close_settings_window()

    def settings_window_open(self) -> bool:
        """对话设置窗口当前是否处于打开状态。"""
        return self._settings_window is not None and self._settings_window.isVisible()

    def _on_settings_toggled(self, checked: bool) -> None:
        if checked:
            if self._open_settings_window():
                self.settings_button.setText("关闭设置窗口")
                return
        # 打开失败（或用户关掉了开关）时回到未选中的状态
        blocked = self.settings_button.blockSignals(True)
        self.settings_button.setChecked(False)
        self.settings_button.blockSignals(blocked)
        self.settings_button.setText("对话设置")
        self._close_settings_window()

    # ------------------------------------------------------------------ #
    # 对话设置窗口
    # ------------------------------------------------------------------ #
    def _open_settings_window(self) -> bool:
        """在独立窗口里显示深度思考设置。窗口已开着时只置顶，不重复创建。"""
        if self._settings_window is None:
            window = QDialog(self)
            window.setWindowTitle(
                f"{CONVERSATION_LABELS.get(self.conv_kind, self.conv_kind)} · 对话设置"
            )
            window.resize(560, 320)
            layout = QVBoxLayout(window)
            layout.setSpacing(8)
            hint = QLabel(
                "这里的改动只影响该对话**后续**的请求；参数随章节数据一起保存。",
                window,
            )
            hint.setWordWrap(True)
            hint.setStyleSheet("color:#57606a; font-size:11px;")
            layout.addWidget(hint)
            layout.addWidget(self.thinking_panel, 1)
            layout.addStretch(1)
            window.finished.connect(self._on_settings_window_closed)
            self._settings_window = window
        # 关键：设置面板平时是隐藏的（不占主界面空间），
        # 挪进窗口后必须显式 show()，否则窗口会是空的。
        self.thinking_panel.show()
        self._settings_window.show()
        self._settings_window.raise_()
        self._settings_window.activateWindow()
        return True

    def _on_settings_window_closed(self, _result: int) -> None:
        window = self._settings_window
        self._settings_window = None
        # 对话设置面板的父控件恒为 self：关闭窗口时把它放回去，
        # 否则 Qt 会连带销毁这个被复用的控件。
        self.thinking_panel.setParent(self)
        self.thinking_panel.hide()
        blocked = self.settings_button.blockSignals(True)
        self.settings_button.setChecked(False)
        self.settings_button.blockSignals(blocked)
        self.settings_button.setText("对话设置")
        if window is not None:
            window.deleteLater()

    def _close_settings_window(self) -> None:
        window = self._settings_window
        if window is None:
            return
        self._settings_window = None
        self.thinking_panel.setParent(self)
        self.thinking_panel.hide()
        window.close()

    def close_settings_window(self) -> None:
        """外部（例如主窗口关闭时）确保设置窗口被收掉。"""
        self._close_settings_window()
        blocked = self.settings_button.blockSignals(True)
        self.settings_button.setChecked(False)
        self.settings_button.blockSignals(blocked)
        self.settings_button.setText("对话设置")

    def _on_history_toggled(self, checked: bool) -> None:
        self.history_browser.setVisible(bool(checked))
        if checked:
            self._refresh_history()

    # ================================================================== #
    # 一键折叠 / 展开全部消息
    # ================================================================== #
    def collapse_all(self) -> None:
        """一键折叠本对话的全部消息（含 AI 回复与软件发给 AI 的消息）。"""
        for bubble in self._bubbles:
            bubble.collapse()
        self._sync_collapse_buttons()

    def expand_all(self) -> None:
        """一键展开本对话的全部消息（思维链等折叠内容）。"""
        for bubble in self._bubbles:
            bubble.expand()
        self._sync_collapse_buttons()

    @property
    def collapsible_bubbles(self) -> list[MessageBubble]:
        """真正带思维链（可折叠内容）的气泡。"""
        return [b for b in self._bubbles if b.reasoning_panel.buffer]

    def _sync_collapse_buttons(self) -> None:
        """按当前气泡状态刷新按钮可用性（全部都收起了，"折叠全部"就没有意义）。"""
        if not hasattr(self, "collapse_all_button"):
            return    # 构造期间可能被提前调用
        collapsible = self.collapsible_bubbles
        collapsed = sum(1 for bubble in collapsible if bubble.is_collapsed)
        total = len(collapsible)
        self.collapse_all_button.setEnabled(not (total > 0 and collapsed == total))
        self.collapse_all_button.setToolTip(
            f"本对话共有 {total} 条消息带思维链，其中 {collapsed} 条处于收起状态。"
            if total
            else "本对话还没有可折叠的思维链内容。"
        )
        self.expand_all_button.setEnabled(total > 0)

    # ================================================================== #
    # 渲染
    # ================================================================== #
    def render(self) -> None:
        self._clear_bubbles()
        conv = self.conversation()
        kind_label = CONVERSATION_LABELS.get(self.conv_kind, self.conv_kind)

        if conv is None:
            self.title_label.setText(f"<b>{kind_label}</b>（尚未创建）")
            self._add_empty_bubble("该对话还没有创建。生成正文（或触发检查）后这里会显示内容。")
            self._refresh_history()
            self._sync_collapse_buttons()
            return

        self.title_label.setText(
            f"<b>{kind_label}</b>　消息 {len(conv.messages)} 条　"
            f"状态：{self._status_text(conv)}"
            f"　{'｜完整对话' if self._detail else '｜只看最后正文'}"
        )
        self.thinking_panel.set_thinking(conv.thinking)

        if self._detail:
            self._render_detail(conv)
        else:
            self._render_last(conv)

        self._refresh_history()
        self._sync_collapse_buttons()
        self._scroll_to_bottom()

    def _render_detail(self, conv: Conversation) -> None:
        for message in conv.messages:
            bubble = self._add_bubble_for(message)
            if message.role == "assistant":
                self._detail_bubble = bubble
        self._detail_rendered_count = len(conv.messages)
        if conv.status == "running" and self._detail_bubble is None:
            self._detail_bubble = self._ensure_streaming_bubble()
        elif conv.status == "running" and self._detail_bubble is not None:
            pass

    def _render_last(self, conv: Conversation) -> None:
        """只展示最后一次 assistant 输出的正文；正在生成时实时流式展示。"""
        last_assistant = conv.last_assistant()
        if last_assistant is not None and last_assistant.content:
            self._last_bubble = self._add_bubble_for(last_assistant)
            return
        if conv.status == "running":
            self._last_bubble = self._ensure_streaming_bubble()
            return
        self._add_empty_bubble(
            "该对话还没有正文内容。生成完成后这里会显示最后一次生成的完整正文。"
        )

    def _add_empty_bubble(self, text: str) -> None:
        label = QLabel(text, self.scroll_container)
        label.setWordWrap(True)
        label.setStyleSheet("color:#8a8f98; font-size:11px; padding:8px;")
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, label)

    def _status_text(self, conv: Conversation) -> str:
        return {
            "idle": "空闲",
            "running": "运行中",
            "done": "已完成",
            "error": "出错",
            "cancelled": "已停止",
        }.get(conv.status, conv.status)

    def _refresh_history(self) -> None:
        conv = self.conversation()
        if conv is None:
            self.history_browser.setHtml("<i>尚无对话。</i>")
            return

        rows: list[str] = []
        for index, message in enumerate(conv.messages, start=1):
            role = {"system": "系统提示词", "user": "用户", "assistant": "AI"}.get(
                message.role, message.role
            )
            stamp = time.strftime("%H:%M:%S", time.localtime(message.created_at or 0.0))
            usage = ""
            if message.usage:
                usage = (
                    f"　<span style='color:#8a8f98'>tokens: 输入 {message.usage.prompt_tokens} / "
                    f"输出 {message.usage.completion_tokens}</span>"
                )
            interrupted = (
                "　<span style='color:#9a6700'>[已中断]</span>" if message.interrupted else ""
            )
            body = _escape(message.content[:4000])
            if len(message.content) > 4000:
                body += "\n……（此处仅显示前 4000 字符）"
            reasoning_hint = (
                f"<div style='color:#8a8f98; font-size:9pt'>含思维链 {len(message.reasoning):,} 字符"
                f"（不回传 API、不参与导出）</div>"
                if message.reasoning
                else ""
            )
            rows.append(
                f"<div style='margin-bottom:10px'>"
                f"<b>#{index} {role}</b>　<span style='color:#57606a'>{message.source}</span>　"
                f"<span style='color:#8a8f98'>{stamp}</span>{usage}{interrupted}"
                f"{reasoning_hint}"
                f"<pre style='white-space:pre-wrap; font-size:9pt; margin:2px 0'>{body}</pre>"
                f"</div>"
            )
        self.history_browser.setHtml("".join(rows) or "<i>尚无消息。</i>")

    # ================================================================== #
    # 气泡管理
    # ================================================================== #
    def _clear_bubbles(self) -> None:
        self._detail_bubble = None
        self._last_bubble = None
        self._detail_rendered_count = 0
        for bubble in self._bubbles:
            self.messages_layout.removeWidget(bubble)
            bubble.setParent(None)
            bubble.deleteLater()
        self._bubbles.clear()
        # 清掉占位提示文字
        for index in range(self.messages_layout.count() - 1, -1, -1):
            item = self.messages_layout.itemAt(index)
            widget = item.widget() if item else None
            if isinstance(widget, QLabel):
                self.messages_layout.removeWidget(widget)
                widget.setParent(None)
                widget.deleteLater()

    def _add_bubble_for(self, message: Message) -> MessageBubble:
        return self._add_bubble(
            message.role,
            source=message.source,
            text=message.content,
            reasoning=message.reasoning,
            created_at=message.created_at,
            usage=message.usage,
            interrupted=message.interrupted,
        )

    def _add_bubble(
        self,
        role: str,
        *,
        source: str = "user",
        text: str = "",
        reasoning: str = "",
        created_at: float | None = None,
        usage: Usage | None = None,
        interrupted: bool = False,
        streaming: bool = False,
    ) -> MessageBubble:
        bubble = MessageBubble(
            role,
            source=source,
            text=text,
            reasoning=reasoning,
            created_at=created_at,
            usage=usage,
            interrupted=interrupted,
            collapse_tokens=self._collapse_tokens,
            global_hide_reasoning=self._global_hide,
            parent=self.scroll_container,
        )
        bubble.requestCopy.connect(self._on_copy)
        self.messages_layout.insertWidget(self.messages_layout.count() - 1, bubble)
        self._bubbles.append(bubble)
        if streaming:
            bubble.begin_stream()
            if self._detail_bubble is None and self._detail:
                self._detail_bubble = bubble
        return bubble

    def _on_copy(self, text: str) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)

    # ================================================================== #
    # 流式接口（由 MainWindow 调用）
    # ================================================================== #
    def stream_started(self, conv: Conversation) -> None:
        """对话开始新一轮输出。

        * 只看最后正文模式：直接为该轮创建一个流式气泡（不重建整个列表，避免闪烁）；
        * 完整对话模式：重建列表，已发出的消息都在，末尾追加流式气泡。
        """
        if self._detail:
            self.render()
        else:
            self._ensure_streaming_bubble()

    def _ensure_streaming_bubble(self) -> MessageBubble:
        """确保存在一个正在流式输出的气泡。

        注意：`self._last_bubble` 可能已经在详情模式下指向某个历史气泡，
        因此这里用 is_streaming 判断，避免把增量写进历史气泡。
        """
        if self._last_bubble is None or not self._last_bubble.is_streaming:
            self._last_bubble = self._add_bubble(
                "assistant", source="assistant", streaming=True
            )
        return self._last_bubble

    def stream_delta(self, kind: str, text: str) -> None:
        """流式增量：只看最后正文 / 完整对话 两种模式都追加到同一个流式气泡。"""
        bubble = self._last_bubble
        if bubble is None or not bubble.is_streaming:
            bubble = self._ensure_streaming_bubble()
        bubble.append(kind, text)
        if (
            self._detail
            and self._detail_bubble is not None
            and self._detail_bubble is not bubble
        ):
            # 详情模式下另有独立气泡时，同步一份（两者内容一致）
            self._detail_bubble.append(kind, text)
        self._scroll_to_bottom()

    def stream_finished(self, ok: bool, error: str = "") -> None:
        if self._last_bubble is not None and self._last_bubble.is_streaming:
            self._last_bubble.end_stream()
        if self._detail_bubble is not None and self._detail_bubble.is_streaming:
            self._detail_bubble.end_stream()
        self.render()
        if not ok and error:
            self.title_label.setText(
                self.title_label.text() + "　<span style='color:#cf222e'>失败</span>"
            )

    def _scroll_to_bottom(self) -> None:
        """延后一格滚动到底部。

        用成员定时器而不是 QTimer.singleShot：本控件被销毁后定时器随之销毁，
        不会再对已删除的 C++ 对象调用方法。
        """
        self._scroll_timer.start()

    def _do_scroll_to_bottom(self) -> None:
        try:
            bar = self.scroll.verticalScrollBar()
            bar.setValue(bar.maximum())
        except RuntimeError:
            pass

    # ================================================================== #
    # 交互
    # ================================================================== #
    def _on_send(self) -> None:
        text = self.input_edit.toPlainText().strip()
        if not text:
            info(self, "提示", "请输入要发送的内容。")
            return
        if self._busy:
            info(self, "请稍候", "请等待当前生成/检查完成或先停止。")
            return
        self.input_edit.clear()
        self.manualMessageSent.emit(text)

    def _on_clear(self) -> None:
        if self._busy:
            info(self, "请稍候", "请等待当前生成/检查完成或先停止。")
            return
        if not confirm(
            self,
            "清空对话",
            "将删除该对话的全部消息（多轮上下文会丢失），确定继续吗？",
        ):
            return
        self.clearRequested.emit()

    def _on_thinking_changed(self, thinking: ThinkingConfig) -> None:
        self.thinkingChanged.emit(thinking)

    # ================================================================== #
    def messages_as_json(self) -> str:
        conv = self.conversation()
        if conv is None:
            return "{}"
        return json.dumps(
            {
                "conversation": conv.conv_id,
                "kind": conv.kind,
                "thinking": conv.thinking.to_dict(),
                "messages": [m.to_dict() for m in conv.messages],
            },
            ensure_ascii=False,
            indent=2,
        )


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
