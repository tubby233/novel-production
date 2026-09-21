"""主窗口：把大纲、章节列表、章节详情、引擎信号与配置串起来。

线程安全说明
------------
* 本类所有槽函数都在 UI 线程执行（引擎信号跨线程自动排队）。
* 一切写操作都通过 ``engine`` 的方法提交，UI 不直接改章节状态。
* 章节对象的内容字段（正文/检查结果）由引擎侧写入，UI 侧只读取用于渲染。
"""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QDialog,
    QDockWidget,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from core import config as config_mod
from core import message_templates as MT
from core import param_help
from core import paths
from core import project as project_mod
from core.api_client import DeepSeekClient
from core.config import AppConfig, model_limits
from core.engine import Engine
from core.exporter import ExportOptions
from core.models import (
    Chapter,
    ChapterState,
    Conversation,
    STATE_LABELS,
    ThinkingConfig,
    Usage,
)

from .chapter_view import ChapterView
from .export_dialog import ExportDialog
from .help_icon import HelpDialog, attach_help
from .outline_panel import ChapterListPanel
from .queue_panel import QueuePanel
from .settings_dialog import SettingsDialog
from .widgets import ask_text, confirm, error, info, warn

CHAPTERS_FILENAME = "chapters.json"
AUTOSAVE_DELAY_MS = 1500
#: 对话存档写盘延迟与轮询间隔
CONVERSATION_SAVE_DELAY_MS = 800
CONVERSATION_POLL_MS = 500


