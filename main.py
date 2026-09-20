"""程序入口。

* 纯 GUI + asyncio 引擎线程：UI 线程不阻塞。
* 启动即加载配置，配置文件损坏时降级为默认值并把问题提示给用户。
* 启动时执行自检：消息模板占位符、参数说明覆盖率。

打包后的自检开关
----------------
设置环境变量 ``NOVELGEN_SELFCHECK=1`` 后启动，程序会**不显示界面**，
只做一次"能不能跑"的自检（依赖导入、Qt 平台插件、配置/模板/参数表、主窗口构造），
把结果写入数据目录下的 ``selfcheck.txt`` 后退出（全部通过时退出码 0）。
这是在没有控制台的 windowed exe 上验证绿色文件夹是否完整的手段，正常使用不会触发。
"""

from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

# 以脚本方式运行时，保证能 import 到 core / gui（PyInstaller 下同样成立）
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtGui import QIcon  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from core import config as config_mod  # noqa: E402
from core import paths  # noqa: E402
from gui.main_window import MainWindow  # noqa: E402

APP_NAME = "小说生成器 · DeepSeek"
SELFCHECK_ENV = "NOVELGEN_SELFCHECK"
SELFCHECK_FILE = "selfcheck.txt"


def run_selfcheck(app: QApplication) -> int:
    """打包后可用性自检。返回进程退出码（0 = 全部通过）。"""
    lines: list[str] = [f"NovelGen 自检 {paths.describe_layout()}", ""]
    ok = True

    def step(name: str, func) -> None:
        nonlocal ok
        try:
            detail = func()
            lines.append(f"[OK]   {name}" + (f" — {detail}" if detail else ""))
        except Exception as exc:  # noqa: BLE001
            ok = False
            lines.append(f"[FAIL] {name} — {type(exc).__name__}: {exc}")
            lines.append(traceback.format_exc())

    step("核心依赖导入", lambda: _import_deps())
    step("Qt 平台插件", lambda: f"platform={app.platformName()}")
    step("资源文件", lambda: _check_assets())
    step("配置文件读写", lambda: str(config_mod.save_config(config_mod.AppConfig())))
    step("消息模板占位符", lambda: _check_templates())
    step("参数说明表", lambda: _check_param_help())
    step("章节切分", lambda: _check_outline())
    step("主窗口构造", lambda: _check_window(app))

    lines.append("")
    lines.append("结论：" + ("全部通过，程序可以正常使用。" if ok else "存在失败项（见上）。"))
    target = paths.config_dir() / SELFCHECK_FILE
    try:
        target.write_text("\n".join(lines), encoding="utf-8")
    except OSError:
        pass
    return 0 if ok else 1


def _import_deps() -> str:
    import certifi
    import httpx
    import openai

    return (
        f"openai {openai.__version__}｜httpx {httpx.__version__}｜"
        f"证书 {Path(certifi.where()).name}"
    )


def _check_assets() -> str:
    icon = paths.resource_path("assets", "icon.ico")
    qss = paths.resource_path("assets", "style.qss")
    return f"icon={icon.exists()}｜style={qss.exists()}"


def _check_templates() -> str:
    from core import message_templates as MT

    bad = [r for r in MT.validate_all({}) if r.missing]
    if bad:
        raise RuntimeError(f"模板占位符异常：{[r.message() for r in bad]}")
    return f"{len(list(MT.TemplateKind))} 个模板校验通过"


def _check_param_help() -> str:
    from core import param_help

    keys = param_help.all_keys()
    if len(keys) < 50:
        raise RuntimeError(f"参数说明条目过少：{len(keys)}")
    return f"{len(keys)} 条参数说明"


def _check_outline() -> str:
    from core import outline_parser

    chapters = outline_parser.split_outline("第一章 甲\n内容\n\n第二章 乙\n内容")
    if len(chapters) != 2:
        raise RuntimeError(f"切分结果异常：{len(chapters)}")
    return f"示例文本切分为 {len(chapters)} 章"


def _check_window(app: QApplication) -> str:
    cfg, _warnings = config_mod.load_config()
    window = MainWindow(cfg)
    window.close()
    app.processEvents()
    return f"窗口标题={window.windowTitle()}"


def _configure_windows_app_id() -> None:
    """让任务栏图标与窗口正确关联（仅 Windows）。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NovelGen.App.1")
    except Exception:
        pass


def _load_stylesheet(app: QApplication) -> None:
    for candidate in (paths.resource_path("assets", "style.qss"), ROOT / "assets" / "style.qss"):
        try:
            if candidate.exists():
                app.setStyleSheet(candidate.read_text(encoding="utf-8"))
                return
        except OSError:
            continue


def _install_excepthook() -> None:
    """未捕获异常也弹窗提示，避免窗口静默消失。"""

    def hook(exc_type, exc_value, exc_tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        detail = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            box = QMessageBox()
            box.setWindowTitle("程序错误")
            box.setIcon(QMessageBox.Icon.Critical)
            box.setText(f"发生未预期的错误：\n{exc_type.__name__}: {exc_value}")
            box.setDetailedText(detail)
            box.exec()
        except Exception:
            print(detail, file=sys.stderr)
        try:
            (paths.config_dir() / "crash.log").write_text(detail, encoding="utf-8")
        except Exception:
            pass

    sys.excepthook = hook


def main() -> int:
    _configure_windows_app_id()

    # Qt6 默认启用高 DPI 缩放，AA_UseHighDpiPixmaps 已弃用，无需再设置。
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName("NovelGen")

    icon_path = paths.resource_path("assets", "icon.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    _load_stylesheet(app)
    _install_excepthook()

    # 打包后可用性自检（不显示界面，结果写入数据目录下的 selfcheck.txt）
    if os.environ.get(SELFCHECK_ENV, "").strip() in ("1", "true", "yes", "on"):
        return run_selfcheck(app)

    cfg, warnings = config_mod.load_config()

    window = MainWindow(cfg)
    window.show()

    if warnings:
        QMessageBox.information(
            window,
            "配置提示",
            "\n".join(warnings) + f"\n\n配置文件：{paths.config_path()}",
        )

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
