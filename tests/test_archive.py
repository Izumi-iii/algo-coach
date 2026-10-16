import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from algo_coach.archive import matches
from algo_coach.models import SolutionSummary
from algo_coach.paths import practice_root, safe_path
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher


@pytest.fixture
def practice(tmp_path):
    root = tmp_path / "practice"
    root.mkdir()
    return root


def session_with_code(coach):
    session = coach.start(coach.problems.resolve("1"))["session_id"]
    coach.turn(session, "检查代码", code="class Solution:\r\n    pass\r\n")
    coach.explain(session, "先检查另一数是否已经出现。")
    coach.note(session, "我漏掉了重复值的处理。")
    return session


def test_preview_is_read_only_and_export_preserves_code_and_notes(tmp_path, practice):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        plan = coach.preview_archive(session, practice)
        assert list(practice.iterdir()) == []
        assert plan == coach.preview_archive(session, practice)
        assert "用户原始记录" in plan.contents["reflection.md"]
        assert "我漏掉了重复值" in plan.contents["reflection.md"]
        assert "SECRET" not in plan.model_dump_json()
        result = coach.apply_archive(session)
        target = Path(result["path"])
        assert (target / "solution.py").read_bytes() == b"class Solution:\r\n    pass\r\n"
        assert matches(plan)
        assert (
            json.loads((target / "metadata.json").read_text("utf-8"))["completion_mode"]
            == "unfinished"
        )
        assert "history" not in plan.contents  # No full chat transcript is exported.


def test_repeated_export_is_idempotent_after_restart(tmp_path, practice):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        plan = coach.preview_archive(session, practice)
        first = coach.apply_archive(session)
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        assert coach.preview_archive(session, practice).plan_id == plan.plan_id
        second = coach.apply_archive(session)
        assert second["status"] == "unchanged"
        assert second["path"] == first["path"]


def test_changed_notes_invalidate_preview_and_create_new_revision(tmp_path, practice):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        first = coach.preview_archive(session, practice)
        coach.apply_archive(session)
        old = (Path(first.root) / first.relative / "reflection.md").read_bytes()
        coach.note(session, "新的复习发现")
        with pytest.raises(ValueError, match="已变化"):
            coach.apply_archive(session, first.plan_id)
        revised = coach.preview_archive(session, practice)
        assert "/reviews/" in revised.relative
        coach.apply_archive(session)
        assert (Path(first.root) / first.relative / "reflection.md").read_bytes() == old
        assert "新的复习发现" in revised.contents["reflection.md"]


def test_directory_appearing_after_preview_is_not_overwritten(tmp_path, practice):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        plan = coach.preview_archive(session, practice)
        target = practice / plan.relative
        target.mkdir(parents=True)
        sentinel = target / "user.txt"
        sentinel.write_text("keep me")
        with pytest.raises(ValueError, match="未覆盖"):
            coach.apply_archive(session)
        assert sentinel.read_text() == "keep me"


def test_missing_notes_are_marked_missing_not_fabricated(tmp_path, practice):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = coach.start(coach.problems.resolve("1"))["session_id"]
        coach.turn(session, "代码", code="pass")
        plan = coach.preview_archive(session, practice)
        assert "用户尚未填写" in plan.contents["reflection.md"]
        assert "用户尚未填写" in plan.contents["explanation.md"]


def test_completion_cannot_hide_hint_or_answer_assistance(tmp_path):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        with pytest.raises(ValueError, match="提示记录"):
            coach.complete(session, "independent")
        coach.complete(session, "hint_assisted")
        coach.turn(session, "/answer")
        with pytest.raises(ValueError, match="完整答案"):
            coach.complete(session, "hint_assisted")
        assert coach.complete(session, "answer_assisted")["completion_mode"] == "answer_assisted"
        assert coach.complete(session, "unfinished")["completion_mode"] == "unfinished"


def test_summarized_draft_is_labelled_and_invalidated_with_new_code(tmp_path, practice):
    class SummarizingTeacher(DemoTeacher):
        mode = "model"

        def summarize(self, state):
            return SolutionSummary(
                approach="查找补数",
                correctness="仍需验证",
                complexity="待分析",
                limitations="当前代码不完整",
            )

    with Coach(tmp_path / "data", SummarizingTeacher()) as coach:
        session = session_with_code(coach)
        coach.summarize(session)
        plan = coach.preview_archive(session, practice)
        assert "模型整理草稿" in plan.contents["explanation.md"]
        assert "查找补数" in plan.contents["explanation.md"]
        state = coach.turn(session, "新代码", code="x = 2")
        assert state["solution_summary"] is None
        assert state["explanation"] == ""
        assert state["completion_mode"] == "unfinished"


def test_demo_does_not_fabricate_a_model_summary(tmp_path):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        with pytest.raises(ValueError, match="没有模型整理能力"):
            coach.summarize(session)


@pytest.mark.parametrize(
    "relative", ["../escape", "/absolute", "a/../../escape", "a\\escape", "C:/escape"]
)
def test_unsafe_paths_are_rejected(practice, relative):
    with pytest.raises(ValueError):
        safe_path(practice, relative)


def test_linked_practice_root_is_rejected(tmp_path, practice):
    link = tmp_path / "link"
    try:
        link.symlink_to(practice, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation requires Windows developer mode/admin privileges")
    with pytest.raises(ValueError, match="符号链接"):
        practice_root(link)


def test_exported_bundle_is_self_contained_and_detects_modification(tmp_path, practice):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        coach.preview_archive(session, practice)
        target = Path(coach.apply_archive(session)["path"])
    command = [sys.executable, "-S", str(target / "tests/replay.py"), "--check"]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, env=env, timeout=30)
    assert result.returncode == 0, result.stderr
    (target / "solution.py").write_text("changed", encoding="utf-8")
    result = subprocess.run(command, cwd=tmp_path, capture_output=True, env=env, timeout=30)
    assert result.returncode != 0


def test_stale_plan_survives_restart_but_cannot_export_other_session(tmp_path, practice):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        plan = coach.preview_archive(session, practice)
        other = session_with_code(coach)
        with pytest.raises(ValueError, match="不属于"):
            coach.apply_archive(other, plan.plan_id)


def test_failed_staging_is_cleaned_and_retry_can_complete(tmp_path, practice, monkeypatch):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        plan = coach.preview_archive(session, practice)
        write = Path.write_bytes

        def fail(path, data):
            if path.name == "reflection.md":
                raise OSError("simulated write failure")
            return write(path, data)

        monkeypatch.setattr(Path, "write_bytes", fail)
        with pytest.raises(OSError):
            coach.apply_archive(session)
        assert not (practice / plan.relative).exists()
        assert not list(practice.glob(".algo-coach-stage-*"))
        monkeypatch.setattr(Path, "write_bytes", write)
        assert coach.apply_archive(session)["status"] == "written"


def test_archive_recovers_when_publication_succeeded_before_journal_update(
    tmp_path, practice, monkeypatch
):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = session_with_code(coach)
        plan = coach.preview_archive(session, practice)
        update = coach.operations.update

        def fail(operation_id, status, **fields):
            if status == "published":
                raise RuntimeError("simulated crash")
            return update(operation_id, status, **fields)

        monkeypatch.setattr(coach.operations, "update", fail)
        with pytest.raises(RuntimeError):
            coach.apply_archive(session)
        assert matches(plan)
        monkeypatch.setattr(coach.operations, "update", update)
        assert coach.apply_archive(session)["status"] == "unchanged"
