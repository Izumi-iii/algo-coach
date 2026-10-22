"""User-paired extension receipts. Browser reports are not server-verified verdicts."""

import hashlib
import json
import re
import secrets
import threading
import time
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from pydantic import Field, StrictInt

from algo_coach.adapters import problem_hash
from algo_coach.models import Problem, StrictModel


class BridgeError(ValueError):
    pass


class SubmissionDetail(StrictModel):
    submission_id: str = Field(pattern=r"^[0-9]{1,30}$")
    question_id: str = Field(pattern=r"^[0-9]{1,30}$")
    frontend_id: str = Field(pattern=r"^[0-9]{1,20}$")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,119}$")
    language: str = Field(min_length=1, max_length=40)
    source_code: str = Field(min_length=1, max_length=131072)


class BrowserReceipt(StrictModel):
    submission_id: str = Field(pattern=r"^[0-9]{1,30}$")
    state: Literal["SUCCESS"]
    status_code: StrictInt = Field(ge=0, le=10000)
    question_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    runtime: str = Field(default="", max_length=200)
    memory: str = Field(default="", max_length=200)
    detail: SubmissionDetail | None = None
    failure_details: dict[str, str] = Field(default_factory=dict, max_length=5)


class BrowserBridge:
    def __init__(self, connection, *, clock=time.monotonic):
        self.db, self.clock = connection, clock
        self.lock = threading.Lock()
        self.pairs = {}
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_browser_receipts "
            "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        connection.commit()

    def pair(self, extension_id, purpose="receipt"):
        if not isinstance(purpose, str) or purpose not in {"receipt", "submit"}:
            raise BridgeError("连接用途无效。")
        if not isinstance(extension_id, str) or not re.fullmatch(r"[a-p]{32}", extension_id):
            raise BridgeError("扩展标识无效。")
        with self.lock:
            self.pairs = {k: v for k, v in self.pairs.items() if v["expires"] > self.clock()}
            if purpose == "submit":
                for token, pair in self.pairs.items():
                    if (
                        pair["origin"] == "chrome-extension://" + extension_id
                        and pair["purpose"] == "submit"
                    ):
                        pair["expires"] = self.clock() + 7200
                        return {"token": token, "expires_in": 7200}
            if len(self.pairs) >= 16:
                raise BridgeError("连接数量已满，请稍后重连。")
            duration = 7200 if purpose == "submit" else 900
            token = secrets.token_urlsafe(32)
            self.pairs[token] = {
                "origin": "chrome-extension://" + extension_id,
                "expires": self.clock() + duration,
                "purpose": purpose,
                "digest": None,
                "receipt": None,
            }
            return {"token": token, "expires_in": duration}

    def authorize(self, token, origin, purpose):
        with self.lock:
            pair = self.pairs.get(token)
            if not pair or pair["origin"] != origin or pair["expires"] <= self.clock():
                raise BridgeError("扩展连接已失效，请刷新本地页面重新连接。")
            if pair["purpose"] != purpose:
                raise BridgeError("此连接不能执行平台提交操作。")

    def allowed_origin(self, origin):
        with self.lock:
            return any(
                p["origin"] == origin and p["expires"] > self.clock() for p in self.pairs.values()
            )

    def receive(self, token, origin, payload):
        receipt = BrowserReceipt.model_validate(payload)
        serialized = json.dumps(receipt.model_dump(), ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self.lock:
            pair = self.pairs.get(token)
            if not pair or pair["expires"] <= self.clock() or pair["origin"] != origin:
                raise BridgeError("扩展连接无效或已过期，请在本地网页重新连接。")
            if pair["digest"]:
                if pair["digest"] != digest:
                    raise BridgeError("本次连接已经使用，请重新连接后读取其他提交。")
                return pair["receipt"]
            record = self.store_receipt(receipt)
            pair.update(digest=digest, receipt=record)
            return record

    def store_receipt(self, receipt):
        allowed = {
            "compile_error",
            "runtime_error",
            "last_testcase",
            "expected_output",
            "code_output",
        }
        if set(receipt.failure_details) - allowed or any(
            len(v) > 4000 for v in receipt.failure_details.values()
        ):
            raise BridgeError("判题详情字段或长度无效。")
        # These are browser-reported data, not remotely attested by this server.
        names = {
            10: "Accepted",
            11: "Wrong Answer",
            12: "Memory Limit",
            13: "Output Limit",
            14: "Time Limit",
            15: "Runtime Error",
            20: "Compile Error",
        }
        record = {
            "receipt_id": uuid4().hex,
            **receipt.model_dump(),
            "source": "browser_extension_report",
            "verdict": names.get(receipt.status_code, "未知判题状态"),
            "received_at": datetime.now(UTC).isoformat(),
            "url": f"https://leetcode.cn/submissions/detail/{receipt.submission_id}/",
            "code_binding": "unbound",
            "scope": "浏览器回传的历史提交，未绑定当前代码；不改变本地验证或教学结论。",
        }
        record.pop("detail", None)
        if receipt.detail:
            detail = receipt.detail
            if detail.submission_id != receipt.submission_id or (
                receipt.question_id is not None and detail.question_id != receipt.question_id
            ):
                raise BridgeError("详情与判题结果的提交或题目标识不一致。")
            code_bytes = detail.source_code.encode("utf-8")
            if len(code_bytes) > 128 * 1024:
                raise BridgeError("提交源码超过 128 KiB，不能用于匹配。")
            record["detail"] = {
                **detail.model_dump(exclude={"source_code"}),
                "language": {"Python3": "python3", "python3": "python3"}.get(
                    detail.language, detail.language
                ),
                "source_code_hash": hashlib.sha256(code_bytes).hexdigest(),
            }
        self.db.execute(
            "INSERT INTO app_browser_receipts VALUES (?, ?)",
            (record["receipt_id"], json.dumps(record, ensure_ascii=False)),
        )
        self.db.commit()
        return record

    def recent(self):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT payload FROM app_browser_receipts ORDER BY rowid DESC LIMIT 10"
            )
        ]

    def load(self, receipt_id):
        row = self.db.execute(
            "SELECT payload FROM app_browser_receipts WHERE id=?", (receipt_id,)
        ).fetchone()
        if not row:
            raise BridgeError("回传记录不存在，请先读取并回传。")
        return json.loads(row[0])


