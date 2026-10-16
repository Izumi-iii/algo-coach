import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime

import pytest

import algo_coach.calendar as calendar
from algo_coach.calendar import parse_date
from algo_coach.models import TeachingReply
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher

DAY = date(2026, 10, 15)
STAMP = "2026-10-15T01:00:00+00:00"


class NoHintTeacher:
    mode = "demo"

    def reply(self, state):
        return TeachingReply(
            reply="请按你自己的计划继续验证。",
            hint_level_used=0,
            stage="implement",
            full_solution=False,
        )


def seed_review(coach):
    original = coach.start(coach.problems.resolve("25"))["session_id"]
    coach.turn(original, "/answer")
    coach.turn(original, "代码", code="class Solution:\n    pass\n")
    coach.note(original, "OLD REFLECTION")
    coach.add_review(original, DAY)
    return original


def test_daily_round_trip_idempotence_and_site_date_isolation(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        problem = coach.problems.resolve("25")
        selection = coach.daily.set(problem, DAY, STAMP)
        repeated = coach.daily.set(problem, DAY, "2026-10-15T02:00:00+00:00")
        assert selection == repeated
        assert "不是平台每日题" in selection.render()
    with Coach(tmp_path, DemoTeacher()) as coach:
        assert coach.daily.get(DAY, "leetcode_cn") == selection
        for day, site in [(date(2026, 10, 16), "leetcode_cn"), (DAY, "leetcode_com")]:
            with pytest.raises(ValueError, match="没有已登记"):
                coach.daily.get(day, site)


def test_replacing_daily_keeps_existing_session_snapshot(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        problem = coach.problems.resolve("25")
        coach.daily.set(problem, DAY, STAMP)
        old = coach.start_daily(DAY)
        other = coach.problems.resolve("1")
        with pytest.raises(ValueError, match="--replace"):
            coach.daily.set(other, DAY, STAMP)
        coach.daily.set(other, DAY, STAMP, replace=True)
        assert coach.start_daily(DAY)["problem"]["frontend_id"] == "1"
        assert coach.get(old["session_id"])["problem"] == problem.model_dump()
        assert coach.get(old["session_id"])["daily_selection"]["problem"] == problem.model_dump()


def test_platform_daily_remains_user_reported_and_exports_provenance(tmp_path):
    practice = tmp_path / "practice"
    practice.mkdir()
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        problem = coach.problems.resolve("25")
        selection = coach.daily.set(problem, DAY, STAMP, platform=True, source_url=problem.url)
        assert selection.kind == "user_reported_platform"
        state = coach.start_daily(DAY)
        assert "未自动核实" in state["response"]
        coach.turn(state["session_id"], "代码", code="pass")
        plan = coach.preview_archive(state["session_id"], practice)
        metadata = json.loads(plan.contents["metadata.json"])
        assert metadata["daily_selection"]["calendar_date"] == "2026-10-15"
        assert "problem" not in metadata["daily_selection"]
        assert metadata["platform_result"] == "not_verified"


@pytest.mark.parametrize(
    "source",
    [
        "",
        "http://leetcode.cn/problems/reverse-nodes-in-k-group/",
        "https://leetcode.cn.evil.test/problems/reverse-nodes-in-k-group/",
        "https://leetcode.com/problems/reverse-nodes-in-k-group/",
        "https://user:secret@leetcode.cn/problems/reverse-nodes-in-k-group/",
        "https://leetcode.cn/problems/two-sum/",
        "https://leetcode.cn/problems/reverse-nodes-in-k-group/?token=secret",
    ],
)
def test_platform_source_must_match_problem(tmp_path, source):
    with Coach(tmp_path, DemoTeacher()) as coach:
        with pytest.raises(ValueError):
            coach.daily.set(
                coach.problems.resolve("25"), DAY, STAMP, platform=True, source_url=source
            )


@pytest.mark.parametrize(
    "value", ["2026-2-3", "2026-02-30", "2025-02-29", "today", "2026-10-15T00:00:00"]
)
def test_invalid_calendar_dates(value):
    with pytest.raises(ValueError):
        parse_date(value)


def test_user_today_changes_at_shanghai_midnight(monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 15, 16, 1, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(calendar, "datetime", Clock)
    assert calendar.today() == date(2026, 10, 16)
    assert parse_date("2024-02-29") == date(2024, 2, 29)


def test_review_due_queue_survives_restart_and_repeated_add(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        session = seed_review(coach)
        repeated = coach.add_review(session, date(2026, 10, 20))
        assert repeated["due_date"] == "2026-10-16"
        assert coach.reviews.due(DAY) == []
    with Coach(tmp_path, DemoTeacher()) as coach:
        due = coach.reviews.due(date(2026, 10, 16))
        assert due == [
            {
                "problem_key": "leetcode_cn:25",
                "title": "K 个一组翻转链表（本地教学摘要）",
                "due_date": "2026-10-16",
            }
        ]
        assert "OLD REFLECTION" not in json.dumps(due)


def test_review_start_resets_old_solution_and_permissions(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        old = seed_review(coach)
        state = coach.start_review("leetcode_cn:25")
        assert state["session_id"] != old
        assert state["current_code"] == ""
        assert state["history"] and len(state["history"]) == 1
        assert not state["answer_revealed"]
        assert state["hint_level"] == state["max_hint_level_used"] == 0
        assert state["reflection"] == "" and state["attempts"] == []
        assert "```python" not in state["response"]
        assert "OLD REFLECTION" not in json.dumps(state, ensure_ascii=False)
        assert state["review_context"] == {"problem_key": "leetcode_cn:25", "version": 1}


def test_independent_review_progression_is_bounded_and_rating_is_idempotent(tmp_path):
    with Coach(tmp_path, NoHintTeacher()) as coach:
        session = coach.start(coach.problems.resolve("1"))["session_id"]
        coach.add_review(session, DAY)
        for due, step in [
            ("2026-10-18", 1),
            ("2026-10-22", 2),
            ("2026-10-29", 3),
            ("2026-10-29", 3),
        ]:
            state = coach.start_review("leetcode_cn:1")
            session = state["session_id"]
            coach.turn(session, "代码", code="pass")
            result = coach.rate_review(session, "good", DAY)
            assert result["step"] == step and result["due_date"] == due
            assert result["self_reported"] and result["verification_status"] == "not_run"
            version = coach.reviews.get("leetcode_cn:1")["version"]
            assert coach.rate_review(session, "good", date(2026, 10, 20)) == result
            assert coach.reviews.get("leetcode_cn:1")["version"] == version
            with pytest.raises(ValueError, match="其他结果"):
                coach.rate_review(session, "hard", DAY)


def test_hint_assisted_review_does_not_extend_interval(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        seed_review(coach)
        session = coach.start_review("leetcode_cn:25")["session_id"]
        coach.turn(session, "/hint")
        coach.turn(session, "代码", code="pass")
        result = coach.rate_review(session, "good", DAY)
        assert result["effective_rating"] == "hard"
        assert result["assisted"] and result["due_date"] == "2026-10-16"


def test_known_failed_code_cannot_extend_review_interval(tmp_path):
    with Coach(tmp_path, NoHintTeacher()) as coach:
        session = coach.start(coach.problems.resolve("1"))["session_id"]
        coach.add_review(session, DAY)
        session = coach.start_review("leetcode_cn:1")["session_id"]
        coach.turn(session, "代码", code="def solve(:")
        result = coach.rate_review(session, "good", DAY)
        assert result["effective_rating"] == "again" and result["step"] == 0


def test_stale_review_session_cannot_overwrite_another_rating(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        original = seed_review(coach)
        with pytest.raises(ValueError, match="本次复习会话"):
            coach.rate_review(original, "again", DAY)
        first = coach.start_review("leetcode_cn:25")["session_id"]
        other = coach.start_review("leetcode_cn:25")["session_id"]
        with pytest.raises(ValueError, match="保存本次"):
            coach.rate_review(first, "good", DAY)
        coach.rate_review(first, "again", DAY)
        with pytest.raises(ValueError, match="另一复习会话"):
            coach.rate_review(other, "again", DAY)


def run_cli(tmp_path, *args, input=None):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALGOCOACH_")}
    return subprocess.run(
        [sys.executable, "-m", "algo_coach", "--data-dir", str(tmp_path / "data"), *args],
        cwd=tmp_path,
        input=input,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def test_daily_cli_requires_no_keys_and_enters_teaching(tmp_path):
    registered = run_cli(tmp_path, "daily", "set", "25", "--date", "2026-10-15")
    assert registered.returncode == 0, registered.stderr
    assert "个人练习计划" in registered.stdout
    shown = run_cli(tmp_path, "daily", "show", "--date", "2026-10-15")
    assert shown.returncode == 0 and "K 个一组" in shown.stdout
    missing = run_cli(tmp_path, "daily", "show", "--date", "2026-10-16")
    assert missing.returncode == 1 and "没有已登记" in missing.stderr
    chat = run_cli(
        tmp_path,
        "--demo",
        "daily",
        "chat",
        "--date",
        "2026-10-15",
        input="/hint\n/review add\n/review\n/quit\n",
    )
    assert chat.returncode == 0, chat.stderr
    assert "会话 ID" in chat.stdout and "复习队列" in chat.stdout


def test_review_cli_starts_fresh_and_records_rating_without_keys(tmp_path):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session = seed_review(coach)
    added = run_cli(tmp_path, "review", "add", session)
    assert added.returncode == 0, added.stderr
    listed = run_cli(tmp_path, "review", "list", "--date", "2026-10-16")
    assert listed.returncode == 0 and "leetcode_cn:25" in listed.stdout
    chat = run_cli(
        tmp_path, "--demo", "review", "chat", "leetcode_cn:25", input="/review rate again\n/quit\n"
    )
    assert chat.returncode == 0, chat.stderr
    assert "新复习会话" in chat.stdout
    assert '"self_reported": true' in chat.stdout
    assert "OLD REFLECTION" not in chat.stdout
