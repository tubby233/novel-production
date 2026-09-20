"""端到端自检（用假的 API 客户端，不联网）。

验证内容：
* 引擎线程 + asyncio 调度是否正常工作
* 章节状态机迁移路径：待生成 → 生成中 → (自动)检查中 → 待决策
* 消息模板是否正确填充（大纲 / 正文 / 检查结果 / 用户输入）
* 思维链增量是否被拆分送达到界面信号
* 停止全部 / 并发数 / 接受 / 标合格 等操作

用法：
    python tools/e2e_test.py
"""

from __future__ import annotations

import asyncio as _asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_workspace_dir(tag: str) -> Path:
    """在项目目录下建一个唯一的工作目录。

    刻意不用 ``tempfile.mkdtemp``：它用 0o700 建目录，在部分受限环境下
    会让随后在其中的 ``mkdir`` 报 WinError 5，也会让清理失败。
    """
    for attempt in range(50):
        target = ROOT / f"selftest_e2e_{tag}_{os.getpid()}_{attempt}"
        if target.exists():
            continue
        try:
            target.mkdir()
        except OSError:
            continue
        return target
    return Path(tempfile.mkdtemp(prefix="novel_gen_e2e_"))    # pragma: no cover


_TMP_DATA = str(_make_workspace_dir("data"))
os.environ["APPDATA"] = _TMP_DATA
os.environ["LOCALAPPDATA"] = _TMP_DATA


def _cleanup_tmp() -> None:
    import shutil

    shutil.rmtree(_TMP_DATA, ignore_errors=True)

from PySide6.QtWidgets import QApplication  # noqa: E402

from core import message_templates as MT  # noqa: E402
from core.config import AppConfig  # noqa: E402
from core.engine import Engine  # noqa: E402
from core.models import Chapter, ChapterState, StreamDelta, Usage  # noqa: E402


