# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— **绿色文件夹版（onedir）**，推荐用法。

产出：``dist/NovelGen/``，一个自带全部依赖的文件夹，可整体拷贝到任何
Windows 10 / 11 电脑（无需安装 Python、无需联网安装依赖）直接双击运行：

    dist/NovelGen/
      NovelGen.exe        ← 双击运行
      _internal/          ← Python 解释器 + PySide6(Qt) + openai/httpx + 证书 + 资源
      config.json         ← 首次运行自动生成（含 api_key，注意保管）
      chapters.json       ← 章节与对话数据（自动保存）
      使用说明.txt        ← 打包时自动放进去的简短说明

打包命令（在项目根目录执行）：
    pyinstaller build.spec --noconfirm

为什么用 onedir 而不是 onefile
-----------------------------
* onefile 每次启动都要把 Qt 解包到 %TEMP%，首次启动慢、杀软更容易误报；
* onedir 启动快、目录结构清晰，且天然就是"绿色文件夹"的形态。
  若仍想要单文件，用 ``build_onefile.spec``。

数据目录策略（见 core/paths.py）
------------------------------
优先写在 exe 同目录（绿色版带着设置走）；该目录不可写（例如放进
``C:\\Program Files``）时自动回退 ``%APPDATA%\\NovelGen``；
也可以用环境变量 ``NOVELGEN_DATA_DIR`` 显式指定。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)  # noqa: F821 - PyInstaller 注入
ENTRY = ROOT / "main.py"
APP_NAME = "NovelGen"

# --------------------------------------------------------------------------- #
# 隐式导入：openai / httpx / pydantic 在运行期动态 import，onefile/onedir 都容易漏
# --------------------------------------------------------------------------- #
hiddenimports: list[str] = []
for package in (
    "openai",
    "httpx",
    "httpcore",
    "anyio",
    "certifi",
    "pydantic",
    "pydantic_core",
    "sniffio",
    "h11",
    "idna",
):
    try:
        hiddenimports += collect_submodules(package)
    except Exception:
        pass

# --------------------------------------------------------------------------- #
# 数据文件：CA 证书（HTTPS 校验必需） + 程序自带资源
# --------------------------------------------------------------------------- #
datas: list[tuple[str, str]] = []
for package in ("certifi",):
    try:
        datas += collect_data_files(package)
    except Exception:
        pass
datas.append((str(ROOT / "assets"), "assets"))

a = Analysis(  # noqa: F821
    [str(ENTRY)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "scipy",
        "pandas",
        "PIL",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "IPython",
        "pytest",
        "setuptools",
        "pip",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,          # onedir：二进制交给 COLLECT
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                      # UPX 易被杀软误报，且可能损坏 Qt 插件
    console=False,                  # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.ico") if (ROOT / "assets" / "icon.ico").exists() else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
)
