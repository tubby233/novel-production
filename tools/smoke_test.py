"""开发期无头自检（不联网、不启动引擎线程）。

用法：
    python tools/smoke_test.py

会做这些事情：
1. 逐个构造 GUI 组件（QApplication 使用 offscreen 平台，无需显示器）
2. 校验所有模板占位符、参数说明覆盖率
3. 校验章节状态机的合法/非法迁移
4. 校验导出逻辑（合格/未确认/仅合格三种情况）
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _make_data_dir() -> str:
    """自检用的数据目录（配置/章节落在这里，避免污染源码目录）。

    刻意不用 ``tempfile.mkdtemp``：它用 0o700 建目录，在部分受限环境下
    会让随后在其中的 ``mkdir`` 报 WinError 5，也会让清理失败。
    """
    for attempt in range(50):
        target = ROOT / f"selftest_data_{os.getpid()}_{attempt}"
        if target.exists():
            continue
        try:
            target.mkdir()
        except OSError:
            continue
        return str(target)
    return tempfile.mkdtemp(prefix="novel_gen_selftest_")      # pragma: no cover


_TMP_DATA = _make_data_dir()
os.environ["APPDATA"] = _TMP_DATA
os.environ["LOCALAPPDATA"] = _TMP_DATA

FAILURES: list[str] = []


def temp_dir(prefix: str) -> Path:
    """建一个临时目录（自检结束后由调用方删除）。

    刻意不用 ``tempfile.mkdtemp``：它用 0o700 建目录，在部分受限环境下
    会让随后在其中的 ``mkdir`` 报 WinError 5。这里用普通权限自己建。
    """
    for attempt in range(50):
        target = ROOT / f"{prefix}{os.getpid()}_{attempt}"
        if target.exists():
            continue
        try:
            target.mkdir()
        except OSError:
            continue
        return target
    fallback = Path(tempfile.mkdtemp(prefix=prefix))   # pragma: no cover
    return fallback


def check(name: str, func) -> None:
    try:
        func()
        print(f"[OK]   {name}")
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(name)
        import traceback

        print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        traceback.print_exc()


def test_param_help() -> None:
    from core import param_help

    keys = param_help.all_keys()
    assert len(keys) >= 55, f"参数说明条目过少：{len(keys)}"
    for key in keys:
        item = param_help.get_help(key)
        assert item.effect or item.kind in ("display", "group"), f"{key} 缺少影响说明"
        html = item.to_html()
        assert item.name_cn in html
    missing = param_help.missing_help_keys(["temperature", "不存在的键"])
    assert missing == ["不存在的键"], missing


def test_templates() -> None:
    from core import message_templates as MT

    for kind in MT.TemplateKind:
        spec = MT.get_spec(kind)
        result = MT.validate_template(kind, spec.default)
        assert result.ok, f"{spec.label} 默认模板缺少占位符：{result.missing}"

    text = MT.build_first_generate_user_msg("第一章大纲")
    assert "第一章大纲" in text
    # 正文里出现花括号不应导致异常
    tricky = MT.build_check_user_msg("正文里有 {花括号} 和 {outline} 字样")
    assert "{outline}" in tricky


def test_project() -> None:
    """项目制：一部小说 = 一个以小说名命名的文件夹，可创建、可读回。"""
    import shutil

    from core import project as project_mod

    base = Path(temp_dir("selftest_proj_"))
    try:
        payload = {
            "version": 1,
            "chapters": [{"chapter_id": "ch_1", "index": 1, "title": "第一章", "outline": "大纲"}],
        }
        created = project_mod.create_project(
            base, "星海/归途", outline_text="第一章 启航\n内容", chapters_payload=payload
        )
        # 非法字符被安全化，文件夹名可直接使用
        assert created.root.name == "星海_归途", created.root.name
        assert created.root.is_dir()
        assert created.chapters_path.exists() and created.outline_path.exists()
        assert created.meta_path.exists()

        # 同名项目不会被静默覆盖
        try:
            project_mod.create_project(base, "星海/归途")
        except FileExistsError:
            pass
        else:  # pragma: no cover
            raise AssertionError("同名项目应拒绝创建")

        # 列出与读回
        found = project_mod.list_projects(base)
        assert [p.name for p in found] == ["星海_归途"], [p.name for p in found]
        reopened = project_mod.load_project(created.root)
        assert project_mod.read_chapters(reopened) == payload["chapters"]
        assert "第一章 启航" in project_mod.read_outline(reopened)
        assert project_mod.is_project_dir(created.root)
        assert not project_mod.is_project_dir(base / "不存在的目录")
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_job_queue() -> None:
    """任务队列：FIFO、拖动重排、按章移除、in-flight 跟踪。"""
    import asyncio

    from core.engine import Job, JobQueue

    queue = JobQueue()
    jobs = [Job(chapter_id=f"ch{i}", kind="generate") for i in (1, 2, 3)]
    for job in jobs:
        queue.put(job)
    assert len(queue) == 3
    assert all(job.job_id for job in jobs), "每个任务都应有稳定的 job_id"

    # 先提交先执行
    first = queue.get_nowait()
    assert first is jobs[0]
    assert first.job_id in queue.inflight
    assert len(queue) == 2

    # 拖动排序：把最后一个提到最前
    queue.reorder([jobs[2].job_id, jobs[1].job_id])
    assert [job.job_id for job in queue.items()] == [jobs[2].job_id, jobs[1].job_id]
    assert queue.get_nowait() is jobs[2]
    assert queue.get_nowait() is jobs[1]
    assert queue.get_nowait() is None

    # 按章移除
    queue.put(Job(chapter_id="a", kind="check"))
    queue.put(Job(chapter_id="b", kind="check"))
    queue.put(Job(chapter_id="a", kind="check"))
    assert queue.remove_chapter("a") == 2
    assert [job.chapter_id for job in queue.items()] == ["b"]

    # 结束跟踪：取出的任务才算"在跑"
    queue.clear()
    task_done_probe = Job(chapter_id="c", kind="generate")
    queue.put(task_done_probe)
    taken = queue.get_nowait()
    assert taken is task_done_probe
    queue.task_done(task_done_probe)
    assert task_done_probe.job_id not in queue.inflight
    assert len(queue) == 0
    assert queue.clear() == []

    # 异步 get()：空队列时挂起，put 后立刻返回
    async def roundtrip() -> None:
        probe = Job(chapter_id="d", kind="generate")
        queue.put(probe)
        got = await asyncio.wait_for(queue.get(), timeout=1)
        assert got is probe

    asyncio.run(roundtrip())


def test_state_machine() -> None:
    from core.models import ALLOWED_TRANSITIONS, ChapterState, can_transition

    assert can_transition(ChapterState.PENDING, ChapterState.GENERATING)
    assert not can_transition(ChapterState.PENDING, ChapterState.PASSED)
    assert can_transition(ChapterState.GENERATING, ChapterState.TO_CHECK)
    assert can_transition(ChapterState.CHECKING, ChapterState.AWAITING_DECISION)
    assert can_transition(ChapterState.FAILED, ChapterState.GENERATING)
    # 排队中：提交后进入、开始执行时离开、被停止时能回退
    assert can_transition(ChapterState.PENDING, ChapterState.QUEUED)
    assert can_transition(ChapterState.TO_CHECK, ChapterState.QUEUED)
    assert can_transition(ChapterState.QUEUED, ChapterState.GENERATING)
    assert can_transition(ChapterState.QUEUED, ChapterState.CHECKING)
    for state in ChapterState:
        assert state in ALLOWED_TRANSITIONS
        assert ALLOWED_TRANSITIONS[state], f"{state} 没有任何出边"
    assert not ChapterState.QUEUED.busy, "排队中不算忙：用户仍可改主意或停止"
    assert ChapterState.QUEUED.queued_or_busy


def test_outline_parser() -> None:
    from core import outline_parser as op

    text = (
        "第一章 觉醒\n本章内容：主角苏醒\n\n"
        "第 2 章 试炼\n本章内容：试炼\n\n"
        "【第三章】归途\n本章内容：返程\n\n"
        "Chapter 4 Return\ncontent"
    )
    chapters = op.split_outline(text)
    assert len(chapters) == 4, [c.title for c in chapters]
    assert chapters[0].outline.startswith("第一章")
    assert op.split_outline("没有章节标题的纯文本") == []
    assert op.split_as_single("整篇内容")[0].outline.strip() == "整篇内容"
    assert len(op.split_by_blank_lines("短句。\n\n" * 30)) >= 1
    ok, _ = op.validate_pattern("[")  # 非法正则
    assert not ok


def test_config() -> None:
    from core import config as cfg_mod

    cfg = cfg_mod.AppConfig()
    assert cfg.api.model == "deepseek-flash"
    assert cfg.sampling.max_tokens == 393_216
    assert cfg.api.timeout_seconds == 1800
    assert cfg.thinking.generate.effort == "high"
    assert cfg.thinking.check.effort == "max"
    limits = cfg_mod.model_limits("deepseek-flash")
    assert limits.context_window == 1_048_576 and limits.max_output == 393_216
    data = cfg_mod.config_to_dict(cfg)
    restored = cfg_mod.config_from_dict(data)
    assert restored.api.model == cfg.api.model
    # 脏数据容错
    dirty, warnings = cfg_mod.config_from_dict({"sampling": {"max_tokens": "abc", "concurrency": 99999}}), None
    assert dirty.sampling.concurrency <= 2500


def test_normalize_params() -> None:
    from core.api_client import DeepSeekClient
    from core.config import ApiConfig, SamplingConfig
    from core.models import ThinkingConfig

    client = DeepSeekClient(ApiConfig(api_key="x"), SamplingConfig(top_p=0.5, max_tokens=393_216))
    thinking = client.normalize_params(
        ThinkingConfig(enabled=True, effort="max"), SamplingConfig(top_p=0.5), prompt_chars=1000
    )
    payload = thinking.payload
    assert payload["extra_body"]["thinking"]["type"] == "enabled"
    assert payload["reasoning_effort"] == "max"
    assert payload["top_p"] == 0.95, payload["top_p"]
    assert "temperature" not in payload
    assert any("抬升" in note for note in thinking.notes)

    plain = client.normalize_params(
        ThinkingConfig(enabled=False), SamplingConfig(temperature=0.8, top_p=0.3), prompt_chars=1000
    )
    assert plain.payload["top_p"] == 1.0
    assert plain.payload["temperature"] == 0.8
    assert plain.payload["extra_body"]["thinking"]["type"] == "disabled"
    # 已弃用参数：SamplingConfig 里已不存在，请求里自然也不会有
    from dataclasses import fields as _fields

    names = {f.name for f in _fields(SamplingConfig)}
    assert "frequency_penalty" not in names and "presence_penalty" not in names
    assert "frequency_penalty" not in plain.payload
    assert "presence_penalty" not in plain.payload
    # 兼容旧配置：读到这两个字段时忽略并给出提示
    from core import config as config_mod

    legacy_warnings: list[str] = []
    legacy_cfg = config_mod.config_from_dict(
        {"sampling": {"frequency_penalty": 0.5, "presence_penalty": 0.5}}, legacy_warnings
    )
    assert legacy_cfg.sampling.top_p == 1.0
    assert len(legacy_warnings) == 2, legacy_warnings
    assert all("弃用" in w for w in legacy_warnings)


def test_exporter() -> None:
    from core import exporter
    from core.models import Chapter, ChapterState

    a = Chapter.create(1, "第一章", "大纲1")
    a.state = ChapterState.PASSED
    a.generated_content = "正文一"
    b = Chapter.create(2, "第二章", "大纲2")
    b.generated_content = "正文二"
    b.check_result = "检查意见"

    opts = exporter.ExportOptions()
    plan = exporter.build_export_plan([a, b], opts)
    assert plan.exported == 2 and plan.unconfirmed == 1
    assert "【未确认】" in plan.text and "检查意见" in plan.text
    assert "第一章" in plan.text and "正文一" in plan.text

    opts2 = exporter.ExportOptions(mode=exporter.MODE_PASSED_ONLY)
    plan2 = exporter.build_export_plan([a, b], opts2)
    assert plan2.exported == 1 and plan2.skipped == 1
    assert "第二章" not in plan2.text


def test_project_load_states() -> None:
    """读取项目：章节状态、正文、检查结果必须完整恢复（不能一律变回待生成）。"""
    import shutil

    from core import project as project_mod
    from core.config import AppConfig
    from core.models import ChapterState
    from gui.main_window import MainWindow

    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    base = Path(temp_dir("selftest_load_"))
    try:
        # 直接写一份"各种状态都有"的项目数据，模拟用户真实存档
        chapters = [
            {
                "chapter_id": "ch_a",
                "index": 1,
                "title": "第1章",
                "outline": "大纲1",
                "outline_snapshot": "大纲1",
                "state": "awaiting_decision",
                "generated_content": "第一章正文",
                "check_result": "第一章检查意见",
                "check_history": ["第一章检查意见"],
                "regen_count": 1,
                "last_error": "",
                "generate_conv": {
                    "conv_id": "gen_a",
                    "kind": "generate",
                    "thinking": {"enabled": True, "effort": "high", "show_reasoning": True},
                    "status": "done",
                    "messages": [
                        {"role": "user", "content": "大纲1", "source": "first_generate"},
                        {"role": "assistant", "content": "第一章正文", "source": "assistant"},
                    ],
                },
                "check_conv": None,
            },
            {
                "chapter_id": "ch_b",
                "index": 2,
                "title": "第2章",
                "outline": "大纲2",
                "state": "passed",
                "generated_content": "第二章正文",
                "check_result": "第二章检查意见",
                "check_history": ["第二章检查意见"],
                "regen_count": 0,
                "last_error": "",
                "generate_conv": None,
                "check_conv": None,
            },
            {
                "chapter_id": "ch_c",
                "index": 3,
                "title": "第3章",
                "outline": "大纲3",
                "state": "queued",          # 运行期状态：应回落而不是丢内容
                "generated_content": "第三章正文",
                "check_result": "",
                "check_history": [],
                "regen_count": 0,
                "last_error": "",
                "generate_conv": None,
                "check_conv": None,
            },
            {
                "chapter_id": "ch_d",
                "index": 4,
                "title": "第4章",
                "outline": "大纲4",
                "state": "pending",
                "generated_content": "",
                "check_result": "",
                "check_history": [],
                "regen_count": 0,
                "last_error": "",
                "generate_conv": None,
                "check_conv": None,
            },
        ]
        project = project_mod.create_project(
            base,
            "状态恢复测试",
            outline_text="大纲1\n大纲2\n大纲3\n大纲4",
            chapters_payload={"version": 1, "chapters": chapters},
        )

        window = MainWindow(AppConfig())
        try:
            assert window._open_project(project), "打开项目应成功"
            loaded = {c.chapter_id: c for c in window.outline_panel.chapters}
            assert set(loaded) == {"ch_a", "ch_b", "ch_c", "ch_d"}, list(loaded)
            checks = {
                "ch_a": (ChapterState.AWAITING_DECISION, "第一章正文", "第一章检查意见"),
                "ch_b": (ChapterState.PASSED, "第二章正文", "第二章检查意见"),
                "ch_c": (ChapterState.TO_CHECK, "第三章正文", ""),   # queued -> 待检查
                "ch_d": (ChapterState.PENDING, "", ""),
            }
            for chapter_id, (state, content, check) in checks.items():
                chapter = loaded[chapter_id]
                assert chapter.state == state, (
                    f"{chapter.title} 状态应为 {state}，实际 {chapter.state}"
                )
                assert chapter.generated_content == content, (
                    f"{chapter.title} 正文未恢复：{chapter.generated_content!r}"
                )
                assert chapter.check_result == check, (
                    f"{chapter.title} 检查结果未恢复：{chapter.check_result!r}"
                )
            # 对话也要恢复
            assert loaded["ch_a"].generate_conv is not None
            assert len(loaded["ch_a"].generate_conv.messages) == 2
        finally:
            window.engine.shutdown()
            window.close()
            app.processEvents()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_split_does_not_overwrite_project() -> None:
    """回归：切分新大纲时**绝不能**把新章节写进"原项目"覆盖原进度。

    事故场景（用户实测）：打开项目 A → 粘贴新大纲点切分 → 用户还在输入小说名
    （对话框开着，可能停留好几秒）→ 自动保存的定时器在这期间到期，把**新大纲的
    章节**写进 A 的 chapters.json → 之后才建新项目 B → A 的进度被覆盖。

    因此本用例刻意"在对话框打开期间让定时器到期"，这正是修复前会覆盖原项目的时刻。
    """
    import shutil
    import time

    from PySide6.QtWidgets import QApplication

    from core import project as project_mod
    from core.config import AppConfig
    from core.models import ChapterState
    from gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    base = Path(temp_dir("selftest_split_"))
    projects_dir = base / "projects"
    try:
        old_chapters = [
            {
                "chapter_id": "old_1",
                "index": 1,
                "title": "旧第1章",
                "outline": "旧大纲1",
                "outline_snapshot": "旧大纲1",
                "state": "awaiting_decision",
                "generated_content": "旧第一章正文",
                "check_result": "旧第一章检查意见",
                "check_history": ["旧第一章检查意见"],
                "regen_count": 2,
                "last_error": "",
                "generate_conv": None,
                "check_conv": None,
            },
            {
                "chapter_id": "old_2",
                "index": 2,
                "title": "旧第2章",
                "outline": "旧大纲2",
                "outline_snapshot": "旧大纲2",
                "state": "passed",
                "generated_content": "旧第二章正文",
                "check_result": "",
                "check_history": [],
                "regen_count": 0,
                "last_error": "",
                "generate_conv": None,
                "check_conv": None,
            },
        ]
        old_project = project_mod.create_project(
            projects_dir,
            "原项目",
            outline_text="旧大纲1\n旧大纲2",
            chapters_payload={"version": 1, "chapters": old_chapters},
        )

        cfg = AppConfig()
        # 自检绝不能往用户真实的 Documents\NovelGenProjects 里建项目
        cfg.ui.project_dir = str(projects_dir)
        window = MainWindow(cfg)
        try:
            assert window._open_project(old_project), "打开原项目应成功"
            assert len(window.outline_panel.chapters) == 2
            loaded_snapshot = old_project.chapters_path.read_text(encoding="utf-8")

            # ---- 模拟用户粘贴新大纲并点"切分章节" ----
            import gui.main_window as mw
            import gui.outline_panel as op

            new_name = f"新项目{os.getpid()}"
            observed: dict[str, object] = {}

            def fake_ask(*_a, **_k):
                """模拟"用户在对话框里停留 1.6 秒"——足够让自动保存定时器到期。"""
                observed["guard"] = getattr(window, "_project_setup_in_progress", None)
                observed["timer_active"] = window._autosave_timer.isActive()
                deadline = time.time() + 1.6
                while time.time() < deadline:
                    app.processEvents()
                    time.sleep(0.05)
                observed["snapshot_while_open"] = old_project.chapters_path.read_text(
                    encoding="utf-8"
                )
                observed["chapters_while_open"] = len(project_mod.read_chapters(old_project))
                return True, new_name

            original_ask = mw.ask_text
            original_confirm = op.confirm
            original_error = mw.error
            original_warn = mw.warn
            original_info = mw.info
            mw.ask_text = fake_ask
            op.confirm = lambda *a, **k: True
            mw.error = lambda *a, **k: None
            mw.warn = lambda *a, **k: None
            mw.info = lambda *a, **k: None       # 立项完成提示：无头环境必须吞掉
            try:
                window.outline_panel.set_outline_text(
                    "第1章 新\n新内容一\n\n第2章 新\n新内容二\n\n第3章 新\n新内容三"
                )
                window.outline_panel.split_outline()
            finally:
                mw.ask_text = original_ask
                op.confirm = original_confirm
                mw.error = original_error
                mw.warn = original_warn
                mw.info = original_info

            # (1) 对话框打开期间：自动保存必须被挂起，原项目一个字节都不能动
            assert observed.get("guard") is True, "立项期间没有置起「禁止自动保存」标记"
            assert observed.get("timer_active") is False, (
                "立项对话框打开时不应安排自动保存（会把新大纲写进原项目）"
            )
            assert observed.get("snapshot_while_open") == loaded_snapshot, (
                "用户还在输入小说名时，原项目的 chapters.json 就被改写了！"
            )
            assert observed.get("chapters_while_open") == 2, (
                f"原项目章节数在立项期间被改成 {observed.get('chapters_while_open')}"
            )

            # (2) 切分完成后：原项目章节数据仍然原封不动
            assert (
                old_project.chapters_path.read_text(encoding="utf-8") == loaded_snapshot
            ), "切分新大纲时把原项目的 chapters.json 覆盖了！"
            reloaded_old = project_mod.read_chapters(old_project)
            assert len(reloaded_old) == 2, f"原项目章节数被改成 {len(reloaded_old)}"
            assert reloaded_old[0]["generated_content"] == "旧第一章正文"
            assert reloaded_old[0]["state"] == "awaiting_decision"
            assert reloaded_old[0]["regen_count"] == 2
            assert project_mod.read_outline(old_project) == "旧大纲1\n旧大纲2", (
                "原项目的 outline.txt 被新大纲覆盖了！"
            )

            # (3) 新项目应该带着新章节
            assert window.project is not None and window.project.name == new_name, (
                f"应切换到新项目，实际 {getattr(window.project, 'name', None)}"
            )
            new_items = project_mod.read_chapters(window.project)
            assert len(new_items) == 3, f"新项目应有 3 章，实际 {len(new_items)}"
            assert "新内容一" in project_mod.read_outline(window.project)

            # (4) 再等一会儿（让任何延迟的定时器有机会触发），原项目仍不能被改动
            for _ in range(40):
                app.processEvents()
                time.sleep(0.05)
            assert (
                old_project.chapters_path.read_text(encoding="utf-8") == loaded_snapshot
            ), "延迟的自动保存把原项目写坏了"

            # (5) 重新打开原项目，进度完好如初
            assert window._open_project(old_project), "重新打开原项目应成功"
            assert len(window.outline_panel.chapters) == 2
            assert window.outline_panel.chapters[0].generated_content == "旧第一章正文"
            assert window.outline_panel.chapters[0].state == ChapterState.AWAITING_DECISION
        finally:
            window.engine.shutdown()
            window.close()
            app.processEvents()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def test_config_regeneration() -> None:
    """回归：手动删掉配置文件后，程序应自动在目录下重新生成一份。"""
    import shutil

    from core import config as config_mod
    from core import paths as core_paths

    base = Path(temp_dir("selftest_cfg_"))
    original = core_paths.config_dir
    try:
        core_paths.config_dir = lambda: base      # type: ignore[assignment]

        # 1) 文件不存在 -> 重新生成，且给出提示
        cfg, warnings = config_mod.load_config()
        assert (base / "config.json").exists(), "删除后没有重新生成配置文件"
        assert any("重新生成" in w for w in warnings), warnings
        import json

        data = json.loads((base / "config.json").read_text(encoding="utf-8"))
        assert "api" in data and "ui" in data and data.get("version") == 1

        # 2) 文件损坏 -> 备份 .bak 并重新生成
        (base / "config.json").write_text("{ 坏掉的 json", encoding="utf-8")
        cfg, warnings = config_mod.load_config()
        assert (base / "config.json.bak").exists(), "损坏的配置没有备份"
        json.loads((base / "config.json").read_text(encoding="utf-8"))   # 必须是合法 JSON
        assert any("重新生成" in w for w in warnings), warnings

        # 3) 空对象 -> 也补一份完整默认配置
        (base / "config.json").write_text("{}", encoding="utf-8")
        cfg, warnings = config_mod.load_config()
        data = json.loads((base / "config.json").read_text(encoding="utf-8"))
        assert len(data) >= 6, data
        assert any("重新生成" in w for w in warnings), warnings

        # 4) 已有设置不能被默认值覆盖
        saved = config_mod.AppConfig()
        saved.api.api_key = "sk-keep-me"
        saved.sampling.concurrency = 5
        config_mod.save_config(saved)
        cfg, warnings = config_mod.load_config()
        assert not warnings, warnings
        assert cfg.api.api_key == "sk-keep-me" and cfg.sampling.concurrency == 5
    finally:
        core_paths.config_dir = original            # type: ignore[assignment]
        shutil.rmtree(base, ignore_errors=True)


def test_gui() -> None:
    from PySide6.QtWidgets import QApplication

    from core import paths as core_paths

    # 把配置/章节数据的目录重定向到临时目录（否则 MainWindow 关闭时会写进源码目录）
    core_paths.config_dir = lambda: Path(_TMP_DATA)  # type: ignore[assignment]

    from core.models import Chapter, ChapterState, Conversation, Message, ThinkingConfig
    from gui.chapter_view import ChapterView
    from gui.conversation_view import ConversationView
    from gui.export_dialog import ExportDialog
    from gui.help_icon import HelpDialog, HelpIcon
    from gui.main_window import MainWindow
    from gui.message_bubble import MessageBubble
    from gui.outline_panel import ChapterListPanel
    from gui.reasoning_panel import ReasoningPanel
    from gui.settings_dialog import SettingsDialog
    from gui.thinking_panel import ThinkingPanel
    from core.config import AppConfig

    app = QApplication.instance() or QApplication([])
    cfg = AppConfig()

    chapter = Chapter.create(1, "第一章 觉醒", "本章内容：主角苏醒")
    chapter.generate_conv = Conversation.create(chapter.chapter_id, "generate", ThinkingConfig())
    chapter.generate_conv.append(Message("system", "生成提示词", source="system"))
    chapter.generate_conv.append(Message("user", "大纲…", source="first_generate"))
    chapter.generate_conv.append(
        Message("assistant", "正文内容", reasoning="思考过程" * 50, source="assistant")
    )
    chapter.state = ChapterState.AWAITING_DECISION
    chapter.generated_content = "正文内容"
    chapter.check_result = "检查意见"

    panel = ChapterListPanel()
    panel.set_chapters([chapter])
    panel.refresh_chapter(chapter)

    # ---- 章节行内的复选框必须可以点选 ----
    from PySide6.QtWidgets import QCheckBox as _QCheckBox

    item = panel.list_widget.item(0)
    row_widget = panel.list_widget.itemWidget(item)
    checkbox = row_widget.findChild(_QCheckBox)
    assert checkbox is not None, "章节行内应有真实复选框"
    assert checkbox.isChecked() is False
    checkbox.setChecked(True)                       # 模拟点击
    assert panel.selected_chapters() == [chapter], "勾选后应能取出选中章节"
    assert chapter.chapter_id in panel.checked_ids()
    checkbox.setChecked(False)
    assert panel.selected_chapters() == []
    # 点标题也能切换勾选
    panel._toggle_row(chapter.chapter_id)
    assert panel.selected_chapters() == [chapter]
    panel.set_all_checked(False)
    assert panel.selected_chapters() == []

    bubble = MessageBubble("assistant", text="正文", reasoning="思考")
    bubble.begin_stream()
    bubble.append("reasoning", "更多思考")
    bubble.append("content", "更多正文")
    bubble.end_stream(final_text="最终正文")

    reasoning = ReasoningPanel()
    reasoning.begin()
    reasoning.append("思考" * 3000)
    reasoning.answering()
    reasoning.finish()
    assert reasoning.estimated_tokens > 0

    thinking_panel = ThinkingPanel("测试", ThinkingConfig(), global_hide_reasoning=True)
    thinking_panel.set_thinking(ThinkingConfig(enabled=False, effort="low"))

    view = ConversationView("generate")
    view.set_chapter(chapter)

    # ---- 新行为：默认只看最后正文 + 对话设置放在独立窗口 ----
    assert view._detail is False, "默认应处于“只看最后正文”模式"
    assert not hasattr(view, "settings_container"), "对话设置已改为独立窗口，不应再内嵌容器"
    assert view.mode_button.text() == "查看详细对话"
    assert len(view._bubbles) == 1, f"默认只应显示 1 条消息气泡，实际 {len(view._bubbles)}"
    assert view._bubbles[0].content == "正文内容", view._bubbles[0].content[:20]

    # 展开 / 收起对话设置：必须在**新窗口**里打开，且窗口里真的要有内容
    view.show_settings(True)
    assert view._settings_window is not None, "对话设置应在独立窗口中打开"
    assert view._settings_window.isVisible()
    assert view.thinking_panel.window() is view._settings_window
    # 回归：设置面板平时是隐藏的，挪进窗口后必须显式 show()，否则窗口是空的
    assert view.thinking_panel.isVisibleTo(view._settings_window), (
        "对话设置窗口里看不到设置面板（窗口会是空的）"
    )
    from PySide6.QtWidgets import QLabel as _SettingsLabel

    texts = [
        child.text()
        for child in view.thinking_panel.findChildren(_SettingsLabel)
        if child.text()
    ]
    checkbox_texts = [
        child.text()
        for child in view.thinking_panel.findChildren(_QCheckBox)
        if child.text()
    ]
    assert any("思考强度" in t for t in texts), f"设置面板缺少内容：{texts}"
    assert any("思考模式" in t for t in checkbox_texts), f"设置面板缺少开关：{checkbox_texts}"
    assert any("思维链" in t for t in checkbox_texts), f"设置面板缺少开关：{checkbox_texts}"
    view.show_settings(False)
    assert view._settings_window is None, "关闭后设置窗口应被回收"
    assert not view.thinking_panel.isVisibleTo(view), "关闭后设置面板应重新隐藏"

    # 一键折叠 / 展开全部消息
    view.show_detail(True)
    view.expand_all()
    assert all(not b.is_collapsed for b in view._bubbles), "展开全部后不应还有收起的消息"
    view.collapse_all()
    # 只有带思维链的 assistant 消息才需要"收起"
    assert all(b.is_collapsed for b in view._bubbles if b.reasoning_panel.buffer)
    assert not view.collapse_all_button.isEnabled(), "全部收起后“折叠全部”应置灰"
    view.expand_all()
    assert view.collapse_all_button.isEnabled()
    view.show_detail(False)

    # 流式：最后正文模式应实时展现在同一个气泡里
    view.stream_started(chapter.generate_conv)
    view.stream_delta("reasoning", "思考片段")
    view.stream_delta("content", "正文片段")
    assert view._last_bubble is not None
    assert "正文片段" in view._last_bubble.content, view._last_bubble.content
    view.stream_finished(True)
    assert len(view._bubbles) == 1, "流式结束后仍只应有一个最后正文气泡"

    # 切换到完整对话
    view.show_detail(True)
    assert view._detail is True
    assert view.mode_button.text() == "只看最后正文"
    assert len(view._bubbles) == 3, f"完整对话应显示 3 条消息，实际 {len(view._bubbles)}"
    view.show_detail(False)
    assert len(view._bubbles) == 1

    # 切换章节后应回到默认视图与折叠状态
    view.show_detail(True)
    view.show_settings(True)
    view.set_chapter(chapter)
    assert view._detail is False and view._settings_window is None
    assert "消息" in view.title_label.text()

    # ---- 章节处于"检查中/待决策"时，点进章节默认显示检查视图 ----
    chapter_view = ChapterView()
    chapter_view.set_chapter(chapter)          # 该章状态为"待决策"
    chapter_view.set_state(ChapterState.AWAITING_DECISION)
    assert chapter_view.current_view() == "检查视图", chapter_view.current_view()
    assert chapter_view.check_view._detail is True, "检查视图应默认展示完整对话"
    chapter_view.show_view("正文")
    chapter_view.set_chapter(chapter)          # 重新选中同一章也应回到检查视图
    assert chapter_view.current_view() == "检查视图", chapter_view.current_view()
    chapter_view._refresh_buttons()

    # ---- 待决策时：决策行只在「正文」「检查视图」显示，且按钮文字写清楚 ----
    assert chapter_view.accept_button.text() == "通过", chapter_view.accept_button.text()
    assert (
        chapter_view.regen_decision_button.text() == "按要求重新生成正文"
    ), chapter_view.regen_decision_button.text()
    for view_name in ("正文", "检查视图"):
        chapter_view.show_view(view_name)
        assert chapter_view.decision_row.isVisibleTo(chapter_view), (
            f"【{view_name}】下应显示决策行"
        )
        assert chapter_view.accept_button.isEnabled(), f"【{view_name}】下“通过”应可用"
        assert chapter_view.regen_decision_button.isEnabled(), (
            f"【{view_name}】下“按要求重新生成正文”应可用"
        )
    # 生成对话视图下不显示（按需求：只在正文与检查视图可见）
    chapter_view.show_view("生成对话")
    assert not chapter_view.decision_row.isVisibleTo(chapter_view), (
        "【生成对话】下不应显示决策行"
    )

    # ---- 非待决策状态：整个决策行都不显示 ----
    for other_state in (
        ChapterState.PENDING,
        ChapterState.GENERATING,
        ChapterState.TO_CHECK,
        ChapterState.CHECKING,
        ChapterState.PASSED,
        ChapterState.FAILED,
        ChapterState.QUEUED,
    ):
        chapter.state = other_state
        chapter_view.set_state(other_state)
        chapter_view.show_view("正文")
        assert not chapter_view.decision_row.isVisibleTo(chapter_view), (
            f"状态 {other_state} 下不应显示决策行"
        )
        chapter_view.show_view("检查视图")
        assert not chapter_view.decision_row.isVisibleTo(chapter_view), (
            f"状态 {other_state} 下不应显示决策行（检查视图）"
        )
    chapter.state = ChapterState.AWAITING_DECISION
    chapter_view.set_state(chapter.state)
    chapter_view.show_view("正文")
    assert chapter_view.decision_row.isVisibleTo(chapter_view)
    chapter_view._refresh_buttons()

    # ---- 其它状态（例如待生成）默认仍进正文页 ----
    pending = Chapter.create(2, "第二章", "第二章大纲")
    chapter_view.set_chapter(pending)
    assert chapter_view.current_view() == "正文", chapter_view.current_view()
    chapter_view._refresh_buttons()

    # ---- "重新生成"按钮：待生成 -> 按大纲；已有正文/待决策 -> 按要求重写 ----
    assert chapter_view.regen_button.isEnabled(), "待生成状态下“重新生成”应可用"
    assert chapter_view.regen_button.text() == "重新生成（按大纲）", (
        f"待生成时应提示按大纲重新生成，实际 {chapter_view.regen_button.text()!r}"
    )
    pending.state = ChapterState.AWAITING_DECISION
    pending.generated_content = "已有正文"
    pending.generate_conv = None
    chapter_view.set_state(pending.state)
    chapter_view._refresh_buttons()
    assert chapter_view.regen_button.text() == "重新生成", chapter_view.regen_button.text()
    assert chapter_view.regen_button.isEnabled()
    # 清空生成对话后引擎会把状态置回"待生成"，此时应回到"按大纲"
    pending.state = ChapterState.PENDING
    chapter_view._refresh_buttons()
    assert chapter_view.regen_button.text() == "重新生成（按大纲）", (
        "清空生成对话（状态回到待生成）后应回到“按大纲重新生成”"
    )

    chapter_view.set_chapter(chapter)
    chapter_view._refresh_buttons()

    # ---- 排队中：状态徽标、按钮互斥、提示条 ----
    chapter.state = ChapterState.QUEUED
    chapter_view.set_state(chapter.state)
    chapter_view._refresh_buttons()
    assert not chapter_view.generate_button.isEnabled(), "排队中不应重复提交生成"
    assert chapter_view.stop_button.isEnabled(), "排队中应能停止（把队列里的任务撤下来）"
    chapter_view.show_toast("已排队")
    assert chapter_view.toast_label.isVisibleTo(chapter_view)
    assert "排队" in chapter_view.toast_label.text()
    chapter_view.hide_toast()
    assert not chapter_view.toast_label.isVisibleTo(chapter_view)
    chapter.state = ChapterState.AWAITING_DECISION
    chapter_view.set_state(chapter.state)
    chapter_view._refresh_buttons()

    # ---- 正文页流式显示 ----
    chapter_view.begin_content_stream()
    assert chapter_view.is_streaming_content()
    assert chapter_view.content_edit.toPlainText() == ""
    chapter_view.append_content_stream("第一段。")
    chapter_view.append_content_stream("第二段。")
    assert chapter_view.content_edit.toPlainText() == "第一段。第二段。"
    chapter_view.end_content_stream("最终正文X")
    assert not chapter_view.is_streaming_content()
    assert chapter_view.content_edit.toPlainText() == "最终正文X"

    # ---- 检查视图：完整对话默认展开 + 结果同步到可编辑框 ----
    chapter.check_result = "新的检查意见"
    chapter_view.show_check_conversation()
    assert chapter_view.current_view() == "检查视图"
    assert chapter_view.check_view._detail is True
    chapter_view.refresh_check_result()
    assert chapter_view.check_edit.toPlainText() == "新的检查意见"

    # ---- 视图切换：按钮组，单击一次即生效 ----
    for name in ("生成对话", "检查视图", "正文"):
        chapter_view.view_buttons[name].click()
        assert chapter_view.current_view() == name, f"单击【{name}】未切换视图"
    assert chapter_view.content_page.isVisibleTo(chapter_view)

    # ---- 键位与自检相关：新选项应已登记说明 ----
    from core import param_help as _ph

    assert _ph.missing_help_keys(
        ["exp_dir", "project_dir", "act_open_project", "act_show_log", "state_queued"]
    ) == []
    # "每章正文前加标题"已移除（正文自带标题，重复添加会难看）
    assert "exp_include_title" not in _ph.all_keys()
    from core.config import UiConfig as _UiConfig
    from dataclasses import fields as _fields2

    assert "export_include_title" not in {f.name for f in _fields2(_UiConfig)}

    icon = HelpIcon("temperature")
    assert "温度" in icon.toolTip()
    dialog = HelpDialog("temperature")
    dialog.deleteLater()

    settings = SettingsDialog(cfg)
    # 设置面板不应再出现"已弃用参数"的说明文字（这些参数已被移除，不再解释）
    from PySide6.QtWidgets import QLabel as _QLabel

    settings_text = " ".join(
        label.text() for label in settings.findChildren(_QLabel)
    )
    assert "frequency_penalty" not in settings_text, "设置面板仍写着已弃用参数说明"
    assert "presence_penalty" not in settings_text, "设置面板仍写着已弃用参数说明"
    assert "deprecated" not in settings_text, "设置面板仍写着已弃用参数说明"
    settings.deleteLater()

    export = ExportDialog([chapter], cfg)
    export._refresh_preview()
    export.deleteLater()

    window = MainWindow(cfg)
    from core import outline_parser as _outline_parser
    from core import project as _project_mod

    window.engine.set_chapters([chapter])
    window.outline_panel.set_chapters([chapter])
    window._on_chapter_selected(chapter.chapter_id)
    window._on_state_changed(chapter.chapter_id, str(ChapterState.AWAITING_DECISION))
    window._on_chapter_updated(chapter.chapter_id)
    window._qa_check()

    # ---- 日志：默认不显示，点击后才在独立窗口里展示 ----
    assert window.log_window is not None, "日志窗口对象应按需创建"
    assert not window.log_window.isVisible(), "日志默认不应显示"
    # 页面上要有一个专门的按钮（不只是菜单项）
    toolbar_texts = [a.text() for a in window.toolbar.actions() if a.text()]
    assert "运行日志" in toolbar_texts, f"工具栏缺少运行日志按钮：{toolbar_texts}"
    window.act_log_button.trigger()
    assert window.log_window.isVisible(), "点击“运行日志”后应弹出日志窗口"
    assert "配置" in window.log_window.view.toPlainText()
    window.log_window.hide()

    # ---- 重新切分的提示：已立项时不应再吓唬"正文将丢失" ----
    unbound_hint = window._resplit_message(12)
    assert "将不再出现在界面上" in unbound_hint or "覆盖" in unbound_hint
    assert "丢失" in unbound_hint
    window.project = _project_mod.Project(name="临时书", root=Path(tempfile.gettempdir()))
    try:
        bound_hint = window._resplit_message(12)
        assert "不会丢失" in bound_hint, bound_hint
        assert "临时书" in bound_hint
        assert "覆盖当前列表：这些章节的正文与对话将不再出现在界面上" not in bound_hint
    finally:
        window.project = None

    # ---- 队列面板：能开关、能显示引擎快照 ----
    # 注意：主窗口没 show() 时 isVisible() 恒为 False，要用 isVisibleTo(窗口) 判断
    assert not window.queue_dock.isVisibleTo(window), "队列面板默认不应显示"
    window.act_queue.setChecked(True)
    assert window.queue_dock.isVisibleTo(window), "点“任务队列”后应显示队列面板"
    window.queue_panel.refresh()
    assert "排队" in window.queue_panel.status_label.text()
    window.set_queue_visible(False)
    assert not window.queue_dock.isVisibleTo(window)

    # ---- 立项：切分大纲后新建以小说名命名的项目文件夹 ----
    # 真实界面上切分成功会弹出"输入小说名"的模态对话框；自检里直接调用立项 API，
    # 因此先断开该信号，避免自检被对话框卡住。
    window.outline_panel.outlineSplit.disconnect(window._on_outline_split)

    base = Path(temp_dir("selftest_winproj_"))
    try:
        outline = "第一章 觉醒\n本章内容：苏醒\n\n第二章 试炼\n本章内容：试炼"
        window.outline_panel.set_outline_text(outline)
        parsed = _outline_parser.split_outline(outline)
        window.outline_panel._apply_parsed(parsed)
        payload = window._chapters_payload()
        project = _project_mod.create_project(
            base, "烟雾测试之书", outline_text=outline, chapters_payload=payload
        )
        window._bind_project(project)
        assert window.project is not None
        assert "烟雾测试之书" in window.windowTitle()
        assert window._chapters_path() == project.chapters_path
        window._autosave_chapters()
        assert project.chapters_path.exists() and project.outline_path.exists()
        # 对话存档也要写出来（AI 回复完成 / 关闭项目 / 关闭软件时都会刷新）
        window._flush_conversations(force=True)
        assert project.conversations_path.exists(), "应生成 conversations.json"

        # ---- 读取项目：项目内容应被完整读回 ----
        ok = window._open_project(project)
        assert ok, "打开项目应成功"
        assert len(window.outline_panel.chapters) == len(parsed)
        # 对话存档应能被读回（条数与写进去的一致）
        saved = _project_mod.read_conversations(project)
        restored = sum(
            1
            for c in window.outline_panel.chapters
            if c.generate_conv is not None or c.check_conv is not None
        )
        if saved:
            assert restored >= 1, "对话存档存在，但没有回填到任何章节"
        window._bind_project(None)
        assert "烟雾测试之书" not in window.windowTitle()
    finally:
        import shutil as _shutil

        _shutil.rmtree(base, ignore_errors=True)

    window.close()
    app.processEvents()


def main() -> int:
    check("param_help", test_param_help)
    check("message_templates", test_templates)
    check("state_machine", test_state_machine)
    check("job_queue", test_job_queue)
    check("outline_parser", test_outline_parser)
    check("config", test_config)
    check("api_client.normalize_params", test_normalize_params)
    check("exporter", test_exporter)
    check("project", test_project)
    check("project_load_states", test_project_load_states)
    check("split_does_not_overwrite_project", test_split_does_not_overwrite_project)
    check("config_regeneration", test_config_regeneration)
    check("gui", test_gui)

    print()
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：{', '.join(FAILURES)}")
        return 1
    print("全部自检通过。")
    import shutil

    shutil.rmtree(_TMP_DATA, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
