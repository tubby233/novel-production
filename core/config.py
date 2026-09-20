"""配置读写与默认值。

* 所有默认值集中在这里，并且与 ``param_help.py`` 的说明保持一致。
* 模型能力上限（上下文 / 单次输出 / 并发）来自 DeepSeek 官方文档，集中定义便于日后更新：
    模型 & 价格        https://api-docs.deepseek.com/zh-cn/quick_start/pricing
    Chat Completions  https://api-docs.deepseek.com/zh-cn/api/create-chat-completion
    限速与隔离        https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit
* 加载时做"字段级缺省填充 + 类型纠正"，用户手改坏了配置文件也能正常启动，
  并把问题记入警告列表由界面提示。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

from . import message_templates as MT
from . import paths
from .models import ThinkingConfig, normalize_effort

CONFIG_VERSION = 1


# --------------------------------------------------------------------------- #
# 模型能力上限（依据官方文档）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ModelLimits:
    model_id: str
    display_name: str
    context_window: int
    max_output: int
    concurrency: int
    notes: str = ""


MODEL_LIMITS: dict[str, ModelLimits] = {
    "deepseek-flash": ModelLimits(
        model_id="deepseek-flash",
        display_name="DeepSeek-V4.1-Flash",
        context_window=1_048_576,   # 1M
        max_output=393_216,         # 384K
        concurrency=2500,
        notes="支持思考模式与非思考模式；支持图像理解。",
    ),
    "deepseek-v4-pro": ModelLimits(
        model_id="deepseek-v4-pro",
        display_name="DeepSeek-V4-Pro-0813",
        context_window=1_048_576,
        max_output=393_216,
        concurrency=500,
        notes="不支持图像理解。",
    ),
}

#: 默认模型
DEFAULT_MODEL = "deepseek-flash"

#: 输入 token 估算系数：中文约 1 token ≈ 1.6 字符。仅用于预算保护与状态栏提示。
CHARS_PER_TOKEN = 1.6


def model_limits(model_id: str) -> ModelLimits:
    """取模型能力。未知模型回落到默认模型的上限，并在界面上提示以服务端为准。"""
    return MODEL_LIMITS.get(model_id.strip(), MODEL_LIMITS[DEFAULT_MODEL])


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text or "") / CHARS_PER_TOKEN))


# --------------------------------------------------------------------------- #
# 配置数据类
# --------------------------------------------------------------------------- #
@dataclass
class ApiConfig:
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = DEFAULT_MODEL
    user_id: str = ""
    timeout_seconds: int = 1800


@dataclass
class SamplingConfig:
    # 思考模式下不生效（设置不报错，API 忽略）
    temperature: float = 1.0
    # 思考模式下限 0.95；非思考模式恒为 1.0
    top_p: float = 1.0
    # 默认取上限：1 ~ 393216（384K）
    max_tokens: int = 393_216
    # 账号级并发上限：deepseek-flash 2500 / deepseek-v4-pro 500
    # 注意：frequency_penalty / presence_penalty 已被 DeepSeek 官方标记为 deprecated
    #      （"传入该参数将不会产生任何效果"），因此本程序不再提供、也不发送这两个参数。
    concurrency: int = 8


@dataclass
class PromptConfig:
    system_generate: str = MT.DEFAULT_GENERATE_SYSTEM
    system_check: str = MT.DEFAULT_CHECK_SYSTEM   # 默认空：程序不预设检查提示词


@dataclass
class ThinkingDefaults:
    generate: ThinkingConfig = field(
        default_factory=lambda: ThinkingConfig(enabled=True, effort="high", show_reasoning=True)
    )
    #: 检查对话默认**关闭**深度思考（检查只是核对文本，开启思考会显著增加耗时与费用）。
    #: 需要更深度的检查时，在"检查视图 -> 对话设置"里单独打开即可。
    check: ThinkingConfig = field(
        default_factory=lambda: ThinkingConfig(enabled=False, effort="max", show_reasoning=True)
    )


@dataclass
class UiConfig:
    global_hide_reasoning: bool = False          # 全局隐藏思维链（默认关闭）
    reasoning_collapse_tokens: int = 2000        # 超过该 token 数自动收起
    auto_check_after_generate: bool = True
    chapter_pattern: str = ""                    # 空 -> 使用 outline_parser 默认正则
    export_mode: str = "all_marked"              # all_marked | passed_only
    export_mark_text: str = "【未确认】"
    export_append_check: bool = True
    export_separator: bool = True
    export_encoding: str = "utf-8-sig"
    export_open_after: bool = True
    #: 默认导出目录（可在设置里修改；留空表示"我的文档"）
    export_dir: str = ""
    #: 小说项目根目录（每个项目 = 其下一个以小说名命名的文件夹）
    project_dir: str = ""
    window_geometry: str = ""                    # base64 化的 QByteArray
    last_outline: str = ""                       # 上次的大纲文本（便于继续工作）
    last_project: str = ""                       # 上次打开的项目文件夹（便于下次自动读取）


@dataclass
class AppConfig:
    api: ApiConfig = field(default_factory=ApiConfig)
    sampling: SamplingConfig = field(default_factory=SamplingConfig)
    prompts: PromptConfig = field(default_factory=PromptConfig)
    thinking: ThinkingDefaults = field(default_factory=ThinkingDefaults)
    ui: UiConfig = field(default_factory=UiConfig)
    #: key = TemplateKind 的字符串值；缺 key 表示使用内置默认模板
    templates: dict[str, str] = field(default_factory=dict)
    version: int = CONFIG_VERSION

    # ---------------- 便捷读取 ---------------- #
    def template(self, kind: MT.TemplateKind | str) -> str:
        """取模板正文：用户配置优先，否则内置默认。"""
        key = str(kind)
        text = self.templates.get(key)
        if text is None or text == "":
            return MT.default_template(key)
        return text

    def is_template_customized(self, kind: MT.TemplateKind | str) -> bool:
        return str(kind) in self.templates

    def limits(self) -> ModelLimits:
        return model_limits(self.api.model)


# --------------------------------------------------------------------------- #
# 序列化 / 反序列化（手写，便于容错）
# --------------------------------------------------------------------------- #
def _as_int(value: Any, default: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        result = int(float(value))
    except (TypeError, ValueError):
        return default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _as_float(value: Any, default: float, lo: float | None = None, hi: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if lo is not None:
        result = max(lo, result)
    if hi is not None:
        result = min(hi, result)
    return result


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "是", "开")
    return default


def _as_str(value: Any, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def config_to_dict(cfg: AppConfig) -> dict:
    def dump(obj: Any) -> Any:
        if is_dataclass(obj) and not isinstance(obj, type):
            return {f.name: dump(getattr(obj, f.name)) for f in fields(obj)}
        if isinstance(obj, dict):
            return {k: dump(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [dump(v) for v in obj]
        return obj

    return dump(cfg)


def config_from_dict(data: dict | None, warnings: list[str] | None = None) -> AppConfig:
    """从 dict 构造配置，容错并记录警告。"""
    warn = warnings if warnings is not None else []
    cfg = AppConfig()
    if not isinstance(data, dict):
        warn.append("配置文件内容不是合法对象，已使用全部默认值。")
        return cfg

    raw_version = data.get("version", CONFIG_VERSION)
    cfg.version = _as_int(raw_version, CONFIG_VERSION)
    if cfg.version != CONFIG_VERSION:
        warn.append(
            f"配置版本为 {cfg.version}，当前程序版本为 {CONFIG_VERSION}；缺失项已用默认值填充。"
        )

    api = data.get("api") or {}
    if isinstance(api, dict):
        cfg.api = ApiConfig(
            api_key=_as_str(api.get("api_key"), ""),
            base_url=_as_str(api.get("base_url"), ApiConfig.base_url) or ApiConfig.base_url,
            model=_as_str(api.get("model"), DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            user_id=_as_str(api.get("user_id"), ""),
            timeout_seconds=_as_int(api.get("timeout_seconds"), 1800, 30, 7200),
        )
    elif api:
        warn.append("api 配置段格式异常，已使用默认值。")

    sampling = data.get("sampling") or {}
    if isinstance(sampling, dict):
        limits = cfg.limits()
        cfg.sampling = SamplingConfig(
            temperature=_as_float(sampling.get("temperature"), 1.0, 0.0, 2.0),
            top_p=_as_float(sampling.get("top_p"), 1.0, 0.0, 1.0),
            max_tokens=_as_int(sampling.get("max_tokens"), 393_216, 1, limits.max_output),
            concurrency=_as_int(sampling.get("concurrency"), 8, 1, 2500),
        )
        # 旧版本配置里的 frequency_penalty / presence_penalty 已弃用：静默忽略
        for legacy in ("frequency_penalty", "presence_penalty"):
            if legacy in sampling:
                warn.append(f"已忽略 {legacy}：DeepSeek 官方已弃用该参数（传入无任何效果）。")
    elif sampling:
        warn.append("sampling 配置段格式异常，已使用默认值。")

    prompts = data.get("prompts") or {}
    if isinstance(prompts, dict):
        cfg.prompts = PromptConfig(
            system_generate=_as_str(prompts.get("system_generate"), MT.DEFAULT_GENERATE_SYSTEM),
            system_check=_as_str(prompts.get("system_check"), ""),
        )

    thinking = data.get("thinking") or {}
    if isinstance(thinking, dict):
        cfg.thinking = ThinkingDefaults(
            generate=ThinkingConfig.from_dict(thinking.get("generate"), cfg.thinking.generate),
            check=ThinkingConfig.from_dict(thinking.get("check"), cfg.thinking.check),
        )

    ui = data.get("ui") or {}
    if isinstance(ui, dict):
        cfg.ui = UiConfig(
            global_hide_reasoning=_as_bool(ui.get("global_hide_reasoning"), False),
            reasoning_collapse_tokens=_as_int(ui.get("reasoning_collapse_tokens"), 2000, 200, 60000),
            auto_check_after_generate=_as_bool(ui.get("auto_check_after_generate"), True),
            chapter_pattern=_as_str(ui.get("chapter_pattern"), ""),
            export_mode=_as_str(ui.get("export_mode"), "all_marked"),
            export_mark_text=_as_str(ui.get("export_mark_text"), "【未确认】"),
            export_append_check=_as_bool(ui.get("export_append_check"), True),
            export_separator=_as_bool(ui.get("export_separator"), True),
            export_encoding=_as_str(ui.get("export_encoding"), "utf-8-sig"),
            export_open_after=_as_bool(ui.get("export_open_after"), True),
            export_dir=_as_str(ui.get("export_dir"), ""),
            project_dir=_as_str(ui.get("project_dir"), ""),
            window_geometry=_as_str(ui.get("window_geometry"), ""),
            last_outline=_as_str(ui.get("last_outline"), ""),
            last_project=_as_str(ui.get("last_project"), ""),
        )
        # 旧版本配置里的"每章正文前加标题"已被移除（正文本身自带标题，加了会重复）：
        # 这里静默忽略，不再提示，避免每次启动都弹窗。

    templates = data.get("templates") or {}
    if isinstance(templates, dict):
        cleaned: dict[str, str] = {}
        for key, value in templates.items():
            if key in {str(k) for k in MT.TemplateKind} and isinstance(value, str):
                cleaned[key] = value
            else:
                warn.append(f"忽略无法识别的模板项：{key}")
        cfg.templates = cleaned
    return cfg


# --------------------------------------------------------------------------- #
# 文件读写
# --------------------------------------------------------------------------- #
def load_config() -> tuple[AppConfig, list[str]]:
    """读取配置。返回 (配置, 警告列表)。

    * 配置文件**不存在或为空**（例如被手动删掉）时，返回默认配置，
      并**立刻在数据目录里重新生成一份 config.json**——这样"删掉配置文件"
      就等于"恢复出厂设置"，而且目录里马上又有一份可编辑的完整配置。
    * 文件损坏（非法 JSON）时备份为 ``.bak`` 并同样重新生成一份默认配置。
    """
    path = paths.config_path()
    if not path.exists():
        return AppConfig(), _regenerate_config(path, reason="配置文件不存在")
    warnings: list[str] = []
    try:
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        warnings.append(f"配置文件不是合法 JSON（{exc}）；已使用默认值，原文件已备份为 .bak。")
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except Exception:
            pass
        warnings.extend(_regenerate_config(path, reason="配置文件损坏"))
        return AppConfig(), warnings
    except OSError as exc:
        warnings.append(f"读取配置文件失败：{exc}；已使用默认值。")
        return AppConfig(), warnings
    if not isinstance(data, dict) or not data:
        warnings.extend(_regenerate_config(path, reason="配置文件内容为空"))
        return AppConfig(), warnings
    return config_from_dict(data, warnings), warnings


def _regenerate_config(path, *, reason: str) -> list[str]:
    """把当前默认配置写回磁盘，返回给用户看的提示（写失败也返回空列表）。"""
    try:
        save_config(AppConfig())
    except OSError as exc:
        return [f"{reason}；无法写入新的配置文件（{exc}），本次使用内存中的默认值。"]
    return [f"{reason}；已在 {path} 重新生成一份默认配置文件。"]


def save_config(cfg: AppConfig) -> Path:
    """原子化保存配置，返回实际写入路径。

    正常情况下写入 :func:`paths.config_path()`；如果那个目录不可写
    （例如程序被放在只读位置），回退到数据目录，保证配置永远能存下来。
    """
    path = paths.config_path()
    try:
        return _write_config(cfg, path)
    except OSError:
        fallback = paths.user_data_dir() / paths.CONFIG_FILENAME
        if fallback == path:
            raise
        return _write_config(cfg, fallback)


def _write_config(cfg: AppConfig, path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(config_to_dict(cfg), ensure_ascii=False, indent=2)
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)
    return path


def export_profile(cfg: AppConfig, path: str | Path) -> Path:
    target = Path(path)
    target.write_text(
        json.dumps(config_to_dict(cfg), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return target


def import_profile(path: str | Path) -> tuple[AppConfig, list[str]]:
    target = Path(path)
    warnings: list[str] = []
    data = json.loads(target.read_text(encoding="utf-8"))
    return config_from_dict(data, warnings), warnings


def reset_all() -> AppConfig:
    return AppConfig()


def thinking_defaults_for(cfg: AppConfig, kind: str) -> ThinkingConfig:
    """按对话类型取默认思考参数。"""
    base = cfg.thinking.generate if kind == "generate" else cfg.thinking.check
    return ThinkingConfig(
        enabled=base.enabled,
        effort=normalize_effort(base.effort),
        show_reasoning=base.show_reasoning,
    )


def resolve_export_dir(cfg: AppConfig) -> str:
    """默认导出目录：配置 > 我的文档。永远返回一个可用的绝对路径字符串。"""
    configured = (cfg.ui.export_dir or "").strip()
    if configured:
        return configured
    return str(paths.default_export_dir())


def resolve_project_dir(cfg: AppConfig) -> str:
    """小说项目根目录：配置 > 我的文档下的 NovelGenProjects。"""
    configured = (cfg.ui.project_dir or "").strip()
    if configured:
        return configured
    return str(paths.default_project_dir())
