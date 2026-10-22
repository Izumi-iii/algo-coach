import json
import shutil
import subprocess
import threading
from uuid import uuid4

import httpx
import pytest

from algo_coach.browser_bridge import current_browser_feedback
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher
from algo_coach.web import WebApplication, make_server

CODE = "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 0]\n"
EXT = "a" * 32
ORIGIN = "chrome-extension://" + EXT


@pytest.fixture
def platform_site(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        app = WebApplication(coach)
        server = make_server(app, 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        with httpx.Client(base_url=base, trust_env=False) as client:
            client.headers.update({"Origin": base, "X-AlgoCoach-Token": app.token})
            yield client, app, coach
        server.shutdown()
        thread.join(5)
        server.server_close()
        app.close()


@pytest.mark.parametrize(
    "mode",
    [
        "complete",
        "accepted",
        "compile",
        "account-switch",
        "login",
        "csrf",
        "submit-timeout",
        "lost-authorization",
        "lost-ack",
    ],
)
def test_real_worker_to_real_http_with_controlled_platform(platform_site, mode):
    if not shutil.which("node"):
        pytest.skip("Node required for actual extension script fixture integration")
    client, app, coach = platform_site
    sid = coach.start(coach.problems.resolve("1"))["session_id"]
    state = coach.turn(sid, "保存代码", code=CODE)
    prepared = client.post(
        "/api/platform/prepare",
        json={
            "session_id": sid,
            "operation_id": uuid4().hex,
            "code_hash": state["code_hash"],
        },
    )
    assert prepared.status_code == 200
    op = prepared.json()["operation_id"]
    pair = client.post("/api/bridge/pair", json={"extension_id": EXT, "purpose": "submit"})
    assert pair.status_code == 200
    data = {
        "base": str(client.base_url).rstrip("/"),
        "origin": ORIGIN,
        "token": pair.json()["token"],
        "operation_id": op,
        "mode": mode,
    }
    proc = subprocess.run(
        [shutil.which("node"), "--max-old-space-size=128", "scripts/test_platform_extension.cjs"],
        input=json.dumps(data),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    observation = json.loads(proc.stdout)
    assert observation["posts"] <= 1
    view = client.get("/api/platform/session/" + sid).json()["submissions"][0]
    assert "source_code" not in view
    if mode in {"complete", "accepted", "compile", "lost-ack"}:
        assert view["state"] == "finished" and view["binding_status"] == "current"
        feedback = current_browser_feedback(coach.get(sid))
        expected = 10 if mode == "accepted" else 20 if mode == "compile" else 11
        assert feedback["submission_id"] == "5678" and feedback["status_code"] == expected
        assert feedback["failure_details"]["expected_output"] == "[0,1]"
    else:
        assert current_browser_feedback(coach.get(sid)) is None
    assert coach.get(sid)["verification"] == state["verification"]


def test_http_auth_capability_and_field_boundaries(platform_site):
    client, app, coach = platform_site
    sid = coach.start(coach.problems.resolve("1"))["session_id"]
    state = coach.turn(sid, "保存代码", code=CODE)
    record = client.post(
        "/api/platform/prepare",
        json={
            "session_id": sid,
            "operation_id": uuid4().hex,
            "code_hash": state["code_hash"],
        },
    ).json()
    pair = client.post("/api/bridge/pair", json={"extension_id": EXT}).json()
    extension_headers = {"Origin": ORIGIN, "X-AlgoCoach-Bridge": pair["token"]}
    payload = {"action": "claim", "operation_id": record["operation_id"]}
    assert (
        client.post("/api/bridge/submit/step", json=payload, headers=extension_headers).status_code
        == 403
    )
    pair = client.post("/api/bridge/pair", json={"extension_id": EXT, "purpose": "submit"}).json()
    extension_headers["X-AlgoCoach-Bridge"] = pair["token"]
    valid = client.post("/api/bridge/submit/step", json=payload, headers=extension_headers)
    assert valid.status_code == 200 and valid.json()["source_code"] == CODE
    assert valid.headers["access-control-allow-origin"] == ORIGIN
    assert (
        client.post(
            "/api/bridge/submit/step",
            json={**payload, "cookies": "PRIVATE"},
            headers=extension_headers,
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/bridge/submit/step",
            json={**payload, "action": "prepare"},
            headers=extension_headers,
        ).status_code
        == 400
    )
    assert (
        client.post("/api/platform/prepare", json={}, headers=extension_headers).status_code == 403
    )
    assert client.get("/platform.js").status_code == 200