def main() -> int:
    app = QApplication.instance() or QApplication([])

    cfg = AppConfig()
    cfg.api.api_key = "test-key"
    cfg.api.model = "deepseek-flash"
    cfg.sampling.concurrency = 3
    cfg.ui.auto_check_after_generate = True

    engine = Engine(cfg)
    # 即使断言失败也保证引擎线程被正确回收（否则 Qt 会提示 QThread 仍在运行）
    import atexit

    atexit.register(engine.shutdown)
    atexit.register(_cleanup_tmp)
    # 让配置/章节数据落到临时目录，避免污染源码目录
    from core import paths as core_paths

    core_paths.config_dir = lambda: Path(_TMP_DATA)  # type: ignore[assignment]

    # ---------------- 记录器 ---------------- #
    events: list[tuple[str, str, str]] = []
    deltas: dict[str, list[tuple[str, str]]] = {}
    captured_requests: list[list[tuple[str, str]]] = []

    engine.signals.chapter_state_changed.connect(
        lambda cid, st: events.append(("state", cid, st))
    )
    engine.signals.delta_received.connect(
        lambda cid, kind, dkind, text: deltas.setdefault(cid, []).append((dkind, text))
    )

    # ---------------- 替换 API 调用 ---------------- #
    async def fake_stream_chat(messages, *, thinking, sampling=None, on_event=None, cancel_event=None):
        captured_requests.append([(m.role, m.content) for m in messages])
        assert thinking.enabled in (True, False)
        yield StreamDelta(kind="reasoning", text="思考片段1")
        yield StreamDelta(kind="reasoning", text="思考片段2")
        yield StreamDelta(kind="content", text="这是")
        yield StreamDelta(kind="content", text="生成的正文。")
        yield StreamDelta(kind="usage", usage=Usage(prompt_tokens=100, completion_tokens=50,
                                                    reasoning_tokens=20, total_tokens=150))
        yield StreamDelta(kind="done")

    class FakeGenerator:
        def __init__(self, client, cfg):
            self.client = client
            self.cfg = cfg

        def apply_config(self, cfg):
            self.cfg = cfg

    # 直接替换引擎内部对象
    engine.start()
    time.sleep(0.4)

    async def patch():
        from core.checker import ChapterChecker
        from core.generator import ChapterGenerator

        gen = ChapterGenerator(engine._client, cfg)
        chk = ChapterChecker(engine._client, cfg)
        # 用假的流式方法替换真实网络调用
        engine._client.stream_chat = fake_stream_chat  # type: ignore[assignment]
        engine._generator = gen
        engine._checker = chk

    engine._call(patch())
    time.sleep(0.3)

    # ---------------- 构造章节 ---------------- #
    chapters = [
        Chapter.create(i, f"第{i}章", f"第{i}章大纲：本章内容……")
        for i in (1, 2, 3)
    ]
    engine.set_chapters(chapters)

    for chapter in chapters:
        engine.submit_generate(chapter.chapter_id, "first")

    # 等待任务完成
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        if all(c.state == ChapterState.AWAITING_DECISION for c in chapters):
            break

    # 状态已就绪，但跨线程排队的 delta 信号可能还没送达：再排空一次事件队列
    for _ in range(40):
        app.processEvents()
        time.sleep(0.02)

    ok = True

    def fail(message: str) -> None:
        nonlocal ok
        ok = False
        print(f"  [FAIL] {message}")

    print("1) 状态机路径")
    for chapter in chapters:
        print(f"   {chapter.title}: {chapter.state}")
        if chapter.state != ChapterState.AWAITING_DECISION:
            fail(f"{chapter.title} 未到达 待决策，实际 {chapter.state}")
        if not chapter.generated_content:
            fail(f"{chapter.title} 正文为空")
        if chapter.generated_content != "这是生成的正文。":
            fail(f"{chapter.title} 正文内容异常：{chapter.generated_content!r}")
        if chapter.check_result != "这是生成的正文。":
            fail(f"{chapter.title} 检查结果未落地：{chapter.check_result!r}")
        if chapter.regen_count != 0:
            fail(f"{chapter.title} 重生成次数不应增加")

    print("2) 对话隔离与会话结构")
    for chapter in chapters:
        gen_conv = chapter.generate_conv
        chk_conv = chapter.check_conv
        if gen_conv is None or chk_conv is None:
            fail(f"{chapter.title} 对话缺失")
            continue
        if gen_conv.conv_id == chk_conv.conv_id:
            fail(f"{chapter.title} 生成对话与检查对话未隔离")
        roles = [m.role for m in gen_conv.messages]
        if roles != ["system", "user", "assistant"]:
            fail(f"{chapter.title} 生成对话角色序列异常：{roles}")
        check_roles = [m.role for m in chk_conv.messages]
        if check_roles != ["user", "assistant"]:
            fail(f"{chapter.title} 检查对话角色序列异常：{check_roles}（检查提示词为空时不应有 system）")

    print("3) 模板填充")
    first_request = captured_requests[0]
    if "第1章大纲" not in first_request[-1][1]:
        fail("首次生成消息未填充 {outline}")
    check_request = [r for r in captured_requests if "以下是小说章节正文" in r[-1][1]]
    if not check_request:
        fail("检查消息未填充 {content}")
    elif "这是生成的正文。" not in check_request[0][-1][1]:
        fail("检查消息内容不正确")

    print("4) 思维链与正文增量")
    for chapter in chapters:
        kinds = [k for k, _ in deltas.get(chapter.chapter_id, [])]
        if "reasoning" not in kinds or "content" not in kinds:
            fail(f"{chapter.title} 未同时收到 reasoning 与 content 增量：{set(kinds)}")
        joined = "".join(t for k, t in deltas[chapter.chapter_id] if k == "reasoning")
        # 每章有两次请求（生成 + 检查），因此思维链增量会出现两轮
        if joined != "思考片段1思考片段2" * 2:
            fail(f"{chapter.title} 思维链增量拼接异常：{joined!r}")

    print("5) 检查历史与状态事件")
    for chapter in chapters:
        if len(chapter.check_history) != 1:
            fail(f"{chapter.title} 检查历史条数异常：{len(chapter.check_history)}")
    states = [st for kind, _, st in events if kind == "state"]
    if "generating" not in states or "checking" not in states or "awaiting_decision" not in states:
        fail(f"状态事件缺失：{sorted(set(states))}")

    print("6) 用户决策：根据检查结果重新生成")
    chapter = chapters[0]
    before = len(chapter.generate_conv.messages)
    engine.submit_generate(chapter.chapter_id, "regen_by_check", "请加强环境描写")
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        if chapter.state == ChapterState.AWAITING_DECISION and chapter.regen_count == 1:
            break
    if chapter.regen_count != 1:
        fail(f"重生成计数未增加：{chapter.regen_count}")
    after = len(chapter.generate_conv.messages)
    if after <= before:
        fail("重新生成未复用生成对话（消息数未增加）")
    regen_msg = chapter.generate_conv.messages[before].content
    if "请加强环境描写" not in regen_msg:
        fail("{check_result} 占位符未填充：" + repr(regen_msg))

    print("7) 接受（标记合格）")
    engine.accept_chapter(chapters[1].chapter_id)
    deadline = time.time() + 5
    while time.time() < deadline and chapters[1].state != ChapterState.PASSED:
        app.processEvents()
        time.sleep(0.05)
    # 状态可能已在引擎线程变更、但信号尚未送达：再排空事件队列后复查
    if chapters[1].state != ChapterState.PASSED:
        for _ in range(20):
            app.processEvents()
            time.sleep(0.02)
    if chapters[1].state != ChapterState.PASSED:
        fail(f"接受失败，状态 {chapters[1].state}")
    else:
        print(f"   第2章 -> {chapters[1].state}")

    print("8) 停止全部")
    for chapter in chapters:
        engine.submit_generate(chapter.chapter_id, "regen_manual", "再改一次")
    time.sleep(0.15)
    engine.stop_all()
    deadline = time.time() + 10
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        busy = [c for c in chapters if c.busy]
        if not busy:
            break
    busy = [c.title for c in chapters if c.busy]
    if busy:
        fail(f"停止全部后仍有章节处于忙状态：{busy}")

    print("8.2) 用户主动停止不作为报错处理")
    logs: list[tuple[str, str]] = []
    engine.signals.log.connect(lambda level, message: logs.append((level, message)))
    finished_events: list[tuple[str, str, bool, str, int]] = []
    engine.signals.conversation_finished.connect(
        lambda cid, kind, ok, err, seq: finished_events.append((cid, kind, ok, err, seq))
    )

    async def slow_forever_stream_chat(
        messages, *, thinking, sampling=None, on_event=None, cancel_event=None
    ):
        """缓慢但**可被取消**的流：每段之间 await sleep（挂在 sleep 上取消，干净退出）。"""
        for index in range(400):
            yield StreamDelta(kind="content", text=f"片段{index}。")
            await _asyncio.sleep(0.3)

    engine._client.stream_chat = slow_forever_stream_chat  # type: ignore[assignment]
    target_stop = chapters[2]
    logs.clear()
    finished_events.clear()
    engine.submit_check(target_stop.chapter_id)
    deadline = time.time() + 8
    while time.time() < deadline and target_stop.state != ChapterState.CHECKING:
        app.processEvents()
        time.sleep(0.02)
    if target_stop.state != ChapterState.CHECKING:
        fail(f"停止测试未进入检查中：{target_stop.state}")
    # 等第一段内容到达（确认流真的在跑），再停止
    for _ in range(60):
        app.processEvents()
        time.sleep(0.02)
        conv = target_stop.check_conv
        if conv is not None and conv.stream_content:
            break
    state_before_stop = target_stop.state
    engine.stop_chapter(target_stop.chapter_id)
    deadline = time.time() + 8
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
        if finished_events:
            break
    for _ in range(40):
        app.processEvents()
        time.sleep(0.02)
    stop_errors = [m for level, m in logs if level in ("warn", "error")]
    if stop_errors:
        fail(f"用户主动停止被当成报错写进日志：{stop_errors}")
    if not any("已按你的要求停止" in m for _, m in logs):
        fail(f"缺少“已停止”的 info 提示，实际日志：{logs}")
    if not finished_events:
        fail("停止后没有收到 conversation_finished 信号")
    elif finished_events[-1][2] is not True:
        fail(f"停止后结束信号的 ok 应为 True（表示不是失败），实际 {finished_events[-1]}")
    elif "已按你的要求停止" not in finished_events[-1][3]:
        fail(f"停止提示文案异常：{finished_events[-1][3]}")
    if target_stop.state == ChapterState.FAILED:
        fail("用户停止不应把章节置为“出错”")
    if target_stop.state != state_before_stop:
        print(f"   停止后状态回落：{state_before_stop} -> {target_stop.state}")
    if not engine.conversations_dirty():
        fail("停止后应标记对话存档为“待写入”")
    engine.mark_conversations_saved()
    # 恢复普通假流，后面的用例继续用
    engine._client.stream_chat = fake_stream_chat  # type: ignore[assignment]

    print("8.3) 清空生成对话：状态回到待生成，重新生成走“按大纲”")
    target_clear = chapters[0]
    engine.clear_conversation(target_clear.chapter_id, "generate")
    deadline = time.time() + 5
    while time.time() < deadline and target_clear.state != ChapterState.PENDING:
        app.processEvents()
        time.sleep(0.02)
    if target_clear.state != ChapterState.PENDING:
        fail(f"清空生成对话后状态应为待生成，实际 {target_clear.state}")
    if target_clear.generate_conv is not None and target_clear.generate_conv.messages:
        fail("清空后生成对话仍有消息")
    if not target_clear.outline_for_generate().strip():
        fail("清空对话把大纲也弄丢了（应使用独立保存的大纲快照）")
    # 复原：按大纲重新生成一次（验证"清空对话后仍能用大纲重新生成"），
    # 让后面的界面检查继续用同一章的完整对话
    engine.submit_generate(target_clear.chapter_id, "first")
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.03)
        if target_clear.state == ChapterState.AWAITING_DECISION:
            break
    for _ in range(30):
        app.processEvents()
        time.sleep(0.02)
    if target_clear.state != ChapterState.AWAITING_DECISION:
        fail(f"清空对话后按大纲重新生成失败：{target_clear.state}")
    elif not target_clear.generate_conv.messages:
        fail("清空对话后重新生成没有产生新的对话消息")
    else:
        first_user = next(
            (m for m in target_clear.generate_conv.messages if m.role == "user"), None
        )
        if first_user is None or "第1章大纲" not in first_user.content:
            fail(f"重新生成发送的不是本章大纲：{first_user.content[:60] if first_user else None}")
        print(f"   清空后用大纲重新生成成功，消息数 {len(target_clear.generate_conv.messages)}")

    print("8.4) 队列面板数据：拖动排序改变执行顺序 + 卡住的“排队中”会被兜底修复")
    stop_before_queue = cfg.sampling.concurrency
    cfg.sampling.concurrency = 1
    engine.apply_config(cfg)
    queued_chapters = [
        Chapter.create(i, f"队列第{i}章", f"队列第{i}章大纲") for i in (1, 2, 3)
    ]
    engine.set_chapters(queued_chapters)
    started_order: list[str] = []
    engine.signals.conversation_started.connect(
        lambda cid, kind, seq: started_order.append(cid) if kind == "generate" else None
    )

    # 前两个任务会卡在闸门上不返回，方便稳定地观察队列
    gate = {"open": False, "seen": 0}

    async def gate_stream(messages, *, thinking, sampling=None, on_event=None, cancel_event=None):
        gate["seen"] += 1
        if gate["seen"] <= 2:
            while not gate["open"]:
                await _asyncio.sleep(0.05)
        yield StreamDelta(kind="content", text="闸门后正文")
        yield StreamDelta(kind="done")

    engine._client.stream_chat = gate_stream  # type: ignore[assignment]
    for chapter in queued_chapters:
        engine.submit_generate(chapter.chapter_id, "first")
    deadline = time.time() + 8
    while time.time() < deadline and len(started_order) < 2:
        app.processEvents()
        time.sleep(0.02)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)

    pending, running = engine.queue_snapshot()
    if len(running) != 1:
        fail(f"并发=1 时应有 1 个任务在跑，实际 {len(running)}")
    if queued_chapters[0].state != ChapterState.GENERATING:
        fail(f"第一个任务应在运行中，实际 {queued_chapters[0].state}")
    # 调度器只在有空闲槽位时才取任务：并发=1 时"在跑 1 个 + 排队 2 个"
    if len(pending) != 2:
        fail(f"并发=1 时应还有 2 个任务在队列里，实际 {len(pending)}")
    if [e.chapter_id for e in pending] != [
        queued_chapters[1].chapter_id,
        queued_chapters[2].chapter_id,
    ]:
        fail(f"排队顺序应为提交顺序（FIFO），实际 {[e.title for e in pending]}")
    if any(e.chapter_id == queued_chapters[0].chapter_id for e in pending):
        fail("正在运行的章节不应出现在排队列表里")
    if pending[0].kind_label != "生成" or not pending[0].reason_label:
        fail(f"队列条目缺少任务/原因标签：{pending[0]}")
    print(
        f"   队列面板：运行 {len(running)} 个，排队 {len(pending)} 个"
        f"（队首 {pending[0].title}，{pending[0].kind_label}·{pending[0].reason_label}）"
    )

    # 拖动排序：把队尾的第三个任务提到队首
    target_entry = pending[-1]
    engine.reorder_queue([target_entry.job_id])
    time.sleep(0.2)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    after, _r2 = engine.queue_snapshot()
    if not after or after[0].job_id != target_entry.job_id:
        fail(f"拖动排序后该任务没有排到队首：{[e.title for e in after]}")
    else:
        print(f"   拖动排序生效：{target_entry.title} 已排到队首")

    # 放行闸门：验证重排后所有任务都能跑完，且被重排的章确实提前执行
    gate["open"] = True
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.03)
        if len(started_order) >= 3:
            break
    if len(started_order) != 3:
        fail(f"重排后任务丢了：只有 {len(started_order)} 个任务真正开始")
    elif len(set(started_order)) != 3:
        fail(f"有章节被重复执行：{started_order}")
    elif started_order[1] != target_entry.chapter_id:
        fail(
            "重排后的执行顺序不对：期望 "
            f"{target_entry.title} 第二个执行，实际顺序 "
            f"{[c.title for c in queued_chapters if c.chapter_id in started_order]}"
        )
    else:
        print(
            "   执行顺序："
            + " -> ".join(c.title for c in queued_chapters if c.chapter_id in started_order)
        )

    # 兜底：手动制造"状态是排队中但队列里没有它"的脏数据，应被自动修复
    stale = queued_chapters[1]
    stale.state = ChapterState.QUEUED
    stale.pre_queue_state = ChapterState.PENDING
    engine.drop_queued_chapter(stale.chapter_id)
    deadline = time.time() + 5
    while time.time() < deadline and stale.state == ChapterState.QUEUED:
        app.processEvents()
        time.sleep(0.02)
    if stale.state == ChapterState.QUEUED:
        fail("排队中但没有任务的章节没有被自动修复")
    else:
        print(f"   卡在“排队中”的章节已自动恢复为 {stale.state}")

    # 收尾：撤掉这一节的章节并换回原来的章节集合，避免污染后面的用例
    for chapter in queued_chapters:
        engine.stop_chapter(chapter.chapter_id)
    engine._client.stream_chat = fake_stream_chat  # type: ignore[assignment]
    cfg.sampling.concurrency = stop_before_queue
    engine.apply_config(cfg)
    engine.set_chapters(chapters)
    for chapter in chapters:
        if chapter.queued_or_busy:
            engine.stop_chapter(chapter.chapter_id)
    deadline = time.time() + 10
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.04)
        if not any(c.queued_or_busy for c in chapters) and not any(
            c.queued_or_busy for c in queued_chapters
        ):
            break

    print("8.5) 界面默认视图（只看最后正文 + 折叠对话设置）")
    from gui.conversation_view import ConversationView

    conv_view = ConversationView("generate")
    conv_view.set_chapter(chapters[0])
    if conv_view._detail:
        fail("对话视图默认不应处于“完整对话”模式")
    if len(conv_view._bubbles) != 1:
        fail(f"默认应只显示 1 条（最后正文）气泡，实际 {len(conv_view._bubbles)}")
    elif chapters[0].generated_content and conv_view._bubbles[0].content != chapters[0].generated_content:
        fail("默认显示的内容不是最后一次生成的正文")
    if conv_view.settings_window_open():
        fail("对话设置默认不应打开（它是独立窗口）")
    conv_view.show_detail(True)
    if len(conv_view._bubbles) < 2:
        fail(f"完整对话模式下气泡数异常：{len(conv_view._bubbles)}")
    conv_view.show_detail(False)
    if len(conv_view._bubbles) != 1:
        fail("切回“只看最后正文”后气泡数未恢复为 1")

    print("8.6) 检查视图：完整对话 + 结果落入可编辑框 + 按要求重新生成回到正文页")
    from gui.chapter_view import ChapterView

    # 先做一次手动检查，模拟"点触发检查 -> 进检查视图 -> 看完整对话"
    target = chapters[2]
    engine.submit_check(target.chapter_id)
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        if target.state == ChapterState.AWAITING_DECISION:
            break
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    if target.state != ChapterState.AWAITING_DECISION:
        fail(f"手动检查后状态异常：{target.state}")

    chapter_view = ChapterView()
    chapter_view.set_chapter(target)
    # 该章状态为"待决策"：按需求，点进章节应默认显示检查视图
    if chapter_view.current_view() != "检查视图":
        fail(f"待决策章节选中后默认视图应为检查视图，实际 {chapter_view.current_view()}")
    chapter_view.show_check_conversation()
    if chapter_view.current_view() != "检查视图":
        fail("show_check_conversation 未切到检查视图")
    if not chapter_view.check_view._detail:
        fail("检查视图应展示完整对话")

    # 决策行（通过 / 按要求重新生成正文）：正文视图与检查视图都显示，生成对话视图不显示
    chapter_view.show_view("正文")
    if not chapter_view.decision_row.isVisibleTo(chapter_view):
        fail("正文视图下应显示决策行（通过 / 按要求重新生成正文）")
    if not chapter_view.accept_button.isEnabled():
        fail("待决策状态下正文视图的“通过”按钮应可用")
    if chapter_view.accept_button.text() != "通过":
        fail(f"“通过”按钮文字不对：{chapter_view.accept_button.text()!r}")
    chapter_view.show_view("生成对话")
    if chapter_view.decision_row.isVisibleTo(chapter_view):
        fail("生成对话视图下不应显示决策行")

    chapter_view.refresh_check_result()
    if chapter_view.check_edit.toPlainText() != target.check_result:
        fail("检查结果未同步到可编辑文本框")

    # 模拟用户手动修改检查意见，再点"按要求重新生成正文"
    chapter_view.check_edit.setPlainText("把结尾改成开放式的。")
    before_messages = len(target.generate_conv.messages)
    before_regen = target.regen_count
    engine.set_check_result(target.chapter_id, chapter_view.check_edit.toPlainText())
    target.check_result = chapter_view.check_edit.toPlainText()
    engine.submit_generate(target.chapter_id, "regen_by_check", target.check_result)
    chapter_view.show_view("正文")          # MainWindow 在按钮回调里做的事
    if chapter_view.current_view() != "正文":
        fail("按要求重新生成后应回到正文页")

    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        if (
            target.state == ChapterState.AWAITING_DECISION
            and target.regen_count == before_regen + 1
        ):
            break
    if target.regen_count != before_regen + 1:
        fail(f"按要求重新生成后计数异常：{target.regen_count}（期望 {before_regen + 1}）")
    regen_msg = target.generate_conv.messages[before_messages].content
    if "把结尾改成开放式的。" not in regen_msg:
        fail("用户修改后的检查意见未被填入 {check_result}：" + repr(regen_msg))

    print("9) 导出")
    from core import exporter
    from core.models import ChapterState as _CS
    print("   最终状态：" + "，".join(f"{c.title}={c.state}" for c in chapters))
    expected_unconfirmed = sum(
        1 for c in chapters if c.state != _CS.PASSED and c.generated_content.strip()
    )
    plan = exporter.build_export_plan(chapters, exporter.ExportOptions())
    if plan.exported != 3:
        fail(f"导出章数异常：{plan.exported}")
    if plan.unconfirmed != expected_unconfirmed:
        fail(f"未确认章数异常：{plan.unconfirmed}（应为 {expected_unconfirmed}）")
    if expected_unconfirmed < 1:
        fail("未确认章数为 0，导出标注逻辑未被覆盖")
    passed_only = exporter.build_export_plan(
        chapters, exporter.ExportOptions(mode=exporter.MODE_PASSED_ONLY)
    )
    if passed_only.exported + passed_only.skipped != len(chapters):
        fail("仅合格模式统计异常")

    print("10) 软件整体接线（MainWindow + 引擎信号：触发检查→检查视图→结果入框→按要求重生成→正文页）")
    from gui.main_window import MainWindow

    window = MainWindow(cfg)
    window.engine.shutdown()          # 复用外部已启动的引擎
    window.engine = engine
    window._connect_engine()
    window.outline_panel.set_chapters(chapters)
    window.engine.set_chapters(chapters)
    window.outline_panel.select_chapter(chapters[0].chapter_id)
    for _ in range(10):
        app.processEvents()
        time.sleep(0.02)

    # 第 1 章此时处于"待决策"：按需求默认进入检查视图
    if window.chapter_view.current_view() != "检查视图":
        fail(
            "待决策章节选中后应默认进入检查视图，实际 "
            f"{window.chapter_view.current_view()}"
        )

    # 手动触发检查：应切到检查视图并展示完整对话
    window._check_current()
    for _ in range(30):
        app.processEvents()
        time.sleep(0.03)
        if window.chapter_view.current_view() == "检查视图":
            break
    if window.chapter_view.current_view() != "检查视图":
        fail("手动触发检查后未切到检查视图")
    if not window.chapter_view.check_view._detail:
        fail("检查视图未展示完整对话")

    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
        if chapters[0].state == ChapterState.AWAITING_DECISION:
            break
    for _ in range(20):
        app.processEvents()
        time.sleep(0.02)
    if window.chapter_view.check_edit.toPlainText() != chapters[0].check_result:
        fail("检查结果未自动填入界面文本框")

    # 手动改检查意见后点"按要求重新生成正文"：应回到正文页并在那里实时显示正文
    chapter_view = window.chapter_view
    _orig_begin = chapter_view.begin_content_stream
    _orig_end = chapter_view.end_content_stream
    _trace: list[str] = []

    def _begin() -> None:
        _trace.append("begin")
        _orig_begin()

    def _end(text: str = "") -> None:
        _trace.append("end")
        _orig_end(text)

    chapter_view.begin_content_stream = _begin          # type: ignore[method-assign]
    chapter_view.end_content_stream = _end              # type: ignore[method-assign]

    chapter_view.check_edit.setPlainText("这里是我手动改过的意见。")
    window._regen_by_check()
    if chapter_view.current_view() != "正文":
        fail("按要求重新生成后未回到正文页")
    deadline = time.time() + 20
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.03)
        if chapters[0].state == ChapterState.AWAITING_DECISION and "end" in _trace:
            break
    if "begin" not in _trace:
        fail(f"按要求重新生成时正文页未进入流式显示状态（轨迹：{_trace}）")
    if "end" not in _trace:
        fail(f"流式结束后正文页未收尾（轨迹：{_trace}）")
    if chapter_view.content_edit.toPlainText() != chapters[0].generated_content:
        fail("流式结束后正文页内容与章节正文不一致")
    last_msg = None
    for message in reversed(chapters[0].generate_conv.messages):
        if message.role == "user":
            last_msg = message.content
            break
    if last_msg is None or "这里是我手动改过的意见。" not in last_msg:
        fail("手动修改后的检查意见未被填入 {check_result}")

    print("10.1) 慢速流：正文页应能在生成过程中实时显示（逐段追加）")
    async def slow_stream_chat(
        messages, *, thinking, sampling=None, on_event=None, cancel_event=None
    ):
        for piece in ("第一段。", "第二段。", "第三段。"):
            yield StreamDelta(kind="content", text=piece)
            await _asyncio.sleep(0.12)
        yield StreamDelta(kind="done")

    engine._client.stream_chat = slow_stream_chat  # type: ignore[assignment]
    cfg.ui.auto_check_after_generate = False
    engine.apply_config(cfg)

    _trace.clear()
    window._regen_by_check()
    seen_mid_text = ""
    deadline = time.time() + 15
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
        if chapter_view.is_streaming_content():
            seen_mid_text = chapter_view.content_edit.toPlainText()
            if seen_mid_text:
                break
    if not seen_mid_text:
        fail("慢速流过程中未观察到正文页的实时内容")
    elif "第一段。" not in seen_mid_text:
        fail(f"实时显示的内容异常：{seen_mid_text!r}")
    else:
        print(f"   生成中实时显示：{seen_mid_text!r}")

    deadline = time.time() + 15
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.03)
        if not chapter_view.is_streaming_content() and "end" in _trace:
            break
    if chapter_view.content_edit.toPlainText() != chapters[0].generated_content:
        fail("慢速流结束后正文页内容与章节正文不一致")
    else:
        print(f"   流式结束后正文：{chapter_view.content_edit.toPlainText()!r}")

    print("10.2) 项目对话持久化：打开项目后能接着之前的对话，正文不被清空")
    from core import project as _project_mod

    proj_base = Path(_make_workspace_dir("proj"))
    try:
        # 先把当前界面（chapters[1] 已合格、chapters[0] 有完整对话）存成一个项目
        window._autosave_chapters()
        project = _project_mod.create_project(
            proj_base,
            "对话持久化测试",
            outline_text=window.outline_panel.outline_text(),
            chapters_payload=window._chapters_payload(),
        )
        window._bind_project(project)
        window._autosave_chapters()
        window._flush_conversations(force=True)

        if not project.conversations_path.exists():
            fail(f"未生成对话存档：{project.conversations_path}")
        records = _project_mod.read_conversations(project)
        if len(records) < 2:
            fail(f"对话存档条数异常：{len(records)}")
        saved_msgs = sum(len(r["conv"]["messages"]) for r in records)
        if saved_msgs <= 0:
            fail("对话存档里没有任何消息")

        # 模拟"关掉软件再打开"：清空内存里的对话与正文，然后重新读项目
        expected_content = chapters[0].generated_content
        expected_msgs = len(chapters[0].generate_conv.messages)
        expected_conv_id = chapters[0].generate_conv.conv_id
        for chapter in chapters:
            chapter.generate_conv = None
            chapter.check_conv = None
            chapter.generated_content = ""
        if not window._open_project(project):
            fail("重新打开项目失败")
        reopened = window.outline_panel.chapters
        if not reopened:
            fail("重新打开项目后没有章节")
        else:
            first = reopened[0]
            if first.generated_content != expected_content:
                fail("重新打开项目后正文丢失（不应被清空）")
            if first.generate_conv is None or not first.generate_conv.messages:
                fail("重新打开项目后生成对话没有恢复")
            elif len(first.generate_conv.messages) != expected_msgs:
                fail(
                    f"重新打开项目后生成对话消息数不符："
                    f"{len(first.generate_conv.messages)} != {expected_msgs}"
                )
            elif first.generate_conv.conv_id != expected_conv_id:
                fail("重新打开项目后对话 id 变了（不是同一段对话）")
            else:
                print(
                    f"   恢复成功：正文 {len(first.generated_content)} 字符，"
                    f"生成对话 {len(first.generate_conv.messages)} 条消息"
                )
            # 检查对话是"每次全新"的，这里只要求它能被存档/读回
            if first.check_conv is None:
                print("   提示：该章检查对话尚未创建，属正常情况")
        window._bind_project(None)
    finally:
        import shutil as _shutil

        _shutil.rmtree(proj_base, ignore_errors=True)

    window.close()
    app.processEvents()

    engine.shutdown()
    app.processEvents()

    print()
    if ok:
        print("端到端自检通过。")
        return 0
    print("端到端自检存在失败项。")
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