class LogWindow(QDialog):
    """运行日志窗口。

    按需求：日志**默认不显示**，只有点击"运行日志"菜单时才在独立窗口里打开，
    因此不占用主界面空间，也不影响正在进行的操作（非模态）。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("运行日志")
        self.resize(760, 380)
        self.setSizeGripEnabled(True)
        layout = QVBoxLayout(self)
        layout.setSpacing(6)

        hint = QLabel(
            "这里显示任务状态、API 提示、用量与错误信息（最多保留最近 1000 条）。",
            self,
        )
        hint.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(hint)

        self.view = QPlainTextEdit(self)
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(1000)
        self.view.setPlaceholderText("暂无日志。")
        layout.addWidget(self.view, 1)

        buttons = QHBoxLayout()
        self.copy_button = QPushButton("复制全部", self)
        self.copy_button.clicked.connect(self._copy_all)
        buttons.addWidget(self.copy_button)
        self.clear_button = QPushButton("清空日志", self)
        self.clear_button.clicked.connect(self.view.clear)
        buttons.addWidget(self.clear_button)
        buttons.addStretch(1)
        self.close_button = QPushButton("关闭", self)
        self.close_button.clicked.connect(self.hide)
        buttons.addWidget(self.close_button)
        layout.addLayout(buttons)

    def append_html(self, html: str) -> None:
        self.view.appendHtml(html)

    def _copy_all(self) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.view.toPlainText())


class ProjectDialog(QDialog):
    """打开已有小说项目（列出项目根目录下识别到的项目文件夹）。"""

    def __init__(
        self, projects: list[project_mod.Project], base_dir: str, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("打开项目")
        self.resize(560, 420)
        self._projects = projects
        self._chosen: project_mod.Project | None = None

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        location = QLabel(f"项目根目录：{base_dir}", self)
        location.setWordWrap(True)
        location.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(location)

        from PySide6.QtWidgets import QListWidget, QListWidgetItem

        self.list_widget = QListWidget(self)
        for project in projects:
            import time as _time

            stamp = _time.strftime(
                "%Y-%m-%d %H:%M", _time.localtime(project.updated_at or project.created_at or 0.0)
            )
            item = QListWidgetItem(f"{project.name}　（{stamp}）\n{project.root}")
            item.setData(Qt.ItemDataRole.UserRole, str(project.root))
            self.list_widget.addItem(item)
        if projects:
            self.list_widget.setCurrentRow(0)
        self.list_widget.itemDoubleClicked.connect(lambda _item: self._accept_current())
        layout.addWidget(self.list_widget, 1)

        buttons = QHBoxLayout()
        self.browse_button = QPushButton("从文件夹打开…", self)
        self.browse_button.setToolTip("项目在其它位置时，直接选择该项目文件夹。")
        self.browse_button.clicked.connect(self._browse)
        buttons.addWidget(self.browse_button)
        buttons.addStretch(1)
        self.open_button = QPushButton("打开", self)
        self.open_button.clicked.connect(self._accept_current)
        buttons.addWidget(self.open_button)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def chosen(self) -> project_mod.Project | None:
        return self._chosen

    def _accept_current(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            info(self, "未选择项目", "请先在上面的列表里选择一个小说项目。")
            return
        root = item.data(Qt.ItemDataRole.UserRole)
        try:
            self._chosen = project_mod.load_project(root)
        except OSError as exc:
            error(self, "打开失败", str(exc))
            return
        self.accept()

    def _browse(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择小说项目文件夹")
        if not folder:
            return
        try:
            self._chosen = project_mod.load_project(folder)
        except OSError as exc:
            error(self, "打开失败", str(exc))
            return
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, cfg: AppConfig) -> None:
        super().__init__()
        self.cfg = cfg
        self.engine = Engine(cfg, self)
        #: 每类对话最近一次"开始"信号的 run_seq（用于丢弃上一轮遗留的陈旧信号）
        self._started_seq: dict[str, int] = {"generate": -1, "check": -1}
        #: 当前打开的小说项目（None = 还没立项，数据仍写在全局数据目录）
        self.project: project_mod.Project | None = None
        #: 运行日志窗口（按需创建，默认不存在也就不会显示）
        self.log_window: LogWindow | None = None
        #: "立项进行中"标记：期间禁止自动保存，避免新大纲覆盖原项目（见 _schedule_autosave）
        self._project_setup_in_progress = False

        self.setWindowTitle("小说生成器 · DeepSeek")
        self.resize(1440, 900)

        self._autosave_timer = QTimer(self)
        self._autosave_timer.setSingleShot(True)
        self._autosave_timer.setInterval(AUTOSAVE_DELAY_MS)
        self._autosave_timer.timeout.connect(self._autosave_chapters)

        #: 对话存档（conversations.json）的落盘定时器。
        #: 触发时机：AI 回复完成 / 清空对话 / 手动消息等（见 _poll_conversations）。
        self._conv_timer = QTimer(self)
        self._conv_timer.setSingleShot(True)
        self._conv_timer.setInterval(CONVERSATION_SAVE_DELAY_MS)
        self._conv_timer.timeout.connect(lambda: self._flush_conversations())
        self._conv_poll_timer = QTimer(self)
        self._conv_poll_timer.setInterval(CONVERSATION_POLL_MS)
        self._conv_poll_timer.timeout.connect(self._poll_conversations)

        self._build_ui()
        self._connect_engine()
        self._build_menus()

        self._load_state()
        self.engine.start()
        # 轮询引擎的"对话已变化"标记：AI 每次回复完成后自动更新对话存档
        self._conv_poll_timer.start()
        self._log("info", f"配置文件：{paths.config_path()}")
        if not paths.config_path().exists():
            # 兜底：load_config 已经重建过，这里只是把结果再确认一次
            try:
                config_mod.save_config(self.cfg)
                self._log("info", f"已重新生成默认配置文件：{paths.config_path()}")
            except OSError as exc:
                self._log("warn", f"无法重新生成配置文件：{exc}")
        self._log("info", "程序不做任何 AI 文本解析：只做模板字符串替换、状态流转与界面展示。")
        self._refresh_project_ui()
        self._qa_check()

    # ================================================================== #
    # 界面
    # ================================================================== #
    def _build_ui(self) -> None:
        # ---- 中央：左列表 + 右详情 ---- #
        from PySide6.QtWidgets import QSplitter

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.outline_panel = ChapterListPanel(self)
        self.chapter_view = ChapterView(
            global_hide_reasoning=self.cfg.ui.global_hide_reasoning,
            collapse_tokens=self.cfg.ui.reasoning_collapse_tokens,
            parent=self,
        )
        self.splitter.addWidget(self.outline_panel)
        self.splitter.addWidget(self.chapter_view)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        self.splitter.setSizes([420, 1020])
        self.setCentralWidget(self.splitter)

        # ---- 右侧：任务队列面板（默认隐藏，工具栏/菜单可开关）---- #
        self.queue_panel = QueuePanel(self.engine, self)
        self.queue_dock = QDockWidget("任务队列", self)
        self.queue_dock.setObjectName("queue_dock")
        self.queue_dock.setAllowedAreas(
            Qt.DockWidgetArea.RightDockWidgetArea | Qt.DockWidgetArea.LeftDockWidgetArea
        )
        self.queue_dock.setWidget(self.queue_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.queue_dock)
        self.queue_dock.setVisible(False)
        self.queue_dock.visibilityChanged.connect(self._on_queue_dock_visibility)

        # ---- 左面板信号 ---- #
        self.outline_panel.chapterSelected.connect(self._on_chapter_selected)
        self.outline_panel.chaptersChanged.connect(self._on_chapters_changed)
        self.outline_panel.generateSelectedRequested.connect(self._generate_selected)
        self.outline_panel.stopSelectedRequested.connect(self._stop_selected)
        # 切分大纲成功 -> 触发立项（由用户输入小说名，见 _on_outline_split）
        self.outline_panel.outlineSplit.connect(self._on_outline_split)
        # 让左侧面板能问到"当前小说是否已存档"，据此给出准确的重新切分提示
        self.outline_panel.resplit_message_provider = self._resplit_message

        # ---- 章节详情信号 ---- #
        view = self.chapter_view
        view.generateRequested.connect(self._generate_current)
        view.triggerCheck.connect(self._check_current)
        view.regenByCheck.connect(self._regen_by_check)
        view.regenManual.connect(self._regen_manual)
        view.accept.connect(self._accept_current)
        view.postpone.connect(self._postpone_current)
        view.stopChapter.connect(self._stop_current)
        view.contentEdited.connect(self._on_content_edited)
        view.checkResultEdited.connect(self._on_check_edited)
        view.generateThinkingChanged.connect(
            lambda t: self._on_thinking_changed("generate", t)
        )
        view.checkThinkingChanged.connect(lambda t: self._on_thinking_changed("check", t))
        view.generateManualMessage.connect(
            lambda text: self._send_manual("generate", text)
        )
        view.checkManualMessage.connect(lambda text: self._send_manual("check", text))
        view.clearGenerateConversation.connect(
            lambda: self._clear_conversation("generate")
        )
        view.clearCheckConversation.connect(lambda: self._clear_conversation("check"))

        # ---- 日志窗口 ---- #
        # 需求：日志默认不显示，只有点击"运行日志"时才在**新窗口**里展示。
        # 因此这里不创建任何 dock/面板，只在需要时按需创建 LogWindow。
        self.act_show_log = QAction("运行日志", self)
        self.act_show_log.setToolTip("在独立窗口中查看运行日志（默认不显示）。")
        self.act_show_log.triggered.connect(self.show_log_window)

        # ---- 工具栏 ---- #
        self.toolbar = QToolBar("主工具栏", self)
        self.toolbar.setMovable(False)
        self.addToolBar(self.toolbar)

        self.act_import_outline = QAction("导入大纲", self)
        self.act_import_outline.setToolTip("从 txt/md 文件导入大纲到左侧编辑区。")
        self.act_import_outline.triggered.connect(self.outline_panel.import_outline)
        self.toolbar.addAction(self.act_import_outline)

        self.act_split = QAction("切分章节", self)
        self.act_split.setToolTip("按当前正则切分大纲为章节列表，并触发立项（输入小说名）。")
        self.act_split.triggered.connect(self.outline_panel.split_outline)
        self.toolbar.addAction(self.act_split)

        self.act_open_project = QAction("打开项目", self)
        self.act_open_project.setToolTip(
            "打开一个已有的小说项目（读取其章节、正文与对话）。"
            "详情见“帮助 -> 参数说明总览”中的“打开项目”一条。"
        )
        self.act_open_project.triggered.connect(self.open_project_dialog)
        self.toolbar.addAction(self.act_open_project)

        self.toolbar.addSeparator()

        self.act_generate_all = QAction("全量生成", self)
        self.act_generate_all.setToolTip(
            "对列表中所有“待生成/出错”的章节排队生成；并发的章节数由设置中的并发数决定。"
        )
        self.act_generate_all.triggered.connect(self._generate_all)
        self.toolbar.addAction(self.act_generate_all)

        self.act_generate_selected = QAction("生成选中", self)
        self.act_generate_selected.setToolTip("只对勾选的章节排队生成。")
        self.act_generate_selected.triggered.connect(self._generate_selected)
        self.toolbar.addAction(self.act_generate_selected)

        self.act_stop_all = QAction("停止全部", self)
        self.act_stop_all.setToolTip("取消所有正在运行与排队中的任务。")
        self.act_stop_all.triggered.connect(self.engine.stop_all)
        self.toolbar.addAction(self.act_stop_all)

        self.toolbar.addSeparator()

        self.toolbar.addWidget(QLabel(" 并发数 "))
        self.concurrency_spin = QSpinBox(self)
        self.concurrency_spin.setRange(1, 2500)
        self.concurrency_spin.setValue(self.cfg.sampling.concurrency)
        attach_help(self.concurrency_spin, "concurrency")
        self.concurrency_spin.valueChanged.connect(self._on_concurrency_changed)
        self.toolbar.addWidget(self.concurrency_spin)
        self.toolbar.addWidget(
            QLabel(f" （{model_limits(self.cfg.api.model).concurrency} 为当前模型账号并发上限） ")
        )

        self.toolbar.addSeparator()
        self.act_export = QAction("一键导出 TXT", self)
        self.act_export.setToolTip("把章节正文导出为 TXT（未确认章节会加标注）。")
        self.act_export.triggered.connect(self.open_export_dialog)
        self.toolbar.addAction(self.act_export)

        # 需求：在页面上单独放一个按钮，随时打开运行日志窗口（不必翻菜单）
        self.act_log_button = QAction("运行日志", self)
        self.act_log_button.setToolTip(
            "在独立窗口中查看运行日志（任务状态、API 提示、用量与错误）。默认不显示。"
        )
        attach_help(self.act_log_button, "act_show_log")
        self.act_log_button.triggered.connect(self.show_log_window)
        self.toolbar.addAction(self.act_log_button)

        # 队列面板开关（右侧停靠，默认不显示）
        self.act_queue = QAction("任务队列", self)
        self.act_queue.setCheckable(True)
        self.act_queue.setToolTip(
            "打开右侧的任务队列面板：查看排队情况，并可拖动调整执行优先级。"
        )
        attach_help(self.act_queue, "act_queue_panel")
        self.act_queue.toggled.connect(self.set_queue_visible)
        self.toolbar.addAction(self.act_queue)

        # ---- 状态栏 ---- #
        self.status_queue = QLabel("队列 0 ｜ 运行 0", self)
        self.status_queue.setToolTip(param_help.tooltip_text("status_queue"))
        self.status_tokens = QLabel("Token：0", self)
        self.status_tokens.setToolTip(param_help.tooltip_text("status_token"))
        self.status_progress = QLabel("进度 0/0", self)
        self.status_progress.setToolTip("已合格章节数 / 总章节数。")
        self.status_config = QLabel(f"配置：{paths.config_path()}", self)
        self.status_config.setToolTip(param_help.tooltip_text("status_config_path"))
        #: 当前项目（小说）与数据落盘位置
        self.status_project = QLabel("项目：未立项", self)
        self.status_project.setToolTip(
            "当前打开的小说项目。点“切分章节”会触发立项（输入小说名，"
            "程序在该目录下新建同名文件夹存放全部数据）。"
        )

        bar = self.statusBar()
        bar.addWidget(self.status_queue)
        bar.addWidget(self.status_tokens)
        bar.addWidget(self.status_progress)
        bar.addPermanentWidget(self.status_project)
        bar.addPermanentWidget(self.status_config)

    def _build_menus(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("文件(&F)")
        file_menu.addAction(self.act_import_outline)
        file_menu.addAction(self.act_split)
        file_menu.addSeparator()
        file_menu.addAction("导入配置…", self._import_config)
        file_menu.addAction("导出配置…", self._export_config)
        file_menu.addSeparator()
        file_menu.addAction(self.act_export)
        file_menu.addSeparator()
        file_menu.addAction("退出", self.close, QKeySequence.StandardKey.Quit)

        # 小说以项目制组织：立项（切分大纲时自动触发）/ 打开已有项目 / 直达项目文件夹
        project_menu = menubar.addMenu("项目(&P)")
        project_menu.addAction(self.act_split)
        project_menu.addAction(self.act_open_project)
        project_menu.addSeparator()
        project_menu.addAction("打开项目根目录", self._open_project_dir)
        project_menu.addAction("打开当前项目文件夹", self._open_current_project_dir)

        run_menu = menubar.addMenu("生成(&R)")
        run_menu.addAction(self.act_generate_all)
        run_menu.addAction(self.act_generate_selected)
        run_menu.addSeparator()
        run_menu.addAction(self.act_stop_all)
        run_menu.addAction("停止本章", self._stop_current)
        run_menu.addSeparator()
        run_menu.addAction(self.act_queue)

        settings_menu = menubar.addMenu("设置(&S)")
        settings_menu.addAction("打开设置…", self.open_settings, QKeySequence("Ctrl+,"))
        settings_menu.addAction("打开配置文件所在目录", self._open_config_dir)

        help_menu = menubar.addMenu("帮助(&H)")
        help_menu.addAction("参数说明总览…", self.show_help_overview)
        help_menu.addAction(self.act_show_log)
        help_menu.addAction("关于…", self.show_about)

    # ================================================================== #
    # 引擎信号
    # ================================================================== #
    def _connect_engine(self) -> None:
        sig = self.engine.signals
        sig.chapter_state_changed.connect(self._on_state_changed)
        sig.chapter_updated.connect(self._on_chapter_updated)
        sig.delta_received.connect(self._on_delta)
        sig.conversation_started.connect(self._on_conversation_started)
        sig.conversation_finished.connect(self._on_conversation_finished)
        sig.phase.connect(self._on_phase)
        sig.usage_received.connect(self._on_usage)
        sig.status.connect(self._on_status)
        sig.log.connect(self._log)
        sig.all_finished.connect(self._on_all_finished)

    def _on_state_changed(self, chapter_id: str, state: str) -> None:
        """状态信号处理。

        注意：这里以**章节对象当前的状态**为准，而不是信号携带的字符串。
        原因：信号在 UI 线程是排队投递的，同一章连续多次迁移时，
        到达顺序不一定与执行顺序完全一致；读模型可保证界面最终收敛到真实状态。
        """
        chapter = self.engine.get(chapter_id)
        if chapter is None:
            return
        self.outline_panel.refresh_chapter(chapter)
        if self._current_id() == chapter_id:
            self.chapter_view.set_state(chapter.state)
        self._schedule_autosave()
        self._refresh_progress()

    def _on_chapter_updated(self, chapter_id: str) -> None:
        chapter = self.engine.get(chapter_id)
        if chapter is None:
            return
        self.outline_panel.refresh_chapter(chapter)
        if self._current_id() == chapter_id:
            # 流式生成中不要用旧正文覆盖实时内容；结束后由 conversation_finished 收尾
            if not self.chapter_view.is_streaming_content():
                self.chapter_view.set_content(chapter.generated_content)
            self.chapter_view.check_view.render()
            self.chapter_view.refresh_check_result()
            self.chapter_view._refresh_history_combo()
            self.chapter_view._refresh_buttons()
        self._schedule_autosave()

    def _on_delta(
        self, chapter_id: str, conv_kind: str, delta_kind: str, text: str, run_seq: int
    ) -> None:
        chapter = self.engine.get(chapter_id)
        if chapter is None or self._current_id() != chapter_id:
            return
        # 只接受"当前这一轮"的增量：run_seq 与最近一次 started 信号一致
        if run_seq != self._started_seq.get(conv_kind):
            return
        target = (
            self.chapter_view.check_view if conv_kind == "check" else self.chapter_view.generate_view
        )
        target.stream_delta(delta_kind, text)
        # 生成中的正文同步显示在正文页（"正文"页正在流式展示）
        if conv_kind == "generate" and delta_kind == "content":
            self.chapter_view.append_content_stream(text)

    def _on_conversation_started(self, chapter_id: str, conv_kind: str, run_seq: int) -> None:
        chapter = self.engine.get(chapter_id)
        if chapter is None or self._current_id() != chapter_id:
            return
        self._started_seq[conv_kind] = run_seq
        view = self.chapter_view
        conv = chapter.check_conv if conv_kind == "check" else chapter.generate_conv
        target = view.check_view if conv_kind == "check" else view.generate_view
        if conv is not None:
            target.stream_started(conv)
        if conv_kind == "generate":
            # 进入正文页并实时显示本次生成的正文
            view.begin_content_stream()
            view.show_view("正文")

    def _on_conversation_finished(
        self, chapter_id: str, conv_kind: str, ok: bool, error_text: str, run_seq: int
    ) -> None:
        chapter = self.engine.get(chapter_id)
        if chapter is None:
            return
        if self._current_id() != chapter_id:
            return
        # 只处理"当前这一轮"的结束信号。上一轮被停止/重做后遗留的排队信号会被丢弃，
        # 否则它会把新一轮的流式显示状态与界面内容清掉（这是一个真实出现过的缺陷）。
        if run_seq != self._started_seq.get(conv_kind):
            self._log(
                "debug",
                f"{chapter.title}：已忽略上一轮任务的结束信号"
                f"（{conv_kind} seq={run_seq}，当前 {self._started_seq.get(conv_kind)}）。",
            )
            return
        target = (
            self.chapter_view.check_view
            if conv_kind == "check"
            else self.chapter_view.generate_view
        )
        target.stream_finished(ok, error_text)
        if conv_kind == "generate":
            self.chapter_view.end_content_stream(chapter.generated_content)
        else:
            # 检查结束：确保完整对话可见，且检查结果已填入可编辑文本框
            self.chapter_view.check_view.show_detail(True)
            self.chapter_view.refresh_check_result()
        if not ok and error_text:
            self._log("error", f"{chapter.title}：{error_text}")
        self._schedule_autosave()

    def _on_phase(self, chapter_id: str, phase: str, detail: str) -> None:
        if self._current_id() == chapter_id:
            self.chapter_view.set_phase(phase, detail)

    def _on_usage(self, chapter_id: str, conv_kind: str, usage: object) -> None:
        if not isinstance(usage, Usage):
            return
        chapter = self.engine.get(chapter_id)
        if chapter is None:
            return
        self._log(
            "info",
            f"{chapter.title}｜{'检查' if conv_kind == 'check' else '生成'}用量："
            f"输入 {usage.prompt_tokens:,}，输出 {usage.completion_tokens:,}"
            + (f"（其中思考 {usage.reasoning_tokens:,}）" if usage.reasoning_tokens else ""),
        )

    def _on_status(self, queue_len: int, running: int, total_usage: object) -> None:
        self.status_queue.setText(f"队列 {queue_len} ｜ 运行 {running}")
        if isinstance(total_usage, Usage):
            self.status_tokens.setText(
                f"Token：总 {total_usage.total_tokens:,}"
                f"（输入 {total_usage.prompt_tokens:,} / 输出 {total_usage.completion_tokens:,}"
                + (f"，思考 {total_usage.reasoning_tokens:,}" if total_usage.reasoning_tokens else "")
                + "）"
            )

    def _on_all_finished(self) -> None:
        self._log("info", "全部任务已完成。")
        self._schedule_autosave()
        self._refresh_progress()

    def _refresh_progress(self) -> None:
        chapters = self.outline_panel.chapters
        passed = sum(1 for c in chapters if c.state == ChapterState.PASSED)
        done = sum(1 for c in chapters if c.has_content)
        self.status_progress.setText(f"进度 已合格 {passed} ｜ 已有正文 {done} ｜ 共 {len(chapters)}")

    # ================================================================== #
    # 章节操作
    # ================================================================== #
    def _current_id(self) -> str:
        chapter = self.outline_panel.current_chapter()
        return chapter.chapter_id if chapter else ""

    def _current_chapter(self) -> Chapter | None:
        return self.outline_panel.current_chapter()

    def _on_chapter_selected(self, chapter_id: str) -> None:
        chapter = self.engine.get(chapter_id)
        self.chapter_view.set_chapter(chapter)

    def _on_chapters_changed(self) -> None:
        self.engine.set_chapters(self.outline_panel.chapters)
        self._refresh_progress()
        self._schedule_autosave()

    def _generate_all(self) -> None:
        targets = [
            c for c in self.outline_panel.chapters
            if c.state in (ChapterState.PENDING, ChapterState.FAILED)
        ]
        if not targets:
            info(self, "无需生成", "没有处于“待生成/出错”状态的章节。")
            return
        if not confirm(
            self, "全量生成", f"将为 {len(targets)} 章排队生成（并发 {self.cfg.sampling.concurrency}）。是否继续？"
        ):
            return
        for chapter in targets:
            self.engine.submit_generate(chapter.chapter_id, "first")
        self._log("info", f"已提交 {len(targets)} 章的生成任务（状态均为“排队中”）。")
        self._after_batch_submit(f"✔ 已提交 {len(targets)} 章的生成任务", targets)

    def _generate_selected(self) -> None:
        selected = self.outline_panel.selected_chapters()
        if not selected:
            info(self, "未选择章节", "请先在左侧勾选要生成的章节。")
            return
        submitted: list[Chapter] = []
        for chapter in selected:
            if chapter.state in (ChapterState.PENDING, ChapterState.FAILED):
                self.engine.submit_generate(chapter.chapter_id, "first")
                submitted.append(chapter)
        if not submitted:
            info(self, "无需生成", "勾选的章节都已有正文或在排队/处理中。")
            return
        self._log("info", f"已提交 {len(submitted)} 章的生成任务（状态均为“排队中”）。")
        self._after_batch_submit(f"✔ 已提交 {len(submitted)} 章的生成任务", submitted)

    def _after_batch_submit(self, prefix: str, chapters: list[Chapter]) -> None:
        """批量提交后的界面刷新与提示（列表徽标 + 当前章视图 + 提示条）。"""
        self.outline_panel.refresh_all()
        current = self._current_chapter()
        if current is not None and any(c.chapter_id == current.chapter_id for c in chapters):
            self.chapter_view.set_state(current.state)
            self.chapter_view.show_toast(
                f"{prefix}：任务都排在队列末尾（状态：排队中），"
                "并发配额空出时按先后顺序自动开始。"
            )
        self._refresh_progress()

    def _generate_current(self) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            info(self, "未选择章节", "请先在左侧选择一个章节。")
            return
        if chapter.queued_or_busy:
            info(self, "请稍候", "该章任务已在队列中或正在执行，请等待完成或先停止。")
            return
        if not chapter.outline_for_generate().strip():
            warn(self, "大纲为空", "该章没有大纲内容，请先编辑大纲。")
            return
        self.engine.submit_generate(chapter.chapter_id, "first")
        self._notify_queued(chapter, "生成正文")

    def _notify_queued(self, chapter: Chapter, action: str) -> None:
        """统一的"已入队"提示：状态改为排队中 + 界面给出明确反馈。"""
        self.outline_panel.refresh_chapter(chapter)
        if self._current_id() == chapter.chapter_id:
            self.chapter_view.set_state(chapter.state)
            self.chapter_view.show_toast(
                f"✔ 已提交「{action}」：任务排在队列末尾，当前状态为“排队中”，"
                "轮到它时会自动开始（可在“运行日志”里看到详细信息）。"
            )
        self._log(
            "info",
            f"{chapter.title}：{action} 已加入队列末尾（状态：排队中，"
            f"当前队列 {self.engine.queue_length()} 个任务）。",
        )
        self._refresh_progress()
        self._schedule_autosave()

    def _check_current(self) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        if chapter.queued_or_busy:
            info(self, "请稍候", "该章任务已在队列中或正在执行，请等待完成或先停止。")
            return
        if not chapter.has_content:
            warn(self, "无法检查", "该章还没有正文，请先生成正文。")
            return
        self.engine.submit_check(chapter.chapter_id)
        # 手动触发检查后：进入检查视图，并展示完整对话
        self.chapter_view.show_check_conversation()
        self._notify_queued(chapter, "触发检查")

    def _regen_by_check(self) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        if chapter.queued_or_busy:
            info(self, "请稍候", "该章任务已在队列中或正在执行，请等待完成或先停止。")
            return
        if not chapter.has_content:
            warn(self, "无法重新生成", "该章还没有正文。")
            return
        check_text = self.chapter_view.check_edit.toPlainText().strip()
        if not check_text:
            warn(self, "检查结果为空", "请先触发检查，或在检查结果框中填写修改要求。")
            return
        # 把（可能被用户编辑过的）当前文本写回，模板将使用界面上的最新内容
        self.engine.set_check_result(chapter.chapter_id, check_text)
        chapter.check_result = check_text
        self.engine.submit_generate(chapter.chapter_id, "regen_by_check", check_text)
        # 按需求：点击后回到正文页，正文会边生成边流式显示
        self.chapter_view.show_view("正文")
        self._notify_queued(chapter, "按要求重新生成正文")

    def _regen_manual(self) -> None:
        """顶部条/检查视图的「重新生成」按钮。

        行为随章节状态自动选择，用户不需要理解两套模板：

        * **待生成 / 出错，或生成对话已被清空** → 当成一次全新生成：
          直接把**该章大纲**（独立保存的快照）重新发给 AI，不弹输入框。
        * **其它状态（已有正文/待决策/合格）** → 弹输入框收集额外指令，
          走 "手动重新生成" 模板（`{user_input}`）。
        """
        chapter = self._current_chapter()
        if chapter is None:
            return
        if chapter.queued_or_busy:
            info(self, "请稍候", "该章任务已在队列中或正在执行，请等待完成或先停止。")
            return
        if not chapter.outline_for_generate().strip():
            warn(self, "大纲为空", "该章没有大纲内容，无法重新生成。")
            return

        conv = chapter.generate_conv
        conv_empty = conv is None or not conv.messages
        if chapter.state in (ChapterState.PENDING, ChapterState.FAILED) or conv_empty:
            self.engine.submit_generate(chapter.chapter_id, "first")
            self.chapter_view.show_view("正文")
            self._notify_queued(chapter, "按本章大纲重新生成")
            return

        ok, text = ask_text(
            self,
            "重新生成",
            "可选的额外指令（可为空，将填入模板中的 {user_input}）：",
            placeholder="例如：节奏再快一些，对话更多，结尾留一个悬念。",
        )
        if not ok:
            return
        if not chapter.has_content:
            warn(self, "无法重新生成", "该章还没有正文，请先点击“生成正文”。")
            return
        # {user_input} 允许为空：直接把用户输入写进 job
        self.engine.submit_generate(chapter.chapter_id, "regen_manual", text)
        self.chapter_view.show_view("正文")
        self._notify_queued(chapter, "重新生成")

    def _accept_current(self) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        if chapter.queued_or_busy:
            info(self, "请稍候", "请等待当前生成/检查完成或先停止。")
            return
        self.engine.accept_chapter(chapter.chapter_id)

    def _postpone_current(self) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        if chapter.queued_or_busy:
            info(self, "请稍候", "请等待当前生成/检查完成或先停止。")
            return
        self.engine.postpone_chapter(chapter.chapter_id)

    def _stop_current(self) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        self.engine.stop_chapter(chapter.chapter_id)

    def _stop_selected(self) -> None:
        selected = self.outline_panel.selected_chapters()
        if not selected:
            info(self, "未选择章节", "请先勾选要停止的章节。")
            return
        for chapter in selected:
            self.engine.stop_chapter(chapter.chapter_id)

    def _on_content_edited(self, text: str) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        chapter.generated_content = text
        self.engine.set_chapter_content(chapter.chapter_id, text)
        self._schedule_autosave()

    def _on_check_edited(self, text: str) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        chapter.check_result = text
        self.engine.set_check_result(chapter.chapter_id, text)

    def _on_thinking_changed(self, conv_kind: str, thinking: ThinkingConfig) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        self.engine.set_thinking(chapter.chapter_id, conv_kind, thinking)
        self._log(
            "info",
            f"{chapter.title}：{conv_kind} 对话思考参数已更新为 "
            f"{'启用' if thinking.enabled else '禁用'} / {thinking.effort}。",
        )

    def _send_manual(self, conv_kind: str, text: str) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        if chapter.busy:
            info(self, "请稍候", "请等待当前生成/检查完成或先停止。")
            return
        self.engine.submit_manual_chat(chapter.chapter_id, conv_kind, text)
        self._log("info", f"{chapter.title}：已发送手动消息（不走模板）。")

    def _clear_conversation(self, conv_kind: str) -> None:
        chapter = self._current_chapter()
        if chapter is None:
            return
        self.engine.clear_conversation(chapter.chapter_id, conv_kind)
        # 界面立刻反馈：清空生成对话后本章回到"待生成"（与引擎侧一致，见 engine.clear_conversation）
        if conv_kind == "generate" and chapter.state != ChapterState.PASSED:
            chapter.state = ChapterState.PENDING
            self.outline_panel.refresh_chapter(chapter)
            self.chapter_view.set_state(chapter.state)
        # 对话视图立刻跟着清空（不等下一轮 chapter_updated 信号，避免"看起来没清掉"）
        view = self.chapter_view
        if conv_kind == "check":
            view.check_view.render()
        else:
            view.generate_view.render()
        view._refresh_buttons()
        # 对话变了就尽快落盘（清空也要存档，否则重新打开项目会看到旧对话）
        self._flush_conversations(force=True)
        if conv_kind == "generate":
            snapshot_ready = bool(chapter.outline_for_generate().strip())
            view.show_toast(
                "✔ 生成对话已清空，本章状态已改为“待生成”。"
                + (
                    "点“重新生成”会重新把本章大纲发给 AI，而不是按修改建议重写。"
                    if snapshot_ready
                    else "注意：该章大纲为空，请先补上大纲再生成。"
                )
            )
        else:
            view.show_toast("✔ 检查对话已清空。")

    def _on_concurrency_changed(self, value: int) -> None:
        self.cfg.sampling.concurrency = int(value)
        self.engine.apply_config(self.cfg)
        self._log("info", f"并发数已设置为 {value}。")
        try:
            config_mod.save_config(self.cfg)
        except OSError as exc:
            self._log("warn", f"配置保存失败：{exc}")

    # ================================================================== #
    # 设置 / 导出 / 帮助
    # ================================================================== #
    def open_settings(self) -> None:
        dialog = SettingsDialog(self.cfg, self)
        dialog.configSaved.connect(self._on_config_saved)
        dialog.exec()

    def _on_config_saved(self, cfg: AppConfig) -> None:
        self.cfg = cfg
        self.engine.apply_config(cfg)
        self.outline_panel.pattern_edit.setText(cfg.ui.chapter_pattern or "")
        self.chapter_view.apply_global_settings(
            hide_reasoning=cfg.ui.global_hide_reasoning,
            collapse_tokens=cfg.ui.reasoning_collapse_tokens,
        )
        self.concurrency_spin.setValue(cfg.sampling.concurrency)
        self._log(
            "info",
            f"设置已保存并生效。默认导出目录：{config_mod.resolve_export_dir(cfg)}；"
            f"项目根目录：{config_mod.resolve_project_dir(cfg)}",
        )

    def open_export_dialog(self) -> None:
        chapters = self.outline_panel.chapters
        if not chapters:
            warn(self, "无可导出内容", "请先切分章节并生成正文。")
            return
        dialog = ExportDialog(chapters, self.cfg, self)
        dialog.exported.connect(self._on_exported)
        # 默认文件名 = 当前小说项目名；目录用"设置 → 默认导出目录"
        if self.project is not None:
            dialog.set_project_name(self.project.name)
        dialog.exec()

    def _on_exported(self, path: str, options: object) -> None:
        if isinstance(options, ExportOptions):
            self.cfg.ui.export_mode = options.mode
            self.cfg.ui.export_mark_text = options.mark_text
            self.cfg.ui.export_mark_confirmed = options.mark_confirmed
            self.cfg.ui.export_append_check = options.append_check_result
            self.cfg.ui.export_separator = options.separator
            self.cfg.ui.export_encoding = options.encoding
            # 只有用户真的换了目录才更新"默认导出目录"，避免只是改了文件名就覆盖设置
            target_dir = str(Path(path).parent)
            if target_dir != config_mod.resolve_export_dir(self.cfg):
                self.cfg.ui.export_dir = target_dir
        try:
            config_mod.save_config(self.cfg)
        except OSError as exc:
            self._log("warn", f"配置保存失败：{exc}")
        self._log("info", f"已导出：{path}")

    def show_help_overview(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout

        dialog = QDialog(self)
        dialog.setWindowTitle("参数说明总览")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(dialog)
        rows: list[str] = ["<h3>参数说明总览</h3>"]
        for key in param_help.all_keys():
            item = param_help.get_help(key)
            rows.append(f"<div style='margin-bottom:10px'>{item.to_html()}</div>")
        browser.setHtml("".join(rows))
        layout.addWidget(browser)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def show_about(self) -> None:
        limits = model_limits(self.cfg.api.model)
        info(
            self,
            "关于",
            "小说生成器（PySide6 + DeepSeek）\n\n"
            "设计原则：\n"
            "1. 程序不解析 AI 回复的语义，只做模板字符串替换、状态流转与界面展示。\n"
            "2. 所有发给 AI 的消息都由“消息模板”生成，模板可在设置中编辑。\n"
            "3. 所有 assistant 输出都要求完整正文，不允许 diff / 局部修改。\n"
            "4. 每个对话（生成/检查）拥有独立的深度思考参数。\n\n"
            f"当前模型：{self.cfg.api.model}（{limits.display_name}）\n"
            f"上下文窗口 {limits.context_window:,} tokens ｜ 单次输出上限 {limits.max_output:,} tokens\n"
            f"账号并发上限 {limits.concurrency}\n\n"
            f"配置文件：{paths.config_path()}\n"
            f"章节数据：{self._chapters_path()}",
        )

    def _open_config_dir(self) -> None:
        from core.exporter import open_in_explorer

        open_in_explorer(paths.config_path())

    # ================================================================== #
    # 运行日志窗口
    # ================================================================== #
    def _ensure_log_window(self) -> LogWindow:
        if self.log_window is None:
            self.log_window = LogWindow(self)
        return self.log_window

    def show_log_window(self) -> None:
        """在新窗口里展示运行日志（默认不显示，只有点击时才打开）。"""
        window = self._ensure_log_window()
        window.show()
        window.raise_()
        window.activateWindow()

    # ================================================================== #
    # 任务队列面板
    # ================================================================== #
    def set_queue_visible(self, visible: bool) -> None:
        """显示/隐藏右侧任务队列面板，并同步工具栏按钮的选中态。"""
        self.queue_dock.setVisible(bool(visible))
        blocked = self.act_queue.blockSignals(True)
        self.act_queue.setChecked(bool(visible))
        self.act_queue.blockSignals(blocked)
        if visible:
            self.queue_panel.refresh()

    def toggle_queue_panel(self) -> None:
        self.set_queue_visible(not self.queue_dock.isVisible())

    def _on_queue_dock_visibility(self, visible: bool) -> None:
        """用户点停靠面板的 × 关闭时，同步取消工具栏按钮的选中态。"""
        if self.act_queue.isChecked() != bool(visible):
            blocked = self.act_queue.blockSignals(True)
            self.act_queue.setChecked(bool(visible))
            self.act_queue.blockSignals(blocked)

    # ================================================================== #
    # 小说项目（一部小说 = 一个以小说名命名的文件夹）
    # ================================================================== #
    def _resplit_message(self, chapter_count: int) -> str:
        """重新切分前给用户看的提示。

        已立项（当前小说的正文与对话都存档在项目文件夹里）时就不再吓唬人：
        重新切分只是让界面开始处理另一部小说，旧内容不会丢。
        """
        if self.project is not None:
            return (
                f"当前列表里有 {chapter_count} 章（属于已存档的小说《{self.project.name}》）。\n\n"
                "重新切分会把它替换成新大纲切出的章节，用来创建/打开另一部小说。\n"
                f"已有内容不会丢失：正文与全部对话都保存在项目文件夹里\n"
                f"{self.project.root}\n"
                "随时可用「项目 -> 打开项目」读回来继续写。\n\n是否继续？"
            )
        return (
            f"当前列表里有 {chapter_count} 章，但还没有立项（数据保存在程序数据目录，"
            "没有独立的小说文件夹）。\n\n"
            "重新切分会覆盖当前列表：这些章节的正文与对话将不再出现在界面上，"
            "存在丢失风险。\n\n"
            "建议先「一键导出 TXT」保存成品，或用「项目 -> 打开项目」先给这部小说立项。\n\n是否继续？"
        )

    def _on_outline_split(self, outline_text: str, parser_name: str) -> None:
        """切分大纲成功 -> 触发立项：请用户输入小说名，新建同名项目文件夹并落盘。

        这是唯一的立项入口。用户取消时，本次切分结果仍然可用，
        只是数据继续写在上次的位置（不强制立项）。

        **重要（曾经的数据覆盖事故）**
        ------------------------------
        切分信号是在"章节列表已经被替换成新大纲"之后才发出来的。如果此时还允许
        自动保存，程序会先把**新大纲的章节**写进**上一个项目**的 chapters.json，
        把原项目进度覆盖掉，然后才去建新项目。因此这里必须：

        1. 从切分进入本方法起就**挂起自动保存**（``_project_setup_in_progress``），
           并**取消已经排队的那一次保存**；
        2. 先把**原项目**的对话存档落盘（AI 回复不丢），但**不**再把内存里的新章节
           写回原项目（磁盘上的原项目保持原样）；
        3. 建好新项目后绑定它，再把新章节写进**新项目**；
        4. 用户取消立项时恢复自动保存，让新章节按原来的规则落盘。
        """
        self._project_setup_in_progress = True
        # 关键：把**已经排队**的自动保存也取消掉。切分前可能刚好有一次待写的自动保存，
        # 它在立项对话框关闭后就会立刻触发；那一次是带着"旧项目"的意图排进来的，必须掐掉。
        self._autosave_timer.stop()
        self._conv_timer.stop()
        try:
            base_dir = config_mod.resolve_project_dir(self.cfg)
            default_name = self.project.name if self.project is not None else parser_name
            ok, name = ask_text(
                self,
                "立项：为这部小说取个名字",
                f"切分成功，共 {len(self.outline_panel.chapters)} 章。\n\n"
                f"请输入小说名。程序会在下面的目录里新建一个以小说名命名的文件夹，"
                f"把这部小说的章节数据、大纲原文与项目信息都放在里面：\n{base_dir}",
                placeholder=default_name or "例如：星海归途",
            )
            if not ok:
                self._log(
                    "info",
                    "已取消立项：本次切分结果仍然可用，数据将保存在上次的位置。",
                )
                if self.project is not None:
                    self._log(
                        "info",
                        f"注意：界面上已经是新大纲的章节，稍后会覆盖写入原项目"
                        f"《{self.project.name}》；如需保留原进度，请重新打开该项目。",
                    )
                return

            novel_name = (name or "").strip() or default_name
            if not novel_name:
                warn(self, "名称为空", "小说名不能为空，已取消立项。")
                return

            payload = self._chapters_payload()
            try:
                project = project_mod.create_project(
                    base_dir, novel_name, outline_text=outline_text, chapters_payload=payload
                )
            except FileExistsError:
                error(
                    self,
                    "立项失败",
                    f"「{project_mod.sanitize_project_name(novel_name)}」项目文件夹已存在且非空：\n"
                    f"{project_mod.project_root_for(base_dir, novel_name)}\n\n"
                    "请换一个小说名，或用「项目 -> 打开项目」直接打开它。",
                )
                return
            except OSError as exc:
                error(self, "立项失败", f"无法创建项目文件夹：{exc}")
                return

            # 先把**原项目**的对话存档落盘（正文不动，原项目的 chapters.json 保持原样）
            if self.project is not None and self.project.root != project.root:
                self._flush_conversations(force=True)
            self._bind_project(project)
            # 现在自动保存指向的是新项目：把新章节写进去
            self._autosave_chapters()
            self._flush_conversations(force=True)
            info(
                self,
                "立项完成",
                f"小说项目「{project.name}」已建立。\n\n"
                f"项目文件夹：{project.root}\n"
                f"· chapters.json：章节、正文、检查结果\n"
                f"· conversations.json：全部生成/检查对话（含思维链，AI 每回复完一次就更新）\n"
                f"· outline.txt：大纲原文\n"
                f"· project.json：项目信息\n\n"
                "之后每次自动保存都会写进这个文件夹；下次启动会自动打开它。",
            )
        finally:
            self._project_setup_in_progress = False
        self._schedule_autosave()

    def open_project_dialog(self) -> None:
        base_dir = config_mod.resolve_project_dir(self.cfg)
        projects = project_mod.list_projects(base_dir)
        dialog = ProjectDialog(projects, base_dir, self)
        if not projects:
            info(
                self,
                "还没有项目",
                f"在 {base_dir} 下还没有识别到小说项目。\n\n"
                "项目会在“切分章节”时自动创建（届时会请你输入小说名）；"
                "也可以用对话框里的“从文件夹打开…”直接选择已有项目文件夹。",
            )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        project = dialog.chosen()
        if project is None:
            return
        self._open_project(project)

    def _open_project(self, project: project_mod.Project) -> bool:
        """读取项目数据并接管界面。返回是否成功。

        读取顺序：``chapters.json``（章节/正文/状态）→ ``conversations.json``（对话存档）。
        对话存档存在时会**覆盖** chapters.json 里内嵌的对话，保证"继续之前的对话"。
        """
        # 打开其它项目前，先把当前项目的对话存档落盘
        if self.project is not None and self.project.root != project.root:
            self._flush_conversations(force=True)

        try:
            items = project_mod.read_chapters(project)
        except Exception as exc:
            error(self, "打开项目失败", f"无法读取章节数据：{exc}")
            return False

        chapters = [self._chapter_from_dict(item) for item in items]
        outline_text = project_mod.read_outline(project)
        if not outline_text and not chapters:
            warn(self, "项目为空", f"「{project.name}」里还没有可读取的内容。")
            return False

        if outline_text:
            self.outline_panel.set_outline_text(outline_text)
            self.cfg.ui.last_outline = outline_text
        self.outline_panel.set_chapters(chapters)
        self.engine.set_chapters(chapters)

        # 对话存档：优先用它恢复全部对话历史
        restored = 0
        records = project_mod.read_conversations(project)
        if records:
            restored = self._restore_conversations(records, chapters)
        self.engine.mark_conversations_saved()

        self._bind_project(project)
        if chapters:
            self.outline_panel.select_chapter(chapters[0].chapter_id)
        else:
            self.chapter_view.set_chapter(None)
        self._refresh_progress()
        self._autosave_chapters()
        if records:
            self._log(
                "info",
                f"已打开小说项目「{project.name}」（{len(chapters)} 章，"
                f"恢复了 {restored} 个对话存档）：{project.chapters_path}",
            )
        else:
            self._log(
                "info",
                f"已打开小说项目「{project.name}」（{len(chapters)} 章）：{project.chapters_path}",
            )
        return True

    def _bind_project(self, project: project_mod.Project | None) -> None:
        self.project = project
        self.cfg.ui.last_project = str(project.root) if project is not None else ""
        self._refresh_project_ui()

    def _refresh_project_ui(self) -> None:
        project = self.project
        if project is None:
            self.setWindowTitle("小说生成器 · DeepSeek")
            self.status_project.setText("项目：未立项")
            self.status_project.setToolTip(
                "还没有立项。点“切分章节”会触发立项（输入小说名，"
                "程序在该目录下新建同名文件夹存放全部数据）。"
            )
            self.status_config.setText(f"配置：{paths.config_path()}")
            return
        self.setWindowTitle(f"小说生成器 · DeepSeek — 《{project.name}》")
        self.status_project.setText(f"项目：《{project.name}》")
        self.status_project.setToolTip(
            f"项目文件夹：{project.root}\n"
            f"章节数据：{project.chapters_path}\n"
            "「项目 -> 打开当前项目文件夹」可直接在资源管理器中打开。"
        )
        self.status_config.setText(f"配置：{paths.config_path()}")

    def _open_project_dir(self) -> None:
        from core.exporter import open_in_explorer

        base = config_mod.resolve_project_dir(self.cfg)
        target = Path(base)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            error(self, "无法打开目录", str(exc))
            return
        open_in_explorer(target)

    def _open_current_project_dir(self) -> None:
        from core.exporter import open_in_explorer

        if self.project is None:
            info(self, "还没有立项", "当前还没有小说项目；点“切分章节”即可立项。")
            return
        open_in_explorer(self.project.chapters_path)

    # ================================================================== #
    # 帮助
    # ================================================================== #
    # ================================================================== #
    # 配置文件导入 / 导出
    # ================================================================== #
    def _import_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "导入配置", "", "JSON 文件 (*.json)")
        if not path:
            return
        try:
            cfg, warnings = config_mod.import_profile(path)
        except Exception as exc:
            error(self, "导入失败", f"无法解析配置：{exc}")
            return
        self.cfg = cfg
        config_mod.save_config(cfg)
        self.engine.apply_config(cfg)
        self.chapter_view.apply_global_settings(
            hide_reasoning=cfg.ui.global_hide_reasoning,
            collapse_tokens=cfg.ui.reasoning_collapse_tokens,
        )
        self.concurrency_spin.setValue(cfg.sampling.concurrency)
        if warnings:
            warn(self, "导入完成（有提示）", "\n".join(warnings))
        else:
            info(self, "导入完成", "配置已生效。")

    def _export_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出配置", "novel_gen_config.json", "JSON 文件 (*.json)"
        )
        if not path:
            return
        try:
            config_mod.export_profile(self.cfg, path)
            info(self, "导出成功", f"已导出到：\n{path}")
        except OSError as exc:
            error(self, "导出失败", str(exc))

    # ================================================================== #
    # 日志与自检
    # ================================================================== #
    def _log(self, level: str, message: str) -> None:
        prefix = {"info": "·", "warn": "!", "error": "×", "debug": "…"}.get(level, "·")
        color = {"warn": "#9a6700", "error": "#cf222e", "debug": "#8a8f98"}.get(level, "#24292f")
        window = self._ensure_log_window()
        window.append_html(
            f"<span style='color:{color}'>{prefix} {_escape(message)}</span>"
        )
        # 日志默认不显示；只有警告/错误才自动弹出来，方便第一时间看到原因
        if level in ("warn", "error"):
            window.show()

    def _qa_check(self) -> None:
        """启动自检：模板占位符 + 参数说明覆盖率。"""
        for result in MT.validate_all(self.cfg.templates):
            if result.missing:
                self._log("warn", f"模板占位符提醒：{result.message()}")

        used_keys = [
            "api_key", "base_url", "model", "user_id", "timeout_seconds",
            "model_limits_view", "test_connection", "temperature", "top_p",
            "max_tokens", "context_window",
            "concurrency", "sampling_note", "effective_preview", "thinking_enabled",
            "reasoning_effort", "reasoning_effort_mapping", "show_reasoning",
            "global_hide_reasoning", "reasoning_collapse_tokens", "tpl_first_generate",
            "tpl_check", "tpl_regen_by_check", "tpl_regen_manual", "system_generate",
            "system_check", "auto_check_after_generate", "chapter_pattern",
            "outline_import", "outline_split", "act_generate", "act_stop_chapter",
            "act_stop_all", "act_trigger_check", "act_regen_by_check", "act_regen_manual",
            "act_accept", "act_postpone", "act_edit_content", "act_send_manual",
            "act_view_history", "act_clear_conversation", "act_switch_view",
            "exp_mode", "exp_mark_text", "exp_mark_confirmed", "exp_append_check", "exp_dir",
            "exp_separator", "exp_encoding", "exp_path", "exp_preview", "exp_open_after",
            "project_dir", "act_open_project", "act_show_log", "state_queued",
            "act_queue_panel",
            "status_queue", "status_token", "status_context_usage", "status_config_path",
            "app_about",
        ]
        missing = param_help.missing_help_keys(used_keys)
        if missing:
            self._log("warn", f"以下参数缺少悬浮说明，请在 core/param_help.py 中补充：{', '.join(missing)}")

    # ================================================================== #
    # 章节数据持久化（便于中断后继续）
    # ================================================================== #
    def _legacy_chapters_path(self) -> Path:
        """没有立项时的数据文件（沿用旧版行为，放在全局数据目录）。"""
        return paths.config_dir() / CHAPTERS_FILENAME

    def _chapters_path(self) -> Path:
        """当前章节数据的落盘位置：有项目就用项目文件夹，否则用全局数据目录。"""
        if self.project is not None:
            return self.project.chapters_path
        return self._legacy_chapters_path()

    def _schedule_autosave(self) -> None:
        """安排一次章节自动保存。

        "立项进行中"（正在等用户输入小说名 / 正在绑定新项目）时**不安排**：
        那一刻界面里已经是新大纲的章节，而 ``self.project`` 还指向旧项目，
        此时保存会把新大纲写进原项目、覆盖原进度（真实发生过的数据事故）。
        """
        if getattr(self, "_project_setup_in_progress", False):
            return
        self._autosave_timer.start()

    def _chapters_payload(self) -> dict:
        return {
            "version": 1,
            "chapters": [self._chapter_to_dict(c) for c in self.outline_panel.chapters],
        }

    def _autosave_chapters(self) -> None:
        """保存章节数据。立项后写进项目文件夹，同时写一份大纲原文。"""
        if getattr(self, "_project_setup_in_progress", False):
            # 定时器可能在"立项进行中"到期：跳过这次写入，避免污染原项目
            return
        payload = self._chapters_payload()
        try:
            if self.project is None:
                self._legacy_chapters_path().write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                return
            project_mod.write_chapters(self.project, payload)
            project_mod.write_outline(self.project, self.outline_panel.outline_text())
        except OSError as exc:
            self._log("warn", f"章节数据保存失败：{exc}")

    # ================================================================== #
    # 对话存档（conversations.json）
    # ================================================================== #
    @staticmethod
    def _conv_to_dict(conv: Conversation | None) -> dict | None:
        if conv is None:
            return None
        return {
            "conv_id": conv.conv_id,
            "kind": conv.kind,
            "thinking": conv.thinking.to_dict(),
            "status": conv.status,
            "messages": [m.to_dict() for m in conv.messages],
        }

    def _conversations_payload(self) -> list[dict]:
        """当前界面里所有章节的对话快照（生成对话 + 检查对话）。"""
        records: list[dict] = []
        for chapter in self.outline_panel.chapters:
            for conv in (chapter.generate_conv, chapter.check_conv):
                if conv is None:
                    continue
                records.append(
                    {
                        "chapter_id": chapter.chapter_id,
                        "index": chapter.index,
                        "title": chapter.title,
                        "conv": self._conv_to_dict(conv),
                    }
                )
        return records

    def _poll_conversations(self) -> None:
        """轮询引擎的脏标记：AI 回复完成 / 清空对话后安排一次对话存档。"""
        if getattr(self, "_project_setup_in_progress", False):
            return
        if self.engine.conversations_dirty():
            self._conv_timer.start()

    def _flush_conversations(self, *, force: bool = False) -> None:
        """把对话存档写入当前项目的 conversations.json。

        * 未立项时没有项目文件夹可写（章节数据落在全局目录），直接跳过；
        * ``force=True`` 用于关闭项目 / 关闭软件这类必须落盘的时机，
          此时即使引擎还没置脏标记也写一次，确保最后一次回复不丢。
        """
        self._conv_timer.stop()
        if self.project is None:
            self.engine.mark_conversations_saved()
            return
        if not force and not self.engine.conversations_dirty():
            return
        try:
            project_mod.write_conversations(self.project, self._conversations_payload())
        except OSError as exc:
            self._log("warn", f"对话存档保存失败：{exc}")
            return
        self.engine.mark_conversations_saved()

    def _restore_conversations(self, records: list[dict], chapters: list[Chapter]) -> int:
        """把对话存档回填到章节对象上。返回成功回填的对话数。"""
        by_id = {c.chapter_id: c for c in chapters}
        restored = 0
        for record in records:
            if not isinstance(record, dict):
                continue
            chapter = by_id.get(record.get("chapter_id") or "")
            raw = record.get("conv")
            if chapter is None or not isinstance(raw, dict):
                continue
            conv = self._conversation_from_dict(raw, chapter.chapter_id)
            if conv is None:
                continue
            if conv.kind == "check":
                chapter.check_conv = conv
            else:
                chapter.generate_conv = conv
            restored += 1
        return restored

    def _restore_chapter_state(self, data: dict) -> ChapterState:
        """从存档里还原章节状态。

        * 旧存档没有 ``state`` 字段时，用"有没有正文/检查结果"推断，
          而不是一律当成"待生成"；
        * "生成中/检查中/排队中"这类运行期状态不会自动恢复，按内容回落到可继续操作的状态。
        """
        raw = data.get("state")
        has_content = bool((data.get("generated_content") or "").strip())
        has_check = bool((data.get("check_result") or "").strip())
        if raw in (None, ""):
            if has_check:
                return ChapterState.AWAITING_DECISION
            if has_content:
                return ChapterState.TO_CHECK
            return ChapterState.PENDING
        try:
            state = ChapterState(str(raw))
        except ValueError:
            state = ChapterState.PENDING
        if state.busy:
            return ChapterState.TO_CHECK if has_content else ChapterState.PENDING
        if state == ChapterState.QUEUED:
            return ChapterState.TO_CHECK if has_content else ChapterState.PENDING
        return state

    def _chapter_to_dict(self, chapter: Chapter) -> dict:
        return {
            "chapter_id": chapter.chapter_id,
            "index": chapter.index,
            "title": chapter.title,
            "outline": chapter.outline,
            # 独立保存的大纲快照：清空生成对话后仍能"重新生成"
            "outline_snapshot": chapter.outline_snapshot,
            "state": str(chapter.state),
            "generated_content": chapter.generated_content,
            "check_result": chapter.check_result,
            "check_history": chapter.check_history,
            "regen_count": chapter.regen_count,
            "last_error": chapter.last_error,
            "generate_conv": self._conv_to_dict(chapter.generate_conv),
            "check_conv": self._conv_to_dict(chapter.check_conv),
        }

    def _load_state(self) -> None:
        """恢复窗口几何、大纲文本与上次的章节数据（优先自动打开上次的小说项目）。"""
        try:
            from PySide6.QtCore import QByteArray

            if self.cfg.ui.window_geometry:
                self.restoreGeometry(QByteArray.fromBase64(self.cfg.ui.window_geometry.encode()))
        except Exception:
            pass

        if self.cfg.ui.last_outline:
            self.outline_panel.set_outline_text(self.cfg.ui.last_outline)

        # ① 上次打开的小说项目
        last_project = (self.cfg.ui.last_project or "").strip()
        if last_project and project_mod.is_project_dir(last_project):
            try:
                project = project_mod.load_project(last_project)
            except OSError as exc:
                self._log("warn", f"打开上次的项目失败：{exc}")
            else:
                if self._open_project(project):
                    return
                self._bind_project(None)

        # ② 回退：全局数据目录里的 chapters.json（未立项时的数据）
        self._load_legacy_chapters()

    def _load_legacy_chapters(self) -> None:
        path = self._legacy_chapters_path()
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            chapters = [self._chapter_from_dict(item) for item in data.get("chapters", [])]
        except Exception as exc:
            self._log("warn", f"恢复上次章节数据失败：{exc}")
            return
        if not chapters:
            return
        self.outline_panel.set_chapters(chapters)
        self.engine.set_chapters(chapters)
        self.outline_panel.select_chapter(chapters[0].chapter_id)
        self._refresh_progress()
        self._log("info", f"已恢复上次的 {len(chapters)} 章数据（{path}）。")

    def _chapter_from_dict(self, data: dict) -> Chapter:
        state = self._restore_chapter_state(data)
        outline = data.get("outline", "")
        chapter_id = data.get("chapter_id") or Chapter.create(1, "第一章", "").chapter_id
        chapter = Chapter(
            chapter_id=chapter_id,
            index=int(data.get("index", 1)),
            title=data.get("title", ""),
            outline=outline,
            state=state,
            generated_content=data.get("generated_content", ""),
            check_result=data.get("check_result", ""),
            check_history=list(data.get("check_history", [])),
            regen_count=int(data.get("regen_count", 0)),
            last_error=data.get("last_error", ""),
            outline_snapshot=data.get("outline_snapshot", "") or outline,
        )
        # 兼容：旧项目把对话内嵌在 chapters.json 里（新的 conversations.json 会覆盖它）
        chapter.generate_conv = self._conversation_from_dict(
            data.get("generate_conv"), chapter_id
        )
        chapter.check_conv = self._conversation_from_dict(data.get("check_conv"), chapter_id)
        return chapter

    def _conversation_from_dict(
        self, raw: dict | None, chapter_id: str
    ) -> Conversation | None:
        """从存档还原一个对话（含每条消息的来源、用量与思维链）。"""
        from core.models import Message, Usage as UsageModel

        if not raw:
            return None
        conv = Conversation(
            conv_id=raw.get("conv_id") or "",
            chapter_id=chapter_id,
            kind=raw.get("kind") or "generate",
            thinking=ThinkingConfig.from_dict(raw.get("thinking")),
            status="idle",
        )
        for item in raw.get("messages", []):
            usage_raw = item.get("usage") or {}
            conv.messages.append(
                Message(
                    role=item.get("role", "user"),
                    content=item.get("content", ""),
                    reasoning=item.get("reasoning", ""),
                    source=item.get("source", "user"),
                    created_at=item.get("created_at", 0.0) or 0.0,
                    usage=UsageModel(**usage_raw) if usage_raw else None,
                    interrupted=bool(item.get("interrupted", False)),
                )
            )
        return conv

    # ================================================================== #
    def closeEvent(self, event) -> None:  # noqa: N802
        running = [c.title for c in self.outline_panel.chapters if c.busy]
        if running:
            if not confirm(
                self,
                "仍有任务在运行",
                "以下章节仍在生成/检查中：\n" + "、".join(running[:8]) + "\n\n确定退出吗？",
            ):
                event.ignore()
                return

        self.engine.stop_all()
        # 关闭软件时把对话存档落盘（放在 stop_all 之后：停止可能又追加了"已中断"消息）
        self._flush_conversations(force=True)
        try:
            from PySide6.QtCore import QByteArray

            self.cfg.ui.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        except Exception:
            pass
        self.cfg.ui.last_outline = self.outline_panel.outline_text()
        if self.project is not None:
            self.cfg.ui.last_project = str(self.project.root)
        try:
            config_mod.save_config(self.cfg)
        except OSError:
            pass
        self._autosave_chapters()
        self.chapter_view.close_conversation_windows()
        if self.log_window is not None:
            self.log_window.close()
        self._conv_poll_timer.stop()
        self.engine.shutdown()
        super().closeEvent(event)


def _escape(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
