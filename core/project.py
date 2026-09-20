"""小说项目管理：一部小说 = 一个以小说名命名的文件夹。

目录结构（全部数据都在项目文件夹内，方便整体拷贝与备份）::

    <项目根目录>/
        └── 我的小说/                <- 小说名，也就是项目名
              ├── project.json       <- 项目元信息（版本 / 小说名 / 创建与更新时间）
              ├── chapters.json      <- 章节数据（大纲、正文、检查结果、对话历史）
              ├── conversations.json <- **对话存档**（该小说全部章节的生成/检查对话）
              └── outline.txt        <- 大纲原文（纯文本，便于外部编辑与恢复）

对话存档为什么单独一份
----------------------
``chapters.json`` 里其实已经内嵌了对话，但它是"整章快照"，读取项目时容易让人以为
只恢复了切分结果。``conversations.json`` 把**所有章节的全部对话历史（含思维链、
每条消息的来源与用量）**单独持久化，AI 每回复完一次就更新，
从而保证"重新打开项目后可以接着之前的对话继续聊"，正文也不会被清掉。

设计要点
--------
* 只有"拆分大纲"这一个入口会触发立项：切分成功后由用户输入小说名，
  程序在该文件夹里新建同名目录并落盘。
* 项目名会做文件名安全化处理（去掉 ``\\ / : * ? " < > |`` 等非法字符）。
* 已存在的同名项目不会被静默覆盖：:func:`create_project` 会报错，
  由界面提示用户改用其它名字。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

PROJECT_FILENAME = "project.json"
CHAPTERS_FILENAME = "chapters.json"
CONVERSATIONS_FILENAME = "conversations.json"
OUTLINE_FILENAME = "outline.txt"
PROJECT_VERSION = 1
#: conversations.json 的结构版本
CONVERSATIONS_VERSION = 1

#: Windows 文件名非法字符 + 控制字符
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
#: Windows 保留设备名（不区分大小写）
_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_project_name(name: str) -> str:
    """把用户输入的小说名整理成合法的文件夹名。

    * 去掉首尾空白与结尾的点（Windows 不允许文件夹名以点结尾）；
    * 非法字符替换为下划线；
    * 命中保留设备名时加前缀；
    * 结果为空时回落为 ``未命名小说``。
    """
    cleaned = _ILLEGAL_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.replace("\n", " ").replace("\r", " ").strip().rstrip(". ")
    if not cleaned:
        return "未命名小说"
    if cleaned.upper() in _RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned[:80]


@dataclass
class Project:
    """一个小说项目（对应磁盘上的一个文件夹）。"""

    name: str
    root: Path
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = PROJECT_VERSION

    # ------------------------------------------------------------------ #
    @property
    def chapters_path(self) -> Path:
        return self.root / CHAPTERS_FILENAME

    @property
    def conversations_path(self) -> Path:
        return self.root / CONVERSATIONS_FILENAME

    @property
    def outline_path(self) -> Path:
        return self.root / OUTLINE_FILENAME

    @property
    def meta_path(self) -> Path:
        return self.root / PROJECT_FILENAME

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "name": self.name,
            "created_at": self.created_at or time.time(),
            "updated_at": time.time(),
        }

    def touch(self) -> None:
        self.updated_at = time.time()


# --------------------------------------------------------------------------- #
# 创建 / 读取
# --------------------------------------------------------------------------- #
def project_root_for(base_dir: str | Path, name: str) -> Path:
    return Path(base_dir).expanduser() / sanitize_project_name(name)


def create_project(
    base_dir: str | Path,
    name: str,
    *,
    outline_text: str = "",
    chapters_payload: dict | None = None,
) -> Project:
    """在 ``base_dir`` 下新建以小说名命名的项目文件夹并写入初始数据。

    * 同名项目已存在时抛 :class:`FileExistsError`（界面据此提示用户改名）。
    * ``chapters_payload`` 为 ``chapters.json`` 的完整内容（含 version/chapters 字段）。
    """
    root = project_root_for(base_dir, name)
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(f"项目文件夹已存在且非空：{root}")
    root.mkdir(parents=True, exist_ok=True)

    project = Project(name=sanitize_project_name(name), root=root, created_at=time.time())
    write_outline(project, outline_text)
    if chapters_payload is not None:
        write_json(project.chapters_path, chapters_payload)
    write_meta(project)
    return project


def load_project(root: str | Path) -> Project:
    """读取项目（只需文件夹存在，元信息缺失时按文件夹名与文件时间推导）。"""
    folder = Path(root)
    if not folder.is_dir():
        raise FileNotFoundError(f"项目文件夹不存在：{folder}")

    name = folder.name
    created_at = 0.0
    version = PROJECT_VERSION
    meta = folder / PROJECT_FILENAME
    if meta.exists():
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
            name = str(data.get("name") or name)
            created_at = float(data.get("created_at") or 0.0)
            version = int(data.get("version") or PROJECT_VERSION)
        except (OSError, ValueError, TypeError):
            pass  # 元信息坏了不影响打开项目
    return Project(name=name, root=folder, created_at=created_at, version=version)


def list_projects(base_dir: str | Path) -> list[Project]:
    """列出项目根目录下的所有项目（按更新时间倒序）。"""
    base = Path(base_dir).expanduser()
    if not base.is_dir():
        return []
    found: list[Project] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        has_data = (child / CHAPTERS_FILENAME).exists() or (child / PROJECT_FILENAME).exists()
        if not has_data:
            continue
        try:
            found.append(load_project(child))
        except OSError:
            continue
    return sorted(found, key=lambda p: p.updated_at or p.created_at or 0.0, reverse=True)


def is_project_dir(folder: str | Path) -> bool:
    target = Path(folder)
    return target.is_dir() and (
        (target / PROJECT_FILENAME).exists() or (target / CHAPTERS_FILENAME).exists()
    )


# --------------------------------------------------------------------------- #
# 读写
# --------------------------------------------------------------------------- #
def write_json(path: Path, payload: dict) -> Path:
    """原子写入 JSON（先写临时文件再替换）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    tmp.replace(path)
    return path


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_meta(project: Project) -> Path:
    return write_json(project.meta_path, project.to_dict())


