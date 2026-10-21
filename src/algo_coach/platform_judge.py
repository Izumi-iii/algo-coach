"""Experimental CN submission transport and durable, explicit submission operations.

No model calls, credential persistence, browser-cookie discovery or POST retries.
Platform reports remain separate from local Verification records.
"""

import hashlib
import json
import os
import re
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx

from algo_coach.adapters import problem_hash
from algo_coach.models import Problem
from algo_coach.verification import inspect_code

BASE = "https://leetcode.cn"
VERDICTS = {
    10: "accepted",
    11: "wrong_answer",
    12: "memory_limit",
    13: "output_limit",
    14: "time_limit",
    15: "runtime_error",
    20: "compile_error",
}


class PlatformError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def stamp():
    return datetime.now(UTC).isoformat()


def code_digest(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class CNSubmissionClient:
    def __init__(self, session=None, csrf=None, *, client=None):
        self.session = (
            session if session is not None else os.getenv("ALGOCOACH_LEETCODE_SESSION", "")
        )
        self.csrf = csrf if csrf is not None else os.getenv("ALGOCOACH_LEETCODE_CSRF", "")
        self.client = client

    def request(self, method, path, payload=None, *, referer="/"):
        if not self.session or not self.csrf:
            raise PlatformError(
                "unconfigured",
                "未配置中国站登录态；请在本地配置环境变量，不要把 Cookie 发到聊天中。",
            )
        if any(
            len(value) > 10000 or not re.fullmatch(r"[A-Za-z0-9._~+/%=-]+", value)
            for value in (self.session, self.csrf)
        ):
            raise PlatformError("credentials", "登录态格式无效。")
        owned = self.client is None
        client = self.client or httpx.Client(
            timeout=httpx.Timeout(15, connect=5), follow_redirects=False
        )
        try:
            with client.stream(
                method,
                BASE + path,
                json=payload,
                follow_redirects=False,
                headers={
                    "Cookie": f"LEETCODE_SESSION={self.session}; csrftoken={self.csrf}",
                    "X-CSRFToken": self.csrf,
                    "Origin": BASE,
                    "Referer": BASE + referer,
                    "User-Agent": "AlgoCoach/0.1",
                    "Accept": "application/json",
                },
            ) as response:
                if response.status_code in (401, 403):
                    raise PlatformError(
                        "access_denied",
                        "中国站拒绝访问，请在原站检查登录或验证；不会绕过访问限制。",
                    )
                if response.status_code == 429:
                    raise PlatformError(
                        "rate_limit", "中国站限流，请稍后再查询；不会自动重新提交。"
                    )
                if response.status_code != 200:
                    raise PlatformError("http_error", "平台返回非预期状态，请检查原站记录。")
                raw = bytearray()
                for part in response.iter_bytes():
                    raw.extend(part)
                    if len(raw) > 256 * 1024:
                        raise PlatformError("response_limit", "平台响应超过限制。")
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise PlatformError("schema", "平台返回结构异常。")
                return data
        except (httpx.HTTPError, ValueError, RecursionError) as exc:
            if isinstance(exc, PlatformError):
                raise
            raise PlatformError("transport", "平台连接或响应解析失败；不会自动重发提交。") from None
        finally:
            if owned:
                client.close()

    def query(self, query, variables=None):
        data = self.request("POST", "/graphql/", {"query": query, "variables": variables or {}})
        if data.get("errors") or not isinstance(data.get("data"), dict):
            raise PlatformError("graphql", "平台查询失败，不保存原始错误。")
        return data["data"]

    def account(self):
        status = self.query("query { userStatus { isSignedIn username } }").get("userStatus") or {}
        if (
            not isinstance(status, dict)
            or status.get("isSignedIn") is not True
            or not isinstance(status.get("username"), str)
            or not status["username"]
        ):
            raise PlatformError("not_signed_in", "登录态未通过原站确认，请重新登录中国站。")
        return code_digest(status["username"])

    def target(self, problem):
        data = self.query(
            "query($slug:String!) { question(titleSlug:$slug) { "
            "questionId questionFrontendId titleSlug isPaidOnly codeSnippets { langSlug } } }",
            {"slug": problem.slug},
        )
        q = data.get("question") or {}
        if not isinstance(q, dict) or not isinstance(q.get("codeSnippets"), list):
            raise PlatformError("target", "远程题目定位结构异常。")
        qid = q.get("questionId")
        if (
            problem.site != "leetcode_cn"
            or q.get("questionFrontendId") != problem.frontend_id
            or q.get("titleSlug") != problem.slug
            or not isinstance(qid, str)
            or not re.fullmatch(r"[0-9]{1,20}", qid)
            or q.get("isPaidOnly") is not False
            or not any(
                isinstance(s, dict) and s.get("langSlug") == "python3"
                for s in q.get("codeSnippets", [])
            )
        ):
            raise PlatformError("target", "题目定位不一致、非公开题或不支持 Python3，未提交。")
        return {
            "question_id": qid,
            "frontend_id": problem.frontend_id,
            "slug": problem.slug,
            "language": "python3",
        }

    def submit(self, target, code):
        return self.request(
            "POST",
            f"/problems/{target['slug']}/submit/",
            {"lang": "python3", "question_id": target["question_id"], "typed_code": code},
            referer=f"/problems/{target['slug']}/",
        )

    def check(self, submission_id):
        return self.request("GET", f"/submissions/detail/{submission_id}/check/")


class PlatformSubmissions:
    def __init__(self, coach, *, client=None, enabled=None):
        self.coach = coach
        self.db = coach.connection
        self.client = client or CNSubmissionClient()
        self.enabled = (
            enabled
            if enabled is not None
            else os.getenv("ALGOCOACH_PLATFORM_SUBMIT_ENABLED", "false").lower() == "true"
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS app_platform_submissions "
            "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.db.commit()

    def save(self, record):
        self.db.execute(
            "INSERT OR REPLACE INTO app_platform_submissions VALUES (?, ?)",
            (record["operation_id"], json.dumps(record, ensure_ascii=False)),
        )
        self.db.commit()

    def load(self, operation_id, session_id):
        if not re.fullmatch(r"[0-9a-f]{32}", operation_id):
            raise PlatformError("operation", "操作 ID 无效。")
        row = self.db.execute(
            "SELECT payload FROM app_platform_submissions WHERE id=?", (operation_id,)
        ).fetchone()
        if not row:
            raise PlatformError("operation", "提交操作不存在，请使用原数据目录。")
        record = json.loads(row[0])
        if record["session_id"] != session_id:
            raise PlatformError("ownership", "提交操作不属于该会话。")
        return record

    @staticmethod
    def view(record):
        return {k: v for k, v in record.items() if k != "submitted_code"}

    def preview(self, session_id):
        state = self.coach.get(session_id)
        code = state.get("current_code", "")
        if (
            not code
            or len(code.encode("utf-8")) > 128 * 1024
            or inspect_code(code).status == "syntax_error"
        ):
            raise PlatformError("code", "请先保存可解析的 Python 代码；本操作只提交保存版本。")
        if code_digest(code) != state.get("code_hash"):
            raise PlatformError("binding", "保存代码与哈希不一致。")
        account = self.client.account()
        problem = Problem.model_validate(state["problem"])
        target = self.client.target(problem)
        record = {
            "operation_id": uuid4().hex,
            "session_id": session_id,
            "source": "leetcode_cn",
            "kind": "submit",
            "created_at": stamp(),
            "state": "preview",
            "verdict": None,
            "account_hash": account,
            "problem_hash": problem_hash(problem),
            "code_hash": state["code_hash"],
            "submitted_code": code,
            "target": target,
            "submission_id": None,
            "last_error": None,
        }
        self.save(record)
        return self.view(record)

    def apply(self, session_id, operation_id):
        record = self.load(operation_id, session_id)
        if record["state"] != "preview":
            # Includes interrupted 'sending': never repeat the POST, even without a task ID.
            return self.view(record)
        if not self.enabled:
            raise PlatformError("disabled", "平台提交默认禁用；审阅预览后显式启用再执行 apply。")
        state = self.coach.get(session_id)
        if (
            code_digest(record["submitted_code"]) != record["code_hash"]
            or state.get("current_code") != record["submitted_code"]
            or state.get("code_hash") != record["code_hash"]
            or problem_hash(Problem.model_validate(state["problem"])) != record["problem_hash"]
        ):
            raise PlatformError("stale", "代码或题面已改变，请重新预览。")
        if self.client.account() != record["account_hash"]:
            raise PlatformError("account", "登录账号已变化，请重新预览。")
        target = self.client.target(Problem.model_validate(state["problem"]))
        if target != record["target"]:
            raise PlatformError("target", "远程题目定位已变化，请重新预览。")
        record.update(state="sending", sent_at=stamp())
        self.save(record)
        try:
            result = self.client.submit(target, record["submitted_code"])
            sid = result.get("submission_id")
            if type(sid) not in (int, str) or not re.fullmatch(r"[0-9]{1,30}", str(sid)):
                raise PlatformError("schema", "提交响应没有有效任务 ID，请在原站检查，勿重复提交。")
            record.update(state="queued", submission_id=str(sid))
        except PlatformError as exc:
            record.update(state="unknown", last_error=exc.code)
        finally:
            self.save(record)
        return self.view(record)

    def poll(self, session_id, operation_id, *, attempts=10, sleep=time.sleep):
        record = self.load(operation_id, session_id)
        if not record["submission_id"] or record["state"] == "finished":
            return self.view(record)
        if not 1 <= attempts <= 20:
            raise PlatformError("budget", "查询次数需在 1～20 之间。")
        if self.client.account() != record["account_hash"]:
            raise PlatformError("account", "登录账号与原提交不一致。")
        for index in range(attempts):
            try:
                result = self.client.check(record["submission_id"])
                record.update(checked_at=stamp(), last_error=None)
                if result.get("state") in {"PENDING", "STARTED"}:
                    record["state"] = "judging"
                elif result.get("state") == "SUCCESS":
                    if str(result.get("submission_id")) != record["submission_id"]:
                        raise PlatformError("binding", "结果提交 ID 不匹配或缺失，未认定判题结论。")
                    if (
                        result.get("question_id") is not None
                        and str(result["question_id"]) != record["target"]["question_id"]
                    ):
                        raise PlatformError("binding", "结果题目 ID 不匹配。")
                    status = result.get("status_code")
                    if type(status) is not int or status not in VERDICTS:
                        raise PlatformError("schema", "未知判题状态，未认定 Accepted 或代码错误。")
                    record.update(
                        state="finished",
                        verdict=VERDICTS[status],
                        platform_status_code=status,
                        detail={
                            k: str(result[k])[:2000]
                            for k in (
                                "last_testcase",
                                "expected_output",
                                "code_output",
                                "status_runtime",
                                "status_memory",
                            )
                            if k in result
                        },
                    )
                else:
                    raise PlatformError("schema", "未知任务状态，稍后可继续查询。")
            except PlatformError as exc:
                record["last_error"] = exc.code
                self.save(record)
                break
            self.save(record)
            if record["state"] == "finished":
                break
            if index + 1 < attempts:
                sleep(2)
        return self.view(record)
