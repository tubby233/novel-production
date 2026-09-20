"""作业队列面板：查看排队情况，并用拖动调整优先级。

需求背景
--------
用户反馈过"其它章都已经到待决策了，却有一章永远显示排队中"，需要一个地方
**看清楚队列里到底有什么**；同时希望能**通过拖动调整优先级**（重要的章节先跑）。

界面组成
--------
* 上半部分「正在运行」：只读，显示当前真正在跑的任务（数量 = 并发数上限内）。
* 下半部分「排队中」：可拖动排序，**从上到下就是执行优先级**（最上面最先跑）。
* 底部按钮：置顶 / 上移 / 下移 / 移出队列 / 立即刷新。
* 一个单发定时器按固定间隔刷新（不依赖引擎信号，避免跨线程细节）。

实现要点
--------
* 引擎侧只允许重排**还没开始**的任务，因此这里拖动不会打断正在跑的任务。
* 刷新时若列表内容没变就跳过重建，避免打断用户正在进行的拖动。
* 拖动结束后一次性把新的顺序提交给引擎（``reorder_queue``）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.engine import QueueEntry

#: 刷新间隔（毫秒）
REFRESH_MS = 700


class QueueListWidget(QListWidget):
    """支持内部拖动排序的列表；拖动结束后回调 ``on_reordered``。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(True)
        self.on_reordered = None       # Callable[[list[str]], None]

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().dropEvent(event)
        callback = self.on_reordered
        if callback is not None:
            callback(self.job_ids())

    def job_ids(self) -> list[str]:
        return [
            self.item(row).data(Qt.ItemDataRole.UserRole)
            for row in range(self.count())
            if self.item(row) is not None
        ]


