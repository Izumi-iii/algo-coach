import copy
from uuid import uuid4

import pytest

from algo_coach.browser_bridge import BridgeError, BrowserBridge, current_browser_feedback
from algo_coach.browser_submit import BrowserSubmissions
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher

CODE = "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 0]\n"
TARGET = {
    "account_hash": "a" * 64,
    "question_id": "999",
    "frontend_id": "1",
    "slug": "two-sum",
    "language": "python3",
}


def result(code=CODE):
    return {
        "submission_id": "5678",
        "state": "SUCCESS",
        "status_code": 11,
        "question_id": "999",
        "failure_details": {"last_testcase": "[2,7]\n9"},
        "detail": {
            "submission_id": "5678",
            "question_id": "999",
            "frontend_id": "1",
            "slug": "two-sum",
            "language": "Python3",
            "source_code": code,
        },
    }


@pytest.fixture
def queue(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        state = coach.turn(sid, "保存代码", code=CODE)
        backend = BrowserSubmissions(coach, BrowserBridge(coach.connection))
        record = backend.prepare(state, uuid4().hex, state["code_hash"])
        yield coach, backend, state, record["operation_id"]


def send(backend, op):
    assert backend.claim(op)["state"] == "checking"
    assert backend.authorize_send(op, TARGET)["authorized"]
    return backend.accepted(op, "5678")


def test_complete_flow_auto_binds_and_keeps_local_verification(queue):
    coach, backend, state, op = queue
    view = backend.recent(state["session_id"])[0]
    assert "source_code" not in view and "problem_snapshot" not in view
    send(backend, op)
    backend.pending(op)
    final = backend.finish(op, result())
    assert final["state"] == "finished" and final["verdict"] == "wrong_answer"
    assert final["failure_details"]["last_testcase"] == "[2,7]\n9"
    current = coach.get(state["session_id"])
    assert current["verification"] == state["verification"]
    assert current["completion_mode"] == state["completion_mode"]
    assert current_browser_feedback(current)["submission_id"] == "5678"
    assert current_browser_feedback(current)["failure_details"]["last_testcase"] == "[2,7]\n9"
    assert backend.finish(op, result()) == final
    assert len(backend.bridge.recent()) == 1
    assert len(current["browser_reports"]) == 1


def test_idempotent_prepare_and_concurrent_active_request(queue):
    _, backend, state, op = queue
    assert backend.prepare(state, op, state["code_hash"])["operation_id"] == op
    assert backend.prepare(state, uuid4().hex, state["code_hash"])["operation_id"] == op
    bad = copy.deepcopy(state)
    bad["session_id"] = uuid4().hex
    with pytest.raises(BridgeError):
        backend.prepare(bad, op, state["code_hash"])


def test_authorization_cannot_be_replayed_even_after_restart(queue):
    coach, backend, _, op = queue
    backend.claim(op)
    backend.authorize_send(op, TARGET)
    reopened = BrowserSubmissions(coach, BrowserBridge(coach.connection))
    assert reopened.claim(op)["state"] == "sending"
    with pytest.raises(BridgeError):
        reopened.authorize_send(op, TARGET)
    backend.fail(op, "transport")
    assert reopened.claim(op)["state"] == "unknown"
    with pytest.raises(BridgeError):
        reopened.authorize_send(op, TARGET)


def test_change_before_send_cancels_and_change_after_send_retains_history(queue):
    coach, backend, state, op = queue
    coach.turn(state["session_id"], "改动", code=CODE + "\n")
    assert backend.claim(op)["state"] == "cancelled"
    with pytest.raises(BridgeError):
        backend.authorize_send(op, TARGET)
    new = coach.get(state["session_id"])
    second = backend.prepare(new, uuid4().hex, new["code_hash"])["operation_id"]
    send(backend, second)
    coach.turn(state["session_id"], "再次改动", code=CODE)
    final = backend.finish(second, result(CODE + "\n"))
    assert final["binding_status"] == "stale"
    assert current_browser_feedback(coach.get(state["session_id"])) is None


@pytest.mark.parametrize("field,value", [("frontend_id", "25"), ("slug", "other")])
def test_preflight_must_match_frontend_and_slug(queue, field, value):
    _, backend, _, op = queue
    backend.claim(op)
    target = {**TARGET, field: value}
    with pytest.raises(BridgeError):
        backend.authorize_send(op, target)
    assert backend.load(op)["state"] == "checking"


@pytest.mark.parametrize(
    "damage", ["id", "qid", "frontend", "slug", "lang", "code", "status", "detail"]
)
def test_mismatched_remote_result_never_becomes_teaching_evidence(queue, damage):
    coach, backend, state, op = queue
    send(backend, op)
    data = result()
    if damage == "id":
        data["submission_id"] = "99"
    if damage == "qid":
        data["detail"]["question_id"] = "1"
    if damage == "frontend":
        data["detail"]["frontend_id"] = "25"
    if damage == "slug":
        data["detail"]["slug"] = "other"
    if damage == "lang":
        data["detail"]["language"] = "cpp"
    if damage == "code":
        data["detail"]["source_code"] += " "
    if damage == "status":
        data["status_code"] = 999
    if damage == "detail":
        data.pop("detail")
    with pytest.raises(BridgeError):
        backend.finish(op, data)
    assert not backend.bridge.recent()
    assert current_browser_feedback(coach.get(state["session_id"])) is None


def test_unknown_submission_recovery_only_reads_existing_id(queue):
    _, backend, state, op = queue
    backend.claim(op)
    backend.authorize_send(op, TARGET)
    backend.fail(op, "transport")
    assert backend.prepare(state, uuid4().hex, state["code_hash"])["operation_id"] == op
    assert backend.recover(op, "9876")["state"] == "queued"
    assert backend.recover(op, "5678")["submission_id"] == "5678"
    with pytest.raises(BridgeError):
        backend.authorize_send(op, TARGET)
    assert backend.finish(op, result())["state"] == "finished"


def test_ack_cannot_replace_existing_submission_id(queue):
    _, backend, _, op = queue
    send(backend, op)
    with pytest.raises(BridgeError):
        backend.accepted(op, "9999")
    with pytest.raises(BridgeError):
        backend.recover(op, "9999")
    assert backend.accepted(op, "5678")["submission_id"] == "5678"


def test_checkpoint_retry_uses_one_receipt(queue, monkeypatch):
    coach, backend, _, op = queue
    send(backend, op)
    original = coach.bind_browser_result
    monkeypatch.setattr(coach, "bind_browser_result", lambda *a: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        backend.finish(op, result())
    assert len(backend.bridge.recent()) == 1
    monkeypatch.setattr(coach, "bind_browser_result", original)
    backend.finish(op, result())
    assert len(backend.bridge.recent()) == 1


def test_login_failure_can_be_retried_but_post_failure_cannot(queue):
    _, backend, state, op = queue
    backend.claim(op)
    assert backend.fail(op, "login")["state"] == "blocked"
    new = backend.prepare(state, uuid4().hex, state["code_hash"])["operation_id"]
    assert new != op
    backend.claim(new)
    backend.authorize_send(new, TARGET)
    assert backend.fail(new, "extension")["state"] == "unknown"
    assert backend.prepare(state, uuid4().hex, state["code_hash"])["operation_id"] == new


def test_saving_identical_code_does_not_bypass_unknown_post(queue):
    coach, backend, state, op = queue
    backend.claim(op)
    backend.authorize_send(op, TARGET)
    backend.fail(op, "transport")
    same = coach.turn(state["session_id"], "原样保存", code=CODE)
    assert same["attempts"][-1]["attempt_id"] != state["attempts"][-1]["attempt_id"]
    assert backend.prepare(same, uuid4().hex, same["code_hash"])["operation_id"] == op


def test_job_survives_a_real_coach_restart(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        state = coach.turn(sid, "保存代码", code=CODE)
        backend = BrowserSubmissions(coach, BrowserBridge(coach.connection))
        op = backend.prepare(state, uuid4().hex, state["code_hash"])["operation_id"]
        backend.claim(op)
        backend.authorize_send(op, TARGET)
    with Coach(tmp_path, DemoTeacher()) as coach:
        backend = BrowserSubmissions(coach, BrowserBridge(coach.connection))
        assert backend.claim(op)["state"] == "sending"
        with pytest.raises(BridgeError):
            backend.authorize_send(op, TARGET)
        backend.recover(op, "5678")
        assert backend.finish(op, result())["state"] == "finished"


@pytest.mark.parametrize("status", [10, 11, 12, 13, 14, 15, 20])
def test_recognized_terminal_status_is_reported_without_promoting_completion(queue, status):
    coach, backend, state, op = queue
    send(backend, op)
    data = result()
    data["status_code"] = status
    assert backend.finish(op, data)["state"] == "finished"
    current = coach.get(state["session_id"])
    assert current_browser_feedback(current)["status_code"] == status
    assert current["completion_mode"] == "unfinished"
    assert current["verification"] == state["verification"]


def test_submission_pair_can_renew_without_exhausting_capacity(queue):
    _, backend, _, _ = queue
    bridge = backend.bridge
    token = bridge.pair("a" * 32, "submit")["token"]
    for _ in range(20):
        assert bridge.pair("a" * 32, "submit")["token"] == token
    assert len(bridge.pairs) == 1
