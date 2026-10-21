import json

import httpx
import pytest

from algo_coach.platform_judge import CNSubmissionClient, PlatformError, PlatformSubmissions
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher


@pytest.fixture
def setup_backend(tmp_path):
    settings = {
        "account": "alice",
        "signed_in": True,
        "qid": "999",
        "frontend": "1",
        "submit": "ok",
        "result": {
            "state": "SUCCESS",
            "status_code": 10,
            "submission_id": 23456,
            "question_id": "999",
        },
    }
    calls = []

    def respond(request):
        calls.append(request)
        assert request.url.host == "leetcode.cn"
        if request.url.path == "/graphql/":
            payload = json.loads(request.content)
            if "userStatus" in payload["query"]:
                return httpx.Response(
                    200,
                    json={
                        "data": {
                            "userStatus": {
                                "isSignedIn": settings["signed_in"],
                                "username": settings["account"],
                            }
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "data": {
                        "question": {
                            "questionId": settings["qid"],
                            "questionFrontendId": settings["frontend"],
                            "titleSlug": "two-sum",
                            "isPaidOnly": False,
                            "codeSnippets": [{"langSlug": "python3"}],
                        }
                    }
                },
            )
        if request.url.path.endswith("/submit/"):
            if settings["submit"] == "timeout":
                raise httpx.ReadTimeout("PRIVATE dummy-session", request=request)
            if settings["submit"] == "interrupt":
                raise KeyboardInterrupt()
            if settings["submit"] == "bad_schema":
                return httpx.Response(200, json={"error": "PRIVATE dummy-session"})
            return httpx.Response(200, json={"submission_id": 23456})
        return httpx.Response(200, json=settings["result"])

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        with Coach(tmp_path, DemoTeacher()) as coach:
            sid = coach.start(coach.problems.resolve("1"))["session_id"]
            coach.turn(
                sid,
                "保存代码",
                code="class Solution:\n"
                "    def twoSum(self, nums, target):\n        return [0, 0]\n",
            )
            client = CNSubmissionClient("dummy-session", "dummy-csrf", client=transport)
            backend = PlatformSubmissions(coach, client=client, enabled=True)
            yield backend, sid, settings, calls


def submits(calls):
    return [r for r in calls if r.url.path.endswith("/submit/")]


def test_preview_apply_poll_bind_internal_id_and_do_not_overwrite_local_report(setup_backend):
    backend, sid, settings, calls = setup_backend
    preview = backend.preview(sid)
    assert preview["target"]["question_id"] == "999"
    assert not submits(calls) and "submitted_code" not in preview
    queued = backend.apply(sid, preview["operation_id"])
    assert queued["state"] == "queued"
    payload = json.loads(submits(calls)[0].content)
    assert payload["question_id"] == "999" and payload["lang"] == "python3"
    assert payload["typed_code"] == backend.coach.get(sid)["current_code"]
    assert backend.apply(sid, preview["operation_id"])["submission_id"] == "23456"
    assert len(submits(calls)) == 1
    result = backend.poll(sid, preview["operation_id"])
    assert result["verdict"] == "accepted" and result["state"] == "finished"
    assert backend.coach.get(sid)["verification"]["status"] == "not_run"
    assert backend.coach.get(sid)["answer_revealed"] is False
    serialized = (
        json.dumps(result)
        + backend.db.execute("SELECT payload FROM app_platform_submissions").fetchone()[0]
    )
    assert "dummy-session" not in serialized and "dummy-csrf" not in serialized


@pytest.mark.parametrize("damage", ["code", "account", "target", "disabled", "ownership"])
def test_apply_preconditions_prevent_submit(setup_backend, damage):
    backend, sid, settings, calls = setup_backend
    plan = backend.preview(sid)
    if damage == "code":
        backend.coach.turn(sid, "换版", code="pass\n")
    elif damage == "account":
        settings["account"] = "bob"
    elif damage == "target":
        settings["qid"] = "998"
    elif damage == "disabled":
        backend.enabled = False
    else:
        sid = backend.coach.start(backend.coach.problems.resolve("1"))["session_id"]
    with pytest.raises(PlatformError):
        backend.apply(sid, plan["operation_id"])
    assert not submits(calls)


@pytest.mark.parametrize("failure", ["timeout", "bad_schema", "interrupt"])
def test_uncertain_submit_is_never_automatically_repeated(setup_backend, failure):
    backend, sid, settings, calls = setup_backend
    plan = backend.preview(sid)
    settings["submit"] = failure
    if failure == "interrupt":
        with pytest.raises(KeyboardInterrupt):
            backend.apply(sid, plan["operation_id"])
    else:
        assert backend.apply(sid, plan["operation_id"])["state"] == "unknown"
    # A fresh service instance reading the same SQLite record does not retry POST.
    restored = PlatformSubmissions(backend.coach, client=backend.client, enabled=True)
    result = restored.apply(sid, plan["operation_id"])
    assert result["state"] in {"sending", "unknown"}
    assert len(submits(calls)) == 1 and "PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize(
    "status,verdict",
    [
        (10, "accepted"),
        (11, "wrong_answer"),
        (12, "memory_limit"),
        (13, "output_limit"),
        (14, "time_limit"),
        (15, "runtime_error"),
        (20, "compile_error"),
    ],
)
def test_terminal_verdicts_are_separate_from_task_success(setup_backend, status, verdict):
    backend, sid, settings, _ = setup_backend
    plan = backend.preview(sid)
    backend.apply(sid, plan["operation_id"])
    settings["result"]["status_code"] = status
    assert backend.poll(sid, plan["operation_id"])["verdict"] == verdict