def bind_receipt(record, state):
    """Match server-loaded receipt to saved state; never promote it to local verification."""
    detail = record.get("detail") or {}
    problem = Problem.model_validate(state["problem"])
    code = state.get("current_code", "")
    if not detail:
        raise BridgeError("回传缺少题目、语言和源码证据，请更新扩展并重新读取。")
    if (
        problem.site != "leetcode_cn"
        or detail.get("frontend_id") != problem.frontend_id
        or detail.get("slug") != problem.slug
    ):
        raise BridgeError("回传题目与当前会话不一致。")
    if detail.get("language") != "python3":
        raise BridgeError("语言不匹配：当前教学代码仅支持 Python3。")
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if (
        not code
        or code_hash != state.get("code_hash")
        or code_hash != detail.get("source_code_hash")
        or not state.get("attempts")
    ):
        raise BridgeError("源码版本不一致，请先保存与原站提交完全相同的代码（含空格和换行）。")
    if record.get("status_code") not in {10, 11, 12, 13, 14, 15, 20}:
        raise BridgeError("未知判题状态，不能用于教学反馈。")
    return {
        "receipt_id": record["receipt_id"],
        "submission_id": record["submission_id"],
        "source": "browser_extension_report",
        "session_id": state["session_id"],
        "attempt_id": state["attempts"][-1]["attempt_id"],
        "problem_hash": problem_hash(problem),
        "site": problem.site,
        "frontend_id": problem.frontend_id,
        "slug": problem.slug,
        "language": "python3",
        "code_hash": code_hash,
        "status_code": record["status_code"],
        "verdict": record["verdict"],
        "failure_details": record.get("failure_details", {}),
        "received_at": record["received_at"],
        "bound_at": datetime.now(UTC).isoformat(),
        "scope": "浏览器报告；已匹配题目、Python3 和源码，非独立认证，不替代本地验证或完成声明。",
    }


def current_browser_feedback(state):
    """Recheck binding on every use, including restored sessions and model context."""
    code = state.get("current_code", "")
    attempts = state.get("attempts") or []
    if not state.get("browser_reports") or not code or not attempts:
        return None
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    problem = Problem.model_validate(state["problem"])
    for report in reversed(state.get("browser_reports", [])):
        if (
            report.get("source") == "browser_extension_report"
            and report.get("session_id") == state.get("session_id")
            and report.get("attempt_id") == attempts[-1]["attempt_id"]
            and report.get("problem_hash") == problem_hash(problem)
            and report.get("site") == problem.site == "leetcode_cn"
            and report.get("frontend_id") == problem.frontend_id
            and report.get("slug") == problem.slug
            and report.get("language") == "python3"
            and report.get("code_hash") == code_hash == state.get("code_hash")
        ):
            return report
    return None
