"""HelpIcon：参数旁的 ⓘ 悬浮解释图标。

用法::

    row.addWidget(HelpIcon("temperature"))          # 只放图标
    widget = HelpLabel("温度", spin, "temperature")  # 标签 + 控件 + ⓘ

行为
----
* 鼠标悬浮：显示 QToolTip（富文本，多行）。
* 鼠标点击：弹出可滚动、可复制的帮助窗口（长文本在 tooltip 里会被截断）。
* 说明内容全部来自 core.param_help，界面不硬编码任何文案。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QTextBrowser,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from core import param_help

ICON_CHAR = "\u24d8"  # ⓘ


class HelpDialog(QDialog):
    """帮助详情窗口：可滚动、可选中复制。"""

    def __init__(self, param_key: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        item = param_help.get_help(param_key)
        self.setWindowTitle(f"参数说明 - {item.name_cn}")
        self.resize(560, 420)

        layout = QVBoxLayout(self)
        browser = QTextBrowser(self)
        browser.setOpenExternalLinks(False)
        browser.setHtml(_dialog_html(param_key))
        layout.addWidget(browser)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)


def _dialog_html(param_key: str) -> str:
    item = param_help.get_help(param_key)
    body = item.to_html()
    extra = ""
    if item.related:
        rows = []
        for key in item.related:
            related_item = param_help.PARAM_HELP.get(key)
            if related_item:
                rows.append(
                    f"<li><b>{related_item.name_cn}</b>（{related_item.name_en}）："
                    f"{related_item.effect or '—'}</li>"
                )
        if rows:
            extra = "<hr><b>相关参数</b><ul>" + "".join(rows) + "</ul>"
    return f"<div style='font-size:10pt; line-height:150%'>{body}{extra}</div>"


class HelpIcon(QLabel):
    """ⓘ 图标。构造时只需给出 param_help 中的 key。"""

    def __init__(
        self,
        param_key: str,
        parent: QWidget | None = None,
        *,
        size: int = 14,
        text: str | None = None,
    ) -> None:
        super().__init__(text or ICON_CHAR, parent)
        self._param_key = param_key
        font = QFont(self.font())
        font.setPointSize(max(8, size - 3))
        font.setBold(True)
        self.setFont(font)
        self.setObjectName("HelpIcon")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setFixedWidth(size + 8)
        self.setToolTip(self._build_tooltip())
        self.setToolTipDuration(60000)

    # ------------------------------------------------------------------ #
    @property
    def param_key(self) -> str:
        return self._param_key

    def set_key(self, param_key: str) -> None:
        self._param_key = param_key
        self.setToolTip(self._build_tooltip())

    def _build_tooltip(self) -> str:
        item = param_help.get_help(self._param_key)
        html = item.tooltip()
        return (
            f"<div style='max-width:460px; white-space:normal; font-size:9pt'>"
            f"{html}<br><i>点击查看完整说明</i></div>"
        )

    # ------------------------------------------------------------------ #
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        QToolTip.hideText()
        dialog = HelpDialog(self._param_key, self.window())
        dialog.exec()
        super().mousePressEvent(event)


class HelpLabel(QWidget):
    """一行：文字标签 + 控件 + ⓘ。"""

    def __init__(
        self,
        label: str,
        field: QWidget,
        param_key: str,
        *,
        parent: QWidget | None = None,
        label_width: int = 0,
        suffix_widgets: list[QWidget] | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.label = QLabel(label, self)
        if label_width:
            self.label.setMinimumWidth(label_width)
        layout.addWidget(self.label)
        layout.addWidget(field, 1)
        for widget in suffix_widgets or []:
            layout.addWidget(widget)
        self.help = HelpIcon(param_key, self)
        layout.addWidget(self.help)

    def set_field_enabled(self, enabled: bool, *, note: str = "") -> None:
        """启用/禁用控件，并在标签后追加"当前不生效"之类的角标。"""
        self.label.setEnabled(enabled)
        self.help.setEnabled(True)
        text = self.label.text().split("　(")[0]
        self.label.setText(text if enabled or not note else f"{text}　({note})")


class HelpActionButton(QWidget):
    """按钮 + 内联 ⓘ：按钮本身带说明，ⓘ 也可点击查看。"""

    def __init__(
        self,
        text: str,
        param_key: str,
        *,
        parent: QWidget | None = None,
        button: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.button = button or _default_button(text, param_key, self)
        layout.addWidget(self.button)
        self.help = HelpIcon(param_key, self, size=12)
        layout.addWidget(self.help)

    @property
    def clicked(self):  # pragma: no cover - 透传
        return self.button.clicked


def _default_button(text: str, param_key: str, parent: QWidget):
    from PySide6.QtWidgets import QPushButton

    button = QPushButton(text, parent)
    item = param_help.get_help(param_key)
    button.setToolTip(
        f"<div style='max-width:420px; white-space:normal; font-size:9pt'>"
        f"{item.tooltip()}</div>"
    )
    button.setToolTipDuration(60000)
    return button


def help_tip(param_key: str) -> str:
    """给任意控件用的 tooltip 文本。"""
    item = param_help.get_help(param_key)
    return (
        f"<div style='max-width:440px; white-space:normal; font-size:9pt'>"
        f"{item.tooltip()}</div>"
    )


def attach_help(widget: QWidget, param_key: str) -> QWidget:
    """给已有控件挂上悬浮说明（不新增 ⓘ 图标时使用）。

    同时兼容 ``QAction``（工具栏/菜单动作）：它只有 ``setToolTip``，
    没有 ``setToolTipDuration``，因此这里用 hasattr 判断而不是直接写死。
    """
    widget.setToolTip(help_tip(param_key))
    if hasattr(widget, "setToolTipDuration"):
        widget.setToolTipDuration(60000)
    return widget
