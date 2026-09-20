"""TXT 导出。

导出规则：
1. 按章节顺序导出。每章前面只加一行**定位行**（如 ``【第 3 章】试炼``）：
   正文本身就包含标题，所以程序**不再在正文前另加章节标题**（会用旧配置项
   ``ExportOptions.include_title`` 的兼容开关仍然存在，但默认关闭，界面已不提供）。
2. 正文来源 = ``chapter.generated_content``，即"最后一次 assistant 输出的 content"。
   程序**不解析**该内容，原样写出；reasoning_content 永不导出。
3. 未标记为"合格"的章节：定位行后加【未确认】标记，并可在文末附上最后一次检查结果。
4. 支持"仅导出已合格章节" / "导出全部（未确认章节加标注）"两种模式。
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import paths
from .models import Chapter, ChapterState

MODE_ALL = "all_marked"
MODE_PASSED_ONLY = "passed_only"

MODE_LABELS: dict[str, str] = {
    MODE_ALL: "导出全部（未确认加标注）",
    MODE_PASSED_ONLY: "仅导出已合格章节",
}

ENCODINGS = ("utf-8-sig", "utf-8", "gbk")


@dataclass
class ExportOptions:
    mode: str = MODE_ALL
    #: 兼容旧配置字段：正文已自带标题，新版本恒为 False（界面不提供开关）
    include_title: bool = False
    mark_text: str = "【未确认】"
    append_check_result: bool = True
    separator: bool = True
    encoding: str = "utf-8-sig"
    filename: str = "novel_export.txt"

    @staticmethod
    def from_config(cfg) -> "ExportOptions":
        ui = cfg.ui
        return ExportOptions(
            mode=ui.export_mode or MODE_ALL,
            include_title=False,
            mark_text=ui.export_mark_text or "【未确认】",
            append_check_result=ui.export_append_check,
            separator=ui.export_separator,
            encoding=ui.export_encoding or "utf-8-sig",
        )


@dataclass
class ExportPlan:
    """导出预览结果。"""

    text: str
    exported: int
    skipped: int
    unconfirmed: int
    total: int

    def summary(self) -> str:
        return (
            f"共 {self.total} 章：导出 {self.exported} 章，"
            f"跳过 {self.skipped} 章，其中未确认 {self.unconfirmed} 章。"
        )


def _is_passed(chapter: Chapter) -> bool:
    return chapter.state == ChapterState.PASSED


def _passable(chapter: Chapter) -> bool:
    """是否有可导出的正文。"""
    return bool((chapter.generated_content or "").strip())


def _chapter_block(chapter: Chapter, opts: ExportOptions) -> str:
    parts: list[str] = []
    title = (chapter.title or f"第{chapter.index}章").strip()
    # 定位行：只是"第几章 / 章节名 / 是否已确认"的坐标，不复制正文（正文自带标题）。
    parts.append(
        f"【第 {chapter.index} 章】{title}"
        if _is_passed(chapter)
        else f"【第 {chapter.index} 章】{title}{opts.mark_text}"
    )
    parts.append((chapter.generated_content or "").strip())

    if opts.append_check_result and not _is_passed(chapter):
        check = (chapter.check_result or "").strip()
        if check:
            parts.append("")
            parts.append("--- 最后一次检查结果（仅供参考，未确认章节） ---")
            parts.append(check)
    return "\n".join(parts)


def build_export_plan(chapters: Sequence[Chapter], opts: ExportOptions) -> ExportPlan:
    blocks: list[str] = []
    exported = skipped = unconfirmed = 0

    for chapter in sorted(chapters, key=lambda c: c.index):
        passed = _is_passed(chapter)
        if opts.mode == MODE_PASSED_ONLY and not passed:
            skipped += 1
            continue
        if not _passable(chapter):
            skipped += 1
            continue
        if not passed:
            unconfirmed += 1
        blocks.append(_chapter_block(chapter, opts))
        exported += 1

    joiner = "\n\n\n" if opts.separator else "\n\n"
    return ExportPlan(
        text=joiner.join(blocks),
        exported=exported,
        skipped=skipped,
        unconfirmed=unconfirmed,
        total=len(chapters),
    )


def build_export_text(chapters: Sequence[Chapter], opts: ExportOptions) -> str:
    return build_export_plan(chapters, opts).text


def export_txt(
    chapters: Sequence[Chapter],
    path: str | Path | None,
    opts: ExportOptions,
) -> tuple[Path, ExportPlan]:
    """写出文件。返回 (路径, 预览计划)。"""
    plan = build_export_plan(chapters, opts)
    target = Path(path) if path else paths.default_export_path()
    if target.is_dir():
        target = target / (opts.filename or "novel_export.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(plan.text, encoding=opts.encoding or "utf-8-sig")
    return target, plan


def open_in_explorer(path: str | Path) -> bool:
    """在资源管理器中定位文件（仅 Windows；其它平台尽力而为）。"""
    target = Path(path)
    try:
        if sys.platform.startswith("win"):
            if target.exists():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(target.parent))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":  # pragma: no cover
            subprocess.Popen(["open", "-R", str(target)])
        else:  # pragma: no cover
            subprocess.Popen(["xdg-open", str(target.parent)])
        return True
    except Exception:
        return False
