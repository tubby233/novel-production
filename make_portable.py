"""一键生成"绿色便携文件夹"。

用法（在项目根目录执行）：
    python make_portable.py

它会做四件事：
1. 生成 assets/icon.ico（若不存在，用标准库画的图标，可自行替换）；
2. 调用 PyInstaller 按 build.spec 打出 onedir 产物到 dist/NovelGen/；
3. 把 packaging/便携版使用说明.txt 复制到文件夹根，命名为 使用说明.txt；
4. 打印产物清单与体积，并提示如何验证。

产出可以直接整体拷贝到任何 Windows 10 / 11 电脑上双击运行（无需 Python）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "NovelGen"
GUIDE_SRC = ROOT / "packaging" / "便携版使用说明.txt"
GUIDE_DST = DIST / "使用说明.txt"


def step(text: str) -> None:
    print(f"\n=== {text} ===")


def ensure_icon() -> None:
    icon = ROOT / "assets" / "icon.ico"
    if icon.exists():
        print(f"图标已存在：{icon}")
        return
    step("生成图标")
    subprocess.run([sys.executable, str(ROOT / "assets" / "make_icon.py")], check=True)


def build() -> None:
    step("调用 PyInstaller 打包（绿色文件夹版）")
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "build.spec",
        "--noconfirm",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
    ]
    print(" ".join(cmd))
    subprocess.run(cmd, check=True, cwd=str(ROOT))


def copy_guide() -> None:
    step("放入使用说明")
    if not GUIDE_SRC.exists():
        print(f"未找到 {GUIDE_SRC}，跳过。")
        return
    shutil.copy2(GUIDE_SRC, GUIDE_DST)
    print(f"已生成 {GUIDE_DST}")


def report() -> None:
    step("产物清单")
    exe = DIST / "NovelGen.exe"
    if not exe.exists():
        print(f"未找到 {exe}，打包可能失败。")
        return
    total = 0
    files = 0
    for path in DIST.rglob("*"):
        if path.is_file():
            files += 1
            total += path.stat().st_size
    print(f"文件夹：{DIST}")
    print(f"文件数：{files}｜总大小：{total / 1024 / 1024:.1f} MB")
    print("顶层内容：")
    for child in sorted(DIST.iterdir()):
        kind = "目录" if child.is_dir() else "文件"
        size = "" if child.is_dir() else f" ({child.stat().st_size / 1024:.0f} KB)"
        print(f"  [{kind}] {child.name}{size}")
    print("\n下一步：")
    print("  1) 直接双击 dist\\NovelGen\\NovelGen.exe 试用；")
    print("  2) 验证打包完整性（不需要 Python 环境）：")
    print("       set NOVELGEN_SELFCHECK=1 && dist\\NovelGen\\NovelGen.exe")
    print("     然后查看 dist\\NovelGen\\selfcheck.txt，末尾应为“全部通过”。")
    print("  3) 把整个 dist\\NovelGen 文件夹拷到任意 Win10/Win11 电脑即可运行。")


def main() -> int:
    ensure_icon()
    build()
    copy_guide()
    report()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
