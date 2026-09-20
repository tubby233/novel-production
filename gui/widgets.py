"""通用小部件：状态徽标、数值输入、模板编辑器、通用对话框。

设计原则：每个控件都与 ``param_help`` 的一个 key 绑定（或显式说明不需要），
以便"全参数都有悬浮解释"这一要求可以被机器检查，而不是靠人工核对。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from core import message_templates as MT
from core import param_help
from core.models import ChapterState, STATE_LABELS

from .help_icon import HelpIcon, attach_help

# --------------------------------------------------------------------------- #
# 状态徽标
# --------------------------------------------------------------------------- #
STATE_COLORS: dict[str, str] = {
    str(ChapterState.PENDING): "#8a8f98",
    str(ChapterState.QUEUED): "#0969da",
    str(ChapterState.GENERATING): "#1f6feb",
    str(ChapterState.TO_CHECK): "#9a6700",
    str(ChapterState.CHECKING): "#8250df",
    str(ChapterState.AWAITING_DECISION): "#bc4c00",
    str(ChapterState.PASSED): "#1a7f37",
    str(ChapterState.FAILED): "#cf222e",
}


class StateBadge(QLabel):
    """章节状态小徽标。"""

    def __init__(self, state: ChapterState = ChapterState.PENDING, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumWidth(56)
        self.set_state(state)

    def set_state(self, state: ChapterState | str) -> None:
        key = str(state)
        label = STATE_LABELS.get(ChapterState(key), key) if key in {str(s) for s in ChapterState} else key
        color = STATE_COLORS.get(key, "#57606a")
        self.setText(label)
        self.setStyleSheet(
            f"QLabel {{ color: white; background: {color}; border-radius: 7px;"
            f" padding: 1px 6px; font-size: 11px; }}"
        )


# --------------------------------------------------------------------------- #
# 数值输入（支持手动输入 + 滑条联动）
# --------------------------------------------------------------------------- #
class NumericField(QWidget):
    """数值输入：SpinBox（可手动输入）+ 可选滑条。

    所有范围/默认值来自 ``param_help``，避免文档与控件不一致。
    """

    valueChanged = Signal(object)

    def __init__(
        self,
        param_key: str,
        *,
        value: float,
        minimum: float,
        maximum: float,
        decimals: int = 0,
        step: float = 1.0,
        use_slider: bool = True,
        slider_scale: int = 100,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        if decimals > 0:
            self.spin: QSpinBox | QDoubleSpinBox = QDoubleSpinBox(self)
            self.spin.setDecimals(decimals)
        else:
            self.spin = QSpinBox(self)
            self.spin.setDecimals(0)
        self.spin.setRange(minimum, maximum)
        self.spin.setSingleStep(step)
        self.spin.setValue(value)
        self.spin.setKeyboardTracking(False)
        self.spin.setMinimumWidth(120)
        attach_help(self.spin, param_key)
        layout.addWidget(self.spin)

        self.slider: QSlider | None = None
        if use_slider:
            self.slider = QSlider(Qt.Orientation.Horizontal, self)
            self.slider.setRange(int(minimum * slider_scale), int(maximum * slider_scale))
            self.slider.setValue(int(round(value * slider_scale)))
            attach_help(self.slider, param_key)
            layout.addWidget(self.slider, 1)
            self.slider.valueChanged.connect(self._on_slider)
        else:
            layout.addStretch(1)

        self.spin.valueChanged.connect(self._on_spin)
        self._updating = False
        self._slider_scale = slider_scale

    # ------------------------------------------------------------------ #
    def _on_spin(self, value: float) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            if self.slider is not None:
                self.slider.setValue(int(round(float(value) * self._slider_scale)))
        finally:
            self._updating = False
        self.valueChanged.emit(self.value())

    def _on_slider(self, raw: int) -> None:
        if self._updating:
            return
        self._updating = True
        try:
            self.spin.setValue(raw / self._slider_scale)
        finally:
            self._updating = False
        self.valueChanged.emit(self.value())

    # ------------------------------------------------------------------ #
    def value(self) -> float:
        return float(self.spin.value())

    def setValue(self, value: float) -> None:  # noqa: N802 - Qt 命名
        self.spin.setValue(value)

    def set_enabled_state(self, enabled: bool) -> None:
        self.spin.setEnabled(enabled)
        if self.slider is not None:
            self.slider.setEnabled(enabled)

    @property
    def field(self) -> QWidget:
        return self.spin


def make_spin(
    param_key: str,
    value: int,
    minimum: int,
    maximum: int,
    *,
    step: int = 1,
    parent: QWidget | None = None,
) -> QSpinBox:
    spin = QSpinBox(parent)
    spin.setRange(minimum, maximum)
    spin.setSingleStep(step)
    spin.setValue(value)
    spin.setKeyboardTracking(False)
    spin.setMinimumWidth(140)
    attach_help(spin, param_key)
    return spin


def make_double_spin(
    param_key: str,
    value: float,
    minimum: float,
    maximum: float,
    *,
    step: float = 0.1,
    decimals: int = 2,
    parent: QWidget | None = None,
) -> QDoubleSpinBox:
    spin = QDoubleSpinBox(parent)
    spin.setDecimals(decimals)
    spin.setRange(minimum, maximum)
    spin.setSingleStep(step)
    spin.setValue(value)
    spin.setKeyboardTracking(False)
    spin.setMinimumWidth(140)
    attach_help(spin, param_key)
    return spin


def make_checkbox(
    text: str, checked: bool, param_key: str, *, parent: QWidget | None = None
) -> QCheckBox:
    box = QCheckBox(text, parent)
    box.setChecked(checked)
    attach_help(box, param_key)
    return box


def make_combo(
    items: list[str],
    current: str,
    param_key: str,
    *,
    editable: bool = False,
    parent: QWidget | None = None,
) -> QComboBox:
    combo = QComboBox(parent)
    combo.addItems(items)
    combo.setEditable(editable)
    if current in items:
        combo.setCurrentText(current)
    elif editable:
        combo.setEditText(current)
    attach_help(combo, param_key)
    return combo


# --------------------------------------------------------------------------- #
# 模板 / 提示词编辑器
# --------------------------------------------------------------------------- #
class TemplateEditor(QWidget):
    """多行模板编辑器 + 占位符校验提示行 + 恢复默认按钮。"""

    changed = Signal()

    def __init__(
        self,
        template_key: MT.TemplateKind,
        *,
        text: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.template_key = template_key
        spec = MT.get_spec(template_key)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        title = QLabel(f"<b>{spec.label}</b>", self)
        header.addWidget(title)
        header.addWidget(HelpIcon(spec.help_key, self))
        header.addStretch(1)
        placeholder_text = "、".join(f"{{{p}}}" for p in spec.placeholders) or "无"
        header.addWidget(QLabel(f"占位符：{placeholder_text}", self))
        self.reset_button = QPushButton("恢复默认模板", self)
        self.reset_button.setToolTip("把本模板恢复为程序内置的默认文案。")
        header.addWidget(self.reset_button)
        layout.addLayout(header)

        self.edit = QPlainTextEdit(self)
        self.edit.setPlainText(text)
        self.edit.setMinimumHeight(110)
        attach_help(self.edit, spec.help_key)
        layout.addWidget(self.edit)

        self.status = QLabel(self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.edit.textChanged.connect(self._on_changed)
        self.reset_button.clicked.connect(self.reset_to_default)
        self._refresh_status()

    # ------------------------------------------------------------------ #
    def text(self) -> str:
        return self.edit.toPlainText()

    def set_text(self, text: str) -> None:
        self.edit.setPlainText(text)

    def reset_to_default(self) -> None:
        self.edit.setPlainText(MT.default_template(self.template_key))

    def validation(self) -> MT.TemplateValidation:
        return MT.validate_template(self.template_key, self.text())

    def _on_changed(self) -> None:
        self._refresh_status()
        self.changed.emit()

    def _refresh_status(self) -> None:
        result = self.validation()
        self.status.setText(result.message())
        if result.ok:
            self.status.setStyleSheet("color:#1a7f37; font-size:11px;")
        elif result.missing:
            self.status.setStyleSheet("color:#9a6700; font-size:11px;")
        else:
            self.status.setStyleSheet("color:#57606a; font-size:11px;")


class PromptEditor(QWidget):
    """提示词编辑器（system message）。"""

    def __init__(
        self,
        param_key: str,
        title: str,
        *,
        text: str,
        placeholder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.addWidget(QLabel(f"<b>{title}</b>", self))
        header.addWidget(HelpIcon(param_key, self))
        header.addStretch(1)
        layout.addLayout(header)

        self.edit = QPlainTextEdit(self)
        self.edit.setPlainText(text)
        self.edit.setMinimumHeight(140)
        if placeholder:
            self.edit.setPlaceholderText(placeholder)
        attach_help(self.edit, param_key)
        layout.addWidget(self.edit)

    def text(self) -> str:
        return self.edit.toPlainText()

    def set_text(self, text: str) -> None:
        self.edit.setPlainText(text)


# --------------------------------------------------------------------------- #
# 通用对话框
# --------------------------------------------------------------------------- #
class TextInputDialog(QDialog):
    """多行文本输入（用于"手动重新生成"的额外指令）。"""

    def __init__(
        self,
        title: str,
        label: str,
        *,
        placeholder: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(520, 260)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(label, self))
        self.edit = QPlainTextEdit(self)
        self.edit.setPlaceholderText(placeholder)
        layout.addWidget(self.edit, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def text(self) -> str:
        return self.edit.toPlainText()


def ask_text(
    parent: QWidget | None,
    title: str,
    label: str,
    *,
    placeholder: str = "",
) -> tuple[bool, str]:
    dialog = TextInputDialog(title, label, placeholder=placeholder, parent=parent)
    if dialog.exec() == QDialog.DialogCode.Accepted:
        return True, dialog.text()
    return False, ""


def confirm(parent: QWidget | None, title: str, text: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Question)
    box.setStandardButtons(QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def info(parent: QWidget | None, title: str, text: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Information)
    box.exec()


def warn(parent: QWidget | None, title: str, text: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Warning)
    box.exec()


def error(parent: QWidget | None, title: str, text: str) -> None:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(text)
    box.setIcon(QMessageBox.Icon.Critical)
    box.exec()


def hline(parent: QWidget | None = None) -> QFrame:
    line = QFrame(parent)
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


def section_title(text: str, param_key: str = "", parent: QWidget | None = None) -> QWidget:
    """小标题行（可带 ⓘ）。"""
    widget = QWidget(parent)
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 6, 0, 2)
    layout.setSpacing(6)
    font = QFont()
    font.setBold(True)
    label = QLabel(text, widget)
    label.setFont(font)
    layout.addWidget(label)
    if param_key:
        layout.addWidget(HelpIcon(param_key, widget))
    layout.addStretch(1)
    return widget


def build_form(parent: QWidget | None = None) -> QFormLayout:
    form = QFormLayout(parent)
    form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
    form.setHorizontalSpacing(10)
    form.setVerticalSpacing(8)
    return form


def labeled(
    text: str,
    field: QWidget,
    param_key: str,
    *,
    parent: QWidget | None = None,
    note: str = "",
) -> QWidget:
    """标签 + 控件 + ⓘ 的横向组合。"""
    row = QWidget(parent)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    label_text = f"{text}　{note}" if note else text
    layout.addWidget(QLabel(label_text, row))
    layout.addWidget(field, 1)
    layout.addWidget(HelpIcon(param_key, row))
    return row
