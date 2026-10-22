"""Loopback-only browser adapter for Coach. No additional runtime dependencies."""

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from filelock import Timeout
from pydantic import ValidationError

from algo_coach.browser_bridge import BridgeError, BrowserBridge, current_browser_feedback
from algo_coach.browser_submit import BrowserSubmissions
from algo_coach.runner import DockerRunner
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher, ModelTeacher

MAX_BODY = 1024 * 1024
IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")


class APIError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def session_view(state):
    """Expose learning data, never credentials, internal tasks or provider responses."""
    history = state.get("history", [])
    return {
        **{
            key: state.get(key)
            for key in (
                "session_id",
                "mode",
                "problem",
                "stage",
                "hint_level",
                "answer_revealed",
                "current_code",
                "code_hash",
                "verification",
                "last_error",
                "reflection",
                "problem_source",
                "daily_selection",
                "response",
            )
        },
        "history": [{"role": m["role"], "content": m["content"]} for m in history[-100:]],
        "history_total": len(history),
        "attempt_count": len(state.get("attempts", [])),
        "answer_checks": state.get("answer_checks", [])[-10:],
        "browser_feedback": current_browser_feedback(state),
    }


class WebApplication:
    """One serialized Coach operation at a time; polling never blocks on a model call."""

    def __init__(self, coach):
        self.coach = coach
        self.mode = coach.teacher.mode
        self.token = secrets.token_urlsafe(32)
        self.operation_lock = threading.Lock()
        self.jobs_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="algo-coach-web")
        self.jobs = {}
        self.bridge = BrowserBridge(coach.connection)
        self.platform = BrowserSubmissions(coach, self.bridge)
        self.problem_list = [
            {"key": p.key, "id": p.frontend_id, "title": p.title} for p in coach.problems.all()
        ]
        self._refresh_sessions()

    def close(self):
        self.executor.shutdown(wait=True)

    def _refresh_sessions(self):
        self.session_list = [
            {"id": sid, "title": title, "mode": mode, "created": created}
            for sid, title, mode, created in self.coach.sessions()
            if mode == self.mode
        ]

    def bootstrap(self):
        return {
            "mode": self.mode,
            "csrf_token": self.token,
            "problems": self.problem_list,
            "sessions": self.session_list,
            "busy": self.operation_lock.locked(),
            "runner_enabled": DockerRunner.from_env().enabled,
        }

    def _get(self, sid):
        if not isinstance(sid, str) or not IDENTIFIER.fullmatch(sid):
            raise APIError("会话 ID 无效")
        state = self.coach.get(sid)
        if state["mode"] != self.mode:
            raise APIError("会话模式不同，请使用对应的模型或演示模式启动。", 409)
        return state

    def get_session(self, sid):
        if not self.operation_lock.acquire(blocking=False):
            raise APIError("正在处理上一项操作，请等待完成后恢复会话。", 409)
        try:
            return session_view(self._get(sid))
        finally:
            self.operation_lock.release()

    def platform_request(self, action, payload):
        fields = {
            "prepare": {"session_id", "operation_id", "code_hash"},
            "recover": {"session_id", "operation_id", "submission_id"},
            "claim": {"operation_id"},
            "authorize": {"operation_id", "target"},
            "accepted": {"operation_id", "submission_id"},
            "pending": {"operation_id"},
            "finish": {"operation_id", "receipt"},
            "fail": {"operation_id", "code"},
        }
        if action not in fields or not isinstance(payload, dict) or set(payload) != fields[action]:
            raise APIError("平台操作字段无效。")
        if not isinstance(payload["operation_id"], str) or not IDENTIFIER.fullmatch(
            payload["operation_id"]
        ):
            raise APIError("平台操作 ID 无效。")
        if not self.operation_lock.acquire(blocking=False):
            raise APIError("正在处理学习操作，请稍后继续查询。", 409)
        try:
            if action == "prepare":
                return self.platform.prepare(
                    self._get(payload["session_id"]), payload["operation_id"], payload["code_hash"]
                )
            if action == "recover":
                self._get(payload["session_id"])
                record = self.platform.load(payload["operation_id"])
                if record["session_id"] != payload["session_id"]:
                    raise APIError("提交操作不属于当前会话。", 403)
                return self.platform.recover(payload["operation_id"], payload["submission_id"])
            record = self.platform.load(payload["operation_id"])
            self._get(record["session_id"])
            if action == "claim":
                return self.platform.claim(payload["operation_id"])
            method = (
                self.platform.authorize_send
                if action == "authorize"
                else getattr(self.platform, action)
            )
            value_key = {
                "authorize": "target",
                "accepted": "submission_id",
                "finish": "receipt",
                "fail": "code",
            }.get(action)
            return (
                method(payload["operation_id"], payload[value_key])
                if value_key
                else method(payload["operation_id"])
            )
        finally:
            self.operation_lock.release()

    @staticmethod
    def validate_request(payload):
        if not isinstance(payload, dict):
            raise APIError("请求必须是 JSON 对象")
        operation = payload.get("operation")
        fields = {
            "create": {"source", "reference"},
            "turn": {"session_id", "text"},
            "code": {"session_id", "code"},
            "test": {"session_id", "code_hash"},
            "bind_browser": {"session_id", "receipt_id", "code_hash"},
            "doctor": set(),
        }
        if not isinstance(operation, str) or operation not in fields:
            raise APIError("不支持此网页操作")
        if set(payload) != fields[operation] | {"operation", "request_id"}:
            raise APIError("请求字段不完整或包含未支持的字段")
        if not isinstance(payload["request_id"], str) or not IDENTIFIER.fullmatch(
            payload["request_id"]
        ):
            raise APIError("请求 ID 无效")
        if any(not isinstance(payload[k], str) for k in fields[operation]):
            raise APIError("请求字段必须是文本")
        if operation == "create":
            if payload["source"] not in {"local", "remote", "daily"}:
                raise APIError("请选择本地题库、中国站题目或中国站每日题")
            if len(payload["reference"]) > 1000:
                raise APIError("题号或链接过长")
        if operation == "turn" and payload["text"].strip().startswith("/"):
            if payload["text"].strip() not in {"/hint", "/answer", "/retry"}:
                raise APIError("网页聊天仅支持 /hint、/answer、/retry 快捷命令")

    def submit(self, payload):
        self.validate_request(payload)
        job_id = payload["request_id"]
        signature = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.jobs_lock:
            previous = self.jobs.get(job_id)
            if previous:
                if previous["signature"] != signature:
                    raise APIError("同一请求 ID 不能用于不同操作", 409)
                return {"job_id": job_id}
            if not self.operation_lock.acquire(blocking=False):
                raise APIError("已有操作正在进行，请等待完成，勿重复发送。", 409)
            # Only one pending job; retain a bounded set of finished request IDs.
            while len(self.jobs) >= 64:
                del self.jobs[next(iter(self.jobs))]
            self.jobs[job_id] = {"status": "running", "signature": signature}
        self.executor.submit(self._execute, job_id, payload)
        return {"job_id": job_id}

    def job(self, job_id):
        with self.jobs_lock:
            value = self.jobs.get(job_id)
            if value is None:
                raise APIError(
                    "任务不存在或服务已重启。请恢复会话核对已保存结果，勿自动重发。", 404
                )
            return {key: value[key] for key in ("status", "result", "error") if key in value}

    def _execute(self, job_id, payload):
        try:
            operation = payload["operation"]
            if operation == "doctor":
                result = {"doctor": DockerRunner.from_env().doctor()}
            elif operation == "create":
                source, reference = payload["source"], payload["reference"].strip()
                if source == "daily":
                    state = self.coach.start_remote_daily()
                elif source == "remote":
                    state = self.coach.start_remote(reference)
                else:
                    state = self.coach.start(self.coach.problems.resolve(reference))
                result = {"state": session_view(state)}
            else:
                sid = payload["session_id"]
                current = self._get(sid)
                if operation == "test":
                    if not current.get("code_hash") or payload["code_hash"] != current["code_hash"]:
                        raise APIError("代码版本已变化，请恢复最新提交后再验证。", 409)
                    state = self.coach.test(sid)
                elif operation == "code":
                    state = self.coach.turn(
                        sid, "请分析我提交的代码，不要执行。", code=payload["code"]
                    )
                elif operation == "bind_browser":
                    state = self.coach.bind_browser_result(
                        sid, payload["receipt_id"], payload["code_hash"]
                    )
                else:
                    state = self.coach.turn(sid, payload["text"])
                result = {"state": session_view(state)}
            outcome = {"status": "completed", "result": result}
        except ValidationError:
            outcome = {"status": "failed", "error": "输入或题面数据未通过校验，请检查内容后重试。"}
        except ValueError as exc:
            outcome = {"status": "failed", "error": str(exc)}
        except Exception:
            # No raw provider exceptions, credentials or tracebacks in HTTP responses.
            outcome = {
                "status": "failed",
                "error": "操作未完成，请恢复会话核对已保存结果；不要重复提交同一请求。",
            }
        finally:
            try:
                self._refresh_sessions()
            except Exception:
                outcome = {
                    "status": "failed",
                    "error": "无法刷新学习记录。请恢复会话核对已保存结果。",
                }
            finally:
                self.operation_lock.release()
        with self.jobs_lock:
            self.jobs[job_id].update(outcome)


