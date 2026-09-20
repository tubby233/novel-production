"""GUI 层（PySide6）。

约定
----
* 界面线程只做渲染与交互，不做网络与阻塞。
* 所有参数控件旁都有 ⓘ（:class:`gui.help_icon.HelpIcon`），说明来自 ``core.param_help``。
* 界面**不解析** AI 输出，只做字符串替换与展示；状态变更一律由 core.engine 的信号驱动。
"""

__all__ = [
    "help_icon",
    "widgets",
    "reasoning_panel",
    "thinking_panel",
    "message_bubble",
    "conversation_view",
    "chapter_view",
    "chapter_list",
    "outline_panel",
    "settings_dialog",
    "export_dialog",
    "main_window",
]
