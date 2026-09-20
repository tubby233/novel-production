"""大纲输入、正则切分与章节列表。

* 大纲可粘贴或导入 txt/md；按正则切分为章节（每章整体作为一个输入单元）。
* 切分结果在列表中展示，可预览、编辑单章大纲、重命名、删除、批量勾选。
* 列表项显示状态徽标与流式进度，只响应 core.engine 发出的状态信号。
* 勾选框使用**行内真实的 QCheckBox**：列表项用 setItemWidget 填充自定义行之后，
  QListWidget 自身绘制的勾选指示器会被行控件完全遮挡、无法点击，
  因此本模块不依赖 ItemIsUserCheckable，而是自己管理勾选状态（_checked 集合）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core import outline_parser
from core.models import Chapter, ChapterState, STATE_LABELS

from .help_icon import HelpIcon, attach_help
from .widgets import StateBadge, ask_text, confirm, info, warn

ENCODINGS = ("utf-8", "utf-8-sig", "gbk", "gb18030", "big5")

#: 用于从大纲首行猜测小说名（去掉"第一章 / 第1章 / Chapter 1"这类前缀）
_CHAPTER_PREFIX = re.compile(
    r"^\s*(?:第\s*[0-9一二三四五六七八九十百千零两]+\s*[章节回卷]|chapter\s*\d+|"
    r"\d+\s*[、.．,，])\s*[:：、.\-—]?\s*",
    re.IGNORECASE,
)


def _strip_chapter_prefix(line: str) -> str:
    return _CHAPTER_PREFIX.sub("", line or "", count=1).strip()


def _read_text_file(path: Path) -> tuple[str | None, str]:
    """依次尝试常见编码读取文本文件。返回 (文本或 None, 使用的编码)。"""
    for encoding in ENCODINGS:
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
        except OSError:
            return None, ""
    # 最后用替换策略兜底，保证用户至少能看到内容
    try:
        return path.read_text(encoding="utf-8", errors="replace"), "utf-8(replace)"
    except OSError:
        return None, ""


class ClickableLabel(QLabel):
    """可点击的 QLabel（用于让标题/序号也能切换勾选，提供更大的点击区域）。"""

    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class ClickableBadge(StateBadge):
    """可点击的状态徽标（同样用于切换勾选）。"""

    clicked = Signal()

    def __init__(self, state: ChapterState = ChapterState.PENDING, parent: QWidget | None = None) -> None:
        super().__init__(state, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击可切换勾选状态。")

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class OutlineEditorDialog(QDialog):
    """编辑单章大纲。"""

    def __init__(self, chapter: Chapter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"编辑大纲 - {chapter.title}")
        self.resize(720, 520)
        layout = QVBoxLayout(self)

        row = QHBoxLayout()
        row.addWidget(QLabel("章节标题", self))
        self.title_edit = QLineEdit(chapter.title, self)
        row.addWidget(self.title_edit, 1)
        layout.addLayout(row)

        layout.addWidget(
            QLabel("章节大纲（整体作为一个输入单元，程序不做更细粒度切分）", self)
        )
        self.edit = QPlainTextEdit(self)
        self.edit.setPlainText(chapter.outline)
        layout.addWidget(self.edit, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str]:
        return self.title_edit.text().strip(), self.edit.toPlainText()


class ChapterListPanel(QWidget):
    """左侧面板：大纲编辑 + 章节列表。"""

    chaptersChanged = Signal()
    chapterSelected = Signal(str)
    generateSelectedRequested = Signal()
    stopSelectedRequested = Signal()
    patternChanged = Signal(str)
    #: 切分大纲成功后发出（大纲原文, 建议的小说名）。主窗口据此触发"立项"。
    outlineSplit = Signal(str, str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.chapters: list[Chapter] = []
        self._checked: set[str] = set()      # 被勾选的章节 id
        self._pattern = outline_parser.DEFAULT_CHAPTER_PATTERN
        self._current_id = ""
        #: 由 MainWindow 注入：收到"当前已有 N 章"后返回重新切分的确认文案。
        #: 已立项时文案会说明"旧内容已存档、不会丢"，未立项时才提醒风险。
        self.resplit_message_provider: Callable[[int], str] | None = None
        self._build_ui()

    # ================================================================== #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        self.splitter = QSplitter(Qt.Orientation.Vertical, self)

        # ---------------- 大纲编辑区 ---------------- #
        outline_box = QWidget(self)
        box = QVBoxLayout(outline_box)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(4)

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(QLabel("<b>大纲文本</b>", outline_box))
        head.addWidget(HelpIcon("outline_import", outline_box, size=12))
        head.addStretch(1)

        self.import_button = QPushButton("导入文件", outline_box)
        attach_help(self.import_button, "outline_import")
        self.import_button.clicked.connect(self.import_outline)
        head.addWidget(self.import_button)

        self.clear_button = QPushButton("清空", outline_box)
        self.clear_button.setToolTip("清空大纲文本（不影响已切分的章节列表）。")
        self.clear_button.clicked.connect(lambda: self.outline_edit.setPlainText(""))
        head.addWidget(self.clear_button)
        box.addLayout(head)

        self.outline_edit = QPlainTextEdit(outline_box)
        self.outline_edit.setPlaceholderText(
            "在此粘贴小说大纲，或点击“导入文件”。\n"
            "大纲需包含形如“第一章 / 第1章 / 第 1 章 / Chapter 1”的章节标题行。"
        )
        self.outline_edit.setMinimumHeight(140)
        attach_help(self.outline_edit, "outline_import")
        box.addWidget(self.outline_edit, 1)

        pattern_row = QHBoxLayout()
        pattern_row.setSpacing(6)
        pattern_row.addWidget(QLabel("切分正则", outline_box))
        self.pattern_edit = QLineEdit(self._pattern, outline_box)
        self.pattern_edit.setPlaceholderText(outline_parser.DEFAULT_CHAPTER_PATTERN)
        attach_help(self.pattern_edit, "chapter_pattern")
        pattern_row.addWidget(self.pattern_edit, 1)
        pattern_row.addWidget(HelpIcon("chapter_pattern", outline_box, size=12))
        box.addLayout(pattern_row)

        action_row = QHBoxLayout()
        action_row.setSpacing(6)
        self.split_button = QPushButton("切分章节", outline_box)
        attach_help(self.split_button, "outline_split")
        self.split_button.clicked.connect(self.split_outline)
        action_row.addWidget(self.split_button)

        self.test_button = QPushButton("测试正则", outline_box)
        self.test_button.setToolTip("只预览正则的匹配结果，不修改章节列表。")
        self.test_button.clicked.connect(self.test_pattern)
        action_row.addWidget(self.test_button)
        action_row.addStretch(1)
        box.addLayout(action_row)

        self.status_label = QLabel("", outline_box)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#57606a; font-size:11px;")
        box.addWidget(self.status_label)

        self.splitter.addWidget(outline_box)

        # ---------------- 章节列表 ---------------- #
        list_box = QWidget(self)
        list_layout = QVBoxLayout(list_box)
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(4)

        list_head = QHBoxLayout()
        list_head.setSpacing(6)
        self.count_label = QLabel("<b>章节列表</b>（0 章）", list_box)
        list_head.addWidget(self.count_label)
        list_head.addStretch(1)

        self.select_all_button = QPushButton("全选", list_box)
        self.select_all_button.clicked.connect(lambda: self.set_all_checked(True))
        list_head.addWidget(self.select_all_button)

        self.select_none_button = QPushButton("全不选", list_box)
        self.select_none_button.clicked.connect(lambda: self.set_all_checked(False))
        list_head.addWidget(self.select_none_button)
        list_layout.addLayout(list_head)

        self.filter_combo = QComboBox(list_box)
        self.filter_combo.addItems(["显示全部", "仅未合格", "仅待决策", "仅出错"])
        self.filter_combo.setToolTip("按状态过滤列表显示；不影响生成与导出。")
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        list_layout.addWidget(self.filter_combo)

        self.list_widget = QListWidget(list_box)
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list_widget.currentItemChanged.connect(self._on_current_changed)
        self.list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list_widget.customContextMenuRequested.connect(self._on_context_menu)
        attach_help(self.list_widget, "act_switch_view")
        list_layout.addWidget(self.list_widget, 1)

        batch_row = QHBoxLayout()
        batch_row.setSpacing(6)
        self.generate_selected_button = QPushButton("生成选中章节", list_box)
        attach_help(self.generate_selected_button, "act_generate")
        self.generate_selected_button.clicked.connect(self.generateSelectedRequested.emit)
        batch_row.addWidget(self.generate_selected_button)

        self.stop_selected_button = QPushButton("停止选中", list_box)
        attach_help(self.stop_selected_button, "act_stop_chapter")
        self.stop_selected_button.clicked.connect(self.stopSelectedRequested.emit)
        batch_row.addWidget(self.stop_selected_button)
        list_layout.addLayout(batch_row)

        self.splitter.addWidget(list_box)
        self.splitter.setStretchFactor(0, 2)
        self.splitter.setStretchFactor(1, 3)
        root.addWidget(self.splitter)

    # ================================================================== #
    # 大纲导入与切分
    # ================================================================== #
    def outline_text(self) -> str:
        return self.outline_edit.toPlainText()

    def set_outline_text(self, text: str) -> None:
        self.outline_edit.setPlainText(text or "")

    def pattern(self) -> str:
        return self.pattern_edit.text().strip() or outline_parser.DEFAULT_CHAPTER_PATTERN

    def import_outline(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入大纲文件", "", "文本文件 (*.txt *.md *.text);;所有文件 (*)"
        )
        if not path:
            return
        text, encoding = _read_text_file(Path(path))
        if text is None:
            warn(self, "导入失败", f"无法读取文件：{path}")
            return
        self.outline_edit.setPlainText(text)
        self.status_label.setText(f"已导入 {path}（编码 {encoding}，{len(text):,} 字符）")

    def test_pattern(self) -> None:
        ok, message = outline_parser.validate_pattern(self.pattern())
        if not ok:
            self.status_label.setStyleSheet("color:#cf222e; font-size:11px;")
            self.status_label.setText(message)
            return
        hits = outline_parser.preview_matches(self.outline_text(), self.pattern(), limit=60)
        self.status_label.setStyleSheet("color:#57606a; font-size:11px;")
        if not hits:
            self.status_label.setText("未匹配到任何章节标题行。请检查正则，或使用下方的兜底切分方式。")
            return
        preview = "，".join(f"第{line}行：{text[:20]}" for line, text in hits[:8])
        more = f"（共 {len(hits)} 处，仅显示前 8 处）" if len(hits) > 8 else ""
        self.status_label.setText(f"匹配到 {len(hits)} 处：{preview}{more}")

    def split_outline(self) -> None:
        pattern = self.pattern()
        ok, message = outline_parser.validate_pattern(pattern)
        if not ok:
            warn(self, "正则错误", message)
            return

        text = self.outline_text()
        if not text.strip():
            warn(self, "无内容", "请先粘贴或导入大纲文本。")
            return

        parsed = outline_parser.split_outline(text, pattern)
        if not parsed:
            self._offer_fallbacks(text)
            return

        if self.chapters:
            message = f"当前已有 {len(self.chapters)} 章。是否用新大纲重新切分？"
            if self.resplit_message_provider is not None:
                try:
                    message = self.resplit_message_provider(len(self.chapters))
                except Exception:  # pragma: no cover - 提示文案失败不应挡住切分
                    pass
            if not confirm(self, "重新切分", message):
                return
        self._apply_parsed(parsed)
        self.status_label.setStyleSheet("color:#1a7f37; font-size:11px;")
        self.status_label.setText(f"已切分 {len(parsed)} 章。")

    def _offer_fallbacks(self, text: str) -> None:
        from PySide6.QtWidgets import QMessageBox

        box = QMessageBox(self)
        box.setWindowTitle("未匹配到章节")
        box.setText(
            "按当前正则没有匹配到任何章节标题。\n\n"
            "你可以：\n"
            "· 修改切分正则后重试\n"
            "· 整篇作为一章\n"
            "· 按空行分段（每段至少 200 字）"
        )
        whole = box.addButton("整篇作为一章", QMessageBox.ButtonRole.AcceptRole)
        blocks = box.addButton("按空行分段", QMessageBox.ButtonRole.ActionRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.exec()

        clicked = box.clickedButton()
        if clicked is cancel:
            return
        if clicked is whole:
            parsed = outline_parser.split_as_single(text)
        elif clicked is blocks:
            parsed = outline_parser.split_by_blank_lines(text)
        else:  # pragma: no cover
            return
        if not parsed:
            warn(self, "切分失败", "没有可用的内容。")
            return
        self._apply_parsed(parsed)
        self.status_label.setStyleSheet("color:#9a6700; font-size:11px;")
        self.status_label.setText(
            f"已使用兜底方式切分为 {len(parsed)} 章，请检查大纲内容是否符合预期。"
        )

    def _apply_parsed(self, parsed: list[outline_parser.ParsedChapter]) -> None:
        self.chapters = [
            Chapter.create(item.index, item.title, item.outline) for item in parsed
        ]
        self._checked &= {c.chapter_id for c in self.chapters}   # 丢弃已不存在的勾选
        self._rebuild_list()
        self.chaptersChanged.emit()
        if self.chapters:
            self.list_widget.setCurrentRow(0)
        # 切分成功即"触发立项"：主窗口会请用户输入小说名并新建项目文件夹
        self.outlineSplit.emit(self.outline_text(), self._suggest_project_name())

    # ================================================================== #
    # 列表
    # ================================================================== #
    def set_chapters(self, chapters: list[Chapter]) -> None:
        self.chapters = chapters
        self._rebuild_list()

    def refresh_all(self) -> None:
        """按模型中的真实状态刷新整张列表（批量提交任务后调用）。"""
        for chapter in self.chapters:
            self.refresh_chapter(chapter)

    def _suggest_project_name(self) -> str:
        """从大纲文本里猜一个小说名，作为立项时的默认值（用户可改）。

        只做非常轻量的猜测：取第一行去掉章节编号后的文字；猜不出来就用「新小说」。
        """
        for raw in (self.outline_text() or "").splitlines():
            line = raw.strip().lstrip("#").strip()
            if not line:
                continue
            cleaned = _strip_chapter_prefix(line)
            cleaned = cleaned.strip("【】[]（）() 《》").strip()
            if cleaned:
                return cleaned[:40]
        return "新小说"

    def _rebuild_list(self) -> None:
        self.list_widget.clear()
        for chapter in self.chapters:
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, chapter.chapter_id)
            item.setFlags(
                Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
            )
            self.list_widget.addItem(item)
            self.list_widget.setItemWidget(item, self._make_row(chapter))
        self._apply_filter()
        self._refresh_count()

    def _make_row(self, chapter: Chapter) -> QWidget:
        row = QWidget(self.list_widget)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(6)

        # 行内真实复选框：可以点击，也可以点标题切换
        checkbox = QCheckBox(row)
        checkbox.setChecked(chapter.chapter_id in self._checked)
        checkbox.setToolTip("勾选后可用“生成选中章节 / 停止选中”批量操作。")
        checkbox.toggled.connect(
            lambda checked, cid=chapter.chapter_id: self._on_row_toggled(cid, checked)
        )
        layout.addWidget(checkbox)

        index_label = ClickableLabel(f"{chapter.index}", row)
        index_label.setMinimumWidth(22)
        index_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        index_label.setStyleSheet("color:#8a8f98;")
        index_label.clicked.connect(lambda cid=chapter.chapter_id: self._toggle_row(cid))
        layout.addWidget(index_label)

        badge = ClickableBadge(chapter.state, row)
        badge.setProperty("chapterId", chapter.chapter_id)
        badge.clicked.connect(lambda cid=chapter.chapter_id: self._toggle_row(cid))
        layout.addWidget(badge)

        title_label = ClickableLabel(chapter.title or f"第{chapter.index}章", row)
        title_label.setToolTip(
            f"<b>{chapter.title}</b><br>大纲 {len(chapter.outline):,} 字符<br>"
            f"正文 {len(chapter.generated_content):,} 字符<br>"
            f"重生成 {chapter.regen_count} 次<br>点击可切换勾选状态"
        )
        title_label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        title_label.clicked.connect(lambda cid=chapter.chapter_id: self._toggle_row(cid))
        layout.addWidget(title_label, 1)

        progress = QProgressBar(row)
        progress.setRange(0, 0)          # 不确定进度（忙碌动画）
        progress.setMaximumWidth(72)
        progress.setFixedHeight(10)
        progress.setTextVisible(False)
        progress.setVisible(chapter.busy)
        progress.setProperty("chapterId", chapter.chapter_id)
        layout.addWidget(progress)
        return row

    # ------------------------------------------------------------------ #
    # 勾选状态
    # ------------------------------------------------------------------ #
    def _on_row_toggled(self, chapter_id: str, checked: bool) -> None:
        if checked:
            self._checked.add(chapter_id)
        else:
            self._checked.discard(chapter_id)
        self._refresh_count()

    def _toggle_row(self, chapter_id: str) -> None:
        """点标题/序号也能切换勾选（复选框本身可点，这里提供更大的点击区域）。"""
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) != chapter_id:
                continue
            widget = self.list_widget.itemWidget(item)
            if widget is None:
                return
            box = widget.findChild(QCheckBox)
            if box is not None:
                box.setChecked(not box.isChecked())
            return

    def _checkbox_of(self, chapter_id: str) -> QCheckBox | None:
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) != chapter_id:
                continue
            widget = self.list_widget.itemWidget(item)
            return widget.findChild(QCheckBox) if widget is not None else None
        return None

    def set_row_checked(self, chapter_id: str, checked: bool) -> None:
        box = self._checkbox_of(chapter_id)
        if box is not None:
            box.setChecked(checked)
        else:
            self._on_row_toggled(chapter_id, checked)

    def refresh_chapter(self, chapter: Chapter) -> None:
        """按**模型中的真实状态**刷新列表项（状态徽标、进度）。

        这里刻意不直接使用信号携带的状态值：引擎线程发出的信号在 UI 线程是排队投递的，
        连续多次状态变更时，信号到达顺序可能与真实执行顺序不完全一致。
        以章节对象当前状态为准，可以让界面在任何情况下都收敛到真实状态。
        """
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            widget = self.list_widget.itemWidget(item)
            if widget is None:
                continue
            item_chapter = self.chapter_by_id(item.data(Qt.ItemDataRole.UserRole))
            if item_chapter is None:
                continue
            for child in widget.findChildren(StateBadge):
                child.set_state(item_chapter.state)
            for child in widget.findChildren(QProgressBar):
                child.setVisible(item_chapter.busy)
        self._apply_filter()
        self._refresh_count()

    def _refresh_count(self) -> None:
        total = len(self.chapters)
        passed = sum(1 for c in self.chapters if c.state == ChapterState.PASSED)
        pending = sum(1 for c in self.chapters if c.state == ChapterState.AWAITING_DECISION)
        visible = sum(
            1 for row in range(self.list_widget.count())
            if not self.list_widget.item(row).isHidden()
        )
        self.count_label.setText(
            f"<b>章节列表</b>（{total} 章，显示 {visible}）合格 {passed}，待决策 {pending}"
        )

    def _apply_filter(self) -> None:
        mode = self.filter_combo.currentText()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            chapter_id = item.data(Qt.ItemDataRole.UserRole)
            chapter = self.chapter_by_id(chapter_id)
            if chapter is None:
                continue
            visible = True
            if mode == "仅未合格":
                visible = chapter.state != ChapterState.PASSED
            elif mode == "仅待决策":
                visible = chapter.state == ChapterState.AWAITING_DECISION
            elif mode == "仅出错":
                visible = chapter.state == ChapterState.FAILED
            item.setHidden(not visible)
        self._refresh_count()

    # ================================================================== #
    # 选中与操作
    # ================================================================== #
    def chapter_by_id(self, chapter_id: str) -> Chapter | None:
        for chapter in self.chapters:
            if chapter.chapter_id == chapter_id:
                return chapter
        return None

    def current_chapter(self) -> Chapter | None:
        return self.chapter_by_id(self._current_id)

    def select_chapter(self, chapter_id: str) -> None:
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.data(Qt.ItemDataRole.UserRole) == chapter_id:
                self.list_widget.setCurrentItem(item)
                return

    def selected_chapters(self) -> list[Chapter]:
        """当前被勾选的章节（按列表顺序）。"""
        return [c for c in self.chapters if c.chapter_id in self._checked]

    def checked_ids(self) -> set[str]:
        return set(self._checked)

    def set_all_checked(self, checked: bool) -> None:
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item.isHidden():
                continue
            chapter_id = item.data(Qt.ItemDataRole.UserRole)
            box = self._checkbox_of(chapter_id)
            if box is not None:
                box.setChecked(checked)          # 触发 toggled -> 更新 _checked
            else:
                self._on_row_toggled(chapter_id, checked)
        self._refresh_count()

    def _on_current_changed(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        self._current_id = current.data(Qt.ItemDataRole.UserRole) or ""
        self.chapterSelected.emit(self._current_id)

    def _on_context_menu(self, position) -> None:
        from PySide6.QtWidgets import QMenu

        item = self.list_widget.itemAt(position)
        if item is None:
            return
        chapter = self.chapter_by_id(item.data(Qt.ItemDataRole.UserRole))
        if chapter is None:
            return

        menu = QMenu(self)
        menu.addAction("编辑大纲…", lambda: self.edit_chapter_outline(chapter))
        menu.addAction("重命名…", lambda: self.rename_chapter(chapter))
        menu.addAction("删除本章", lambda: self.delete_chapter(chapter))
        menu.addSeparator()
        menu.addAction("全选", lambda: self.set_all_checked(True))
        menu.addAction("全不选", lambda: self.set_all_checked(False))
        menu.exec(self.list_widget.viewport().mapToGlobal(position))

    def edit_chapter_outline(self, chapter: Chapter) -> None:
        dialog = OutlineEditorDialog(chapter, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        title, outline = dialog.values()
        chapter.title = title or chapter.title
        chapter.outline = outline
        self._rebuild_list()
        self.chaptersChanged.emit()

    def rename_chapter(self, chapter: Chapter) -> None:
        ok, text = ask_text(
            self, "重命名章节", "新的章节标题：", placeholder=chapter.title
        )
        if ok and text.strip():
            chapter.title = text.strip()
            self._rebuild_list()
            self.chaptersChanged.emit()

    def delete_chapter(self, chapter: Chapter) -> None:
        if chapter.busy:
            info(self, "请稍候", "该章正在生成/检查中，请先停止后再删除。")
            return
        if not confirm(self, "删除章节", f"确定删除「{chapter.title}」及其已生成内容吗？此操作不可撤销。"):
            return
        self.chapters = [c for c in self.chapters if c.chapter_id != chapter.chapter_id]
        self._checked.discard(chapter.chapter_id)
        for index, item in enumerate(self.chapters, start=1):
            item.index = index
        self._rebuild_list()
        self.chaptersChanged.emit()