class QueuePanel(QWidget):
    """队列面板（主窗口右侧停靠）。"""

    def __init__(self, engine, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._pending: list[QueueEntry] = []
        self._updating = False
        self._build_ui()

        self._timer = QTimer(self)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    # ================================================================== #
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        hint = QLabel(
            "从上到下就是执行优先级：**拖动条目即可调整顺序**，最上面的最先执行。\n"
            "只影响还没开始的任务；正在运行的不受拖动影响。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color:#57606a; font-size:11px;")
        root.addWidget(hint)

        self.running_label = QLabel("<b>正在运行</b>（0）", self)
        root.addWidget(self.running_label)
        self.running_list = QListWidget(self)
        self.running_list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.running_list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.running_list.setMaximumHeight(110)
        root.addWidget(self.running_list)

        self.pending_label = QLabel("<b>排队中</b>（0）", self)
        root.addWidget(self.pending_label)
        self.pending_list = QueueListWidget(self)
        self.pending_list.on_reordered = self._on_reordered
        self.pending_list.setToolTip("拖动调整优先级：越靠上越早执行。")
        root.addWidget(self.pending_list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        self.top_button = QPushButton("置顶", self)
        self.top_button.setToolTip("把选中的任务移到队首，下一个执行。")
        self.top_button.clicked.connect(lambda: self._move(-10_000))
        buttons.addWidget(self.top_button)

        self.up_button = QPushButton("上移", self)
        self.up_button.clicked.connect(lambda: self._move(-1))
        buttons.addWidget(self.up_button)

        self.down_button = QPushButton("下移", self)
        self.down_button.clicked.connect(lambda: self._move(1))
        buttons.addWidget(self.down_button)

        self.drop_button = QPushButton("移出队列", self)
        self.drop_button.setToolTip("取消该章还没开始的任务（等价于对该章点“停止本章”）。")
        self.drop_button.clicked.connect(self._drop_selected)
        buttons.addWidget(self.drop_button)
        root.addLayout(buttons)

        footer = QHBoxLayout()
        self.status_label = QLabel("", self)
        self.status_label.setWordWrap(True)
        self.status_label.setStyleSheet("color:#8a8f98; font-size:11px;")
        footer.addWidget(self.status_label, 1)
        self.refresh_button = QPushButton("立即刷新", self)
        self.refresh_button.clicked.connect(self.refresh)
        footer.addWidget(self.refresh_button)
        root.addLayout(footer)

    # ================================================================== #
    # 刷新
    # ================================================================== #
    def refresh(self) -> None:
        """从引擎取快照并刷新两个列表；内容没变时不动列表（避免打断拖动）。"""
        if self._updating:
            return
        try:
            pending, running = self.engine.queue_snapshot()
        except Exception as exc:  # pragma: no cover - 引擎未就绪
            self.status_label.setText(f"无法读取队列：{exc}")
            return
        self._updating = True
        try:
            self._refresh_running(running)
            self._refresh_pending(pending)
            self.status_label.setText(
                f"排队 {len(pending)} 个 ｜ 运行 {len(running)} 个 ｜ "
                f"并发上限 {getattr(self.engine.cfg.sampling, 'concurrency', '?')}"
            )
        finally:
            self._updating = False

    def _refresh_running(self, running: list[QueueEntry]) -> None:
        self.running_label.setText(f"<b>正在运行</b>（{len(running)}）")
        if self.running_list.count() == len(running):
            same = all(
                self.running_list.item(row).text() == self._entry_text(entry)
                for row, entry in enumerate(running)
            )
            if same:
                return
        self.running_list.clear()
        for entry in running:
            item = QListWidgetItem(self._entry_text(entry))
            item.setToolTip(f"{entry.title}｜{entry.reason_label}")
            self.running_list.addItem(item)

    def _refresh_pending(self, pending: list[QueueEntry]) -> None:
        self.pending_label.setText(f"<b>排队中</b>（{len(pending)}）")
        ids = [entry.job_id for entry in pending]
        if ids == [entry.job_id for entry in self._pending] and self.pending_list.count() == len(ids):
            # 顺序与数量都没变：只更新显示文本
            for row, entry in enumerate(pending):
                item = self.pending_list.item(row)
                if item is not None and item.text() != self._entry_text(entry):
                    item.setText(self._entry_text(entry))
        else:
            current = self._selected_job_id()
            self.pending_list.clear()
            for entry in pending:
                item = QListWidgetItem(self._entry_text(entry))
                item.setData(Qt.ItemDataRole.UserRole, entry.job_id)
                item.setData(Qt.ItemDataRole.UserRole + 1, entry.chapter_id)
                item.setToolTip(
                    f"<b>{entry.title}</b><br>任务：{entry.kind_label}"
                    + (f"（{entry.reason_label}）" if entry.reason_label else "")
                    + "<br>拖动可调整优先级"
                )
                self.pending_list.addItem(item)
            if current:
                self.select_job(current)
        self._pending = pending
        self._refresh_buttons()

    @staticmethod
    def _entry_text(entry: QueueEntry) -> str:
        suffix = f"　{entry.reason_label}" if entry.reason_label else ""
        return f"{entry.position}. {entry.title}　[{entry.kind_label}]{suffix}"

    def _refresh_buttons(self) -> None:
        row = self.pending_list.currentRow()
        total = self.pending_list.count()
        has_selection = 0 <= row < total
        self.up_button.setEnabled(has_selection and row > 0)
        self.top_button.setEnabled(has_selection and row > 0)
        self.down_button.setEnabled(has_selection and 0 <= row < total - 1)
        self.drop_button.setEnabled(has_selection)

    # ================================================================== #
    # 选择与排序
    # ================================================================== #
    def _selected_job_id(self) -> str:
        item = self.pending_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else ""

    def select_job(self, job_id: str) -> None:
        for row in range(self.pending_list.count()):
            item = self.pending_list.item(row)
            if item is not None and item.data(Qt.ItemDataRole.UserRole) == job_id:
                self.pending_list.setCurrentRow(row)
                return

    def _move(self, delta: int) -> None:
        row = self.pending_list.currentRow()
        total = self.pending_list.count()
        if not (0 <= row < total):
            return
        target = max(0, min(total - 1, row + delta))
        if target == row:
            return
        item = self.pending_list.takeItem(row)
        self.pending_list.insertItem(target, item)
        self.pending_list.setCurrentRow(target)
        self._commit_order()

    def _on_reordered(self, job_ids: list[str]) -> None:
        """拖动结束后把新顺序提交给引擎。"""
        self._commit_order(job_ids)

    def _commit_order(self, job_ids: list[str] | None = None) -> None:
        order = job_ids or self.pending_list.job_ids()
        if not order:
            return
        try:
            self.engine.reorder_queue(order)
        except Exception as exc:  # pragma: no cover - 引擎未就绪
            self.status_label.setText(f"调整优先级失败：{exc}")
            return
        self._pending = []      # 强制下一次刷新用新顺序重建
        self._refresh_buttons()
        self.status_label.setText("已按新顺序调整优先级（下一个执行最上面的任务）。")

    def _drop_selected(self) -> None:
        item = self.pending_list.currentItem()
        if item is None:
            return
        chapter_id = item.data(Qt.ItemDataRole.UserRole + 1) or ""
        if not chapter_id:
            return
        try:
            self.engine.drop_queued_chapter(chapter_id)
        except Exception as exc:  # pragma: no cover
            self.status_label.setText(f"移出队列失败：{exc}")
            return
        self._pending = []
        self.status_label.setText("已移出队列。")
