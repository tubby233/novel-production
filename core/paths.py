"""路径解析：兼容源码运行、PyInstaller onedir（绿色文件夹）与 onefile（单文件）。

数据目录（config.json / chapters.json）的选择顺序：

1. 环境变量 ``NOVELGEN_DATA_DIR``（把程序放在只读位置时的兜底，指向可写目录）；
2. **exe 同目录**（绿色版首选：整个文件夹拷到 U 盘或任意电脑都能带着设置走）；
3. ``%APPDATA%\\NovelGen``（上述两者都不可写时的回退）。

onefile 构建下 ``sys.executable`` 位于临时解包目录 ``_MEIPASS`` 内，
此时"exe 同目录"要取它的上一级，才能落到真正的 exe 旁边。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "NovelGen"
CONFIG_FILENAME = "config.json"
LOG_FILENAME = "novel_gen.log"

#: 覆盖数据目录的环境变量名
DATA_DIR_ENV = "NOVELGEN_DATA_DIR"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包产物中。"""
    return bool(getattr(sys, "frozen", False))


def is_onefile() -> bool:
    """是否是 onefile 构建（运行时把依赖解包到 _MEIPASS 临时目录）。

    注意：PyInstaller 6 的 **onedir 也会设置 _MEIPASS**（指向 exe 同级的
    ``_internal``），所以不能只看 _MEIPASS。判定依据是"exe 同级是否存在
    _internal 目录"：存在即 onedir（依赖在磁盘上，未解包到临时目录）。
    """
    if not is_frozen():
        return False
    internal = Path(sys.executable).resolve().parent / "_internal"
    return not internal.is_dir()


def app_dir() -> Path:
    """程序所在目录。

    * 源码运行：项目根目录；
    * onedir：exe 所在目录（即绿色文件夹根，``_internal`` 的同级）；
    * onefile：解包目录的上一级（真正的 exe 位置）。
    """
    if is_frozen():
        exe = Path(sys.executable).resolve()
        if is_onefile():
            meipass = Path(str(getattr(sys, "_MEIPASS"))).resolve()
            # _MEIPASS 形如 ...\_MEIxxxxxx，其父目录才是 exe 所在目录
            if meipass != exe.parent and meipass.parent != exe.parent:
                return meipass.parent
        return exe.parent
    # core/paths.py -> core -> 项目根
    return Path(__file__).resolve().parent.parent


def resource_path(*parts: str) -> Path:
    """只读资源（图标、样式表）。

    onedir 下资源随依赖一起放在 ``_internal`` 里（PyInstaller 6 默认布局），
    ``sys._MEIPASS`` 会指向它；源码运行时指向项目根目录。
    """
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else app_dir()
    return root.joinpath(*parts)


def user_data_dir() -> Path:
    """用户数据目录（回退位置）。"""
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if base:
        return Path(base) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def _is_writable(directory: Path) -> bool:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def config_dir() -> Path:
    """数据目录：环境变量 > exe 同目录 > %APPDATA%\\NovelGen。"""
    override = os.environ.get(DATA_DIR_ENV, "").strip()
    if override:
        target = Path(override).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
            return target
        except Exception:
            pass  # 覆盖路径不可用时继续走下面的默认逻辑

    preferred = app_dir()
    if _is_writable(preferred):
        return preferred

    fallback = user_data_dir()
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def config_path() -> Path:
    return config_dir() / CONFIG_FILENAME


def log_path() -> Path:
    return config_dir() / LOG_FILENAME


def default_export_dir() -> Path:
    docs = Path.home() / "Documents"
    return docs if docs.exists() else Path.home()


def default_export_path() -> Path:
    return default_export_dir() / "novel_export.txt"


#: 默认的小说项目根目录名（用户可在设置里改成任意目录）
PROJECTS_DIRNAME = "NovelGenProjects"


def default_project_dir() -> Path:
    """默认项目根目录：我的文档下的 NovelGenProjects（不存在时会自动创建）。"""
    return default_export_dir() / PROJECTS_DIRNAME


def describe_layout() -> str:
    """给"关于"与启动日志用的一行说明：当前运行形态与数据目录。"""
    if not is_frozen():
        mode = "源码运行"
    elif is_onefile():
        mode = "打包版（单文件 onefile）"
    else:
        mode = "打包版（绿色文件夹 onedir）"
    return f"{mode}｜程序目录：{app_dir()}｜数据目录：{config_dir()}"
