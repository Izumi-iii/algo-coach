import copy
import json
from uuid import uuid4

import pytest

from algo_coach.browser_bridge import (
    BridgeError,
    BrowserBridge,
    bind_receipt,
    current_browser_feedback,
)
from algo_coach.errors import TeachingPolicyError
from algo_coach.models import TeachingReply
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher, ModelTeacher, validate_reply
from algo_coach.web import session_view

CODE = "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 0]\n"
EXT = "a" * 32
ORIGIN = "chrome-extension://" + EXT


def payload(code=CODE):
    return {
        "submission_id": "12345",
        "state": "SUCCESS",
        "status_code": 20,
        "question_id": "999",
        "detail": {
            "submission_id": "12345",
            "question_id": "999",
            "frontend_id": "1",
            "slug": "two-sum",
            "language": "Python3",
            "source_code": code,
        },
    }


def receive(coach, data=None):
    bridge = BrowserBridge(coach.connection)
    pair = bridge.pair(EXT)
    return bridge.receive(pair["token"], ORIGIN, data or payload())


@pytest.fixture
def saved(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        state = coach.turn(sid, "分析代码", code=CODE)
        yield coach, state


def test_bind_restore_model_context_and_version_expiry(saved, tmp_path):
    coach, state = saved
    sid, digest = state["session_id"], state["code_hash"]
    receipt = receive(coach)
    assert "source_code" not in receipt["detail"]
    raw = coach.connection.execute("SELECT payload FROM app_browser_receipts").fetchone()[0]
    assert 'source_code"' not in raw and "class Solution" not in raw
    bound = coach.bind_browser_result(sid, receipt["receipt_id"], digest)
    assert bound["verification"] == state["verification"]
    assert bound["completion_mode"] == state["completion_mode"]
    assert bound["history"] == state["history"]
    report = current_browser_feedback(bound)
    assert report["verdict"] == "Compile Error"
    assert report["code_hash"] == digest and report["language"] == "python3"
    assert session_view(bound)["browser_feedback"] == report
    # A second click is idempotent and never calls the model.
    assert (
        len(coach.bind_browser_result(sid, receipt["receipt_id"], digest)["browser_reports"]) == 1
    )

    # Inspect the actual adapter's model request, with no external service.
    class Model:
        def invoke(self, messages):
            context = json.loads(messages[1].content)
            assert context["browser_feedback"]["receipt_id"] == receipt["receipt_id"]
            return {
                "parsed": {
                    "reply": "浏览器回传报告：Compile Error。请先核对语法。",
                    "hint_level_used": 0,
                    "stage": "debug",
                    "full_solution": False,
                }
            }

    teacher = ModelTeacher.__new__(ModelTeacher)
    teacher.model = Model()
    teacher.reply(bound)
    validate_reply(
        TeachingReply(
            reply="浏览器回传报告：Compile Error。",
            hint_level_used=0,
            stage="debug",
            full_solution=False,
        ),
        bound,
    )
    new = coach.turn(sid, "新版本", code=CODE + "\n")
    assert current_browser_feedback(new) is None
    assert len(new["browser_reports"]) == 1
    # Even restoring identical bytes creates a different attempt requiring explicit rebind.
    identical = coach.turn(sid, "保存相同源码", code=CODE)
    assert current_browser_feedback(identical) is None
    rebound = coach.bind_browser_result(sid, receipt["receipt_id"], digest)
    assert current_browser_feedback(rebound)
    # Validate actual checkpoint restore using a separate connection after this Coach closes.
    coach.connection.commit()


def test_bound_report_survives_coach_restart(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        state = coach.turn(sid, "保存代码", code=CODE)
        receipt = receive(coach)
        coach.bind_browser_result(sid, receipt["receipt_id"], state["code_hash"])
    with Coach(tmp_path, DemoTeacher()) as coach:
        assert current_browser_feedback(coach.get(sid))["submission_id"] == "12345"


def test_reselecting_an_older_receipt_changes_active_feedback(saved):
    coach, state = saved
    sid, digest = state["session_id"], state["code_hash"]
    first = receive(coach)
    coach.bind_browser_result(sid, first["receipt_id"], digest)
    data = payload()
    data["status_code"] = 11
    second = receive(coach, data)
    new = coach.bind_browser_result(sid, second["receipt_id"], digest)
    assert current_browser_feedback(new)["status_code"] == 11
    old = coach.bind_browser_result(sid, first["receipt_id"], digest)
    assert current_browser_feedback(old)["status_code"] == 20
    assert len(old["browser_reports"]) == 3


@pytest.mark.parametrize(
    "damage",
    [
        "frontend",
        "slug",
        "site",
        "language",
        "code",
        "newline",
        "empty",
        "missing",
        "unknown",
        "hash",
    ],
)
def test_mismatches_cannot_enter_teaching(saved, damage):
    coach, state = saved
    data = payload()
    if damage == "frontend":
        data["detail"]["frontend_id"] = "25"
    if damage == "slug":
        data["detail"]["slug"] = "another-problem"
    if damage == "language":
        data["detail"]["language"] = "cpp"
    if damage == "code":
        data["detail"]["source_code"] += " "
    if damage == "newline":
        data["detail"]["source_code"] = CODE.replace("\n", "\r\n")
    if damage == "missing":
        data.pop("detail")
    if damage == "unknown":
        data["status_code"] = 999
    record = receive(coach, data)
    candidate = copy.deepcopy(state)
    if damage == "site":
        candidate["problem"]["site"] = "local"
    if damage == "empty":
        candidate["current_code"] = ""
    if damage == "hash":
        candidate["code_hash"] = "fake"
    with pytest.raises(BridgeError):
        bind_receipt(record, candidate)
    assert "browser_reports" not in coach.get(state["session_id"])


@pytest.mark.parametrize("field", ["submission_id", "question_id"])
def test_inconsistent_detail_identifiers_rejected(saved, field):
    coach, _ = saved
    data = payload()
    data["detail"][field] = "777"
    with pytest.raises(BridgeError):
        receive(coach, data)
    assert not BrowserBridge(coach.connection).recent()


def test_stale_request_unknown_receipt_and_cross_session(saved):
    coach, state = saved
    record = receive(coach)
    with pytest.raises(ValueError):
        coach.bind_browser_result(state["session_id"], record["receipt_id"], "old-hash")
    with pytest.raises(BridgeError):
        coach.bind_browser_result(state["session_id"], uuid4().hex, state["code_hash"])
    other = coach.start(coach.problems.resolve("25"))
    other = coach.turn(other["session_id"], "保存代码", code=CODE)
    with pytest.raises(BridgeError):
        coach.bind_browser_result(other["session_id"], record["receipt_id"], other["code_hash"])


@pytest.mark.parametrize("text", ["浏览器回传报告：Accepted。", "浏览器报告：Wrong Answer。"])
def test_model_cannot_change_bound_verdict(saved, text):
    coach, state = saved
    receipt = receive(coach)
    bound = coach.bind_browser_result(
        state["session_id"], receipt["receipt_id"], state["code_hash"]
    )
    reply = TeachingReply(reply=text, hint_level_used=0, stage="debug", full_solution=False)
    with pytest.raises(TeachingPolicyError):
        validate_reply(reply, bound)
    with pytest.raises(TeachingPolicyError):
        validate_reply(reply, state)
