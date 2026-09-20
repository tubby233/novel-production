"""单章详情视图：正文 / 生成对话 / 检查视图 三视图切换。

布局要点
--------
* 顶部条固定放"与视图无关"的操作：**停止本章 / 触发检查 / 重新生成**。
* 顶部条下面是**决策行**：**通过 / 按要求重新生成正文 / 暂不处理**。
  需求：这一行只在章节处于 **待决策** 状态时出现，且只在 **正文视图 / 检查视图**
  可见（"生成对话"视图是查看与继续对话的地方，不显示决策按钮）；按钮文字写清楚，
  不使用只有一个符号的图标按钮。
* **默认视图跟随章节状态**：
    检查中 / 待决策 -> 检查视图（用户最需要看的就是检查结果）；
    其它状态      -> 正文页。
* **检查视图**：上方是"检查结果（可编辑）"，下方是**完整对话**（默认已展开），
  检查完成后结果自动填入文本框。
* 队列提示用轻量的"提示条"（toast）给出，不打断操作。

职责边界
--------
* 本视图**只展示与收集输入**：正文文本、检查结果文本、用户点击了哪个按钮。
* 状态迁移、并发调度、消息模板填充全部在 core.engine 中完成。
* 忙状态下（生成中/检查中）所有会改写数据的按钮视觉置灰，
  点击时弹出提示"请等待当前生成/检查完成或先停止"，同时提供"立即停止"入口。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.models import Chapter, ChapterState, STATE_LABELS

from .conversation_view import ConversationView
from .help_icon import HelpIcon, attach_help
from .widgets import StateBadge, confirm, info

BUSY_HINT = "请等待当前生成/检查完成或先停止"

VIEW_CONTENT = "正文"
VIEW_GENERATE = "生成对话"
VIEW_CHECK = "检查视图"

#: 默认进入"检查视图"的状态：这两种状态下用户最关心的是检查结果与决策按钮
CHECK_FIRST_STATES = (ChapterState.CHECKING, ChapterState.AWAITING_DECISION)

#: 提示条显示时长（毫秒）
TOAST_MS = 6000


class ChapterView(QWidget):
    """某一章的完整操作界面。"""

    generateRequested = Signal()
    triggerCheck = Signal()
    regenByCheck = Signal()
    regenManual = Signal()
    accept = Signal()
    postpone = Signal()
    stopChapter = Signal()
    contentEdited = Signal(str)
    checkResultEdited = Signal(str)
    generateThinkingChanged = Signal(object)
    checkThinkingChanged = Signal(object)
    generateManualMessage = Signal(str)
    checkManualMessage = Signal(str)
    clearGenerateConversation = Signal()
    clearCheckConversation = Signal()

    def __init__(
        self,
        *,
        global_hide_reasoning: bool = False,
        collapse_tokens: int = 2000,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._chapter: Chapter | None = None
        self._loading = False
        self._busy = False
        self._streaming = False
        self._last_state = ChapterState.PENDING
        self._global_hide_reasoning = bool(global_hide_reasoning)
        self._collapse_tokens = int(collapse_tokens)
        self._build_ui()
        self._refresh_buttons()

    # ================================================================== #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---------------- 顶部条 ---------------- #
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title_label = QLabel("未选择章节", self)
        self.title_label.setStyleSheet("font-size: 14px; font-weight: bold;")
        header.addWidget(self.title_label)

        self.badge = StateBadge(ChapterState.PENDING, self)
        header.addWidget(self.badge)

        self.phase_label = QLabel("", self)
        self.phase_label.setStyleSheet("color:#0969da; font-size:11px;")
        header.addWidget(self.phase_label)
        header.addStretch(1)

        # 这三个操作与"当前在哪个视图"无关，放在顶部条上随时可用
        self.stop_button = QPushButton("停止本章", self)
        attach_help(self.stop_button, "act_stop_chapter")
        self.stop_button.clicked.connect(self.stopChapter.emit)
        header.addWidget(self.stop_button)

        self.check_trigger_button = QPushButton("触发检查", self)
        attach_help(self.check_trigger_button, "act_trigger_check")
        self.check_trigger_button.clicked.connect(lambda: self._guarded(self.triggerCheck.emit))
        header.addWidget(self.check_trigger_button)

        self.regen_button = QPushButton("重新生成", self)
        self.regen_button.setToolTip(
            "重新生成整章正文。\n"
            "· 本章处于“待生成/出错”或生成对话已被清空时：直接按本章大纲重新生成；\n"
            "· 已有正文时：可以填写额外的修改要求，按你的要求重写。\n"
            "点击后任务排在队列末尾，状态先变为“排队中”。"
        )
        attach_help(self.regen_button, "act_regen_manual")
        self.regen_button.clicked.connect(lambda: self._guarded(self.regenManual.emit))
        header.addWidget(self.regen_button)

        header.addSpacing(12)
        header.addWidget(QLabel("视图", self))

        # 视图切换用**按钮组**：单击即切换（下拉框需要"展开再选"，要两下）
        self.view_buttons: dict[str, QPushButton] = {}
        self.view_group = QButtonGroup(self)
        self.view_group.setExclusive(True)
        for name in (VIEW_CONTENT, VIEW_GENERATE, VIEW_CHECK):
            button = QPushButton(name, self)
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            self.view_group.addButton(button)
            header.addWidget(button)
            self.view_buttons[name] = button
            button.clicked.connect(lambda _=False, n=name: self._on_view_button(n))

        tooltip = (
            "<div style='max-width:420px; white-space:normal; font-size:9pt'>"
            "<b>正文</b>：默认视图，显示最近一次生成的完整正文（生成中会实时流式追加）。<br>"
            "<b>生成对话</b>：该章的生成对话（默认只看最后正文，可展开完整 message history）。<br>"
            "<b>检查视图</b>：检查结果（可编辑）+ 按要求重新生成正文，下方是完整检查对话。</div>"
        )
        for button in self.view_buttons.values():
            button.setToolTip(tooltip)
            button.setToolTipDuration(60000)
        header.addWidget(HelpIcon("act_switch_view", self, size=12))
        root.addLayout(header)

        # ---------------- 决策行（只有"待决策"时出现） ---------------- #
        # 需求：通过 / 按要求重新生成正文只在 **正文视图** 与 **检查视图** 里显示，
        #       且只有章节处于"待决策"状态时才显示；按钮文字要写清楚，不要只放一个 √。
        self.decision_row = QWidget(self)
        decision_layout = QHBoxLayout(self.decision_row)
        decision_layout.setContentsMargins(0, 0, 0, 0)
        decision_layout.setSpacing(8)

        self.accept_button = QPushButton("通过", self)
        self.accept_button.setToolTip("把本章标记为合格（导出时不再加【未确认】标记）。")
        self.accept_button.setMinimumWidth(96)
        self.accept_button.setStyleSheet(
            "QPushButton { font-weight: bold; color: #ffffff; background: #1a7f37;"
            " border: 1px solid #1a7f37; border-radius: 4px; padding: 4px 12px; }"
            "QPushButton:hover { background: #15692e; }"
            "QPushButton:disabled { color:#8a8f98; background:#f6f8fa; border-color:#d8dee4; }"
        )
        attach_help(self.accept_button, "act_accept")
        self.accept_button.clicked.connect(lambda: self._guarded(self.accept.emit))
        decision_layout.addWidget(self.accept_button)

        self.regen_decision_button = QPushButton("按要求重新生成正文", self)
        self.regen_decision_button.setToolTip(
            "按当前检查结果框里的文本重新生成整章正文（生成后自动再次进入检查）。"
        )
        attach_help(self.regen_decision_button, "act_regen_by_check")
        self.regen_decision_button.clicked.connect(
            lambda: self._guarded(self.regenByCheck.emit)
        )
        decision_layout.addWidget(self.regen_decision_button)

        self.postpone_button = QPushButton("暂不处理", self)
        self.postpone_button.setToolTip("保持“待决策”，稍后再决定。")
        attach_help(self.postpone_button, "act_postpone")
        self.postpone_button.clicked.connect(lambda: self._guarded(self.postpone.emit))
        decision_layout.addWidget(self.postpone_button)

        self.decision_hint_inline = QLabel(
            "检查已完成：可直接修改检查结果，再点「按要求重新生成正文」；"
            "满意则点「通过」。",
            self.decision_row,
        )
        self.decision_hint_inline.setWordWrap(True)
        self.decision_hint_inline.setStyleSheet("color:#9a6700; font-size:11px;")
        decision_layout.addWidget(self.decision_hint_inline, 1)
        self.decision_row.setVisible(False)
        root.addWidget(self.decision_row)

        # ---------------- 队列提示条（默认隐藏） ---------------- #
        self.toast_label = QLabel("", self)
        self.toast_label.setWordWrap(True)
        self.toast_label.setVisible(False)
        self.toast_label.setStyleSheet(
            "QLabel { color:#0a3069; background:#ddf4ff; border:1px solid #54aeff;"
            " border-radius:4px; padding:4px 8px; font-size:11px; }"
        )
        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.setInterval(TOAST_MS)
        self._toast_timer.timeout.connect(lambda: self.toast_label.setVisible(False))
        root.addWidget(self.toast_label)

        # ---------------- 主体 ---------------- #
        self.splitter = QSplitter(Qt.Orientation.Vertical, self)

        # 1) 正文
        self.content_page = self._build_content_page()
        self.splitter.addWidget(self.content_page)

        # 2) 生成对话
        self.generate_view = ConversationView(
            "generate",
            global_hide_reasoning=self._global_hide_reasoning,
            collapse_tokens=self._collapse_tokens,
            parent=self,
        )
        self.generate_view.manualMessageSent.connect(self.generateManualMessage.emit)
        self.generate_view.clearRequested.connect(self.clearGenerateConversation.emit)
        self.generate_view.stopRequested.connect(self.stopChapter.emit)
        self.generate_view.thinkingChanged.connect(self.generateThinkingChanged.emit)
        self.splitter.addWidget(self.generate_view)

        # 3) 检查视图
        self.check_page = self._build_check_page(
            self._global_hide_reasoning, self._collapse_tokens
        )
        self.splitter.addWidget(self.check_page)

        root.addWidget(self.splitter, 1)
        self.view_buttons[VIEW_CONTENT].setChecked(True)
        self._on_view_button(VIEW_CONTENT)

    # ------------------------------------------------------------------ #
    def _build_content_page(self) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(6)
        self.content_hint = QLabel(
            "默认展示该章最近一次生成的完整正文（导出时也以这里的内容为准）。", page
        )
        self.content_hint.setStyleSheet("color:#8a8f98; font-size:11px;")
        bar.addWidget(self.content_hint)
        bar.addStretch(1)

        self.edit_toggle = QPushButton("✎ 编辑正文", page)
        self.edit_toggle.setCheckable(True)
        attach_help(self.edit_toggle, "act_edit_content")
        self.edit_toggle.toggled.connect(self._on_edit_toggled)
        bar.addWidget(self.edit_toggle)

        self.copy_content_button = QPushButton("复制正文", page)
        self.copy_content_button.setToolTip("复制当前正文到剪贴板。")
        self.copy_content_button.clicked.connect(self._on_copy_content)
        bar.addWidget(self.copy_content_button)

        self.generate_button = QPushButton("生成正文", page)
        attach_help(self.generate_button, "act_generate")
        self.generate_button.clicked.connect(lambda: self._guarded(self._emit_generate))
        bar.addWidget(self.generate_button)
        layout.addLayout(bar)

        self.content_edit = QPlainTextEdit(page)
        self.content_edit.setReadOnly(True)
        self.content_edit.setPlaceholderText(
            "该章还没有正文。点击“生成正文”，或在工具栏对勾选的章节批量生成。"
        )
        attach_help(self.content_edit, "act_edit_content")
        self.content_edit.textChanged.connect(self._on_content_changed)
        layout.addWidget(self.content_edit, 1)
        return page

    def _build_check_page(self, global_hide_reasoning: bool, collapse_tokens: int) -> QWidget:
        page = QWidget(self)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # ---------------- 检查结果（可编辑） + 按要求重新生成 ---------------- #
        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(QLabel("<b>检查结果（可直接修改）</b>", page))
        head.addWidget(HelpIcon("act_regen_by_check", page, size=12))
        head.addStretch(1)

        self.history_combo = QComboBox(page)
        self.history_combo.setMinimumWidth(160)
        self.history_combo.setToolTip(
            "<div style='max-width:420px; white-space:normal; font-size:9pt'>"
            "历次检查结果（每次检查都是全新对话，历史结果保留在这里）。<br>"
            "选择后会把该次结果填入下方文本框，可继续编辑。</div>"
        )
        self.history_combo.currentIndexChanged.connect(self._on_history_selected)
        head.addWidget(self.history_combo)

        self.copy_check_button = QPushButton("复制检查结果", page)
        self.copy_check_button.clicked.connect(self._on_copy_check)
        head.addWidget(self.copy_check_button)
        layout.addLayout(head)

        result_row = QHBoxLayout()
        result_row.setSpacing(8)
        self.check_edit = QPlainTextEdit(page)
        self.check_edit.setPlaceholderText(
            "检查完成后，检查对话的输出会自动填入这里；你可以直接修改，"
            "再点右侧的“按要求重新生成正文”。"
        )
        self.check_edit.setMinimumHeight(110)
        self.check_edit.setMaximumHeight(260)
        attach_help(self.check_edit, "act_regen_by_check")
        self.check_edit.textChanged.connect(self._on_check_changed)
        result_row.addWidget(self.check_edit, 1)

        # 「按要求重新生成正文」「通过」都在顶部的**决策行**里（正文/检查视图都会显示），
        # 因此这里只留检查结果编辑框与提示，不再重复放一份按钮。
        layout.addLayout(result_row)

        self.check_action_hint = QLabel(
            "提示：「通过」「按要求重新生成正文」「暂不处理」在章节视图顶部的**决策行**"
            "（正文视图与检查视图都能看到），且只在章节处于“待决策”状态时出现。",
            page,
        )
        self.check_action_hint.setWordWrap(True)
        self.check_action_hint.setStyleSheet("color:#8a8f98; font-size:11px;")
        layout.addWidget(self.check_action_hint)

        self.decision_hint = QLabel("", page)
        self.decision_hint.setWordWrap(True)
        self.decision_hint.setStyleSheet("color:#9a6700; font-size:11px;")
        layout.addWidget(self.decision_hint)

        # ---------------- 检查对话（完整对话视图） ---------------- #
        conv_head = QHBoxLayout()
        conv_head.setSpacing(6)
        conv_head.addWidget(QLabel("<b>检查对话（完整对话）</b>", page))
        conv_head.addWidget(HelpIcon("act_view_history", page, size=12))
        conv_head.addStretch(1)
        layout.addLayout(conv_head)

        self.check_view = ConversationView(
            "check",
            global_hide_reasoning=global_hide_reasoning,
            collapse_tokens=collapse_tokens,
            parent=page,
        )
        self.check_view.manualMessageSent.connect(self.checkManualMessage.emit)
        self.check_view.clearRequested.connect(self.clearCheckConversation.emit)
        self.check_view.stopRequested.connect(self.stopChapter.emit)
        self.check_view.thinkingChanged.connect(self.checkThinkingChanged.emit)
        layout.addWidget(self.check_view, 1)

        # 检查视图默认就展示完整对话（按需求：进入检查视图即可看到全过程）
        self.check_view.show_detail(True)
        return page

    # ------------------------------------------------------------------ #
    def show_check_conversation(self) -> None:
        """进入检查视图并切到"完整对话"（手动触发检查后调用）。"""
        self.show_view(VIEW_CHECK)
        self.check_view.show_detail(True)

    # ================================================================== #
    # 数据绑定
    # ================================================================== #
    def set_chapter(self, chapter: Chapter | None) -> None:
        self._chapter = chapter
        self._loading = True
        try:
            if chapter is None:
                self.title_label.setText("未选择章节")
                self.badge.set_state(ChapterState.PENDING)
                self._streaming = False
                self._last_state = ChapterState.PENDING
                self.content_edit.setPlainText("")
                self.check_edit.setPlainText("")
                self.generate_view.set_chapter(None)
                self.check_view.set_chapter(None)
                self.history_combo.clear()
                return

            self.title_label.setText(f"{chapter.title}")
            self.badge.set_state(chapter.state)
            self._last_state = chapter.state
            if self.content_edit.toPlainText() != chapter.generated_content:
                self.content_edit.setPlainText(chapter.generated_content)
            if self.check_edit.toPlainText() != chapter.check_result:
                self.check_edit.setPlainText(chapter.check_result)
            self.generate_view.set_chapter(chapter)
            self.check_view.set_chapter(chapter)
            self._refresh_history_combo()
        finally:
            self._loading = False
        self._refresh_buttons()
        self.hide_toast()
        # ---- 选中章节时的默认视图：检查中 / 待决策 -> 检查视图，其它 -> 正文 ---- #
        self._apply_default_view(chapter.state)
        self._refresh_decision_row(chapter.state)

    def _apply_default_view(self, state: ChapterState) -> None:
        """按章节状态选择默认视图。

        需求：章节处于**检查中**或**待决策**时，点进章节默认显示"检查视图"
        （用户此时最需要看的是检查结果与决策按钮）；其余状态仍默认进正文页。
        """
        if state in CHECK_FIRST_STATES:
            self.show_view(VIEW_CHECK)
            self.check_view.show_detail(True)
        else:
            self.show_view(VIEW_CONTENT)

    # ================================================================== #
    # 决策行与提示条
    # ================================================================== #
    def _refresh_decision_row(self, state: ChapterState) -> None:
        """决策行（通过 / 按要求重新生成正文 / 暂不处理）的显示规则。

        需求：只在 **待决策** 状态下显示，且只在 **正文视图 / 检查视图** 可见
        （生成对话视图下不显示——那里是查看/继续对话的地方）。
        """
        if not hasattr(self, "decision_row"):
            return
        awaiting = state == ChapterState.AWAITING_DECISION
        in_conversation = self.current_view() == VIEW_GENERATE
        self.decision_row.setVisible(awaiting and not in_conversation)

    def show_toast(self, text: str, *, autohide: bool = True) -> None:
        """在主界面内显示一条轻量提示（不弹模态对话框、不打断操作）。"""
        if not hasattr(self, "toast_label"):
            return          # 界面还没搭好（构造期调用）：静默忽略
        self.toast_label.setText(text)
        self.toast_label.setVisible(True)
        if autohide:
            self._toast_timer.start()

    def hide_toast(self) -> None:
        if not hasattr(self, "toast_label"):
            return
        self._toast_timer.stop()
        self.toast_label.setVisible(False)

    def begin_content_stream(self) -> None:
        """开始一段新的流式正文（清空正文框并进入"边生成边显示"状态）。"""
        self._streaming = True
        self._loading = True
        try:
            self.content_edit.setPlainText("")
        finally:
            self._loading = False
        self.content_hint.setStyleSheet("color:#0969da; font-size:11px;")
        self.content_hint.setText("⏳ 正在生成：下面的正文会实时追加…")

    def append_content_stream(self, text: str) -> None:
        """把流式增量追加到正文框（仅在流式状态下生效）。"""
        if not self._streaming or not text:
            return
        self._loading = True
        try:
            cursor = self.content_edit.textCursor()
            cursor.movePosition(QTextCursor.MoveOperation.End)
            cursor.insertText(text)
            self.content_edit.setTextCursor(cursor)
            self.content_edit.ensureCursorVisible()
        finally:
            self._loading = False

    def end_content_stream(self, final_text: str = "") -> None:
        """流式结束：以引擎侧的权威正文为准刷新，并恢复提示条。"""
        self._streaming = False
        if final_text:
            self.set_content(final_text)
        self.content_hint.setStyleSheet("color:#8a8f98; font-size:11px;")
        self.content_hint.setText(
            "默认展示该章最近一次生成的完整正文（导出时也以这里的内容为准）。"
        )

    def refresh_check_result(self) -> None:
        """把章节对象里的检查结果同步到可编辑文本框（内容一致时不覆盖）。"""
        chapter = self._chapter
        if chapter is None:
            return
        if self.check_edit.toPlainText() == chapter.check_result:
            return
        self._loading = True
        try:
            self.check_edit.setPlainText(chapter.check_result)
        finally:
            self._loading = False

    def is_streaming_content(self) -> bool:
        """正文页是否正在实时追加生成内容。"""
        return self._streaming

    def set_state(self, state: ChapterState) -> None:
        previous = self._last_state
        self._last_state = state
        self.badge.set_state(state)
        self._busy = state.busy
        self._refresh_buttons()
        # 进入"检查中"时自动展示检查视图（待决策由 set_chapter/_apply_default_view 处理，
        # 这里不强行切页，避免打断正在看正文的用户）
        if state == ChapterState.CHECKING and previous != ChapterState.CHECKING:
            self.show_view(VIEW_CHECK)
        else:
            self._refresh_decision_row(state)

    def _current_state(self) -> ChapterState:
        if self._chapter is not None:
            return self._chapter.state
        return self._last_state

    def set_phase(self, phase: str, detail: str) -> None:
        self.phase_label.setText(detail or "")

    def show_generate_settings_window(self) -> None:
        """在新窗口里打开生成对话的设置。"""
        self.generate_view.show_settings(True)

    def show_check_settings_window(self) -> None:
        """在新窗口里打开检查对话的设置（需求：点检查视图的对话设置在新窗口显示）。"""
        self.check_view.show_settings(True)

    def close_conversation_windows(self) -> None:
        """关闭两个对话各自的"对话设置"窗口（主窗口退出时调用）。"""
        self.generate_view.close_settings_window()
        self.check_view.close_settings_window()

    def set_content(self, text: str) -> None:
        self._loading = True
        try:
            if self.content_edit.toPlainText() != text:
                self.content_edit.setPlainText(text)
        finally:
            self._loading = False

    def apply_global_settings(self, *, hide_reasoning: bool, collapse_tokens: int) -> None:
        self.generate_view.apply_global_settings(
            hide_reasoning=hide_reasoning, collapse_tokens=collapse_tokens
        )
        self.check_view.apply_global_settings(
            hide_reasoning=hide_reasoning, collapse_tokens=collapse_tokens
        )

    def current_view(self) -> str:
        for name, button in self.view_buttons.items():
            if button.isChecked():
                return name
        return VIEW_CONTENT

    def show_view(self, name: str) -> None:
        """切换视图（按钮复选框状态 + 页面可见性）。单击按钮或程序调用都走这里。"""
        if name not in (VIEW_CONTENT, VIEW_GENERATE, VIEW_CHECK):
            return
        button = self.view_buttons[name]
        button.setChecked(True)          # 单击按钮时它已经是选中态，这里不会重复触发
        self._on_view_button(name)

    def _on_view_button(self, name: str) -> None:
        current = self.current_view()
        if name != current:
            self.view_buttons[name].setChecked(True)
        self.content_page.setVisible(name == VIEW_CONTENT)
        self.generate_view.setVisible(name == VIEW_GENERATE)
        self.check_page.setVisible(name == VIEW_CHECK)
        if name == VIEW_GENERATE:
            self.generate_view.render()
        elif name == VIEW_CHECK:
            self.check_view.render()
        # 决策行只在正文/检查视图显示
        self._refresh_decision_row(self._current_state())

    # ================================================================== #
    # 按钮状态（互斥规则）
    # ================================================================== #
    def _refresh_buttons(self) -> None:
        chapter = self._chapter
        busy = bool(chapter and chapter.busy)
        queued = bool(chapter and chapter.queued)
        #: 已提交（排队中）或正在跑：都不应重复提交
        submitted = busy or queued
        self._busy = busy
        state = chapter.state if chapter else ChapterState.PENDING
        has_content = bool(chapter and chapter.has_content)
        has_check = bool(chapter and chapter.check_result.strip())

        # 生成正文：仅 待生成 / 出错（排队中也置灰，避免重复提交）
        can_generate = state in (ChapterState.PENDING, ChapterState.FAILED)
        self.generate_button.setEnabled(can_generate)
        self._style_disabled(self.generate_button, not can_generate)

        has_outline = bool(chapter and chapter.outline_for_generate().strip())
        has_conv = bool(chapter and chapter.generate_conv is not None)
        #: 点击后走"按大纲重新生成"而不是"按你的要求重写"。
        #: 待生成/出错 = 还没正经生成过；清空生成对话后引擎会把状态也置回"待生成"，
        #: 因此这里只看状态即可（有正文却没有对话对象的旧数据也一并按"按要求重写"处理更安全）。
        outline_restart = state in (ChapterState.PENDING, ChapterState.FAILED)

        # 重新生成：只要没提交、且有大纲（待生成/对话已清空时按大纲重来；否则可按要求重写）
        can_regen = (not submitted) and has_outline and (outline_restart or has_content)
        self.regen_button.setEnabled(can_regen)
        self._style_disabled(self.regen_button, not can_regen)
        if outline_restart:
            self.regen_button.setText("重新生成（按大纲）")
            self.regen_button.setToolTip(
                "直接把本章大纲重新发给 AI 生成正文（不弹输入框）。\n"
                "清空生成对话后，本章回到“待生成”，点这里就会重新发送大纲。"
            )
        else:
            self.regen_button.setText("重新生成")
            self.regen_button.setToolTip(
                "重新生成整章正文：可以填写额外的修改要求，按你的要求重写。\n"
                "想完全按大纲重来时，先点生成对话里的“清空对话”。"
            )

        # 触发检查：未提交且有正文
        self.check_trigger_button.setEnabled(not submitted and has_content)
        self._style_disabled(self.check_trigger_button, submitted or not has_content)

        # 决策行（通过 / 按要求重新生成正文 / 暂不处理）：
        # 只在"待决策" + 正文或检查视图下出现（生成对话视图下不显示）
        decision_ok = (not submitted) and state == ChapterState.AWAITING_DECISION
        self.accept_button.setEnabled(decision_ok)
        self.postpone_button.setEnabled(decision_ok)

        # 按要求重新生成正文：待决策 + 有检查结果
        can_regen_by_check = decision_ok and has_check
        self.regen_decision_button.setEnabled(can_regen_by_check)
        self._refresh_decision_row(state)

        # 停止本章：跑着的时候当然可点；排队中也允许点（把排队的任务撤下来）
        self.stop_button.setEnabled(busy or queued)
        self.generate_view.set_busy(busy)
        self.check_view.set_busy(busy)

        if busy:
            self.decision_hint.setText(f"⏳ 该章正在{STATE_LABELS.get(state, '')}：{BUSY_HINT}。")
        elif queued:
            self.decision_hint.setText(
                "🕒 该章任务已排在队列末尾（状态：排队中），轮到它时会自动开始；"
                "也可以点“停止本章”把它撤下来，或在工具栏「任务队列」里调整优先级。"
            )
        elif state == ChapterState.AWAITING_DECISION:
            self.decision_hint.setText(
                "检查已完成：用上方「决策行」里的按钮处理——"
                "「按要求重新生成正文」按检查意见重写，「通过」标记合格，「暂不处理」稍后再说。"
            )
        elif not has_content:
            self.decision_hint.setText(
                "该章还没有正文：点“重新生成（按大纲）”会按本章大纲生成正文。"
                if has_outline
                else "该章还没有正文，也没有大纲：请先在左侧编辑本章大纲。"
            )
        else:
            self.decision_hint.setText("")

    @staticmethod
    def _style_disabled(button: QPushButton, disabled: bool) -> None:
        """视觉置灰（保持可点击，以便点击时给出提示）。"""
        button.setStyleSheet(
            "QPushButton { color: #8a8f98; background: #f6f8fa; border: 1px solid #d8dee4; }"
            if disabled
            else ""
        )

    def _guarded(self, action) -> None:
        """忙状态下的点击守卫：给出明确提示而不是静默无反应。"""
        if self._busy:
            info(self, "请稍候", f"{BUSY_HINT}。\n\n你也可以点击“停止本章”立即中断当前任务。")
            return
        action()

    # ================================================================== #
    # 交互
    # ================================================================== #
    def _emit_generate(self) -> None:
        self.generateRequested.emit()

    def _on_edit_toggled(self, checked: bool) -> None:
        if self._loading:
            return
        if checked:
            if self._busy:
                self.edit_toggle.setChecked(False)
                info(self, "请稍候", f"{BUSY_HINT}。")
                return
            if not confirm(
                self,
                "编辑正文",
                "将切换到可编辑状态。导出时使用这里的内容；\n"
                "注意：之后重新生成本章会覆盖你的修改。是否继续？",
            ):
                self.edit_toggle.setChecked(False)
                return
            self.content_edit.setReadOnly(False)
            self.edit_toggle.setText("✓ 完成编辑")
            self.content_hint.setText(
                "编辑模式：这里的内容就是导出内容（程序不做任何解析或格式转换）。"
            )
        else:
            self.content_edit.setReadOnly(True)
            self.edit_toggle.setText("✎ 编辑正文")
            self.content_hint.setText(
                "默认展示该章最近一次生成的完整正文（导出时也以这里的内容为准）。"
            )

    def _on_content_changed(self) -> None:
        if self._loading or self.content_edit.isReadOnly():
            return
        self.contentEdited.emit(self.content_edit.toPlainText())

    def _on_check_changed(self) -> None:
        if self._loading:
            return
        self.checkResultEdited.emit(self.check_edit.toPlainText())

    def _refresh_history_combo(self) -> None:
        chapter = self._chapter
        blocked = self.history_combo.blockSignals(True)
        try:
            self.history_combo.clear()
            if chapter is None or not chapter.check_history:
                self.history_combo.addItem("历次检查结果：无")
                self.history_combo.setEnabled(False)
                return
            self.history_combo.setEnabled(True)
            for index, text in enumerate(chapter.check_history, start=1):
                preview = " ".join(text.split())[:28]
                self.history_combo.addItem(f"第 {index} 次：{preview}…", index - 1)
        finally:
            self.history_combo.blockSignals(blocked)

    def _on_history_selected(self, position: int) -> None:
        chapter = self._chapter
        if chapter is None or position < 0:
            return
        index = self.history_combo.itemData(position)
        if index is None or index >= len(chapter.check_history):
            return
        self.check_edit.setPlainText(chapter.check_history[index])

    def _on_copy_content(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.content_edit.toPlainText())

    def _on_copy_check(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.check_edit.toPlainText())
