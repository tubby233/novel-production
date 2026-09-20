"""core —— 纯逻辑层。

本包内的模块**不导入 PySide6**（engine.py 例外，它需要 QObject/QThread 做信号总线），
因此 models / param_help / message_templates / outline_parser / exporter 可独立单测。

核心设计约束（全项目通用）：
1. 程序不解析 AI 回复的语义。只做字符串替换、状态流转、界面展示。
2. 所有发给 AI 的消息都由 message_templates 的模板生成。
3. assistant 输出一律要求完整正文；程序从不做 diff / 局部修改。
"""

__all__ = [
    "models",
    "paths",
    "errors",
    "param_help",
    "message_templates",
    "config",
    "outline_parser",
    "api_client",
    "generator",
    "checker",
    "engine",
    "exporter",
]