def make_server(app, port=8765):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_data(
            self, status, value, content_type="application/json; charset=utf-8", *, cors_origin=None
        ):
            body = (
                json.dumps(value, ensure_ascii=False).encode() if isinstance(value, dict) else value
            )
            self.send_response(status)
            for name, data in {
                "Content-Type": content_type,
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; "
                    "object-src 'none'; base-uri 'none'; form-action 'self'"
                ),
            }.items():
                self.send_header(name, data)
            if cors_origin:
                self.send_header("Access-Control-Allow-Origin", cors_origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "POST")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-AlgoCoach-Bridge")
                self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def trusted_host(self):
            actual_port = self.server.server_address[1]
            host = self.headers.get("Host", "")
            if host not in {f"127.0.0.1:{actual_port}", f"localhost:{actual_port}"}:
                raise APIError("仅允许本机地址访问", 403)
            return host

        def trusted_request(self, *, mutation=False):
            host = self.trusted_host()
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                raise APIError("不接受跨站请求", 403)
            if mutation:
                if origin != f"http://{host}" or not secrets.compare_digest(
                    self.headers.get("X-AlgoCoach-Token", "").encode(), app.token.encode()
                ):
                    raise APIError("页面已过期或请求来源不符，请刷新页面。", 403)

        def do_GET(self):
            try:
                self.trusted_request()
                path = urlsplit(self.path).path
                assets = {
                    "/": ("index.html", "text/html; charset=utf-8"),
                    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/platform.js": ("platform.js", "text/javascript; charset=utf-8"),
                    "/style.css": ("style.css", "text/css; charset=utf-8"),
                }
                if path in assets:
                    name, content_type = assets[path]
                    self.send_data(
                        200,
                        files("algo_coach").joinpath("web_assets", name).read_bytes(),
                        content_type,
                    )
                elif path == "/api/bootstrap":
                    self.send_data(200, app.bootstrap())
                elif re.fullmatch(r"/api/platform/session/[a-f0-9]{32}", path):
                    if not app.operation_lock.acquire(blocking=False):
                        raise APIError("正在处理学习操作，请稍后刷新。", 409)
                    try:
                        sid = path.rsplit("/", 1)[1]
                        app._get(sid)
                        self.send_data(200, {"submissions": app.platform.recent(sid)})
                    finally:
                        app.operation_lock.release()
                elif path == "/api/bridge/results":
                    if not app.operation_lock.acquire(blocking=False):
                        raise APIError("正在处理学习操作，请稍后刷新。", 409)
                    try:
                        self.send_data(200, {"receipts": app.bridge.recent()})
                    finally:
                        app.operation_lock.release()
                elif re.fullmatch(r"/api/sessions/[a-f0-9]{32}", path):
                    self.send_data(200, app.get_session(path.rsplit("/", 1)[1]))
                elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", path):
                    self.send_data(200, app.job(path.rsplit("/", 1)[1]))
                else:
                    raise APIError("页面或接口不存在", 404)
            except (APIError, ValueError) as exc:
                self.send_data(getattr(exc, "status", 400), {"error": str(exc)})
            except Exception:
                self.send_data(500, {"error": "无法读取本地数据，请稍后重试。"})

        def do_POST(self):
            cors_origin = None
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_BODY:
                    raise APIError("请求为空或过大", 413)
                # Drain the bounded request before returning an error. On Windows, closing
                # a socket with unread body bytes can reset it before the error is delivered.
                self.connection.settimeout(5)
                raw = self.rfile.read(length)
                if self.path == "/api/bridge/submit/step":
                    self.trusted_host()
                    origin = self.headers.get("Origin", "")
                    app.bridge.authorize(
                        self.headers.get("X-AlgoCoach-Bridge", ""), origin, "submit"
                    )
                    cors_origin = origin
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise APIError("仅接受 JSON 请求", 415)
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or not isinstance(payload.get("action"), str):
                        raise APIError("扩展操作无效。")
                    action = payload.pop("action")
                    if action not in {
                        "claim",
                        "authorize",
                        "accepted",
                        "pending",
                        "finish",
                        "fail",
                    }:
                        raise APIError("不支持的扩展操作。")
                    self.send_data(200, app.platform_request(action, payload), cors_origin=origin)
                    return
                if self.path == "/api/bridge/results":
                    self.trusted_host()
                    origin = self.headers.get("Origin", "")
                    if not app.bridge.allowed_origin(origin):
                        raise APIError("扩展未连接或来源不符。", 403)
                    cors_origin = origin
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise APIError("仅接受 JSON 请求", 415)
                    if not app.operation_lock.acquire(blocking=False):
                        raise APIError("正在处理学习操作，请稍后重试回传。", 409)
                    try:
                        receipt = app.bridge.receive(
                            self.headers.get("X-AlgoCoach-Bridge", ""), origin, json.loads(raw)
                        )
                    finally:
                        app.operation_lock.release()
                    self.send_data(200, receipt, cors_origin=origin)
                    return
                self.trusted_request(mutation=True)
                if self.path == "/api/bridge/pair":
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise APIError("仅接受 JSON 请求", 415)
                    payload = json.loads(raw)
                    if not isinstance(payload, dict) or set(payload) not in (
                        {"extension_id"},
                        {"extension_id", "purpose"},
                    ):
                        raise APIError("连接字段无效。")
                    self.send_data(
                        200,
                        app.bridge.pair(payload["extension_id"], payload.get("purpose", "receipt")),
                    )
                    return
                if self.path in {"/api/platform/prepare", "/api/platform/recover"}:
                    if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                        raise APIError("仅接受 JSON 请求", 415)
                    self.send_data(
                        200, app.platform_request(self.path.rsplit("/", 1)[1], json.loads(raw))
                    )
                    return
                if self.path != "/api/actions":
                    raise APIError("接口不存在", 404)
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise APIError("仅接受 JSON 请求", 415)
                payload = json.loads(raw)
                self.send_data(202, app.submit(payload))
            except (APIError, ValueError, UnicodeError, OSError, RecursionError) as exc:
                if isinstance(exc, BridgeError):
                    self.send_data(403, {"error": str(exc)}, cors_origin=cors_origin)
                    return
                message = str(exc) if isinstance(exc, APIError) else "请求内容无效或未完整发送"
                self.send_data(
                    getattr(exc, "status", 400), {"error": message}, cors_origin=cors_origin
                )

        def do_OPTIONS(self):
            origin = self.headers.get("Origin", "")
            if self.path in {
                "/api/bridge/results",
                "/api/bridge/submit/step",
            } and app.bridge.allowed_origin(origin):
                try:
                    self.trusted_host()
                    if self.headers.get("Access-Control-Request-Method") != "POST":
                        raise APIError("不支持此方法", 405)
                    requested = {
                        h.strip().lower()
                        for h in self.headers.get("Access-Control-Request-Headers", "").split(",")
                        if h.strip()
                    }
                    if requested - {"content-type", "x-algocoach-bridge"}:
                        raise APIError("不支持请求头", 403)
                    self.send_data(200, {}, cors_origin=origin)
                except APIError:
                    self.send_data(403, {"error": "扩展请求无效。"})
                return
            self.send_data(405, {"error": "不支持跨站访问"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AlgoCoach 本地 Web 测试界面")
    parser.add_argument("--demo", action="store_true", help="离线演示，不调用模型")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口应在 1～65535 之间")
    load_dotenv(Path.cwd() / ".env", override=False)
    data_dir = args.data_dir or Path(os.getenv("ALGOCOACH_DATA_DIR", ".algo-coach"))
    try:
        teacher = DemoTeacher() if args.demo else ModelTeacher()
        with Coach(data_dir, teacher) as coach:
            app = WebApplication(coach)
            try:
                server = make_server(app, args.port)
                print(f"AlgoCoach Web：http://127.0.0.1:{args.port}（按 Ctrl+C 停止）", flush=True)
                print(
                    "演示模式：不调用模型。"
                    if args.demo
                    else "模型模式：对话与提交代码会发送给配置的模型服务。",
                    flush=True,
                )
                try:
                    server.serve_forever()
                finally:
                    server.server_close()
            finally:
                app.close()
    except KeyboardInterrupt:
        print("\n服务已停止，已完成的会话保留在本地。")
        return 0
    except Timeout:
        print("学习目录正在被其他进程使用。请退出对应 CLI/Web，或用 --data-dir 指定独立目录。")
    except (ValueError, OSError):
        print("启动失败：请检查 .env 模型配置、学习目录权限及端口占用。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
