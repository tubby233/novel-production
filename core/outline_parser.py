"""大纲正则切分。

规则：匹配到"第X章 / 第N章 / Chapter N"之类的标题行即认为是新一章的开始，
该行到下一个标题行之前的所有内容，整体作为这一章的**单个输入单元**（不再细分）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 中文数字（含"两"这类常见用法）
CN_NUM = "零〇一二三四五六七八九十百千万两"

#: 默认章节标题正则。要点：
#:   - 允许"第 1 章"这类中间带空格的写法
#:   - 允许【第一章】这类带方括号的写法
#:   - 允许 Chapter 12 形式
#:   - 只在行首匹配（MULTILINE），避免把正文中间的"第一章"当成标题
DEFAULT_CHAPTER_PATTERN = (
    r"^[ \t　]*(?:"
    r"【[ \t　]*)?"
    r"(?:"
    r"第[ \t　]*[0-9" + CN_NUM + r"]+[ \t　]*[章回节卷篇]"
    r"|Chapter[ \t　]+\d+"
    r")"
    r"(?:[ \t　]*】)?"
)

#: 标题行最大长度：超过则大概率是正文里恰好以"第一章"开头的句子
MAX_TITLE_LINE_LEN = 80


@dataclass
class ParsedChapter:
    index: int
    title: str        # 归一化标题，例如 "第1章"
    raw_title: str    # 原始标题行文本
    outline: str      # 本章大纲全文（默认含标题行）

    def as_dict(self) -> dict:
        return {
            "index": self.index,
            "title": self.title,
            "raw_title": self.raw_title,
            "outline": self.outline,
        }


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """编译章节正则。失败时抛 re.error，由界面展示。"""
    return re.compile(pattern, re.MULTILINE)


def validate_pattern(pattern: str) -> tuple[bool, str]:
    """校验正则是否可用。返回 (是否合法, 说明)。"""
    if not pattern.strip():
        return False, "正则不能为空。"
    try:
        compile_pattern(pattern)
    except re.error as exc:
        return False, f"正则编译失败：{exc}"
    return True, "正则可用。"


def _normalize_title(raw_title: str) -> str:
    """标题归一化：压缩空白，仅用于显示。不做任何语义理解。"""
    text = re.sub(r"[ \t　]+", " ", raw_title.strip())
    return text


def preview_matches(
    text: str, pattern: str = DEFAULT_CHAPTER_PATTERN, limit: int = 200
) -> list[tuple[int, str]]:
    """预览匹配结果：返回 (行号从 1 开始, 该行文本) 列表，用于设置界面的"测试正则"。"""
    if not text:
        return []
    try:
        regex = compile_pattern(pattern)
    except re.error:
        return []
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if regex.match(line.rstrip()):
            hits.append((lineno, line.strip()))
            if len(hits) >= limit:
                break
    return hits


def split_outline(
    text: str,
    pattern: str = DEFAULT_CHAPTER_PATTERN,
    *,
    keep_title_in_outline: bool = True,
    max_title_line_len: int = MAX_TITLE_LINE_LEN,
) -> list[ParsedChapter]:
    """按正则切分大纲。

    返回章节列表。**未匹配到任何章节时返回空列表**（由界面提供兜底选项），
    绝不静默产出"整篇一章"，以免用户误以为已切分。
    """
    if not text or not text.strip():
        return []

    regex = compile_pattern(pattern)
    lines = text.splitlines()

    starts: list[int] = []      # 匹配到的标题行下标
    for idx, line in enumerate(lines):
        stripped = line.rstrip()
        if len(stripped.strip()) > max_title_line_len:
            continue
        if regex.match(stripped):
            starts.append(idx)

    if not starts:
        return []

    chapters: list[ParsedChapter] = []
    for order, start in enumerate(starts):
        end = starts[order + 1] if order + 1 < len(starts) else len(lines)
        block = lines[start:end]
        raw_title = block[0].strip()
        body = block if keep_title_in_outline else block[1:]
        outline = "\n".join(body).strip("\n")
        chapters.append(
            ParsedChapter(
                index=order + 1,
                title=_normalize_title(raw_title),
                raw_title=raw_title,
                outline=outline,
            )
        )
    return chapters


def split_as_single(text: str) -> list[ParsedChapter]:
    """兜底方案一：整篇作为一章。"""
    body = (text or "").strip()
    if not body:
        return []
    first_line = body.splitlines()[0].strip()
    title = first_line if len(first_line) <= 30 else "全文"
    return [ParsedChapter(index=1, title=title, raw_title=first_line, outline=body)]


def split_by_blank_lines(text: str, min_chars: int = 200) -> list[ParsedChapter]:
    """兜底方案二：按空行分段，把小段合并到至少 min_chars 长度。"""
    blocks: list[str] = []
    current: list[str] = []
    for line in (text or "").splitlines():
        if line.strip():
            current.append(line)
        else:
            if current:
                blocks.append("\n".join(current))
                current = []
    if current:
        blocks.append("\n".join(current))

    merged: list[str] = []
    buffer = ""
    for block in blocks:
        buffer = f"{buffer}\n\n{block}" if buffer else block
        if len(buffer) >= min_chars:
            merged.append(buffer)
            buffer = ""
    if buffer:
        merged.append(buffer)

    chapters: list[ParsedChapter] = []
    for i, block in enumerate(merged, start=1):
        first_line = block.splitlines()[0].strip()
        raw_title = first_line if len(first_line) <= 30 else ""
        chapters.append(
            ParsedChapter(
                index=i,
                title=raw_title or f"第{i}段",
                raw_title=raw_title,
                outline=block,
            )
        )
    return chapters
