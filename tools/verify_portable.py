"""验证打包后的绿色文件夹（不需要目标机器安装 Python）。

用法：
    python tools/verify_portable.py                    # 默认检查 dist/NovelGen
    python tools/verify_portable.py D:\\some\\NovelGen  # 检查指定文件夹

检查内容：
1. 文件夹结构与关键文件是否存在（exe、_internal、Qt 平台插件、证书、资源）；
2. 用子进程运行 exe 的自检开关（NOVELGEN_SELFCHECK=1），读取 selfcheck.txt；
3. 明确提示"移动后仍可运行"的关键判定依据。

注意：自检开关是 exe 内建的（见 main.py），不会弹出界面；
用 offscreen 平台运行，因此无需显示器。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIST = ROOT / "dist" / "NovelGen"
FAILURES: list[str] = []


def check(condition: bool, message: str, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {message}" + (f" — {detail}" if detail else ""))
    else:
        FAILURES.append(message)
        print(f"[FAIL] {message}" + (f" — {detail}" if detail else ""))


def check_layout(dist: Path) -> None:
    print(f"\n=== 1) 文件夹结构：{dist} ===")
    exe = dist / "NovelGen.exe"
    check(exe.exists(), "存在 NovelGen.exe", str(exe))

    internal = dist / "_internal"
    check(internal.is_dir(), "存在 _internal（运行库目录）")

    qt_plugins = internal / "PySide6" / "plugins" / "platforms"
    if not qt_plugins.is_dir():
        # PyInstaller 6 可能把 Qt 插件放在 _internal 根下的其它位置
        candidates = list(internal.glob("**/platforms/qwindows.dll"))
        check(bool(candidates), "找到 Qt 平台插件 qwindows.dll", str(candidates[:1]))
    else:
        check((qt_plugins / "qwindows.dll").exists(), "找到 Qt 平台插件 qwindows.dll")

    cert = list(internal.glob("**/certifi/cacert.pem"))
    check(bool(cert), "找到 CA 证书 cacert.pem（HTTPS 必需）", str(cert[:1]))

    style = list(internal.glob("**/assets/style.qss"))
    icon = list(internal.glob("**/assets/icon.ico"))
    check(bool(style), "找到界面样式 style.qss")
    check(bool(icon), "找到图标 icon.ico")

    python_dll = list(internal.glob("python3*.dll"))
    check(bool(python_dll), "找到内置 Python 解释器（目标机无需安装 Python）", str(python_dll[:1]))

    check((dist / "使用说明.txt").exists(), "根目录有 使用说明.txt")


def run_selfcheck(dist: Path) -> None:
    print("\n=== 2) 运行 exe 内建自检（无需显示器、不联网）===")
    exe = dist / "NovelGen.exe"
    if not exe.exists():
        check(False, "跳过自检：exe 不存在")
        return

    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["NOVELGEN_SELFCHECK"] = "1"
    # 让自检把 config.json / selfcheck.txt 写到被测文件夹里
    env["NOVELGEN_DATA_DIR"] = str(dist)

    report = dist / "selfcheck.txt"
    if report.exists():
        report.unlink()

    started = time.time()
    try:
        proc = subprocess.run(
            [str(exe)],
            cwd=str(dist),
            env=env,
            capture_output=True,
            timeout=180,
        )
        rc = proc.returncode
        stderr = proc.stderr.decode("utf-8", "replace") if proc.stderr else ""
    except subprocess.TimeoutExpired:
        check(False, "自检超时（180 秒）")
        return

    elapsed = time.time() - started
    check(rc == 0, f"exe 退出码为 0（实际 {rc}）", f"耗时 {elapsed:.1f}s")
    if stderr.strip():
        print("       stderr:", stderr.strip()[:400])

    if not report.exists():
        check(False, f"未生成 {report.name}")
        return
    text = report.read_text(encoding="utf-8", errors="replace")
    print("\n--- selfcheck.txt ---")
    print(text.rstrip())
    print("--- end ---\n")
    check("[FAIL]" not in text, "自检报告中没有任何 [FAIL]")
    check("全部通过" in text, "自检结论为“全部通过”")


def main() -> int:
    dist = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_DIST
    if not dist.exists():
        print(f"未找到目录：{dist}\n请先执行  python make_portable.py  生成绿色文件夹。")
        return 1

    print(f"被检查的绿色文件夹：{dist}")
    check_layout(dist)
    run_selfcheck(dist)

    print()
    if FAILURES:
        print(f"检查未通过（{len(FAILURES)} 项）：" + "；".join(FAILURES))
        return 1
    print("绿色文件夹检查全部通过：可整体拷贝到任意 Windows 10/11 电脑直接运行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
