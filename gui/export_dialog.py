"""导出对话框：选项 + 预览 + 写出 TXT。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from core import exporter, paths
from core.config import AppConfig
from core.exporter import ExportOptions, MODE_ALL, MODE_PASSED_ONLY
from core.models import Chapter

from .help_icon import HelpIcon, attach_help
from .widgets import error, info, section_title


class ExportDialog(QDialog):
    """导出 TXT。"""

    exported = Signal(str, object)   # 路径, ExportOptions

    def __init__(
        self,
        chapters: list[Chapter],
        cfg: AppConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.chapters = chapters
        self.cfg = cfg
        self._build_ui()
        self._load_from_config()
        self._refresh_preview()

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        self.setWindowTitle("导出 TXT")
        self.resize(760, 620)
        root = QVBoxLayout(self)
        root.setSpacing(8)

        root.addWidget(section_title("导出范围", "exp_mode", self))
        self.mode_all = QRadioButton("导出全部", self)
        self.mode_passed = QRadioButton("仅导出已合格章节", self)
        self.mode_group = QButtonGroup(self)
        self.mode_group.addButton(self.mode_all, 0)
        self.mode_group.addButton(self.mode_passed, 1)
        row = QHBoxLayout()
        row.addWidget(self.mode_all)
        row.addWidget(self.mode_passed)
        row.addWidget(HelpIcon("exp_mode", self))
        row.addStretch(1)
        root.addLayout(row)

        mark_row = QHBoxLayout()
        mark_row.addWidget(QLabel("未确认标记文本", self))
        self.mark_edit = QLineEdit(self)
        attach_help(self.mark_edit, "exp_mark_text")
        mark_row.addWidget(self.mark_edit, 1)
        mark_row.addWidget(HelpIcon("exp_mark_text", self))
        root.addLayout(mark_row)

        self.append_check_box = QCheckBox("在未确认章节文末附上最后一次检查结果", self)
        attach_help(self.append_check_box, "exp_append_check")
        root.addWidget(self.append_check_box)

        self.mark_confirm_box = QCheckBox("在未确认章节正文前单独加一行标记", self)
        self.mark_confirm_box.setToolTip(
            "默认关闭：导出内容就是合并后的生成正文，一个字都不加。\n"
            "开启后会在未确认章节的正文之前单独写一行你设置的标记文本，便于在成品里搜索定位。"
        )
        attach_help(self.mark_confirm_box, "exp_mark_confirmed")
        root.addWidget(self.mark_confirm_box)

        # 说明：正文里本来就包含章节标题，因此程序既不加标题、也不再加定位行，
        # 每章只写"生成正文"本身（按需附检查结果）。
        title_note = QLabel(
            "说明：生成的正文里已包含章节标题，导出时程序**不再添加任何题目或定位行**，"
            "直接把各章正文按顺序合并。",
            self,
        )
        title_note.setWordWrap(True)
        title_note.setStyleSheet("color:#57606a; font-size:11px;")
        root.addWidget(title_note)

        self.separator_box = QCheckBox("章节之间插入空行", self)
        attach_help(self.separator_box, "exp_separator")
        root.addWidget(self.separator_box)

        encoding_row = QHBoxLayout()
        encoding_row.addWidget(QLabel("文件编码", self))
        self.encoding_combo = QComboBox(self)
        self.encoding_combo.addItems(list(exporter.ENCODINGS))
        attach_help(self.encoding_combo, "exp_encoding")
        encoding_row.addWidget(self.encoding_combo)
        encoding_row.addWidget(HelpIcon("exp_encoding", self))
        encoding_row.addStretch(1)
        root.addLayout(encoding_row)

        path_row = QHBoxLayout()
        path_row.addWidget(QLabel("保存到", self))
        self.path_edit = QLineEdit(self)
        attach_help(self.path_edit, "exp_path")
        path_row.addWidget(self.path_edit, 1)
        browse = QPushButton("浏览…", self)
        browse.clicked.connect(self._browse)
        path_row.addWidget(browse)
        path_row.addWidget(HelpIcon("exp_path", self))
        root.addLayout(path_row)

        self.open_box = QCheckBox("导出完成后打开所在目录", self)
        attach_help(self.open_box, "exp_open_after")
        root.addWidget(self.open_box)

        preview_head = QHBoxLayout()
        preview_head.addWidget(QLabel("<b>预览</b>", self))
        self.preview_button = QPushButton("生成预览", self)
        attach_help(self.preview_button, "exp_preview")
        self.preview_button.clicked.connect(self._refresh_preview)
        preview_head.addWidget(self.preview_button)
        self.summary_label = QLabel("", self)
        self.summary_label.setStyleSheet("color:#57606a; font-size:11px;")
        preview_head.addWidget(self.summary_label, 1)
        preview_head.addWidget(HelpIcon("exp_preview", self))
        root.addLayout(preview_head)

        self.preview_edit = QPlainTextEdit(self)
        self.preview_edit.setReadOnly(True)
        self.preview_edit.setPlaceholderText("点击“生成预览”查看导出结果的开头部分。")
        root.addWidget(self.preview_edit, 1)

        buttons = QDialogButtonBox(self)
        self.export_button = buttons.addButton("导出", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.addButton("取消", QDialogButtonBox.ButtonRole.RejectRole)
        buttons.accepted.connect(self._on_export)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for widget in (
            self.mode_all, self.mode_passed, self.mark_edit, self.append_check_box,
            self.mark_confirm_box, self.separator_box, self.encoding_combo,
        ):
            if hasattr(widget, "toggled"):
                widget.toggled.connect(self._refresh_preview)
            if hasattr(widget, "textChanged"):
                widget.textChanged.connect(self._refresh_preview)
            if hasattr(widget, "currentTextChanged"):
                widget.currentTextChanged.connect(self._refresh_preview)

    # ------------------------------------------------------------------ #
    def _load_from_config(self) -> None:
        opts = ExportOptions.from_config(self.cfg)
        if opts.mode == MODE_PASSED_ONLY:
            self.mode_passed.setChecked(True)
        else:
            self.mode_all.setChecked(True)
        self.mark_edit.setText(opts.mark_text)
        self.append_check_box.setChecked(opts.append_check_result)
        self.mark_confirm_box.setChecked(opts.mark_confirmed)
        self.separator_box.setChecked(opts.separator)
        self.encoding_combo.setCurrentText(opts.encoding)
        self.open_box.setChecked(self.cfg.ui.export_open_after)
        # 默认导出目录 = 设置里的"默认导出目录"（不再用项目文件夹）
        default_dir = self.cfg.ui.export_dir or str(paths.default_export_dir())
        self._default_dir = default_dir
        self.path_edit.setText(str(Path(default_dir) / self._filename_from_project()))

    def _filename_from_project(self) -> str:
        """默认文件名：``<小说名>.txt``（未立项时用默认名）。"""
        project = getattr(self, "project", None)
        if project is not None:
            name = str(getattr(project, "name", "") or "").strip()
            if name:
                # 去掉文件名里不合法的字符，避免写盘失败
                safe = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in name).strip()
                if safe:
                    return f"{safe}.txt"
        return "novel_export.txt"

    def set_project_name(self, name: str) -> None:
        """按当前小说项目名设置默认文件名（主窗口打开对话框后调用，目录不变）。"""
        safe = "".join("_" if ch in '\\/:*?"<>|' else ch for ch in (name or "")).strip()
        if not safe:
            return
        current = Path(self.path_edit.text().strip() or self._default_dir or ".")
        directory = current if current.is_dir() else current.parent
        self.path_edit.setText(str(directory / f"{safe}.txt"))

    def options(self) -> ExportOptions:
        return ExportOptions(
            mode=MODE_PASSED_ONLY if self.mode_passed.isChecked() else MODE_ALL,
            mark_text=self.mark_edit.text() or "【未确认】",
            mark_confirmed=self.mark_confirm_box.isChecked(),
            append_check_result=self.append_check_box.isChecked(),
            separator=self.separator_box.isChecked(),
            encoding=self.encoding_combo.currentText(),
        )

    # ------------------------------------------------------------------ #
    def _refresh_preview(self, *_: object) -> None:
        plan = exporter.build_export_plan(self.chapters, self.options())
        self.summary_label.setText(plan.summary())
        head = plan.text[:6000]
        if len(plan.text) > 6000:
            head += "\n\n……（预览仅显示开头 6000 字符）"
        self.preview_edit.setPlainText(head or "（没有可导出的内容）")

    def _browse(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出为", self.path_edit.text() or str(paths.default_export_path()),
            "文本文件 (*.txt)",
        )
        if path:
            self.path_edit.setText(path)

    def _on_export(self) -> None:
        target = self.path_edit.text().strip()
        if not target:
            error(self, "导出失败", "请填写导出路径。")
            return
        options = self.options()
        try:
            written, plan = exporter.export_txt(self.chapters, target, options)
        except OSError as exc:
            error(self, "导出失败", f"无法写入文件：{exc}")
            return
        except UnicodeEncodeError as exc:
            error(
                self,
                "导出失败",
                f"当前编码无法表示某些字符：{exc}\n请把文件编码改为 utf-8 或 utf-8-sig 后重试。",
            )
            return

        info(self, "导出完成", f"{plan.summary()}\n\n文件已保存到：\n{written}")
        self.exported.emit(str(written), options)
        if self.open_box.isChecked():
            exporter.open_in_explorer(written)
        self.accept()
