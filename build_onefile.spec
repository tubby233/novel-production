# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置 —— **单文件版（onefile）**，可选用法。

产出：``dist/NovelGen.exe``（单文件，运行时把依赖解包到 %TEMP%）。

打包命令：
    pyinstaller build_onefile.spec --noconfirm

取舍：单文件便于传输，但每次启动都要解包 Qt（首次启动较慢），
且部分杀毒软件对"自解压"行为更敏感。**推荐优先使用 build.spec（绿色文件夹版）**。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH)  # noqa: F821
ENTRY = ROOT / "main.py"
APP_NAME = "NovelGen"

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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "assets" / "icon.ico") if (ROOT / "assets" / "icon.ico").exists() else None,
)
