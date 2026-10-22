"""HTTP tests exercise the real Coach, with explicit offline teaching and verdict fixtures."""

import threading
import time
from uuid import uuid4

import httpx
import pytest

from algo_coach.models import Verification
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher
from algo_coach.teaching_stream import emit
from algo_coach.verification import inspect_code
from algo_coach.web import WebApplication, make_server


class FixtureVerifier:
    def verify(self, problem, code):
        return Verification(
            status="wrong_answer",
            code_hash=inspect_code(code).code_hash,
            executed=True,
            case_count=1,
            passed_count=0,
            detail="Fixture-only simulated verdict",
            failure={"expected": [0, 1], "actual": [0, 0]},
        )


@pytest.fixture
def site(tmp_path):
    with Coach(tmp_path, DemoTeacher(), verifier=FixtureVerifier()) as coach:
        app = WebApplication(coach)
        server = make_server(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with httpx.Client(base_url=base, trust_env=False) as client:
            yield client, app, coach, tmp_path
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
        app.close()


def submit(client, app, operation, **fields):
    payload = {"operation": operation, "request_id": uuid4().hex, **fields}
    response = client.post(
        "/api/actions",
        json=payload,
        headers={"Origin": str(client.base_url).rstrip("/"), "X-AlgoCoach-Token": app.token},
    )
    assert response.status_code == 202, response.text
    return response.json()["job_id"]


def result(client, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        data = client.get(f"/api/jobs/{job_id}").json()
        if data["status"] != "running":
            assert data["status"] == "completed", data
            return data["result"]
        time.sleep(0.01)
    pytest.fail("HTTP fixture job did not finish")


def test_sse_preview_is_live_readonly_history_available_and_disconnect_does_not_resubmit(site):
    client, app, coach, _ = site
    first = result(client, submit(client, app, "create", source="local", reference="1"))["state"]
    second = result(client, submit(client, app, "create", source="local", reference="25"))["state"]
    started, release = threading.Event(), threading.Event()
    original, calls = coach.teacher.reply, []

    def reply(state):
        calls.append(1)
        emit("preview", "已检查的第一段。")
        started.set()
        assert release.wait(5)
        return original(state)

    coach.teacher.reply = reply
    job_id = submit(client, app, "turn", session_id=first["session_id"], text="/hint")
    try:
        assert started.wait(2)
        with client.stream("GET", f"/api/jobs/{job_id}/events") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            line = next(line for line in response.iter_lines() if line.startswith("data: "))
            import json

            snapshot = json.loads(line[6:])
            assert snapshot["status"] == "running"
            assert snapshot["preview"] == "已检查的第一段。"
            cached = client.get(f"/api/sessions/{second['session_id']}").json()
            assert cached["read_only_snapshot"] is True
            assert cached["problem"]["frontend_id"] == "25"
        assert client.get("/api/bootstrap").json()["busy"]
    finally:
        release.set()
    result(client, job_id)
    assert len(calls) == 1
    final = client.get(f"/api/jobs/{job_id}").json()
    assert final["preview"] == "" and final["status"] == "completed"
    assert (
        client.get(
            f"/api/jobs/{job_id}/events", headers={"Origin": "https://untrusted.invalid"}
        ).status_code
        == 403
    )


def test_web_workflow_preserves_versions_reports_and_resumes(site):
    client, app, coach, path = site
    page = client.get("/")
    assert page.status_code == 200 and 'lang="zh-CN"' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert client.get("/app.js").status_code == 200
    state = result(client, submit(client, app, "create", source="local", reference="leetcode1"))[
        "state"
    ]
    sid = state["session_id"]
    assert state["problem"]["frontend_id"] == "1" and state["mode"] == "demo"
    state = result(client, submit(client, app, "turn", session_id=sid, text="/hint"))["state"]
    assert state["hint_level"] == 1 and not state["answer_revealed"]
    code = "class Solution:\n    def twoSum(self, nums, target):\n        return [0,0]\n"
    state = result(client, submit(client, app, "code", session_id=sid, code=code))["state"]
    assert state["verification"]["status"] == "not_run"
    assert not state["verification"]["executed"]
    assert "usage" not in state and "attempts" not in state
    assert all(set(m) == {"role", "content"} for m in state["history"])
    state = result(
        client, submit(client, app, "test", session_id=sid, code_hash=state["code_hash"])
    )["state"]
    assert state["verification"]["status"] == "wrong_answer"
    assert state["verification"]["failure"]["actual"] == [0, 0]
    changed = result(
        client, submit(client, app, "code", session_id=sid, code=code + "# new version\n")
    )["state"]
    assert changed["verification"]["status"] == "not_run"
    assert client.get(f"/api/sessions/{sid}").json()["current_code"] == changed["current_code"]
    assert client.get("/api/bootstrap").json()["sessions"][0]["id"] == sid
    assert client.get("/api/bootstrap").json()["mode"] == "demo"
    # Independent checkpoint reader proves this is persisted Coach state, not browser-only memory.
    assert coach.get(sid)["code_hash"] == changed["code_hash"]
    assert (path / "checkpoints.sqlite").exists()


def test_no_file_serving_or_cross_origin_mutations(site):
    client, app, _, _ = site
    for path in ["/.env", "/pyproject.toml", "/web_assets/../.env", "/api/jobs/" + "a" * 32]:
        assert client.get(path).status_code == 404
    assert client.get("/", headers={"Host": "attacker.example"}).status_code == 403
    assert (
        client.get("/api/bootstrap", headers={"Origin": "https://attacker.example"}).status_code
        == 403
    )
    payload = {"operation": "doctor", "request_id": uuid4().hex}
    assert client.post("/api/actions", json=payload).status_code == 403
    assert (
        client.post(
            "/api/actions",
            json=payload,
            headers={"Origin": "https://attacker.example", "X-AlgoCoach-Token": app.token},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/actions",
            json=payload,
            headers={"Origin": str(client.base_url).rstrip("/"), "X-AlgoCoach-Token": "wrong"},
        ).status_code
        == 403
    )
    assert client.options("/api/actions").status_code == 405


def test_request_allowlist_and_stale_test_binding(site):
    client, app, _, _ = site
    headers = {"Origin": str(client.base_url).rstrip("/"), "X-AlgoCoach-Token": app.token}
    for payload in [
        [],
        {"operation": [], "request_id": uuid4().hex},
        {"operation": "push", "request_id": uuid4().hex},
        {"operation": "doctor", "request_id": uuid4().hex, "path": ".env"},
        {
            "operation": "turn",
            "request_id": uuid4().hex,
            "session_id": "a" * 32,
            "text": " /archive apply",
        },
    ]:
        assert client.post("/api/actions", json=payload, headers=headers).status_code == 400
    assert (
        client.post(
            "/api/actions", content=b"{", headers={**headers, "Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/actions", content=b"x", headers={**headers, "Content-Type": "text/plain"}
        ).status_code
        == 415
    )
    state = result(client, submit(client, app, "create", source="local", reference="1"))["state"]
    job = submit(client, app, "test", session_id=state["session_id"], code_hash="stale")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job}").json()
        if response["status"] != "running":
            break
        time.sleep(0.01)
    assert response["status"] == "failed" and "版本已变化" in response["error"]


def test_async_jobs_serialize_model_work_and_deduplicate_requests(site):
    client, app, _, _ = site
    started, release = threading.Event(), threading.Event()
    original = app.coach.start
    calls = []

    def delayed(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    app.coach.start = delayed
    headers = {"Origin": str(client.base_url).rstrip("/"), "X-AlgoCoach-Token": app.token}
    payload = {
        "operation": "create",
        "request_id": uuid4().hex,
        "source": "local",
        "reference": "1",
    }
    try:
        first = client.post("/api/actions", json=payload, headers=headers)
        assert first.status_code == 202 and started.wait(2)
        assert client.get("/api/bootstrap").json()["busy"]
        assert client.post("/api/actions", json=payload, headers=headers).json() == first.json()
        other = {**payload, "request_id": uuid4().hex}
        assert client.post("/api/actions", json=other, headers=headers).status_code == 409
        assert (
            client.post(
                "/api/actions", json={**payload, "reference": "25"}, headers=headers
            ).status_code
            == 409
        )
        assert client.get("/api/jobs/" + first.json()["job_id"]).json()["status"] == "running"
    finally:
        release.set()
    assert result(client, first.json()["job_id"])["state"]["problem"]["frontend_id"] == "1"
    assert calls == [1]


def test_browser_refresh_and_server_restart_preserve_learning_data(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        app = WebApplication(coach)
        state = coach.start(coach.problems.resolve("25"))
        state = coach.turn(state["session_id"], "/hint")
        sid = state["session_id"]
        app.close()
    with Coach(tmp_path, DemoTeacher()) as coach:
        app = WebApplication(coach)
        restored = app.get_session(sid)
        assert restored["hint_level"] == 1 and restored["history"][-1]["role"] == "assistant"
        assert app.bootstrap()["sessions"][0]["id"] == sid
        app.close()


def test_remote_and_daily_selection_use_existing_source_boundary(site):
    client, app, coach, _ = site
    calls = []

    def remote(reference):
        calls.append(("remote", reference))
        return coach.start(coach.problems.resolve("1"))

    def daily():
        calls.append(("daily", None))
        return coach.start(coach.problems.resolve("25"))

    coach.start_remote = remote
    coach.start_remote_daily = daily
    assert (
        result(client, submit(client, app, "create", source="remote", reference="leetcode1"))[
            "state"
        ]["problem"]["frontend_id"]
        == "1"
    )
    assert (
        result(client, submit(client, app, "create", source="daily", reference=""))["state"][
            "problem"
        ]["frontend_id"]
        == "25"
    )
    assert calls == [("remote", "leetcode1"), ("daily", None)]


def test_unexpected_error_has_no_raw_exception_and_does_not_block_next_job(site):
    client, app, coach, _ = site
    original = coach.start

    def broken(*args, **kwargs):
        raise RuntimeError("PRIVATE_PROVIDER_BODY_AND_KEY")

    coach.start = broken
    job = submit(client, app, "create", source="local", reference="1")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        outcome = client.get(f"/api/jobs/{job}").json()
        if outcome["status"] != "running":
            break
        time.sleep(0.01)
    assert outcome["status"] == "failed"
    assert "PRIVATE" not in str(outcome)
    assert not client.get("/api/bootstrap").json()["busy"]
    coach.start = original
    assert result(client, submit(client, app, "create", source="local", reference="1"))["state"]
