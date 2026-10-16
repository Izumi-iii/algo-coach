import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from filelock import Timeout
from pydantic import ValidationError

from algo_coach.models import Problem, TeachingReply
from algo_coach.problems import LocalProblems
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher, ModelTeacher, validate_reply
from algo_coach.verification import inspect_code


def start(coach, reference="25"):
    return coach.start(coach.problems.resolve(reference))


def test_hints_never_automatically_unlock_complete_answer(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        session = start(coach)["session_id"]
        for _ in range(6):
            state = coach.turn(session, "/hint")
            assert state["hint_level"] <= 3
            assert "```python" not in state["response"]
            assert not state["answer_revealed"]
        assert state["hint_level"] == 3
        state = coach.turn(session, "/answer")
        assert state["hint_level"] == 4
        assert state["answer_revealed"]
        assert "class Solution" in state["response"]


def test_negated_answer_request_does_not_grant_permission(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        session = start(coach)["session_id"]
        state = coach.turn(session, "不要直接给答案，只解释题意")
        assert state["hint_level"] == 0
        assert not state["answer_revealed"]


def test_restart_restores_problem_attempts_permissions_and_reflection(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        session = start(coach)["session_id"]
        coach.turn(session, "/answer")
        state = coach.turn(session, "检查代码", code="def solve():\n    return 1\n")
        code_hash = state["code_hash"]
        coach.turn(session, "/finish")
        coach.note(session, "我漏掉了不足 k 个节点的情况")
    with Coach(tmp_path, DemoTeacher()) as coach:
        state = coach.get(session)
        assert state["problem"]["frontend_id"] == "25"
        assert state["answer_revealed"]
        assert state["hint_level"] == 4
        assert state["stage"] == "reflect"
        assert state["code_hash"] == code_hash
        assert state["reflection"] == "我漏掉了不足 k 个节点的情况"
        assert len(state["attempts"]) == 1
        assert coach.sessions()[0][0] == session


def test_changed_code_gets_new_verification_without_losing_original(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        session = start(coach)["session_id"]
        broken = "def solve(:\n"
        old = coach.turn(session, "检查代码", code=broken)
        assert old["verification"]["status"] == "syntax_error"
        fixed = "def solve():\n    return 1\n"
        state = coach.turn(session, "检查代码", code=fixed)
        assert state["verification"]["status"] == "not_run"
        assert state["code_hash"] != old["code_hash"]
        assert state["verification"]["code_hash"] == state["code_hash"]
        assert state["attempts"][0]["code"] == broken
        assert state["attempts"][1]["code"] == fixed
        assert "未执行代码" in state["response"]


def test_syntax_check_never_executes_code(tmp_path):
    target = tmp_path / "should-not-exist.txt"
    result = inspect_code(f"from pathlib import Path\nPath({str(target)!r}).write_text('oops')")
    assert result.status == "not_run"
    assert not target.exists()


def test_utf8_bom_does_not_make_a_valid_windows_file_invalid():
    code = "\ufeffdef solve():\r\n    return 1\r\n"
    result = inspect_code(code)
    assert result.status == "not_run"
    assert result.code_hash != inspect_code(code.removeprefix("\ufeff")).code_hash


@pytest.mark.parametrize("code", ["def solve(:", "\x00", "return = 3"])
def test_invalid_syntax_has_no_passed_status(code):
    assert inspect_code(code).status == "syntax_error"


class BrokenTeacher:
    mode = "model"

    def reply(self, state):
        raise RuntimeError("provider error containing SECRET-KEY must never be persisted")


def test_model_failure_preserves_input_and_never_falls_back(tmp_path):
    with Coach(tmp_path, BrokenTeacher()) as coach:
        session = start(coach)["session_id"]
        state = coach.turn(session, "我不理解指针的变化")
        assert state["last_error"] == "model_service_error"
        assert state["history"][-1]["content"] == "我不理解指针的变化"
        assert "离线脚本" not in state["response"]
        assert "SECRET-KEY" not in json.dumps(state)
    with Coach(tmp_path, BrokenTeacher()) as coach:
        assert coach.get(session)["last_error"] == "model_service_error"


class LeakingTeacher:
    mode = "model"

    def reply(self, state):
        return TeachingReply(
            reply="class Solution: pass",
            hint_level_used=4,
            stage="implement",
            full_solution=True,
        )


def test_unauthorized_full_reply_is_not_shown_or_stored(tmp_path):
    with Coach(tmp_path, LeakingTeacher()) as coach:
        session = start(coach)["session_id"]
        state = coach.turn(session, "/hint")
        assert state["last_error"]
        assert "class Solution: pass" not in json.dumps(state)
        assert not state["answer_revealed"]
        assert state["hint_level"] == 1
        state = coach.turn(session, "/retry")
        assert state["hint_level"] == 1


def test_output_cannot_claim_dynamic_test_evidence():
    draft = TeachingReply(
        reply="代码已通过全部测试",
        hint_level_used=0,
        stage="debug",
        full_solution=False,
    )
    with pytest.raises(ValueError):
        validate_reply(draft, {"hint_level": 0})


def test_single_process_lock_protects_session_directory(tmp_path):
    with Coach(tmp_path, DemoTeacher()):
        with pytest.raises(Timeout):
            Coach(tmp_path, DemoTeacher())
    with Coach(tmp_path, DemoTeacher()) as coach:
        assert coach.sessions() == []


def test_demo_session_cannot_become_real_model_session(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        session = start(coach)["session_id"]
    with Coach(tmp_path, BrokenTeacher()) as coach:
        with pytest.raises(ValueError, match="模式"):
            coach.turn(session, "继续")


def test_problem_reimport_does_not_change_existing_session(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        original = coach.problems.resolve("25")
        session = coach.start(original)["session_id"]
        replacement = original.model_copy(update={"statement": "新的题面修订"})
        path = tmp_path / "replacement.json"
        path.write_text(replacement.model_dump_json(), encoding="utf-8")
        coach.problems.import_file(path)
        assert coach.problems.resolve("25").statement == "新的题面修订"
        assert coach.get(session)["problem"]["statement"] == original.statement


@pytest.mark.parametrize(
    "reference",
    [
        "25",
        "leetcode25",
        "LeetCode 25",
        "0025",
        "https://leetcode.cn/problems/reverse-nodes-in-k-group/",
    ],
)
def test_local_lookup_normalizes_references(tmp_path, reference):
    assert LocalProblems(tmp_path).resolve(reference).frontend_id == "25"


@pytest.mark.parametrize(
    "reference",
    [
        "https://127.0.0.1/problems/test/",
        "http://leetcode.cn/problems/two-sum/",
        "https://leetcode.cn@evil.example/problems/two-sum/",
        "../25",
        "99999",
    ],
)
def test_invalid_or_missing_reference_is_not_inferred(tmp_path, reference):
    with pytest.raises(ValueError):
        LocalProblems(tmp_path).resolve(reference)


def test_import_example_can_be_resolved_and_is_validated(tmp_path):
    catalogue = LocalProblems(tmp_path)
    source = Path(__file__).parents[1] / "examples/problems/sum-local.json"
    problem = catalogue.import_file(source)
    assert catalogue.resolve("sum-demo", "local") == problem
    invalid = problem.model_dump()
    invalid["frontend_id"] = "../../escape"
    with pytest.raises(ValidationError):
        Problem.model_validate(invalid)


def test_model_backend_requires_explicit_configuration(monkeypatch):
    monkeypatch.delenv("ALGOCOACH_MODEL", raising=False)
    monkeypatch.delenv("ALGOCOACH_API_KEY", raising=False)
    with pytest.raises(ValueError, match="--demo"):
        ModelTeacher()


def test_cli_demo_and_recovery_across_processes(tmp_path):
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    command = [sys.executable, "-m", "algo_coach", "--data-dir", str(tmp_path), "--demo"]
    created = subprocess.run(
        command + ["chat", "25"],
        input="/hint\n/finish\n/note 需要保存组后继\n/quit\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert created.returncode == 0, created.stderr
    assert "离线脚本演示" in created.stdout
    with Coach(tmp_path, DemoTeacher()) as coach:
        session = coach.sessions()[0][0]
    resumed = subprocess.run(
        command + ["resume", session],
        input="/status\n/quit\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=30,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert '"hint_level": 1' in resumed.stdout
    assert "需要保存组后继" in resumed.stdout


def test_cli_without_credentials_does_not_start_demo(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALGOCOACH_")}
    env.pop("PYTHONIOENCODING", None)
    result = subprocess.run(
        [sys.executable, "-m", "algo_coach", "chat", "25"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=tmp_path,
        timeout=30,
    )
    assert result.returncode == 1
    assert "请在 .env 配置" in result.stderr
    assert "离线脚本演示" not in result.stdout
    assert not (tmp_path / ".algo-coach").exists()
