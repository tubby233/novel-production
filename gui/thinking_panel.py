"""逐对话的深度思考参数面板。

每个对话（生成对话 / 检查对话）各有一份，互不影响。
参数改动只对该对话**后续**的请求生效；若该对话正在运行，界面会明确提示。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from core.models import EFFORT_CHOICES, ThinkingConfig
from .help_icon import HelpIcon, attach_help


class ThinkingPanel(QGroupBox):
    """深度思考参数：开关 + 强度 + 展示思维链。"""

    thinkingChanged = Signal(object)   # ThinkingConfig

    def __init__(
        self,
        title: str,
        thinking: ThinkingConfig,
        *,
        global_hide_reasoning: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(title, parent)
        self._global_hide = global_hide_reasoning
        self._loading = False
        self._build_ui(thinking)
        self._refresh()

    # ------------------------------------------------------------------ #
    def _build_ui(self, thinking: ThinkingConfig) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 6, 8, 6)
        root.setSpacing(4)

        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)

        # 思考开关
        self.enabled_box = QCheckBox("启用思考模式", self)
        self.enabled_box.setChecked(thinking.enabled)
        attach_help(self.enabled_box, "thinking_enabled")
        grid.addWidget(self.enabled_box, 0, 0)
        grid.addWidget(HelpIcon("thinking_enabled", self, size=12), 0, 1)

        # 思考强度
        grid.addWidget(QLabel("思考强度", self), 0, 2)
        self.effort_combo = QComboBox(self)
        self.effort_combo.addItems(list(EFFORT_CHOICES))
        self.effort_combo.setCurrentText(thinking.effort)
        self.effort_combo.setMinimumWidth(90)
        attach_help(self.effort_combo, "reasoning_effort")
        grid.addWidget(self.effort_combo, 0, 3)
        grid.addWidget(HelpIcon("reasoning_effort", self, size=12), 0, 4)

        # 展示思维链
        self.show_reasoning_box = QCheckBox("展示思维链", self)
        self.show_reasoning_box.setChecked(thinking.show_reasoning)
        attach_help(self.show_reasoning_box, "show_reasoning")
        grid.addWidget(self.show_reasoning_box, 1, 0)
        grid.addWidget(HelpIcon("show_reasoning", self, size=12), 1, 1)

        grid.setColumnStretch(5, 1)
        root.addLayout(grid)

        # 约束提示 + 生效参数预览
        self.note_label = QLabel(self)
        self.note_label.setWordWrap(True)
        self.note_label.setStyleSheet("color:#9a6700; font-size:11px;")
        root.addWidget(self.note_label)

        self.effective_label = QLabel(self)
        self.effective_label.setWordWrap(True)
        self.effective_label.setStyleSheet("color:#57606a; font-size:11px;")
        root.addWidget(self.effective_label)

        self.hint_label = QLabel(self)
        self.hint_label.setWordWrap(True)
        self.hint_label.setStyleSheet("color:#0969da; font-size:11px;")
        self.hint_label.setVisible(False)
        root.addWidget(self.hint_label)

        self.enabled_box.toggled.connect(self._on_changed)
        self.effort_combo.currentTextChanged.connect(self._on_changed)
        self.show_reasoning_box.toggled.connect(self._on_changed)

    # ------------------------------------------------------------------ #
    def thinking(self) -> ThinkingConfig:
        return ThinkingConfig(
            enabled=self.enabled_box.isChecked(),
            effort=self.effort_combo.currentText() or "high",
            show_reasoning=self.show_reasoning_box.isChecked(),
        ).normalized()

    def set_thinking(self, thinking: ThinkingConfig) -> None:
        self._loading = True
        try:
            self.enabled_box.setChecked(thinking.enabled)
            self.effort_combo.setCurrentText(thinking.effort)
            self.show_reasoning_box.setChecked(thinking.show_reasoning)
        finally:
            self._loading = False
        self._refresh()

    def set_global_hide(self, hidden: bool) -> None:
        self._global_hide = bool(hidden)
        self._refresh()

    def set_running(self, running: bool) -> None:
        """对话正在运行时，改动只对后续请求生效，界面给出提示。"""
        self.hint_label.setText(
            "该对话正在运行：当前请求仍使用启动时的参数，改动将从下一次请求开始生效。"
        )
        self.hint_label.setVisible(running)

    # ------------------------------------------------------------------ #
    def _on_changed(self, *_: object) -> None:
        if self._loading:
            return
        self._refresh()
        self.thinkingChanged.emit(self.thinking())

    def _refresh(self) -> None:
        thinking = self.thinking()
        self.effort_combo.setEnabled(thinking.enabled)
        self.show_reasoning_box.setEnabled(not self._global_hide)
        self.show_reasoning_box.setToolTip(
            "全局隐藏思维链已开启，该开关被禁用（由设置中的全局开关控制）。"
            if self._global_hide
            else self.show_reasoning_box.toolTip()
        )

        if thinking.enabled:
            self.note_label.setText(
                "⚠ 思考模式下 temperature 不生效；"
                "top_p 生效但下限 0.95（小于 0.95 会自动抬升）；非思考模式下 top_p 恒为 1.0。"
            )
        else:
            self.note_label.setText(
                "ⓘ 已关闭思考模式：temperature 生效，top_p 固定为 1.0（传入值被忽略）。"
            )

        if thinking.enabled:
            effective = (
                f"本次生效：thinking=enabled，reasoning_effort={thinking.effort}，"
                "top_p=max(设置值, 0.95)，temperature 被忽略"
            )
        else:
            effective = "本次生效：thinking=disabled，temperature 生效，top_p=1.0"
        self.effective_label.setText(effective)
        self.effective_label.setToolTip(effective)
