"""TXT 导出。

导出规则（按用户要求，**正文原样合并，不加任何题目**）：

1. 按章节顺序导出，**只写 ``chapter.generated_content``**（最后一次 assistant 输出的正文）。
   生成的正文里本来就带章节标题，因此程序**不再**在每章前面补
   ``【第 N 章】章节名`` 这类定位行，也不再加章节标题。
2. 程序**不解析**该内容，原样写出；``reasoning_content`` 永不导出。
3. 未标记为"合格"的章节：仍可在文末附上"最后一次检查结果"（该提示行本身就是定位信息）。
   可选地在正文**之前**单独加一行未确认标记（默认关闭，见 ``mark_confirmed``）。
4. 支持"仅导出已合格章节" / "导出全部"两种模式。
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
    MODE_ALL: "导出全部（未确认章节可选加标注）",
    MODE_PASSED_ONLY: "仅导出已合格章节",
}

ENCODINGS = ("utf-8-sig", "utf-8", "gbk")


@dataclass
class ExportOptions:
    mode: str = MODE_ALL
    mark_text: str = "【未确认】"
    #: 是否在未确认章节的正文前单独加一行标记（默认关闭：导出内容就是纯正文）
    mark_confirmed: bool = False
    append_check_result: bool = True
    separator: bool = True
    encoding: str = "utf-8-sig"
    filename: str = "novel_export.txt"

    @staticmethod
    def from_config(cfg) -> "ExportOptions":
        ui = cfg.ui
        return ExportOptions(
            mode=ui.export_mode or MODE_ALL,
            mark_text=ui.export_mark_text or "【未确认】",
            mark_confirmed=getattr(ui, "export_mark_confirmed", False),
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
    """一章的导出内容 = 生成正文原文（按需附检查结果），不添加任何题目。"""
    parts: list[str] = []
    passed = _is_passed(chapter)
    if opts.mark_confirmed and not passed and opts.mark_text.strip():
        # 默认关闭。开启时也只加独立的一行标记，不改动正文本身。
        parts.append(opts.mark_text.strip())
    parts.append((chapter.generated_content or "").strip())

    if opts.append_check_result and not passed:
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
