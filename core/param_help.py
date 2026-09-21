"""全应用参数的集中说明表（唯一数据源）。

设计要点
--------
* 每个可交互控件在界面上都通过 ``HelpIcon(param_key)`` 关联一个 ``ParamHelp`` 条目。
* 界面**只**调用 ``get_help() / tooltip_text() / html_text()``，说明文案全部在这里维护，
  便于统一修改与（将来）国际化。
* 数值型参数在这里声明取值范围与默认值，界面的 SpinBox / Slider 直接复用，
  避免"文档写一套、控件限制另一套"。
* 若某个 key 缺失，``get_help`` 返回占位说明而**不抛异常**，保证界面永不因缺文案崩溃。
* 开发期自检：``missing_help_keys(USED_KEYS)`` 可检查覆盖率。

数值默认值依据 DeepSeek 官方文档：
  模型 & 价格        https://api-docs.deepseek.com/zh-cn/quick_start/pricing
  思考模式          https://api-docs.deepseek.com/zh-cn/guides/thinking_mode
  Chat Completions  https://api-docs.deepseek.com/zh-cn/api/create-chat-completion
  限速与隔离        https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Iterable


class HelpKind(StrEnum):
    TEXT = "text"          # 文本输入
    PASSWORD = "password"  # 密钥输入
    INT = "int"
    FLOAT = "float"
    CHOICE = "choice"
    BOOL = "bool"
    TEMPLATE = "template"  # 多行模板/提示词编辑
    ACTION = "action"      # 按钮
    GROUP = "group"        # 分组/只读说明
    DISPLAY = "display"    # 只读展示


@dataclass(frozen=True)
class ParamHelp:
    """单个参数的完整说明。"""

    key: str
    name_cn: str
    name_en: str
    kind: HelpKind
    range_text: str = ""                       # 取值范围 / 可选项
    choices: tuple[str, ...] = ()              # choice 型可选项
    default_text: str = ""                     # 默认值（展示用字符串）
    effect: str = ""                           # 参数影响（调大/调小会怎样）
    constraints: tuple[str, ...] = ()          # 特殊约束
    advice: tuple[str, ...] = ()               # 相关建议
    example: str = ""                          # 示例
    related: tuple[str, ...] = ()              # 相关参数 key

    # ------------------------------------------------------------------ #
    # 渲染
    # ------------------------------------------------------------------ #
    @property
    def title(self) -> str:
        return f"{self.name_cn}（{self.name_en}）"

    def to_plain(self) -> str:
        """纯文本版说明（日志 / 复制用）。"""
        lines = [self.title]
        if self.range_text:
            lines.append(f"范围：{self.range_text}")
        if self.default_text:
            lines.append(f"默认：{self.default_text}")
        if self.effect:
            lines.append(f"影响：{self.effect}")
        for item in self.constraints:
            lines.append(f"注意：{item}")
        for item in self.advice:
            lines.append(f"建议：{item}")
        if self.example:
            lines.append(f"示例：{self.example}")
        return "\n".join(lines)

    def to_html(self) -> str:
        """富文本版说明（QToolTip / 帮助弹窗用）。"""
        parts: list[str] = [f"<b>{self.title}</b>"]
        if self.kind == HelpKind.ACTION:
            parts.append("<i>操作按钮</i>")
        if self.range_text:
            parts.append(f"<b>范围：</b>{_esc(self.range_text)}")
        if self.choices:
            parts.append(f"<b>可选：</b>{' / '.join(self.choices)}")
        if self.default_text:
            parts.append(f"<b>默认：</b>{_esc(self.default_text)}")
        if self.effect:
            parts.append(f"<b>影响：</b>{_esc(self.effect)}")
        for item in self.constraints:
            parts.append(f"<b>注意：</b>{_esc(item)}")
        for item in self.advice:
            parts.append(f"<b>建议：</b>{_esc(item)}")
        if self.example:
            parts.append(f"<b>示例：</b><code>{_esc(self.example)}</code>")
        if self.related:
            names = "、".join(
                f"{PARAM_HELP[k].name_cn}" for k in self.related if k in PARAM_HELP
            )
            if names:
                parts.append(f"<b>另见：</b>{names}")
        return "<br>".join(parts)

    def tooltip(self) -> str:
        """QToolTip 用文本（自动换行交给 Qt 处理）。"""
        return self.to_html()


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


# --------------------------------------------------------------------------- #
# 具体参数（写在这里的就是"标准答案"）
# --------------------------------------------------------------------------- #
PARAM_HELP: dict[str, ParamHelp] = {}


def _reg(help_item: ParamHelp) -> ParamHelp:
    PARAM_HELP[help_item.key] = help_item
    return help_item


# =========================== 一、API 与模型 ================================ #

_reg(ParamHelp(
    key="api_key",
    name_cn="API 密钥",
    name_en="api_key",
    kind=HelpKind.PASSWORD,
    range_text="形如 sk-xxxxxxxx 的字符串（DeepSeek 开放平台创建）",
    default_text="（空）",
    effect="决定请求的身份与计费账号。密钥无效时所有请求都会返回 401 / 403 鉴权错误。",
    constraints=(
        "以明文保存在配置文件 config.json 中，请不要把该文件分享给他人。",
        "密钥与 base_url、model 三者必须匹配同一个服务商。",
    ),
    advice=(
        "填写后先点“测试连接”，确认通过再开始批量生成。",
        "多人共用一台电脑时，建议设置完就清空。",
    ),
    related=("base_url", "model", "test_connection"),
))

_reg(ParamHelp(
    key="base_url",
    name_cn="接口地址",
    name_en="base_url",
    kind=HelpKind.TEXT,
    range_text="OpenAI 兼容格式的根地址（官方为 https://api.deepseek.com）",
    default_text="https://api.deepseek.com",
    effect="决定请求发往哪个服务。填错会连接失败、返回 404 或鉴权失败。",
    constraints=(
        "官方根地址不带 /v1，SDK 会自动拼接 /chat/completions；若你的中转服务要求带 /v1，按对方文档填写。",
        "不要填写完整的 /chat/completions 路径。",
    ),
    advice=(
        "使用官方 API 时保持默认值即可。",
        "使用代理/中转时，务必与代理方确认是否需要 /v1 后缀。",
    ),
    related=("api_key", "model"),
))

_reg(ParamHelp(
    key="model",
    name_cn="模型名称",
    name_en="model",
    kind=HelpKind.CHOICE,
    choices=("deepseek-flash", "deepseek-v4-pro"),
    range_text="可手动输入任意服务端支持的模型 id",
    default_text="deepseek-flash（即 DeepSeek-V4.1-Flash）",
    effect=(
        "决定生成质量、速度、价格与能力上限。"
        "deepseek-flash 上下文 1M、单次输出最大 384K、账号并发上限 2500；"
        "deepseek-v4-pro 并发上限 500 且不支持图像理解。"
    ),
    constraints=(
        "文档枚举中的合法 id 为 deepseek-flash 与 deepseek-v4-pro；名称写错会返回 400 model not found。",
        "本程序调用的是 Chat Completions 接口。",
    ),
    advice=(
        "正文生成与检查都用 deepseek-flash 即可（速度快、价格低）。",
        "需要更强推理且不在意成本时，可临时改为 deepseek-v4-pro 做检查。",
    ),
    related=("api_key", "max_tokens", "concurrency"),
))

_reg(ParamHelp(
    key="user_id",
    name_cn="用户标识",
    name_en="user_id",
    kind=HelpKind.TEXT,
    range_text="仅可含 [a-zA-Z0-9-_]，最长 512 字符",
    default_text="（空）",
    effect=(
        "用于服务端的内容安全处理、KVCache（上下文硬盘缓存）隔离与调度隔离。"
        "不填不影响功能。"
    ),
    constraints=(
        "该参数需放在请求的 extra_body 中（程序已处理）。",
        "请不要在其中包含任何隐私信息。",
    ),
    advice=(
        "单人使用留空即可。",
        "若同一台机器上要区分多个使用者/多部小说的缓存，可填一个代号。",
    ),
    related=("api_key", "base_url"),
))

_reg(ParamHelp(
    key="timeout_seconds",
    name_cn="请求超时",
    name_en="timeout",
    kind=HelpKind.INT,
    range_text="30 ~ 7200 秒",
    default_text="1800（30 分钟）",
    effect="单次请求等待服务端响应的最长时间。超时后本次请求失败，章节状态置为“出错”，可重试。",
    constraints=(
        "服务端的保活机制：请求发出后若 10 分钟内仍未开始推理，服务器会关闭连接（流式会持续收到 : keep-alive 注释）。",
        "思考强度 max + 384K 输出上限时，单次生成耗时可能很长，超时不要设置过小。",
    ),
    advice=(
        "默认 1800 秒足够覆盖绝大多数章节。",
        "若频繁超时，先降低 max_tokens 或思考强度，而不是无限加大超时。",
    ),
    related=("max_tokens", "reasoning_effort"),
))

_reg(ParamHelp(
    key="model_limits_view",
    name_cn="当前模型能力（只读）",
    name_en="model limits",
    kind=HelpKind.DISPLAY,
    default_text="上下文 1,048,576 tokens（1M）｜单次输出最大 393,216 tokens（384K）｜账号并发上限 2500",
    effect="展示所选模型的能力上限，作为 max_tokens、并发数的填写参考。切换模型后自动刷新。",
    constraints=("这些数值由模型决定、不可修改；本表依据 DeepSeek 官方文档维护。",),
    advice=("若你的服务商限速更严，请以对方文档为准填写并发数。",),
    related=("model", "max_tokens", "concurrency"),
))

_reg(ParamHelp(
    key="test_connection",
    name_cn="测试连接",
    name_en="test connection",
    kind=HelpKind.ACTION,
    effect="用当前设置发一条极短请求，验证 api_key / base_url / model 是否可用，并回显模型能力与延迟。",
    constraints=("会消耗极少量 token（通常几条）。",),
    advice=("每次修改密钥或地址后都先测试，避免批量生成开始后才发现配置错误。",),
))

# =========================== 二、采样参数 ================================== #

_reg(ParamHelp(
    key="max_tokens",
    name_cn="单次最大输出",
    name_en="max_tokens",
    kind=HelpKind.INT,
    range_text="1 ~ 393216（384K，即 deepseek-flash 的单次输出上限）",
    default_text="393216（取上限）",
    effect=(
        "限制单次 completion 能生成的最大 token 数。"
        "取值过小会导致正文被截断（finish_reason=\"length\"）。"
    ),
    constraints=(
        "它是“上限”而不是“目标”：模型自然写完就会停止，不会因为值大而硬凑长度。",
        "输入 token + 输出 token 的总长度仍受 1M 上下文窗口限制。",
        "服务端默认值：非思考模式 8K；思考模式 64K；思考强度 max 时 128K。本程序显式设为上限以避免章节被截断。",
        "思维链（reasoning）token 计入 completion_tokens，因此开启思考时实际可用的正文长度会少于 max_tokens。",
    ),
    advice=(
        "章节 3000~5000 字时 8K 已足够；不确定就保留 384K。",
        "若想控制成本与耗时，可改为 65536（思考模式默认）或 131072（max 默认）。",
        "快捷预设：8K / 64K / 128K / 384K。",
    ),
    related=("context_window", "reasoning_effort", "timeout_seconds"),
))

_reg(ParamHelp(
    key="context_window",
    name_cn="上下文窗口（只读）",
    name_en="context window",
    kind=HelpKind.DISPLAY,
    range_text="1,048,576 tokens（1M）",
    default_text="1M",
    effect="单次请求中“输入 + 输出”token 总数不能超过该值；超出会返回 400 错误。",
    constraints=(
        "由模型决定，不可配置。",
        "本程序每章使用独立对话、不跨章拼接上下文，输入通常只有几 K token，因此极少触及上限。",
    ),
    advice=(
        "一般无需关心。",
        "若在同一对话里进行大量手动追加消息，请留意状态栏的上下文占用提示。",
    ),
    related=("max_tokens", "model"),
))

_reg(ParamHelp(
    key="temperature",
    name_cn="温度",
    name_en="temperature",
    kind=HelpKind.FLOAT,
    range_text="0.0 ~ 2.0（步进 0.01）",
    default_text="1.0",
    effect=(
        "值越高，输出越随机、越有创造性，情节更容易发散；"
        "值越低，输出越确定、越保守，容易重复和僵化。"
    ),
    constraints=(
        "思考模式启用时此参数不生效：设置不会报错，但 API 会忽略它。",
        "官方建议：temperature 与 top_p 二者改其一即可，不建议同时修改。",
    ),
    advice=(
        "非思考模式写正文建议 0.7 ~ 1.3；追求灵感发散可用 1.3 ~ 1.5；严格贴合大纲可用 0.5 ~ 0.8。",
        "思考模式下无需调整，直接忽略即可。",
    ),
    example="temperature = 0.9",
    related=("top_p", "thinking_enabled"),
))

_reg(ParamHelp(
    key="top_p",
    name_cn="核采样",
    name_en="top_p",
    kind=HelpKind.FLOAT,
    range_text="0.0 ~ 1.0（步进 0.01）",
    default_text="1.0",
    effect=(
        "模型只从累积概率前 top_p 的候选 token 中采样。值越小候选越集中、输出越保守稳定。"
    ),
    constraints=(
        "思考模式下生效，但下限为 0.95：小于 0.95 的值会被自动抬升至 0.95。",
        "非思考模式下该参数恒为 1.0，传入的任何值都会被忽略。",
        "取值必须大于 0 且不超过 1。",
    ),
    advice=(
        "一般保持 1.0。",
        "确需收紧时设 0.95 ~ 0.98，并在“本次生效参数预览”里确认实际生效值。",
    ),
    example="top_p = 0.95",
    related=("temperature", "thinking_enabled", "effective_preview"),
))

_reg(ParamHelp(
    key="concurrency",
    name_cn="并发数",
    name_en="concurrency",
    kind=HelpKind.INT,
    range_text="1 ~ 2500（deepseek-flash 账号级上限 2500；deepseek-v4-pro 为 500）",
    default_text="8",
    effect=(
        "同时进行的章节任务数量。越大整体越快，但更容易触发限速、占满带宽与配额。"
    ),
    constraints=(
        "并发限制以“账号”为单位，与使用哪个 API Key 无关。",
        "超过账号并发限制会返回 HTTP 429 错误。",
        "调小并发数不会中断已经在跑的任务，只影响后续排队任务。",
    ),
    advice=(
        "几十章的规模建议 5 ~ 20，从 8 起步。",
        "出现 429 就立即下调（错误面板提供一键降低并发）。",
        "本项目每章大纲自包含、章节之间无需连贯，因此可以放心并发。",
    ),
    related=("model", "max_tokens"),
))

_reg(ParamHelp(
    key="sampling_note",
    name_cn="生效规则说明",
    name_en="sampling rules",
    kind=HelpKind.GROUP,
    effect="汇总 DeepSeek 文档中关于思考模式与采样参数的相互作用规则。",
    constraints=(
        "思考模式不支持 temperature：设置不报错，但不会生效。",
        "top_p 在思考模式下生效，但下限为 0.95（小于 0.95 会被抬升）；非思考模式下恒为 1.0。",
        "frequency_penalty 与 presence_penalty 已被 DeepSeek 官方弃用（传入不会产生任何效果），本程序不提供、也不发送这两个参数。",
        "思考模式下思维链通过 reasoning_content 返回；本项目未携带 tools 参数，因此 reasoning_content 无需回传。",
    ),
    advice=("开启思考模式时，界面会自动把 temperature 等控件置灰并标注“当前不生效”。",),
    related=("thinking_enabled", "top_p", "temperature"),
))

_reg(ParamHelp(
    key="effective_preview",
    name_cn="本次生效参数预览",
    name_en="effective params",
    kind=HelpKind.DISPLAY,
    effect=(
        "显示程序在发送前对参数做规范化处理后的真实入参，"
        "例如 top_p 被抬升至 0.95、temperature 被忽略、max_tokens 因上下文预算被下调。"
    ),
    constraints=("只读展示，不参与请求构造。",),
    advice=("当“改了参数却没效果”时，先看这里确认 API 实际收到了什么。",),
    related=("sampling_note", "top_p"),
))

# =========================== 三、深度思考 ================================== #

_reg(ParamHelp(
    key="thinking_enabled",
    name_cn="思考模式开关",
    name_en="thinking",
    kind=HelpKind.BOOL,
    choices=("启用", "禁用"),
    default_text="启用（与 API 默认一致）",
    effect=(
        "启用后模型会先输出一段思维链再给出正文，通常质量更好、更贴合大纲，但耗时与 token 消耗显著增加。"
    ),
    constraints=(
        "映射为请求的 extra_body={\"thinking\": {\"type\": \"enabled\" / \"disabled\"}}。",
        "启用时 temperature 不生效；top_p 生效但下限 0.95。",
        "API 端该字段默认为 enabled，即默认开启思考。",
    ),
    advice=(
        "生成对话建议启用，思考强度 high。",
        "只看速度的场景（例如快速出草稿）可以禁用思考。",
    ),
    related=("reasoning_effort", "show_reasoning", "top_p"),
))

_reg(ParamHelp(
    key="reasoning_effort",
    name_cn="思考强度",
    name_en="reasoning_effort",
    kind=HelpKind.CHOICE,
    choices=("low", "high", "max"),
    range_text="low / high / max（传入 none 表示关闭思考）",
    default_text="high（API 默认值）",
    effect=(
        "强度越高，模型思考越深入，但耗时和 token 消耗也越高。"
        "官方说明：未设置 max_tokens 时，思考模式默认输出 64K，思考强度为 max 时默认 128K。"
    ),
    constraints=(
        "仅在思考模式启用时生效。",
        "官方映射关系：minimal→low、low→low、medium→high、high→high、xhigh→high、max→max、ultra→max。",
        "若想用单个参数同时控制开关，可传 reasoning_effort=\"none\" 表示关闭思考；本程序统一使用 extra_body.thinking.type 做开关，本项只控制强度。",
    ),
    advice=(
        "low 适合简单任务；high 适合一般创作；max 适合复杂推理或高质量检查。",
        "推荐组合：生成对话 high，检查对话 max。",
    ),
    related=("thinking_enabled", "max_tokens", "show_reasoning"),
))

_reg(ParamHelp(
    key="reasoning_effort_mapping",
    name_cn="思考强度映射说明",
    name_en="effort mapping",
    kind=HelpKind.GROUP,
    effect="展示官方公布的请求强度与实际推理强度映射表。",
    constraints=(
        "minimal → low；low → low；medium → high；high → high；xhigh → high；max → max；ultra → max。",
    ),
    advice=("从别处复制来的 medium / xhigh 实际效果等同 high，不必纠结。",),
    related=("reasoning_effort",),
))

_reg(ParamHelp(
    key="show_reasoning",
    name_cn="展示思维链（逐对话）",
    name_en="show reasoning",
    kind=HelpKind.BOOL,
    default_text="开启",
    effect="决定该对话是否在界面渲染 reasoning_content（思维链）。关闭后仍然在后台记录，可随时重新打开查看。",
    constraints=(
        "仅当设置中的“全局隐藏思维链”关闭时生效。",
        "只影响显示，不影响请求参数，也不影响导出（导出永不包含思维链）。",
    ),
    advice=(
        "生成对话可关闭以节省屏幕空间；检查对话建议开启，便于判断检查是否到位。",
    ),
    related=("global_hide_reasoning", "thinking_enabled", "reasoning_collapse_tokens"),
))

_reg(ParamHelp(
    key="global_hide_reasoning",
    name_cn="全局隐藏思维链",
    name_en="global hide reasoning",
    kind=HelpKind.BOOL,
    default_text="关闭",
    effect="开启后，所有对话都不再展示思维链，仅在后台记录（可在消息历史中查看、可随时关闭本开关恢复显示）。",
    constraints=("开启后各对话的“展示思维链”开关会被禁用并置灰。",),
    advice=("录屏、演示或分享界面时开启。",),
    related=("show_reasoning",),
))

_reg(ParamHelp(
    key="reasoning_collapse_tokens",
    name_cn="思维链自动收起阈值",
    name_en="collapse threshold",
    kind=HelpKind.INT,
    range_text="200 ~ 60000 tokens",
    default_text="2000",
    effect="思维链 token 数超过该阈值时自动折叠，避免把正文挤出视野；未超过时默认展开。",
    constraints=("仅影响界面显示，不影响请求与计费。",),
    advice=("屏幕小或想专注正文时可设为 1000；需要通读思考过程可设为 60000。",),
    related=("show_reasoning",),
))

# =========================== 四、消息模板 ================================== #

_reg(ParamHelp(
    key="tpl_first_generate",
    name_cn="首次生成消息模板",
    name_en="first generate template",
    kind=HelpKind.TEMPLATE,
    range_text="必须包含占位符 {outline}；其余花括号内容会原样保留",
    default_text="内置默认文案（见设置界面）",
    effect="每章第一次生成正文时，程序把该章大纲填入 {outline} 后作为 user 消息发送。",
    constraints=(
        "程序只做字符串替换，不做任何语义解析：模板里除 {outline} 以外的内容会原样发送。",
        "缺少 {outline} 时设置界面会给出黄色警告，但仍允许保存（由用户自行负责）。",
        "必须要求模型输出完整正文，不要要求 diff、局部修改或只输出片段。",
    ),
    advice=(
        "在模板里明确写“只输出正文内容，不要输出任何解释、说明或标记”，可减少模型附加说明。",
        "每章大纲已包含本章内容、出场角色、前情提要与后续剧情，无需在模板里再补充跨章上下文。",
    ),
    related=("tpl_check", "system_generate", "placeholder_status_first"),
))

_reg(ParamHelp(
    key="tpl_check",
    name_cn="检查消息模板",
    name_en="check template",
    kind=HelpKind.TEMPLATE,
    range_text="必须包含占位符 {content}；可选占位符 {word_count}",
    default_text="内置默认文案（见设置界面）",
    effect=(
        "触发检查时，程序把该章最后一次生成的正文填入 {content}，"
        "并把**程序统计好的字数**填入 {word_count}，作为检查对话的第一条 user 消息发送。"
    ),
    constraints=(
        "字数由程序统计（不计空格与换行），检查对话不需要也不应该自己去数。",
        "内置默认模板会明确告诉模型“不要自己数字数”，因此不会出现字数统计偏差。",
        "检查对话是全新独立对话，system 消息来自“检查提示词”，与本模板互不干扰。",
        "检查结果会原样放进界面的可编辑文本框，程序不解析其内容。",
    ),
    advice=(
        "检查提示词留空时，建议在检查模板里写清检查维度（例如设定一致性、文风、节奏）。",
        "想按字数判断篇幅，直接引用 {word_count} 即可。",
    ),
    related=("system_check", "tpl_regen_by_check"),
))

_reg(ParamHelp(
    key="tpl_regen_by_check",
    name_cn="根据检查结果重新生成的消息模板",
    name_en="regen by check template",
    kind=HelpKind.TEMPLATE,
    range_text="必须包含占位符 {check_result}",
    default_text="内置默认文案（见设置界面）",
    effect=(
        "点击“根据检查结果重新生成”时，程序读取检查视图中**当前编辑框里的内容**"
        "（含你的手动修改），填入 {check_result} 后追加到该章生成对话，触发重新生成。"
    ),
    constraints=(
        "填入的是界面上的实时文本，而不是首次检查的原始返回值。",
        "这条消息会进入生成对话的多轮上下文，因此可以叠加多次修改意见。",
    ),
    advice=("保持“只输出正文内容”的要求，避免模型只回复修改说明。",),
    related=("tpl_check", "act_regen_by_check", "tpl_regen_manual"),
))

_reg(ParamHelp(
    key="tpl_regen_manual",
    name_cn="手动重新生成的消息模板",
    name_en="manual regen template",
    kind=HelpKind.TEMPLATE,
    range_text="包含占位符 {user_input}（允许为空字符串）",
    default_text="内置默认文案（见设置界面）",
    effect="点击“手动重新生成”时，弹出输入框收集你的指令，填入 {user_input} 后追加到生成对话并重新生成。",
    constraints=(
        "{user_input} 允许为空：此时模板会留下空白，模型将仅按“重新生成本章完整正文”执行。",
        "输入框内容不经任何解析，原样嵌入。",
    ),
    advice=("把常说的要求（例如“节奏加快”“对白更多”）写进模板正文，输入框只填当次差异。",),
    related=("act_regen_manual", "tpl_regen_by_check"),
))

_reg(ParamHelp(
    key="placeholder_status_first",
    name_cn="占位符校验结果",
    name_en="placeholder check",
    kind=HelpKind.DISPLAY,
    effect="显示当前模板是否包含全部必需占位符，并列出识别到的未知占位符。",
    constraints=(
        "校验只做花括号占位符的集合比对，不判断文案内容是否合理。",
        "未知占位符不会被替换，会原样发送给模型。",
        "缺少必需占位符不会阻止保存，仅在界面上提示。",
    ),
    advice=("看到黄色警告时建议补齐；确实不需要该占位符时可忽略警告。",),
    related=("tpl_first_generate",),
))

_reg(ParamHelp(
    key="system_generate",
    name_cn="生成提示词（system）",
    name_en="generate system prompt",
    kind=HelpKind.TEMPLATE,
    range_text="任意文本；为空则不发送 system 消息",
    default_text="内置创作指导（含“只输出完整正文，禁止 diff / 片段 / 解释”）",
    effect="创建生成对话时作为 system 消息注入，对该对话的全部轮次生效。",
    constraints=(
        "修改后只对**新建**的生成对话生效；已存在的对话保留原 system 消息。",
        "需与消息模板配合：模板负责当次任务说明，system 负责长期风格约定。",
    ),
    advice=(
        "在这里约定文风、人称、字数区间、禁用词等长期规则。",
        "明确禁止输出大纲式条目、修改说明与思考过程。",
    ),
    related=("system_check", "tpl_first_generate"),
))

_reg(ParamHelp(
    key="system_check",
    name_cn="检查提示词（system）",
    name_en="check system prompt",
    kind=HelpKind.TEMPLATE,
    range_text="任意文本；默认为空",
    default_text="（空，程序不预设任何内容）",
    effect="创建检查对话时作为 system 消息注入。为空时该检查对话不发送 system 消息，只发送检查消息模板。",
    constraints=(
        "程序只预留可编辑文本框并持久化保存，不预设内容、不解析返回。",
        "检查提示词决定“检查什么”，检查消息模板决定“怎么把正文交过去”。",
    ),
    advice=(
        "建议写清检查维度与输出形式（例如逐条列出问题与修改建议）。",
        "若希望检查结果便于直接喂回生成对话，可要求它以“修改要求”的口吻书写。",
    ),
    related=("system_generate", "tpl_check"),
))

# =========================== 五、界面与流程 ================================ #

_reg(ParamHelp(
    key="auto_check_after_generate",
    name_cn="生成完成后自动检查",
    name_en="auto check",
    kind=HelpKind.BOOL,
    default_text="开启",
    effect="开启时，正文生成完成会立即自动进入检查环节；关闭时需手动点击“触发检查”。",
    constraints=("检查始终使用全新对话，与生成对话完全分离。",),
    advice=("全自动流水线保持开启；想先人工读一遍再检查时关闭。",),
    related=("act_trigger_check",),
))

_reg(ParamHelp(
    key="chapter_pattern",
    name_cn="章节标题正则",
    name_en="chapter pattern",
    kind=HelpKind.TEXT,
    range_text="Python 正则表达式，默认匹配“第X章 / 第N章 / Chapter N”",
    default_text=r"^\s*第\s*[0-9零一二三四五六七八九十百千两]+\s*[章回节卷篇]",
    effect="决定大纲文本如何切分为章节列表。匹配到的行作为该章起始，直到下一处匹配之前都属于该章。",
    constraints=(
        "正则编译失败或未匹配到任何章节时，程序会提示，并提供“整篇作为一章 / 按空行分段”的兜底选项。",
        "每章大纲整体作为一个输入单元，程序不再做更细粒度切分。",
    ),
    advice=("导入后先在“切分预览”里检查标题是否切对，再开始生成。",),
    example=r"^\s*第\s*\d+\s*章",
    related=("outline_import",),
))

_reg(ParamHelp(
    key="outline_import",
    name_cn="导入大纲",
    name_en="import outline",
    kind=HelpKind.ACTION,
    effect="从 .txt / .md 文件读取大纲文本到编辑区（自动尝试 utf-8、utf-8-sig、gbk 编码）。",
    constraints=("只读取文本，不做任何内容改写。",),
    advice=("导入后立即点“切分章节”并检查预览。",),
    related=("chapter_pattern", "outline_split"),
))

_reg(ParamHelp(
    key="outline_split",
    name_cn="切分章节",
    name_en="split chapters",
    kind=HelpKind.ACTION,
    effect=(
        "用章节标题正则把编辑区文本切分并替换当前章节列表；"
        "切分成功后**触发立项**：输入小说名，程序在项目根目录下新建同名文件夹存放该小说的全部数据。"
    ),
    constraints=(
        "会覆盖列表中已有的同序号章节，请先导出/备份已生成内容。",
        "同名项目文件夹已存在时会提示换一个名字，不会静默覆盖。",
    ),
    advice=("切分后可用列表的“编辑大纲”逐章微调。",),
    related=("chapter_pattern", "project_dir", "act_open_project"),
))

# =========================== 六、项目管理 ================================= #

_reg(ParamHelp(
    key="project_dir",
    name_cn="项目根目录",
    name_en="project root",
    kind=HelpKind.TEXT,
    default_text="我的文档\\NovelGenProjects",
    effect=(
        "存放小说项目的文件夹。每次“切分章节”触发立项时，"
        "程序在该目录下新建一个以小说名命名的子文件夹，"
        "章节数据（chapters.json）、大纲原文（outline.txt）与项目信息（project.json）都写在那里。"
    ),
    constraints=(
        "一部小说一个文件夹，整个文件夹可直接拷贝到别处备份。",
        "需要保证该目录可写；不可写时保存会失败并在日志中提示。",
    ),
    advice=("把项目根目录放在同步盘里，可以顺带备份所有小说。",),
    related=("outline_split", "act_open_project", "exp_dir"),
))

_reg(ParamHelp(
    key="act_open_project",
    name_cn="打开项目",
    name_en="open project",
    kind=HelpKind.ACTION,
    effect="列出项目根目录下已有的小说项目，选择后读取该小说的章节、正文、检查结果与对话历史。",
    constraints=("打开项目会替换当前界面的章节列表与大纲文本，请先确认已保存。",),
    advice=("下次启动会自动打开上次使用的小说项目。",),
    related=("project_dir", "outline_split"),
))

# =========================== 六、章节操作按钮 ============================== #

_reg(ParamHelp(
    key="act_generate",
    name_cn="生成正文",
    name_en="generate",
    kind=HelpKind.ACTION,
    effect="为该章创建（或复用）生成对话，用“首次生成消息模板”填充本章大纲后开始流式生成。",
    constraints=(
        "仅当章节处于“待生成 / 出错”状态时可用。",
        "章节处于“生成中 / 检查中”时按钮置灰，点击会提示“请等待当前生成/检查完成或先停止”。",
    ),
    advice=("已切分好的章节可勾选后使用工具栏“生成选中章节”批量并发执行。",),
    related=("act_stop_chapter", "act_regen_manual"),
))

_reg(ParamHelp(
    key="act_stop_chapter",
    name_cn="停止本章",
    name_en="stop chapter",
    kind=HelpKind.ACTION,
    effect="取消该章当前正在进行的生成或检查任务。已经收到的正文会被保留下来，不会丢弃。",
    constraints=(
        "停止后若已有非空正文，章节进入“待检查”；若正文为空则回到“待生成”。",
        "被中断的 assistant 消息会标记为“已中断”，内容可能不完整。",
    ),
    advice=("发现模型跑偏时，立即停止，再用“手动重新生成”附上纠正指令。",),
    related=("act_stop_all", "act_generate"),
))

_reg(ParamHelp(
    key="act_stop_all",
    name_cn="停止全部",
    name_en="stop all",
    kind=HelpKind.ACTION,
    effect="取消所有正在运行与排队中的章节任务。",
    constraints=(
        "已生成的部分内容同样会被保留。",
        "排队中（还没开始跑）的任务被取消后，章节会回到提交前的状态，不会卡在“排队中”。",
    ),
    advice=("要立刻释放并发配额或结束工作时段时使用。",),
    related=("act_stop_chapter",),
))

_reg(ParamHelp(
    key="act_trigger_check",
    name_cn="触发检查",
    name_en="trigger check",
    kind=HelpKind.ACTION,
    effect=(
        "新建一个全新的检查对话：system 为“检查提示词”，首条 user 消息为检查消息模板填充本章正文后的内容。"
    ),
    constraints=(
        "需要本章已有非空正文。",
        "章节处于“生成中 / 检查中”时不可用，点击会给出提示。",
        "每次检查都是全新对话，与上一次检查互不影响，历次结果保留在“历次检查结果”下拉框中。",
        "检查对话默认**关闭**深度思考；可在检查视图的“对话设置”里单独开启。",
    ),
    advice=("对同一章可用不同检查提示词反复检查，结果会累积保存。",),
    related=("tpl_check", "system_check", "reasoning_effort"),
))

_reg(ParamHelp(
    key="act_regen_by_check",
    name_cn="根据检查结果重新生成",
    name_en="regenerate by check",
    kind=HelpKind.ACTION,
    effect=(
        "读取检查结果编辑框中的**当前文本**，填入“根据检查结果重新生成的消息模板”，"
        "作为一条 user 消息追加到该章生成对话，并重新生成完整正文。"
    ),
    constraints=(
        "仅当章节处于“待决策 / 合格”状态时可用。",
        "填入的是你编辑后的文本，因此可以先手动删掉不认可的检查意见。",
        "点击后任务**排在队列末尾**，章节状态变为“排队中”，界面上会给出明确提示。",
        "生成完成后会再次自动进入检查环节。",
        "重生成次数没有上限，循环完全由你控制。",
    ),
    advice=("如果只认可检查意见中的一部分，先在文本框里删掉其余内容再点此按钮。",),
    related=("tpl_regen_by_check", "act_accept", "act_postpone", "act_regen_manual"),
))

_reg(ParamHelp(
    key="act_regen_manual",
    name_cn="重新生成",
    name_en="regenerate",
    kind=HelpKind.ACTION,
    effect=(
        "重新生成整章正文。按钮会自动选择正确的做法，不需要你判断用哪套模板：\n"
        "· 本章处于“待生成/出错”，**或生成对话已被清空**时：把该章大纲（独立保存的快照）"
        "重新作为 user 消息发给 AI，等同于重新生成一次；\n"
        "· 已有正文且对话还在时：弹出输入框收集你的额外指令（可为空），"
        "填入“手动重新生成的消息模板”后追加到生成对话。"
    ),
    constraints=(
        "不依赖检查结果，可在任意非忙状态使用。",
        "输入的指令不会经过任何解析，原样嵌入模板。",
        "点击后任务**排在队列末尾**，章节状态变为“排队中”，界面上会给出明确提示。",
        "大纲与对话分开保存：即使先“清空对话”再点本按钮，也能用同一份大纲正常重新生成。",
    ),
    advice=("用于“字数不够”“换个结局”等自由调整；想完全重来就先清空生成对话。",),
    related=("tpl_regen_manual", "act_regen_by_check", "act_clear_conversation"),
))

_reg(ParamHelp(
    key="act_regenerate_by_outline",
    name_cn="重新生成（按大纲）",
    name_en="regenerate from outline",
    kind=HelpKind.ACTION,
    effect=(
        "“重新生成”按钮在**待生成 / 出错 / 生成对话已清空**时显示的形态："
        "直接把该章大纲作为 user 消息发给 AI 重新写一遍，不弹输入框。"
    ),
    constraints=(
        "使用独立保存的章节大纲快照（chapters.json 的 outline_snapshot），因此清空对话后依然可用。",
        "不会改动检查结果与历次检查历史。",
    ),
    advice=("想从零重写整章时用它；只想微调时用不带“（按大纲）”字样的那次点击。",),
    related=("act_regen_manual", "act_clear_conversation", "act_generate"),
))

_reg(ParamHelp(
    key="act_accept",
    name_cn="通过",
    name_en="accept / pass",
    kind=HelpKind.ACTION,
    effect=(
        "把本章标记为“合格”。导出时已合格章节不会被添加【未确认】标注。"
        "按钮位于章节视图的决策行，**正文视图与检查视图都能看到**。"
    ),
    constraints=(
        "**只有章节处于“待决策”状态时才显示**（其它状态整行都不出现）。",
        "生成对话视图下不显示，需要决策时切回正文或检查视图即可。",
    ),
    advice=("满意当前正文时点它；之后仍可继续重生成（状态会回到生成中）。",),
    related=("act_postpone", "exp_mode", "act_regen_by_check"),
))

_reg(ParamHelp(
    key="act_postpone",
    name_cn="暂不处理",
    name_en="postpone",
    kind=HelpKind.ACTION,
    effect="保持当前状态不变，稍后再决定。不会修改正文、检查结果或状态。",
    constraints=("仅当章节处于“待决策”状态时可用。",),
    advice=("可以先处理其他章节，最后统一回来决策。",),
    related=("act_accept", "act_regen_by_check"),
))

_reg(ParamHelp(
    key="act_edit_content",
    name_cn="编辑正文",
    name_en="edit content",
    kind=HelpKind.ACTION,
    effect="直接编辑将用于导出的章节正文文本（纯文本，程序不做任何解析或格式转换）。",
    constraints=(
        "导出始终取该文本框中的内容，而不是模型原始返回；一旦手动修改，后续重新生成会覆盖它。",
        "编辑不会改变对话历史。",
    ),
    advice=("导出前做少量润色时可以在这里改。",),
    related=("act_view_history", "exp_mode"),
))

_reg(ParamHelp(
    key="act_send_manual",
    name_cn="对话内手动发送",
    name_en="send message",
    kind=HelpKind.ACTION,
    effect="把你输入框中的文本作为一条 user 消息直接发送（**不经过任何模板**），并在该对话中继续多轮交互。",
    constraints=(
        "仅当章节处于非忙状态（已停止或生成/检查已完成）时可用。",
        "该消息会进入该对话的上下文，影响后续生成。",
        "若该对话有 tools 参数才需要回传 reasoning_content；本项目不带 tools，因此思维链不回传。",
    ),
    advice=("用于追问、微调或让模型解释某处设定。",),
    related=("act_view_history", "act_clear_conversation"),
))

_reg(ParamHelp(
    key="act_view_history",
    name_cn="查看消息历史",
    name_en="view history",
    kind=HelpKind.ACTION,
    effect="以只读方式展示该对话的完整 message history（含角色、来源、时间、token 用量，可切换 JSON 视图）。",
    constraints=(
        "思维链仅用于展示，**不会**回传给 API，也不参与导出。",
        "历史消息不可编辑、不可删除（需用“清空对话”）。",
    ),
    advice=("排查“模型为什么这么写”时查看此处。",),
    related=("act_clear_conversation",),
))

_reg(ParamHelp(
    key="act_clear_conversation",
    name_cn="清空对话",
    name_en="clear conversation",
    kind=HelpKind.ACTION,
    effect="删除该对话的全部消息并重新开始（保留对话本身的思考参数设置）。",
    constraints=(
        "需要二次确认；清空后该章此前的多轮上下文全部丢失。",
        "忙时不可用。",
    ),
    advice=("上下文被无关内容污染时使用；只想重生成正文请改用“手动重新生成”。",),
    related=("act_send_manual", "act_view_history"),
))

_reg(ParamHelp(
    key="act_switch_view",
    name_cn="切换视图",
    name_en="switch view",
    kind=HelpKind.ACTION,
    effect="在“正文 / 生成对话 / 检查视图”之间切换。默认显示“正文”，即最后一次生成的完整正文。",
    constraints=("任何状态都可自由切换。",),
    advice=("想观察流式输出过程时切到对应对话视图。",),
    related=("act_edit_content", "act_trigger_check"),
))

# =========================== 七、导出 ====================================== #

_reg(ParamHelp(
    key="exp_mode",
    name_cn="导出范围",
    name_en="export mode",
    kind=HelpKind.CHOICE,
    choices=("导出全部", "仅导出已合格章节"),
    default_text="导出全部",
    effect="决定导出哪些章节：全部章节，或只导出你点过“通过”的合格章节。",
    constraints=(
        "“未确认”指状态不是“合格”的章节（待生成/生成中/待检查/检查中/待决策/出错）。",
        "仅导出已合格章节时，未合格章节会被整体跳过。",
        "导出内容是各章**生成正文按顺序合并**，程序不加标题、不加定位行。",
    ),
    advice=("通读阶段用“导出全部”，准备交稿时用“仅导出已合格章节”。",),
    related=("exp_mark_text", "act_accept"),
))

_reg(ParamHelp(
    key="exp_mark_text",
    name_cn="未确认标记文本",
    name_en="unconfirmed mark",
    kind=HelpKind.TEXT,
    range_text="任意文本，默认【未确认】",
    default_text="【未确认】",
    effect="开启“在未确认章节正文前单独加一行标记”后，用这段文本标记未确认章节，便于在成品里搜索定位。",
    constraints=("只单独加一行，不修改正文任何内容。",),
    advice=("可用【未确认】或 [待审]，便于全文档搜索。",),
    related=("exp_mode", "exp_append_check", "exp_mark_confirmed"),
))

_reg(ParamHelp(
    key="exp_mark_confirmed",
    name_cn="加未确认标记行",
    name_en="mark unconfirmed",
    kind=HelpKind.BOOL,
    default_text="关闭",
    effect="开启后在未确认章节的正文**之前**单独写一行标记文本；关闭时导出内容就是合并后的纯生成正文。",
    constraints=(
        "程序**不会**再给每章补“【第 N 章】章节名”这类定位行，也不会加章节标题——"
        "生成的正文里本来就带标题。",
        "默认关闭，保证导出结果与正文完全一致。",
    ),
    advice=("准备交稿/发布时保持关闭；需要通读定位待办时再临时开启。",),
    related=("exp_mark_text", "exp_mode"),
))

_reg(ParamHelp(
    key="exp_append_check",
    name_cn="附最后一次检查结果",
    name_en="append check result",
    kind=HelpKind.BOOL,
    default_text="开启",
    effect="在未确认章节的正文之后，追加一段“最后一次检查结果”，供你后续参考。",
    constraints=(
        "只对未确认章节生效；已合格章节不会附加。",
        "追加内容明确标注为参考信息，与正文区分。",
    ),
    advice=("交稿或发布前记得关闭本项。",),
    related=("exp_mode", "exp_mark_text"),
))

_reg(ParamHelp(
    key="exp_dir",
    name_cn="默认导出目录",
    name_en="default export dir",
    kind=HelpKind.TEXT,
    default_text="我的文档",
    effect="导出对话框默认打开的目录；也可以在设置里随时改成任意文件夹。",
    constraints=("只影响默认值，导出时仍可在对话框里另选路径与文件名。",),
    advice=("建议为每部小说建一个目录，导出文件就不会和其它文件混在一起。",),
    related=("exp_path", "project_dir"),
))

_reg(ParamHelp(
    key="exp_separator",
    name_cn="章节间插入空行",
    name_en="separator",
    kind=HelpKind.BOOL,
    default_text="开启",
    effect="在章节之间插入空行，提升可读性。",
    constraints=("不影响正文内部任何格式。",),
    advice=("保持开启即可。",),
    related=("exp_mode",),
))

_reg(ParamHelp(
    key="exp_encoding",
    name_cn="文件编码",
    name_en="encoding",
    kind=HelpKind.CHOICE,
    choices=("utf-8-sig", "utf-8", "gbk"),
    default_text="utf-8-sig",
    effect="决定导出文件的编码。utf-8-sig 带 BOM，Windows 记事本打开中文不会乱码。",
    constraints=("选择 gbk 时，超出 GBK 字符集的生僻字可能报错。",),
    advice=("Windows 环境保持 utf-8-sig；需要喂给老式软件时用 gbk。",),
    related=("exp_path",),
))

_reg(ParamHelp(
    key="exp_path",
    name_cn="导出路径",
    name_en="export path",
    kind=HelpKind.TEXT,
    default_text="<默认导出目录>\\<小说名>.txt",
    effect="导出文件的保存位置与文件名。**默认文件名就是当前小说项目名**（如「星海归途.txt」）。",
    constraints=(
        "默认目录取“设置 → 界面与流程 → 默认导出目录”，**不是**项目文件夹。",
        "若目标文件已存在会询问是否覆盖。",
    ),
    advice=("文件名建议带上日期或书名，便于区分多次导出。",),
    related=("exp_encoding", "exp_dir", "project_dir"),
))

_reg(ParamHelp(
    key="exp_preview",
    name_cn="导出预览",
    name_en="preview",
    kind=HelpKind.ACTION,
    effect="按当前选项模拟导出，显示“共 N 章、导出 M 章、跳过 K 章”以及开头片段。",
    constraints=("只生成预览文本，不写文件。",),
    advice=("导出前先看一眼，确认标注与范围符合预期。",),
    related=("exp_mode", "exp_open_after"),
))

_reg(ParamHelp(
    key="exp_open_after",
    name_cn="导出后打开所在目录",
    name_en="open folder",
    kind=HelpKind.BOOL,
    default_text="开启",
    effect="导出成功后自动在资源管理器中打开文件所在目录并选中文件。",
    constraints=("仅使用系统默认的文件管理器，不执行任何其他命令。",),
    advice=("保持开启即可。",),
    related=("exp_path",),
))

# =========================== 八、状态栏与通用 ============================== #

_reg(ParamHelp(
    key="act_queue_panel",
    name_cn="任务队列面板",
    name_en="job queue panel",
    kind=HelpKind.ACTION,
    effect=(
        "打开右侧的任务队列面板：上半部分显示正在运行的任务，下半部分按**执行优先级**"
        "列出排队中的任务（最上面的最先执行）。"
    ),
    constraints=(
        "**拖动条目即可调整优先级**；也可以用置顶/上移/下移/移出队列按钮。",
        "调整优先级只影响还没开始的任务，不会打断正在运行的任务。",
        "队列顺序默认按「先提交先执行」（FIFO）维护；同一章同时只会有一个任务。",
    ),
    advice=("批量生成很多章时打开它，把想先看的章节拖到最上面。",),
    related=("state_queued", "concurrency", "act_stop_all", "status_queue"),
))

_reg(ParamHelp(
    key="status_queue",
    name_cn="队列 / 运行中",
    name_en="queue / running",
    kind=HelpKind.DISPLAY,
    effect="显示正在排队的任务数与正在执行的任务数；排队任务会在并发数允许时自动开始。",
    constraints=(
        "并发数调小后，溢出的任务会留在队列中等待，不会丢失。",
        "已提交但还没轮到的章节状态显示为“排队中”。",
    ),
    advice=("队列长时间不下降时，检查是否触发了 429 限速。",),
    related=("concurrency",),
))

_reg(ParamHelp(
    key="status_token",
    name_cn="Token 累计",
    name_en="token usage",
    kind=HelpKind.DISPLAY,
    effect="本会话内所有请求的 token 用量累加（来自 API 返回的 usage，不做估算），并单独显示思维链 token 数。",
    constraints=(
        "思维链 token 计入 completion_tokens，因此同样计费。",
        "计费还受时段影响：高峰时段为北京时间周一至周五 9:00-12:00、14:00-18:00，其余为空闲时段（价格减半）。",
    ),
    advice=("用 max_tokens 与思考强度控制成本。",),
    related=("max_tokens", "reasoning_effort"),
))

_reg(ParamHelp(
    key="status_context_usage",
    name_cn="上下文占用（估算）",
    name_en="context usage",
    kind=HelpKind.DISPLAY,
    effect="估算当前对话的输入 + 已生成 token 占 1M 上下文窗口的比例，仅作提示。",
    constraints=(
        "估算按字符数粗略换算，与官方 tokenizer 结果可能有偏差。",
        "真正触发 400 的判定以服务端为准；程序在发送前会用该估算做一次预算保护。",
    ),
    advice=("每章独立对话时几乎不会接近上限，可忽略。",),
    related=("context_window", "max_tokens"),
))

_reg(ParamHelp(
    key="status_config_path",
    name_cn="配置文件位置",
    name_en="config path",
    kind=HelpKind.DISPLAY,
    effect="显示实际生效的 config.json 路径。免安装版优先放在 exe 同目录；若该目录不可写则回退到 %APPDATA%\\NovelGen。",
    constraints=("配置以明文 JSON 保存，包含 api_key，请注意保管。",),
    advice=("备份该文件即可备份全部设置（含提示词与消息模板）。",),
    related=("api_key",),
))

_reg(ParamHelp(
    key="app_about",
    name_cn="关于 / 帮助",
    name_en="about",
    kind=HelpKind.ACTION,
    effect="显示版本信息、快捷键、设计原则与参数默认值的文档依据。",
    constraints=("程序不做任何 AI 文本的语义解析，仅做字符串替换、状态流转与界面展示。",),
    advice=("第一次使用建议先读“参数说明总览”。",),
    related=("sampling_note",),
))

_reg(ParamHelp(
    key="act_show_log",
    name_cn="运行日志",
    name_en="run log",
    kind=HelpKind.ACTION,
    effect=(
        "在**独立窗口**中打开运行日志（任务状态、API 提示、用量与错误信息）。"
        "日志默认不显示，只有点击这个菜单项时才打开，不会占用主界面空间。"
    ),
    constraints=(
        "日志最多保留最近 1000 条，超出自动丢弃最早的记录。",
        "出现警告或错误时会自动弹出日志窗口，方便第一时间看到原因。",
    ),
    advice=("程序行为不符合预期时先看日志，通常会直接写明原因。",),
    related=("status_queue", "status_token"),
))

_reg(ParamHelp(
    key="state_queued",
    name_cn="排队中",
    name_en="queued",
    kind=HelpKind.DISPLAY,
    effect=(
        "章节的生成/检查任务已提交，正排在队列末尾等待开始；"
        "轮到它时状态会自动变为“生成中”或“检查中”。"
    ),
    constraints=(
        "排队中的任务被“停止本章/停止全部”取消后，章节会回到提交前的状态。",
        "并发数（同时进行的任务数）决定同时有几个任务在跑，其余都在排队。",
    ),
    advice=("任务很多时可以在状态栏看队列长度，或调大并发数。",),
    related=("concurrency", "act_stop_all", "status_queue"),
))


# --------------------------------------------------------------------------- #
# 读取接口
# --------------------------------------------------------------------------- #
_PLACEHOLDER = ParamHelp(
    key="__missing__",
    name_cn="暂无说明",
    name_en="no help",
    kind=HelpKind.GROUP,
    effect="该参数尚未在 param_help.py 中登记说明。",
    advice=("请在 core/param_help.py 中补充对应 key 的说明。",),
)


def get_help(key: str) -> ParamHelp:
    """按 key 读取参数说明。不存在时返回占位说明，绝不抛异常。"""
    item = PARAM_HELP.get(key)
    if item is None:
        return ParamHelp(
            key=key,
            name_cn=_PLACEHOLDER.name_cn,
            name_en=key,
            kind=_PLACEHOLDER.kind,
            effect=_PLACEHOLDER.effect,
            advice=_PLACEHOLDER.advice,
        )
    return item


def tooltip_text(key: str) -> str:
    return get_help(key).tooltip()


def html_text(key: str) -> str:
    return get_help(key).to_html()


def plain_text(key: str) -> str:
    return get_help(key).to_plain()


def all_keys() -> list[str]:
    return sorted(PARAM_HELP)


def missing_help_keys(used_keys: Iterable[str]) -> list[str]:
    """自检：界面上用到但未登记说明的 key。"""
    return sorted({k for k in used_keys if k not in PARAM_HELP})