@pytest.mark.parametrize(
    "damage", ["id", "question", "missing_id", "status", "bool_status", "unknown_state"]
)
def test_unknown_or_mismatched_result_never_becomes_accepted(setup_backend, damage):
    backend, sid, settings, _ = setup_backend
    plan = backend.preview(sid)
    backend.apply(sid, plan["operation_id"])
    result = settings["result"]
    if damage == "id":
        result["submission_id"] = 9
    elif damage == "question":
        result["question_id"] = "9"
    elif damage == "missing_id":
        del result["submission_id"]
    elif damage == "status":
        result["status_code"] = 999
    elif damage == "bool_status":
        result["status_code"] = True
    else:
        result["state"] = "NEW"
    checked = backend.poll(sid, plan["operation_id"])
    assert checked["verdict"] is None and checked["last_error"] in {"binding", "schema"}


def test_poll_budget_and_current_draft_do_not_change_old_submission(setup_backend):
    backend, sid, settings, calls = setup_backend
    plan = backend.preview(sid)
    backend.apply(sid, plan["operation_id"])
    settings["result"] = {"state": "PENDING"}
    sleeps = []
    pending = backend.poll(sid, plan["operation_id"], attempts=2, sleep=sleeps.append)
    assert pending["state"] == "judging" and pending["verdict"] is None and sleeps == [2]
    backend.coach.turn(sid, "换版", code="pass\n")
    settings["result"] = {"state": "SUCCESS", "status_code": 10, "submission_id": 23456}
    checked = backend.poll(sid, plan["operation_id"])
    assert checked["code_hash"] != backend.coach.get(sid)["code_hash"]
    assert backend.coach.get(sid)["verification"]["status"] == "not_run"
    assert len(submits(calls)) == 1


def test_wrong_remote_frontend_and_signed_out_stop_preview(setup_backend):
    backend, sid, settings, calls = setup_backend
    settings["frontend"] = "2"
    with pytest.raises(PlatformError, match="题目"):
        backend.preview(sid)
    settings["signed_in"] = False
    with pytest.raises(PlatformError, match="登录"):
        backend.preview(sid)
    assert not submits(calls)


@pytest.mark.parametrize("status", [401, 403, 429, 302, 500])
def test_transport_errors_are_controlled_and_never_follow_redirects(status):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            status, headers={"Location": "https://attacker.invalid/"}, text="PRIVATE credential"
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = CNSubmissionClient("dummy-session", "dummy-csrf", client=transport)
        with pytest.raises(PlatformError) as error:
            client.account()
        assert "PRIVATE" not in str(error.value) and len(calls) == 1


def test_missing_credentials_do_not_make_network_calls():
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: pytest.fail("unexpected network"))
    ) as transport:
        with pytest.raises(PlatformError, match="未配置"):
            CNSubmissionClient("", "", client=transport).account()


@pytest.mark.parametrize(
    "response",
    [{"data": {"userStatus": "bad"}}, {"data": []}, {"errors": [{"message": "PRIVATE"}]}],
)
def test_malformed_graphql_responses_are_controlled(response):
    with httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=response))
    ) as transport:
        with pytest.raises(PlatformError) as error:
            CNSubmissionClient("dummy", "dummy", client=transport).account()
        assert "PRIVATE" not in str(error.value)


def test_cli_doctor_without_credentials_is_read_only(tmp_path, monkeypatch, capsys):
    from algo_coach.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ALGOCOACH_LEETCODE_SESSION", "")
    monkeypatch.setenv("ALGOCOACH_LEETCODE_CSRF", "")
    assert main(["platform", "doctor"]) == 1
    assert "未配置" in capsys.readouterr().err
    assert not (tmp_path / ".algo-coach").exists()


def test_saved_operation_can_be_read_after_actual_connection_restart(tmp_path):
    class ReadOnlyClient:
        def account(self):
            return "account-hash"

        def target(self, problem):
            return {
                "question_id": "999",
                "frontend_id": "1",
                "slug": "two-sum",
                "language": "python3",
            }

    with Coach(tmp_path, DemoTeacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        coach.turn(sid, "提交", code="pass\n")
        plan = PlatformSubmissions(coach, client=ReadOnlyClient()).preview(sid)
    with Coach(tmp_path, DemoTeacher()) as coach:
        backend = PlatformSubmissions(coach)
        restored = backend.view(backend.load(plan["operation_id"], sid))
        assert restored == plan and restored["state"] == "preview"
