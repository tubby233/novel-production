"""消息模板：定义、默认值、占位符校验与替换。

**这是全程序唯一允许构造"发给 AI 的消息"的地方。**

关键实现决定
------------
* 替换使用 ``str.replace`` 而不是 ``str.format``：
  小说大纲/正文里出现 ``{`` ``}`` 的概率不低，``format`` 会直接抛异常；
  ``replace`` 同时天然满足"只做字符串替换、不做任何解析"的设计原则。
* 只有白名单内的占位符会被替换，其它 ``{xxx}`` 原样保留并计入 ``unknown``。
* 占位符校验只做集合比对（正则提取花括号标识符），不判断文案是否合理。
* 缺失必需占位符**不会**阻止保存，只由设置界面给出提示。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping


class TemplateKind(StrEnum):
    FIRST_GENERATE = "tpl_first_generate"       # 首次生成，占位符 {outline}
    CHECK = "tpl_check"                         # 检查，占位符 {content}
    REGEN_BY_CHECK = "tpl_regen_by_check"       # 根据检查结果重新生成，占位符 {check_result}
    REGEN_MANUAL = "tpl_regen_manual"           # 手动重新生成，占位符 {user_input}


@dataclass(frozen=True)
class TemplateSpec:
    key: TemplateKind
    label: str                                 # 界面标题
    default: str                               # 默认内容
    required: tuple[str, ...]                  # 必需占位符
    optional: tuple[str, ...] = ()             # 可选占位符（允许为空值）
    help_key: str = ""                         # param_help 中的说明 key

    @property
    def placeholders(self) -> tuple[str, ...]:
        return self.required + self.optional


# --------------------------------------------------------------------------- #
# 默认模板（与需求文档给定文案一致）
# --------------------------------------------------------------------------- #
DEFAULT_FIRST_GENERATE = (
    "以下是小说的章节大纲，请严格按照大纲生成完整的章节正文，"
    "只输出正文内容，不要输出任何解释、说明或标记。\n"
    "大纲如下：\n"
    "{outline}"
)

DEFAULT_CHECK = (
    "以下是小说章节正文，请按检查要求进行检查。\n"
    "正文如下：\n"
    "{content}"
)

DEFAULT_REGEN_BY_CHECK = (
    "检查意见如下：\n"
    "{check_result}\n"
    "请严格按照以上检查意见重新生成本章完整正文，只输出正文内容，"
    "不要输出任何解释、说明或标记。"
)

DEFAULT_REGEN_MANUAL = (
    "请重新生成本章完整正文。{user_input}\n"
    "只输出正文内容，不要输出任何解释、说明或标记。"
)

# --------------------------------------------------------------------------- #
# 默认提示词
# --------------------------------------------------------------------------- #
DEFAULT_GENERATE_SYSTEM = (
    "你是一位专业的中文小说作者，擅长按大纲写出完整、连贯、有画面感的章节正文。\n"
    "写作要求：\n"
    "1. 严格按照给定大纲写作，不遗漏大纲中的情节要点与出场角色。\n"
    "2. 只输出章节正文本身，输出必须是**完整正文**：\n"
    "   - 不允许输出 diff、补丁、局部修改片段或“仅改动某段”的说明；\n"
    "   - 不允许输出大纲、要点列表、写作计划、思考过程或自我评价；\n"
    "   - 不允许添加任何解释性文字、markdown 代码块或分隔标记。\n"
    "3. 正文使用简体中文，段落之间用换行分隔。\n"
    "4. 保持人物设定与文风统一，兼顾节奏与细节描写。"
)

DEFAULT_CHECK_SYSTEM = ""  # 需求明确：检查提示词由用户自行提供，程序不预设内容


TEMPLATE_SPECS: dict[TemplateKind, TemplateSpec] = {
    TemplateKind.FIRST_GENERATE: TemplateSpec(
        key=TemplateKind.FIRST_GENERATE,
        label="首次生成消息模板",
        default=DEFAULT_FIRST_GENERATE,
        required=("outline",),
        help_key="tpl_first_generate",
    ),
    TemplateKind.CHECK: TemplateSpec(
        key=TemplateKind.CHECK,
        label="检查消息模板",
        default=DEFAULT_CHECK,
        required=("content",),
        help_key="tpl_check",
    ),
    TemplateKind.REGEN_BY_CHECK: TemplateSpec(
        key=TemplateKind.REGEN_BY_CHECK,
        label="根据检查结果重新生成的消息模板",
        default=DEFAULT_REGEN_BY_CHECK,
        required=("check_result",),
        help_key="tpl_regen_by_check",
    ),
    TemplateKind.REGEN_MANUAL: TemplateSpec(
        key=TemplateKind.REGEN_MANUAL,
        label="手动重新生成的消息模板",
        default=DEFAULT_REGEN_MANUAL,
        required=(),
        optional=("user_input",),
        help_key="tpl_regen_manual",
    ),
}


def get_spec(key: TemplateKind | str) -> TemplateSpec:
    return TEMPLATE_SPECS[TemplateKind(key)]


def default_template(key: TemplateKind | str) -> str:
    return get_spec(key).default


# --------------------------------------------------------------------------- #
# 占位符提取与校验
# --------------------------------------------------------------------------- #
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def extract_placeholders(text: str) -> list[str]:
    """按出现顺序提取 ``{name}`` 形式的占位符（去重）。"""
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text or ""):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


@dataclass(frozen=True)
class TemplateValidation:
    key: TemplateKind
    missing: tuple[str, ...]     # 缺少的必需占位符
    unknown: tuple[str, ...]     # 不在白名单内的占位符（会原样保留）

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unknown

    def message(self) -> str:
        """界面展示用的一行提示。"""
        if not self.missing and not self.unknown:
            names = "、".join(f"{{{p}}}" for p in get_spec(self.key).placeholders) or "（无需占位符）"
            return f"✅ 占位符检查通过：{names}"
        parts: list[str] = []
        if self.missing:
            names = "、".join(f"{{{p}}}" for p in self.missing)
            parts.append(f"⚠ 缺少必需占位符：{names}（仍可保存，但生成时该处不会被替换）")
        if self.unknown:
            names = "、".join(f"{{{p}}}" for p in self.unknown)
            parts.append(f"ⓘ 未知占位符：{names}（会原样保留并发送给模型）")
        return "　".join(parts)


def validate_template(key: TemplateKind | str, text: str) -> TemplateValidation:
    spec = get_spec(key)
    found = set(extract_placeholders(text))
    allowed = set(spec.placeholders)
    missing = tuple(p for p in spec.required if p not in found)
    unknown = tuple(p for p in extract_placeholders(text) if p not in allowed)
    return TemplateValidation(key=spec.key, missing=missing, unknown=unknown)


class MissingPlaceholderError(Exception):
    """strict 模式下缺少必需占位符。"""

    def __init__(self, key: TemplateKind, missing: tuple[str, ...]) -> None:
        self.key = key
        self.missing = missing
        super().__init__(
            f"模板 {get_spec(key).label} 缺少必需占位符：{', '.join('{' + m + '}' for m in missing)}"
        )


def validate_all(templates: Mapping[str, str]) -> list[TemplateValidation]:
    """校验全部模板。templates 里缺 key 时用默认值校验（默认值必然合法）。"""
    result: list[TemplateValidation] = []
    for key in TemplateKind:
        text = templates.get(str(key)) or default_template(key)
        result.append(validate_template(key, text))
    return result


# --------------------------------------------------------------------------- #
# 替换（唯一入口）
# --------------------------------------------------------------------------- #
def render_template(
    key: TemplateKind | str,
    values: Mapping[str, str] | None = None,
    *,
    text: str | None = None,
    strict: bool = False,
) -> str:
    """把 ``values`` 中的占位符替换进模板。

    参数
    ----
    key    模板类型
    values 占位符 -> 文本；未提供的必需占位符会被替换为空串（并在 strict 下抛错）
    text   直接指定模板正文；为 None 时由调用方传入用户配置值（见 config.get_template）
    strict True 时缺少必需占位符抛 MissingPlaceholderError

    返回替换后的字符串。未知 ``{xxx}`` 原样保留。
    """
    spec = get_spec(key)
    body = spec.default if text is None else text
    values = values or {}

    missing = tuple(p for p in spec.required if p not in values)
    if missing and strict:
        raise MissingPlaceholderError(spec.key, missing)

    for placeholder in spec.placeholders:
        token = "{" + placeholder + "}"
        if token in body:
            body = body.replace(token, str(values.get(placeholder, "")))
    return body


# --------------------------------------------------------------------------- #
# 四类消息的便捷构造
# --------------------------------------------------------------------------- #
def build_first_generate_user_msg(outline: str, *, text: str | None = None) -> str:
    return render_template(TemplateKind.FIRST_GENERATE, {"outline": outline}, text=text)


def build_check_user_msg(content: str, *, text: str | None = None) -> str:
    return render_template(TemplateKind.CHECK, {"content": content}, text=text)


def build_regen_by_check_user_msg(check_result: str, *, text: str | None = None) -> str:
    return render_template(
        TemplateKind.REGEN_BY_CHECK, {"check_result": check_result}, text=text
    )


def build_regen_manual_user_msg(user_input: str = "", *, text: str | None = None) -> str:
    return render_template(
        TemplateKind.REGEN_MANUAL, {"user_input": user_input}, text=text
    )