def write_outline(project: Project, outline_text: str) -> Path:
    project.root.mkdir(parents=True, exist_ok=True)
    project.outline_path.write_text(outline_text or "", encoding="utf-8")
    return project.outline_path


def read_outline(project: Project) -> str:
    try:
        return project.outline_path.read_text(encoding="utf-8")
    except OSError:
        return ""


def write_chapters(project: Project, payload: dict) -> Path:
    write_json(project.chapters_path, payload)
    project.touch()
    try:
        write_meta(project)
    except OSError:
        pass
    return project.chapters_path


def read_chapters(project: Project) -> list[dict]:
    if not project.chapters_path.exists():
        return []
    data = read_json(project.chapters_path)
    chapters = data.get("chapters") if isinstance(data, dict) else None
    return list(chapters or [])


# --------------------------------------------------------------------------- #
# 对话存档（conversations.json）
# --------------------------------------------------------------------------- #
def write_conversations(project: Project, records: list[dict]) -> Path:
    """写入该小说全部章节的对话存档。"""
    payload = {
        "version": CONVERSATIONS_VERSION,
        "updated_at": time.time(),
        "conversations": records,
    }
    path = write_json(project.conversations_path, payload)
    project.touch()
    try:
        write_meta(project)
    except OSError:
        pass
    return path


def read_conversations(project: Project) -> list[dict]:
    """读取对话存档；文件不存在或损坏时返回空列表（调用方回落到 chapters.json）。"""
    if not project.conversations_path.exists():
        return []
    try:
        data = read_json(project.conversations_path)
    except (OSError, ValueError):
        return []
    records = data.get("conversations") if isinstance(data, dict) else None
    return list(records or [])


def has_conversations(project: Project) -> bool:
    return project.conversations_path.exists()


def describe(projects: Iterable[Project]) -> str:
    return "，".join(p.name for p in projects)
