import threading

import httpx
import pytest

from algo_coach.browser_bridge import BridgeError, BrowserBridge
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher
from algo_coach.web import WebApplication, make_server

EXTENSION = "a" * 32
ORIGIN = "chrome-extension://" + EXTENSION
RECEIPT = {
    "submission_id": "12345",
    "state": "SUCCESS",
    "status_code": 10,
    "question_id": "1",
    "runtime": "1 ms",
    "memory": "10 MB",
}


@pytest.fixture
def bridge_site(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        app = WebApplication(coach)
        server = make_server(app, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_address[1]}", trust_env=False
        ) as client:
            yield client, app, coach
        server.shutdown()
        worker.join(5)
        server.server_close()
        app.close()


def connect(client, app):
    response = client.post(
        "/api/bridge/pair",
        json={"extension_id": EXTENSION},
        headers={"Origin": str(client.base_url).rstrip("/"), "X-AlgoCoach-Token": app.token},
    )
    assert response.status_code == 200
    return response.json()["token"]


def test_roundtrip_cors_idempotence_and_separate_history(bridge_site):
    client, app, coach = bridge_site
    sid = coach.start(coach.problems.resolve("1"))["session_id"]
    before = coach.get(sid)
    token = connect(client, app)
    headers = {"Origin": ORIGIN, "X-AlgoCoach-Bridge": token}
    preflight = client.options(
        "/api/bridge/results",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-algocoach-bridge",
        },
    )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == ORIGIN
    response = client.post("/api/bridge/results", json=RECEIPT, headers=headers)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ORIGIN
    record = response.json()
    assert record["verdict"] == "Accepted" and record["code_binding"] == "unbound"
    assert client.post("/api/bridge/results", json=RECEIPT, headers=headers).json() == record
    assert (
        client.post(
            "/api/bridge/results", json={**RECEIPT, "submission_id": "9"}, headers=headers
        ).status_code
        == 403
    )
    assert len(client.get("/api/bridge/results").json()["receipts"]) == 1
    assert coach.get(sid) == before
    raw = coach.connection.execute("SELECT payload FROM app_browser_receipts").fetchone()[0]
    assert token not in raw and "extension_id" not in raw


@pytest.mark.parametrize("damage", ["origin", "token", "host", "pair_csrf", "pair_origin"])
def test_unpaired_or_untrusted_requests_are_rejected(bridge_site, damage):
    client, app, _ = bridge_site
    token = connect(client, app)
    headers = {"Origin": ORIGIN, "X-AlgoCoach-Bridge": token}
    if damage == "origin":
        headers["Origin"] = "https://attacker.invalid"
    if damage == "token":
        headers["X-AlgoCoach-Bridge"] = "bad"
    if damage == "host":
        headers["Host"] = "attacker.invalid"
    if damage.startswith("pair_"):
        headers = {"Origin": str(client.base_url).rstrip("/"), "X-AlgoCoach-Token": app.token}
        headers["Origin" if damage == "pair_origin" else "X-AlgoCoach-Token"] = "bad"
        response = client.post(
            "/api/bridge/pair", json={"extension_id": EXTENSION}, headers=headers
        )
    else:
        response = client.post("/api/bridge/results", json=RECEIPT, headers=headers)
    assert response.status_code == 403
    assert not client.get("/api/bridge/results").json()["receipts"]


@pytest.mark.parametrize(
    "damage", ["cookie", "bool_status", "pending", "bad_id", "oversized", "secret_error"]
)
def test_receipt_whitelist_rejects_unneeded_or_malformed_data(bridge_site, damage):
    client, app, _ = bridge_site
    token = connect(client, app)
    payload = dict(RECEIPT)
    if damage == "cookie":
        payload["cookies"] = "PRIVATE"
    elif damage == "bool_status":
        payload["status_code"] = True
    elif damage == "pending":
        payload["state"] = "PENDING"
    elif damage == "bad_id":
        payload["submission_id"] = "<script>"
    elif damage == "oversized":
        payload["runtime"] = "x" * 201
    else:
        payload["error"] = "PRIVATE"
    response = client.post(
        "/api/bridge/results", json=payload, headers={"Origin": ORIGIN, "X-AlgoCoach-Bridge": token}
    )
    assert response.status_code == 400
    assert "PRIVATE" not in response.text
    assert not client.get("/api/bridge/results").json()["receipts"]


def test_pair_expiry_and_persisted_receipt_after_restart(tmp_path):
    clock = [0]
    with Coach(tmp_path, DemoTeacher()) as coach:
        bridge = BrowserBridge(coach.connection, clock=lambda: clock[0])
        token = bridge.pair(EXTENSION)["token"]
        bridge.receive(token, ORIGIN, RECEIPT)
        clock[0] = 901
        assert not bridge.allowed_origin(ORIGIN)
        with pytest.raises(BridgeError):
            bridge.receive(token, ORIGIN, RECEIPT)
    with Coach(tmp_path, DemoTeacher()) as coach:
        bridge = BrowserBridge(coach.connection)
        assert not bridge.allowed_origin(ORIGIN)
        assert bridge.recent()[0]["submission_id"] == "12345"


def test_unrecognized_status_is_not_accepted_and_pair_capacity_is_bounded(bridge_site):
    client, app, _ = bridge_site
    token = connect(client, app)
    response = client.post(
        "/api/bridge/results",
        json={**RECEIPT, "status_code": 999},
        headers={"Origin": ORIGIN, "X-AlgoCoach-Bridge": token},
    )
    assert response.json()["verdict"] == "未知判题状态"
    for _ in range(15):
        app.bridge.pair(EXTENSION)
    with pytest.raises(BridgeError):
        app.bridge.pair(EXTENSION)


def test_preflight_does_not_open_general_cors(bridge_site):
    client, app, _ = bridge_site
    connect(client, app)
    for path, origin, method, header in [
        ("/api/actions", ORIGIN, "POST", "content-type"),
        ("/api/bridge/results", "https://attacker.invalid", "POST", "content-type"),
        ("/api/bridge/results", ORIGIN, "DELETE", "content-type"),
        ("/api/bridge/results", ORIGIN, "POST", "authorization"),
    ]:
        response = client.options(
            path,
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": header,
            },
        )
        assert response.status_code in (403, 405)
        assert "access-control-allow-origin" not in response.headers
