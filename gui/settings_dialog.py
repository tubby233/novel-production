"""设置对话框：API / 采样 / 深度思考 / 提示词与模板 / 界面与流程 / 大纲切分。

要点
----
* 每个控件旁都有 ⓘ（说明来自 core.param_help）。
* 消息模板编辑器实时做占位符校验：缺必需占位符给出黄色警告，但**允许保存**。
* 思考模式启用时，temperature 控件置灰并标注"当前不生效"；
  top_p 旁标注下限 0.95 的约束。
* frequency_penalty / presence_penalty 已被 DeepSeek 官方弃用（传入无效果），
  因此设置界面不再提供这两项，仅在采样页给出一行说明。
* base_url 未填版本段时自动补 /v1。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from core import config as config_mod
from core import message_templates as MT
from core import outline_parser
from core import paths as core_paths
from core.config import AppConfig, MODEL_LIMITS, model_limits
from core.models import ThinkingConfig

from .help_icon import HelpIcon, attach_help
from .widgets import (
    NumericField,
    PromptEditor,
    TemplateEditor,
    confirm,
    error,
    hline,
    info,
    make_checkbox,
    make_combo,
    section_title,
    warn,
)

PAGES = (
    "API 与模型",
    "采样参数",
    "深度思考",
    "提示词",
    "消息模板",
    "界面与流程",
    "大纲切分",
)


class SettingsDialog(QDialog):
    """设置对话框。保存后通过 :attr:`configSaved` 把新配置交给主窗口。"""

    configSaved = Signal(object)

    def __init__(self, cfg: AppConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.cfg = cfg
        self._loading = True
        self._thinking_edited = False
        self._test_thread = None
        self._test_worker = None

        self.setWindowTitle("设置")
        self.resize(920, 680)
        self._build_ui()
        self._load_from_config()
        self._loading = False
        self._refresh_sampling_state()

    # ================================================================== #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)

        body = QHBoxLayout()
        body.setSpacing(10)

        self.nav = QListWidget(self)
        self.nav.setFixedWidth(150)
        for name in PAGES:
            self.nav.addItem(QListWidgetItem(name))
        self.nav.currentRowChanged.connect(self._on_nav)
        body.addWidget(self.nav)

        self.stack = QStackedWidget(self)
        self.stack.addWidget(self._build_api_page())
        self.stack.addWidget(self._build_sampling_page())
        self.stack.addWidget(self._build_thinking_page())
        self.stack.addWidget(self._build_prompts_page())
        self.stack.addWidget(self._build_templates_page())
        self.stack.addWidget(self._build_ui_page())
        self.stack.addWidget(self._build_outline_page())
        body.addWidget(self.stack, 1)
        root.addLayout(body, 1)

        root.addWidget(hline(self))

        self.path_label = QLabel(self)
        self.path_label.setStyleSheet("color:#57606a; font-size:11px;")
        self.path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.path_label)

        buttons = QHBoxLayout()
        self.defaults_button = QPushButton("恢复本页默认", self)
        self.defaults_button.clicked.connect(self._restore_page_defaults)
        buttons.addWidget(self.defaults_button)

        self.import_button = QPushButton("导入配置", self)
        self.import_button.setToolTip("从 JSON 文件导入全部设置（含提示词与消息模板）。")
        self.import_button.clicked.connect(self._import_profile)
        buttons.addWidget(self.import_button)

        self.export_button = QPushButton("导出配置", self)
        self.export_button.setToolTip("把当前全部设置导出为 JSON 文件，便于备份与迁移。")
        self.export_button.clicked.connect(self._export_profile)
        buttons.addWidget(self.export_button)

        buttons.addStretch(1)
        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel, self
        )
        box.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        box.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        box.accepted.connect(self._on_save)
        box.rejected.connect(self.reject)
        buttons.addWidget(box)
        root.addLayout(buttons)

        self.nav.setCurrentRow(0)

    # ------------------------------------------------------------------ #
    # 页面 1：API 与模型
    # ------------------------------------------------------------------ #
    def _build_api_page(self) -> QWidget:
        page, layout = self._scroll_page()

        layout.addWidget(section_title("接口与凭据", "api_key", page))

        self.api_key_edit = QLineEdit(page)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-…")
        attach_help(self.api_key_edit, "api_key")
        row = QHBoxLayout()
        row.addWidget(self.api_key_edit, 1)
        self.show_key_box = QCheckBox("显示", page)
        self.show_key_box.setToolTip("临时显示密钥明文，便于核对。")
        self.show_key_box.toggled.connect(
            lambda checked: self.api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )
        row.addWidget(self.show_key_box)
        row.addWidget(HelpIcon("api_key", page))
        layout.addLayout(row)

        self.base_url_edit = QLineEdit(page)
        attach_help(self.base_url_edit, "base_url")
        layout.addWidget(self._labeled("接口地址 base_url", self.base_url_edit, "base_url", page))

        self.model_combo = make_combo(
            list(MODEL_LIMITS.keys()), config_mod.DEFAULT_MODEL, "model", editable=True, parent=page
        )
        self.model_combo.setToolTip(
            "官方可用 id：deepseek-flash（DeepSeek-V4.1-Flash）、deepseek-v4-pro。\n"
            "如需变更，可直接手动输入其它模型 id。"
        )
        self.model_combo.currentTextChanged.connect(self._refresh_model_limits)
        layout.addWidget(self._labeled("模型名称 model（可手动输入）", self.model_combo, "model", page))

        self.user_id_edit = QLineEdit(page)
        attach_help(self.user_id_edit, "user_id")
        layout.addWidget(self._labeled("用户标识 user_id", self.user_id_edit, "user_id", page))

        self.timeout_spin = QSpinBox(page)
        self.timeout_spin.setRange(30, 7200)
        self.timeout_spin.setSingleStep(30)
        self.timeout_spin.setSuffix(" 秒")
        attach_help(self.timeout_spin, "timeout_seconds")
        layout.addWidget(
            self._labeled("请求超时", self.timeout_spin, "timeout_seconds", page)
        )

        layout.addWidget(section_title("模型能力（只读，依据 DeepSeek 官方文档）", "model_limits_view", page))
        self.limits_label = QLabel(page)
        self.limits_label.setWordWrap(True)
        self.limits_label.setStyleSheet(
            "color:#24292f; background:#f6f8fa; border:1px solid #d8dee4;"
            " border-radius:4px; padding:6px;"
        )
        layout.addWidget(self.limits_label)
        layout.addWidget(HelpIcon("model_limits_view", page))

        test_row = QHBoxLayout()
        self.test_button = QPushButton("测试连接", page)
        attach_help(self.test_button, "test_connection")
        self.test_button.clicked.connect(self._test_connection)
        test_row.addWidget(self.test_button)
        self.test_result = QLabel("", page)
        self.test_result.setWordWrap(True)
        self.test_result.setStyleSheet("font-size:11px;")
        test_row.addWidget(self.test_result, 1)
        layout.addLayout(test_row)

        return page

    # ------------------------------------------------------------------ #
    # 页面 2：采样参数
    # ------------------------------------------------------------------ #
    def _build_sampling_page(self) -> QWidget:
        page, layout = self._scroll_page()

        layout.addWidget(section_title("采样参数", "sampling_note", page))

        self.temperature_field = NumericField(
            "temperature", value=1.0, minimum=0.0, maximum=2.0, decimals=2, step=0.05, parent=page
        )
        layout.addWidget(self._labeled("温度 temperature", self.temperature_field, "temperature", page))

        self.top_p_field = NumericField(
            "top_p", value=1.0, minimum=0.0, maximum=1.0, decimals=2, step=0.01, parent=page
        )
        layout.addWidget(self._labeled("核采样 top_p", self.top_p_field, "top_p", page))

        self.max_tokens_spin = QSpinBox(page)
        self.max_tokens_spin.setRange(1, 393_216)
        self.max_tokens_spin.setSingleStep(1024)
        self.max_tokens_spin.setKeyboardTracking(False)
        self.max_tokens_spin.setGroupSeparatorShown(True)
        attach_help(self.max_tokens_spin, "max_tokens")
        max_row = QHBoxLayout()
        max_row.addWidget(QLabel("单次最大输出 max_tokens", page))
        max_row.addWidget(self.max_tokens_spin, 1)
        for label, value in (("8K", 8192), ("64K", 65_536), ("128K", 131_072), ("384K 上限", 393_216)):
            button = QPushButton(label, page)
            button.setToolTip(f"把 max_tokens 设为 {value:,}。")
            button.clicked.connect(lambda _=False, v=value: self.max_tokens_spin.setValue(v))
            max_row.addWidget(button)
        max_row.addWidget(HelpIcon("max_tokens", page))
        layout.addLayout(max_row)

        note = QLabel(
            "提示：max_tokens 是上限而非目标，模型自然写完就会停止；"
            "开启思考时思维链 token 也计入该上限。",
            page,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(note)

        layout.addWidget(hline(page))
        self.concurrency_spin = QSpinBox(page)
        self.concurrency_spin.setRange(1, 2500)
        self.concurrency_spin.setValue(8)
        attach_help(self.concurrency_spin, "concurrency")
        layout.addWidget(self._labeled("并发数", self.concurrency_spin, "concurrency", page))

        self.effective_label = QLabel(page)
        self.effective_label.setWordWrap(True)
        self.effective_label.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(section_title("本次生效参数预览", "effective_preview", page))
        layout.addWidget(self.effective_label)
        layout.addWidget(HelpIcon("effective_preview", page))

        for signal_source in (
            self.temperature_field.valueChanged,
            self.top_p_field.valueChanged,
        ):
            signal_source.connect(lambda *_: self._refresh_sampling_state())
        self.max_tokens_spin.valueChanged.connect(lambda *_: self._refresh_sampling_state())

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    # 页面 3：深度思考默认值
    # ------------------------------------------------------------------ #
    def _build_thinking_page(self) -> QWidget:
        from .thinking_panel import ThinkingPanel

        page, layout = self._scroll_page()
        layout.addWidget(section_title("各对话的默认思考参数", "thinking_enabled", page))

        self.generate_thinking = ThinkingPanel(
            "生成对话默认值", ThinkingConfig(enabled=True, effort="high", show_reasoning=True), parent=page
        )
        self.check_thinking = ThinkingPanel(
            "检查对话默认值", ThinkingConfig(enabled=True, effort="max", show_reasoning=True), parent=page
        )
        for panel in (self.generate_thinking, self.check_thinking):
            panel.thinkingChanged.connect(lambda *_: self._mark_thinking_edited())
        layout.addWidget(self.generate_thinking)
        layout.addWidget(self.check_thinking)

        layout.addWidget(
            section_title("思考强度映射（只读）", "reasoning_effort_mapping", page)
        )
        mapping = QLabel(
            "minimal → low；low → low；medium → high；high → high；xhigh → high；"
            "max → max；ultra → max。<br>"
            "若想只用一个参数控制开关，可传 reasoning_effort=\"none\" 表示关闭思考。",
            page,
        )
        mapping.setWordWrap(True)
        mapping.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(mapping)
        layout.addWidget(HelpIcon("reasoning_effort_mapping", page))

        layout.addWidget(hline(page))
        self.hide_reasoning_box = make_checkbox(
            "全局隐藏思维链（后台仍记录）", False, "global_hide_reasoning", parent=page
        )
        layout.addWidget(self.hide_reasoning_box)

        self.collapse_spin = QSpinBox(page)
        self.collapse_spin.setRange(200, 60_000)
        self.collapse_spin.setSingleStep(100)
        self.collapse_spin.setSuffix(" tokens")
        attach_help(self.collapse_spin, "reasoning_collapse_tokens")
        layout.addWidget(
            self._labeled(
                "思维链自动收起阈值", self.collapse_spin, "reasoning_collapse_tokens", page
            )
        )

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    # 页面 4：提示词
    # ------------------------------------------------------------------ #
    def _build_prompts_page(self) -> QWidget:
        page, layout = self._scroll_page()
        layout.addWidget(section_title("提示词（system message）", "system_generate", page))

        self.generate_prompt = PromptEditor(
            "system_generate",
            "生成提示词",
            text=MT.DEFAULT_GENERATE_SYSTEM,
            parent=page,
        )
        layout.addWidget(self.generate_prompt)

        self.check_prompt = PromptEditor(
            "system_check",
            "检查提示词",
            text="",
            placeholder="程序不预设内容，请在此填写你的检查要求（可留空）。",
            parent=page,
        )
        layout.addWidget(self.check_prompt)

        note = QLabel(
            "说明：提示词在创建对话时注入为该对话的 system 消息，"
            "修改后只对新建的对话生效；已存在的对话保留原提示词。",
            page,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(note)
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    # 页面 5：消息模板
    # ------------------------------------------------------------------ #
    def _build_templates_page(self) -> QWidget:
        page, layout = self._scroll_page()
        layout.addWidget(section_title("消息模板（user message）", "tpl_first_generate", page))
        note = QLabel(
            "程序只做字符串替换：白名单内的占位符会被替换，其它 {xxx} 会原样保留并发送。"
            "缺少必需占位符时会给出黄色警告，但仍可保存。",
            page,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(note)

        self.template_editors: dict[str, TemplateEditor] = {}
        for kind in MT.TemplateKind:
            editor = TemplateEditor(kind, text=MT.default_template(kind), parent=page)
            layout.addWidget(editor)
            layout.addWidget(hline(page))
            self.template_editors[str(kind)] = editor
        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    # 页面 6：界面与流程
    # ------------------------------------------------------------------ #
    def _build_ui_page(self) -> QWidget:
        page, layout = self._scroll_page()
        layout.addWidget(section_title("流程", "auto_check_after_generate", page))

        self.auto_check_box = make_checkbox(
            "正文生成完成后自动进入检查环节", True, "auto_check_after_generate", parent=page
        )
        layout.addWidget(self.auto_check_box)

        layout.addWidget(section_title("导出默认值", "exp_mode", page))
        self.export_mode_combo = make_combo(
            [config_mod.AppConfig().ui.export_mode], "all_marked", "exp_mode", parent=page
        )
        self.export_mode_combo.clear()
        self.export_mode_combo.addItem("导出全部（未确认加标注）", "all_marked")
        self.export_mode_combo.addItem("仅导出已合格章节", "passed_only")
        layout.addWidget(self._labeled("导出范围", self.export_mode_combo, "exp_mode", page))

        self.export_mark_edit = QLineEdit(page)
        attach_help(self.export_mark_edit, "exp_mark_text")
        layout.addWidget(self._labeled("未确认标记文本", self.export_mark_edit, "exp_mark_text", page))

        self.export_append_box = make_checkbox(
            "未确认章节文末附最后一次检查结果", True, "exp_append_check", parent=page
        )
        layout.addWidget(self.export_append_box)

        self.export_separator_box = make_checkbox(
            "章节之间插入空行", True, "exp_separator", parent=page
        )
        layout.addWidget(self.export_separator_box)

        self.export_encoding_combo = make_combo(
            ["utf-8-sig", "utf-8", "gbk"], "utf-8-sig", "exp_encoding", parent=page
        )
        layout.addWidget(self._labeled("文件编码", self.export_encoding_combo, "exp_encoding", page))

        # 默认导出目录：可随时修改（导出对话框里也能另选路径）
        self.export_dir_edit = QLineEdit(page)
        attach_help(self.export_dir_edit, "exp_dir")
        self.export_dir_edit.setPlaceholderText("留空 = 我的文档")
        export_dir_row = QWidget(page)
        export_dir_layout = QHBoxLayout(export_dir_row)
        export_dir_layout.setContentsMargins(0, 0, 0, 0)
        export_dir_layout.setSpacing(6)
        export_dir_layout.addWidget(QLabel("默认导出目录", export_dir_row))
        export_dir_layout.addWidget(self.export_dir_edit, 1)
        export_dir_button = QPushButton("浏览…", export_dir_row)
        export_dir_button.setToolTip("选择导出文件的默认保存目录。")
        export_dir_button.clicked.connect(self._browse_export_dir)
        export_dir_layout.addWidget(export_dir_button)
        export_dir_layout.addWidget(HelpIcon("exp_dir", export_dir_row))
        layout.addWidget(export_dir_row)

        self.export_open_box = make_checkbox(
            "导出完成后打开所在目录", True, "exp_open_after", parent=page
        )
        layout.addWidget(self.export_open_box)

        # ---------------- 小说项目目录 ---------------- #
        layout.addWidget(section_title("小说项目（一部小说一个文件夹）", "project_dir", page))
        project_note = QLabel(
            "每次“切分章节”会触发立项：请你输入小说名，程序在下面的目录里新建一个"
            "以小说名命名的文件夹，把章节数据、大纲原文与项目信息都放在里面，"
            "之后可在“项目 -> 打开项目”里随时读取。",
            page,
        )
        project_note.setWordWrap(True)
        project_note.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(project_note)

        self.project_dir_edit = QLineEdit(page)
        attach_help(self.project_dir_edit, "project_dir")
        self.project_dir_edit.setPlaceholderText("留空 = 我的文档\\NovelGenProjects")
        project_dir_row = QWidget(page)
        project_dir_layout = QHBoxLayout(project_dir_row)
        project_dir_layout.setContentsMargins(0, 0, 0, 0)
        project_dir_layout.setSpacing(6)
        project_dir_layout.addWidget(QLabel("项目根目录", project_dir_row))
        project_dir_layout.addWidget(self.project_dir_edit, 1)
        project_dir_button = QPushButton("浏览…", project_dir_row)
        project_dir_button.setToolTip("选择存放所有小说项目的文件夹。")
        project_dir_button.clicked.connect(self._browse_project_dir)
        project_dir_layout.addWidget(project_dir_button)
        project_dir_layout.addWidget(HelpIcon("project_dir", project_dir_row))
        layout.addWidget(project_dir_row)

        layout.addWidget(section_title("配置文件", "status_config_path", page))
        self.config_path_label = QLabel(page)
        self.config_path_label.setWordWrap(True)
        self.config_path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.config_path_label.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(self.config_path_label)
        layout.addWidget(HelpIcon("status_config_path", page))

        layout.addStretch(1)
        return page

    # ------------------------------------------------------------------ #
    # 页面 7：大纲切分
    # ------------------------------------------------------------------ #
    def _build_outline_page(self) -> QWidget:
        page, layout = self._scroll_page()
        layout.addWidget(section_title("章节标题正则", "chapter_pattern", page))

        self.pattern_edit = QLineEdit(page)
        self.pattern_edit.setPlaceholderText(outline_parser.DEFAULT_CHAPTER_PATTERN)
        attach_help(self.pattern_edit, "chapter_pattern")
        layout.addWidget(self._labeled("切分正则", self.pattern_edit, "chapter_pattern", page))

        row = QHBoxLayout()
        self.test_pattern_button = QPushButton("用示例文本测试", page)
        self.test_pattern_button.setToolTip("用内置示例大纲测试正则的匹配效果。")
        self.test_pattern_button.clicked.connect(self._test_pattern)
        row.addWidget(self.test_pattern_button)
        row.addStretch(1)
        layout.addLayout(row)

        self.pattern_result = QLabel(page)
        self.pattern_result.setWordWrap(True)
        self.pattern_result.setStyleSheet("color:#57606a; font-size:11px;")
        layout.addWidget(self.pattern_result)

        sample = QPlainTextEdit(page)
        sample.setReadOnly(True)
        sample.setMaximumHeight(160)
        sample.setPlainText(
            "第一章 觉醒\n本章内容：主角苏醒……\n出场角色：主角、长老\n"
            "前情提要：……\n后续剧情：……\n\n"
            "第 2 章 试炼\n本章内容：……\n\n【第三章】归途\n本章内容：……"
        )
        layout.addWidget(QLabel("内置示例（用于验证正则）", page))
        layout.addWidget(sample)
        layout.addStretch(1)
        return page

    # ================================================================== #
    # 辅助
    # ================================================================== #
    def _scroll_page(self) -> tuple[QWidget, QVBoxLayout]:
        container = QWidget(self)
        outer = QVBoxLayout(container)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        inner = QWidget(scroll)
        layout = QVBoxLayout(inner)
        layout.setSpacing(8)
        layout.setContentsMargins(4, 4, 8, 4)
        scroll.setWidget(inner)
        outer.addWidget(scroll)
        return container, layout

    def _labeled(
        self, text: str, field: QWidget, param_key: str, parent: QWidget, note: str = ""
    ) -> QWidget:
        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(QLabel(f"{text}　{note}" if note else text, row))
        if isinstance(field, NumericField):
            layout.addWidget(field, 1)
        else:
            layout.addWidget(field, 1)
        layout.addWidget(HelpIcon(param_key, row))
        return row

    def _on_nav(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)

    def _mark_thinking_edited(self) -> None:
        if not self._loading:
            self._thinking_edited = True

    # ================================================================== #
    # 装载 / 保存
    # ================================================================== #
    def _load_from_config(self) -> None:
        cfg = self.cfg
        self.api_key_edit.setText(cfg.api.api_key)
        self.base_url_edit.setText(cfg.api.base_url)
        self.model_combo.setEditText(cfg.api.model)
        self.user_id_edit.setText(cfg.api.user_id)
        self.timeout_spin.setValue(cfg.api.timeout_seconds)

        self.temperature_field.setValue(cfg.sampling.temperature)
        self.top_p_field.setValue(cfg.sampling.top_p)
        self.max_tokens_spin.setValue(cfg.sampling.max_tokens)
        self.concurrency_spin.setValue(cfg.sampling.concurrency)

        self.generate_thinking.set_thinking(cfg.thinking.generate)
        self.check_thinking.set_thinking(cfg.thinking.check)
        self.hide_reasoning_box.setChecked(cfg.ui.global_hide_reasoning)
        self.collapse_spin.setValue(cfg.ui.reasoning_collapse_tokens)

        self.generate_prompt.set_text(cfg.prompts.system_generate)
        self.check_prompt.set_text(cfg.prompts.system_check)

        for kind_key, editor in self.template_editors.items():
            editor.set_text(cfg.template(kind_key))

        self.auto_check_box.setChecked(cfg.ui.auto_check_after_generate)
        index = self.export_mode_combo.findData(cfg.ui.export_mode)
        self.export_mode_combo.setCurrentIndex(index if index >= 0 else 0)
        self.export_mark_edit.setText(cfg.ui.export_mark_text)
        self.export_append_box.setChecked(cfg.ui.export_append_check)
        self.export_separator_box.setChecked(cfg.ui.export_separator)
        self.export_encoding_combo.setCurrentText(cfg.ui.export_encoding)
        self.export_open_box.setChecked(cfg.ui.export_open_after)
        self.export_dir_edit.setText(cfg.ui.export_dir or "")
        self.project_dir_edit.setText(cfg.ui.project_dir or "")
        self.pattern_edit.setText(cfg.ui.chapter_pattern or "")
        self.config_path_label.setText(f"配置文件：{core_paths.config_path()}")
        self.path_label.setText(
            "配置以明文 JSON 保存（包含 api_key），请妥善保管；"
            f"当前路径：{core_paths.config_path()}"
        )
        self._refresh_model_limits()
        self._refresh_sampling_state(force=True)

    def _refresh_model_limits(self) -> None:
        model_id = self.model_combo.currentText().strip() or config_mod.DEFAULT_MODEL
        limits = model_limits(model_id)
        known = model_id in MODEL_LIMITS
        self.limits_label.setText(
            f"模型：{model_id}（{limits.display_name}）"
            f"{'' if known else '　⚠ 不在内置列表中，以下数值以默认模型为准，实际以服务端为准'}<br>"
            f"上下文窗口：{limits.context_window:,} tokens（1M）　|　"
            f"单次输出上限：{limits.max_output:,} tokens（384K）　|　"
            f"账号并发上限：{limits.concurrency}<br>{limits.notes}"
        )
        if hasattr(self, "max_tokens_spin"):
            self.max_tokens_spin.setMaximum(limits.max_output)
            if self.max_tokens_spin.value() > limits.max_output:
                self.max_tokens_spin.setValue(limits.max_output)
        if hasattr(self, "concurrency_spin"):
            self.concurrency_spin.setMaximum(limits.concurrency)
            if self.concurrency_spin.value() > limits.concurrency:
                self.concurrency_spin.setValue(limits.concurrency)
        self._refresh_sampling_state()

    def _refresh_sampling_state(self, *, force: bool = False) -> None:
        if self._loading and not force:
            return
        thinking_on = self._thinking_enabled_in_dialog()
        # temperature：思考模式下不生效（置灰但保留数值；具体原因见控件旁的 ⓘ）
        self.temperature_field.set_enabled_state(not thinking_on)

        top_p = self.top_p_field.value()
        if thinking_on:
            effective = f"max({top_p:g}, 0.95) = {max(0.95, top_p):g}"
        else:
            effective = "1.0（固定）"
        self.effective_label.setText(
            f"thinking={'enabled' if thinking_on else 'disabled'}　|　"
            f"reasoning_effort={self._current_effort()}　|　"
            f"top_p 实际生效值：{effective}　|　"
            f"temperature：{'不生效' if thinking_on else f'{self.temperature_field.value():g}'}　|　"
            f"max_tokens={self.max_tokens_spin.value():,}"
        )

    def _thinking_enabled_in_dialog(self) -> bool:
        try:
            return self.generate_thinking.thinking().enabled
        except Exception:  # pragma: no cover
            return True

    def _current_effort(self) -> str:
        try:
            return self.generate_thinking.thinking().effort
        except Exception:  # pragma: no cover
            return "high"

    # ------------------------------------------------------------------ #
    def _on_save(self) -> None:
        warnings: list[str] = []
        model_id = self.model_combo.currentText().strip() or config_mod.DEFAULT_MODEL
        base_url = self.base_url_edit.text().strip() or "https://api.deepseek.com"
        if not base_url.rstrip("/").endswith(("/v1", "/beta")):
            base_url = base_url.rstrip("/") + "/v1"
            warnings.append(f"base_url 已自动补全版本段，保存为：{base_url}")

        pattern = self.pattern_edit.text().strip()
        if pattern:
            ok, message = outline_parser.validate_pattern(pattern)
            if not ok:
                error(self, "正则错误", message)
                self.nav.setCurrentRow(PAGES.index("大纲切分"))
                return

        missing_templates = [
            editor.validation().message()
            for editor in self.template_editors.values()
            if editor.validation().missing
        ]

        cfg = self.cfg
        cfg.api = config_mod.ApiConfig(
            api_key=self.api_key_edit.text().strip(),
            base_url=base_url,
            model=model_id,
            user_id=self.user_id_edit.text().strip(),
            timeout_seconds=self.timeout_spin.value(),
        )
        cfg.sampling = config_mod.SamplingConfig(
            temperature=self.temperature_field.value(),
            top_p=self.top_p_field.value(),
            max_tokens=self.max_tokens_spin.value(),
            concurrency=self.concurrency_spin.value(),
        )
        cfg.prompts = config_mod.PromptConfig(
            system_generate=self.generate_prompt.text(),
            system_check=self.check_prompt.text(),
        )
        if self._thinking_edited:
            cfg.thinking = config_mod.ThinkingDefaults(
                generate=self.generate_thinking.thinking(),
                check=self.check_thinking.thinking(),
            )
        cfg.ui.global_hide_reasoning = self.hide_reasoning_box.isChecked()
        cfg.ui.reasoning_collapse_tokens = self.collapse_spin.value()
        cfg.ui.auto_check_after_generate = self.auto_check_box.isChecked()
        cfg.ui.export_mode = self.export_mode_combo.currentData() or "all_marked"
        cfg.ui.export_mark_text = self.export_mark_edit.text() or "【未确认】"
        cfg.ui.export_append_check = self.export_append_box.isChecked()
        cfg.ui.export_separator = self.export_separator_box.isChecked()
        cfg.ui.export_encoding = self.export_encoding_combo.currentText()
        cfg.ui.export_open_after = self.export_open_box.isChecked()
        cfg.ui.export_dir = self.export_dir_edit.text().strip()
        cfg.ui.project_dir = self.project_dir_edit.text().strip()
        cfg.ui.chapter_pattern = pattern

        for kind_key, editor in self.template_editors.items():
            cfg.templates[kind_key] = editor.text()

        try:
            path = config_mod.save_config(cfg)
        except OSError as exc:
            error(self, "保存失败", f"无法写入配置文件：{exc}")
            return

        if missing_templates:
            warn(
                self,
                "模板占位符提醒",
                "以下模板缺少必需占位符（已按你的选择保存）：\n\n"
                + "\n".join(missing_templates)
                + "\n\n生成时该占位符位置不会被替换。",
            )
        if warnings:
            info(self, "已自动调整", "\n".join(warnings))

        self.configSaved.emit(cfg)
        self.accept()

    def _restore_page_defaults(self) -> None:
        page = PAGES[max(0, self.nav.currentRow())]
        if not confirm(self, "恢复默认", f"把「{page}」页的所有项恢复为程序默认值？"):
            return
        default = config_mod.AppConfig()
        if page == "API 与模型":
            self.api_key_edit.setText(default.api.api_key)
            self.base_url_edit.setText(default.api.base_url)
            self.model_combo.setEditText(default.api.model)
            self.user_id_edit.setText(default.api.user_id)
            self.timeout_spin.setValue(default.api.timeout_seconds)
        elif page == "采样参数":
            self.temperature_field.setValue(default.sampling.temperature)
            self.top_p_field.setValue(default.sampling.top_p)
            self.max_tokens_spin.setValue(default.sampling.max_tokens)
            self.concurrency_spin.setValue(default.sampling.concurrency)
        elif page == "深度思考":
            self.generate_thinking.set_thinking(default.thinking.generate)
            self.check_thinking.set_thinking(default.thinking.check)
            self.hide_reasoning_box.setChecked(default.ui.global_hide_reasoning)
            self.collapse_spin.setValue(default.ui.reasoning_collapse_tokens)
            self._mark_thinking_edited()
        elif page == "提示词":
            self.generate_prompt.set_text(default.prompts.system_generate)
            self.check_prompt.set_text(default.prompts.system_check)
        elif page == "消息模板":
            for kind_key, editor in self.template_editors.items():
                editor.set_text(MT.default_template(kind_key))
        elif page == "界面与流程":
            self.auto_check_box.setChecked(default.ui.auto_check_after_generate)
            self.export_mode_combo.setCurrentIndex(0)
            self.export_mark_edit.setText(default.ui.export_mark_text)
            self.export_append_box.setChecked(default.ui.export_append_check)
            self.export_separator_box.setChecked(default.ui.export_separator)
            self.export_encoding_combo.setCurrentText(default.ui.export_encoding)
            self.export_open_box.setChecked(default.ui.export_open_after)
            self.export_dir_edit.setText(default.ui.export_dir)
            self.project_dir_edit.setText(default.ui.project_dir)
        elif page == "大纲切分":
            self.pattern_edit.setText("")
        self._refresh_model_limits()

    # ------------------------------------------------------------------ #
    def _export_profile(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出配置", "novel_gen_config.json", "JSON 文件 (*.json)"
        )
        if not path:
            return
        self._on_save_silent()
        try:
            config_mod.export_profile(self.cfg, path)
            info(self, "导出成功", f"配置已导出到：\n{path}")
        except OSError as exc:
            error(self, "导出失败", str(exc))

    def _on_save_silent(self) -> None:
        """把界面上的值写回 cfg 但不关闭对话框（导出配置时用）。"""
        cfg = self.cfg
        cfg.api.model = self.model_combo.currentText().strip() or cfg.api.model
        cfg.api.timeout_seconds = self.timeout_spin.value()
        cfg.sampling.max_tokens = self.max_tokens_spin.value()
        cfg.sampling.concurrency = self.concurrency_spin.value()
        cfg.sampling.temperature = self.temperature_field.value()
        cfg.sampling.top_p = self.top_p_field.value()
        cfg.prompts.system_generate = self.generate_prompt.text()
        cfg.prompts.system_check = self.check_prompt.text()
        cfg.ui.export_dir = self.export_dir_edit.text().strip()
        cfg.ui.project_dir = self.project_dir_edit.text().strip()
        for kind_key, editor in self.template_editors.items():
            cfg.templates[kind_key] = editor.text()

    def _import_profile(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "导入配置", "", "JSON 文件 (*.json)"
        )
        if not path:
            return
        try:
            imported, warnings = config_mod.import_profile(path)
        except Exception as exc:
            error(self, "导入失败", f"无法解析配置文件：{exc}")
            return
        self.cfg = imported
        self._loading = True
        try:
            self._load_from_config()
        finally:
            self._loading = False
        self._refresh_sampling_state()
        if warnings:
            warn(self, "导入完成（有提示）", "\n".join(warnings))
        else:
            info(self, "导入完成", "配置已载入界面，点击“保存”后生效。")

    # ------------------------------------------------------------------ #
    def _browse_export_dir(self) -> None:
        start = self.export_dir_edit.text().strip() or str(core_paths.default_export_dir())
        folder = QFileDialog.getExistingDirectory(self, "选择默认导出目录", start)
        if folder:
            self.export_dir_edit.setText(folder)

    def _browse_project_dir(self) -> None:
        start = self.project_dir_edit.text().strip() or str(core_paths.default_project_dir())
        folder = QFileDialog.getExistingDirectory(self, "选择项目根目录", start)
        if folder:
            self.project_dir_edit.setText(folder)

    def _test_pattern(self) -> None:
        sample = (
            "第一章 觉醒\n本章内容：主角苏醒\n\n"
            "第 2 章 试炼\n本章内容：试炼开始\n\n"
            "【第三章】归途\n本章内容：返程"
        )
        pattern = self.pattern_edit.text().strip() or outline_parser.DEFAULT_CHAPTER_PATTERN
        ok, message = outline_parser.validate_pattern(pattern)
        if not ok:
            self.pattern_result.setStyleSheet("color:#cf222e; font-size:11px;")
            self.pattern_result.setText(message)
            return
        hits = outline_parser.preview_matches(sample, pattern)
        self.pattern_result.setStyleSheet("color:#1a7f37; font-size:11px;")
        if hits:
            self.pattern_result.setText(
                "匹配到 " + "；".join(f"第{line}行 {text}" for line, text in hits)
            )
        else:
            self.pattern_result.setStyleSheet("color:#9a6700; font-size:11px;")
            self.pattern_result.setText("示例文本未匹配到任何章节标题。")

    # ------------------------------------------------------------------ #
    def _test_connection(self) -> None:
        from core.api_client import DeepSeekClient

        self.test_button.setEnabled(False)
        self.test_result.setText("正在测试…")
        self.test_result.setStyleSheet("color:#57606a; font-size:11px;")

        api = config_mod.ApiConfig(
            api_key=self.api_key_edit.text().strip(),
            base_url=(self.base_url_edit.text().strip() or "https://api.deepseek.com"),
            model=self.model_combo.currentText().strip(),
            user_id=self.user_id_edit.text().strip(),
            timeout_seconds=self.timeout_spin.value(),
        )
        sampling = config_mod.SamplingConfig(
            temperature=0.0, top_p=1.0, max_tokens=64, concurrency=1,
        )
        client = DeepSeekClient(api, sampling, max_retries=0)

        import asyncio
        import threading

        from PySide6.QtCore import QMetaObject, Qt as QtNs

        def worker() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                ok, message = loop.run_until_complete(client.test_connection())
            except Exception as exc:  # pragma: no cover - 兜底
                ok, message = False, f"测试异常：{type(exc).__name__}: {exc}"
            finally:
                try:
                    loop.run_until_complete(client.aclose())
                except Exception:
                    pass
                loop.close()
            self._test_output = (ok, message)
            # 回到 UI 线程更新结果
            QMetaObject.invokeMethod(
                self, "_apply_test_result", QtNs.ConnectionType.QueuedConnection
            )

        self._test_thread = threading.Thread(target=worker, daemon=True)
        self._test_thread.start()

    def _apply_test_result(self) -> None:  # pragma: no cover - 需要真实网络
        ok, message = getattr(self, "_test_output", (False, "无结果"))
        self.test_button.setEnabled(True)
        self.test_result.setStyleSheet(
            f"color:{'#1a7f37' if ok else '#cf222e'}; font-size:11px;"
        )
        self.test_result.setText(message.replace("\n", " "))
