"""Durable browser submission intents. Only the paired extension contacts LeetCode."""

import json
import re

from pydantic import Field

from algo_coach.adapters import problem_hash
from algo_coach.browser_bridge import BridgeError, BrowserReceipt
from algo_coach.models import Problem, StrictModel
from algo_coach.platform_judge import VERDICTS, code_digest, stamp

ACTIVE = {"prepared", "checking", "sending", "unknown", "queued", "judging"}
ERRORS = {
    "login": "请在 LeetCode 中国站完成登录，再继续查询。",
    "account": "原站账号已变化，请恢复提交时的账号。",
    "target": "远程题目或 Python3 语言不匹配，已停止。",
    "csrf": "原站提交令牌不可用，请刷新原站页面后再试。",
    "access": "原站拒绝访问或要求验证，请在原站处理。",
    "transport": "原站连接或响应异常；已发送的提交不会自动重发。",
    "schema": "原站接口结构或判题状态未识别，未认定通过。",
    "detail": "暂未取得可匹配的原提交详情，请稍后继续查询。",
    "binding": "提交结果的题目、语言或源码与提交快照不一致。",
    "extension": "扩展任务中断，请重新连接后查询；不会自动重发提交。",
}


class Preflight(StrictModel):
    account_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    question_id: str = Field(pattern=r"^[0-9]{1,30}$")
    frontend_id: str = Field(pattern=r"^[0-9]{1,20}$")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,119}$")
    language: str = Field(pattern=r"^python3$")


class BrowserSubmissions:
    def __init__(self, coach, bridge):
        self.coach, self.db, self.bridge = coach, coach.connection, bridge
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS app_browser_submissions "
            "(id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.db.commit()

    def save(self, record):
        self.db.execute(
            "INSERT OR REPLACE INTO app_browser_submissions VALUES (?, ?)",
            (record["operation_id"], json.dumps(record, ensure_ascii=False)),
        )
        self.db.commit()

    def load(self, operation_id):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise BridgeError("提交操作 ID 无效。")
        row = self.db.execute(
            "SELECT payload FROM app_browser_submissions WHERE id=?", (operation_id,)
        ).fetchone()
        if not row:
            raise BridgeError("提交操作不存在，请使用原学习目录。")
        return json.loads(row[0])

    @staticmethod
    def view(record):
        return {k: v for k, v in record.items() if k not in {"source_code", "problem_snapshot"}}

    def recent(self, session_id):
        return [
            self.view(record)
            for (payload,) in self.db.execute(
                "SELECT payload FROM app_browser_submissions ORDER BY rowid DESC"
            )
            if (record := json.loads(payload))["session_id"] == session_id
        ][:10]

    def matches_current(self, record):
        state = self.coach.get(record["session_id"])
        return (
            state.get("code_hash") == record["code_hash"]
            and state.get("current_code") == record["source_code"]
            and state.get("attempts", [{}])[-1].get("attempt_id") == record["attempt_id"]
            and problem_hash(Problem.model_validate(state["problem"])) == record["problem_hash"]
        )

    def prepare(self, state, operation_id, code_hash):
        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            raise BridgeError("提交操作 ID 无效。")
        code = state.get("current_code", "")
        if (
            not code.strip()
            or code_hash != state.get("code_hash")
            or code_digest(code) != code_hash
        ):
            raise BridgeError("请先保存当前代码，再提交到 LeetCode。")
        if len(code.encode("utf-8")) > 128 * 1024 or not state.get("attempts"):
            raise BridgeError("代码大小或保存版本无效。")
        problem = Problem.model_validate(state["problem"])
        if problem.site != "leetcode_cn" or not problem.frontend_id.isdigit():
            raise BridgeError("当前仅支持中国站普通数字题号和 Python3。")
        old = self.db.execute(
            "SELECT payload FROM app_browser_submissions WHERE id=?", (operation_id,)
        ).fetchone()
        attempt_id = state["attempts"][-1]["attempt_id"]
        if old:
            previous = json.loads(old[0])
            if (previous["session_id"], previous["attempt_id"], previous["code_hash"]) != (
                state["session_id"],
                attempt_id,
                code_hash,
            ):
                raise BridgeError("同一操作 ID 不能用于其他会话或版本。")
            return self.view(previous)
        for (payload,) in self.db.execute("SELECT payload FROM app_browser_submissions"):
            previous = json.loads(payload)
            if (
                previous["session_id"] == state["session_id"]
                and previous["code_hash"] == code_hash
                and previous["problem_hash"] == problem_hash(problem)
                and previous["state"] in ACTIVE
            ):
                return self.view(previous)
        record = {
            "operation_id": operation_id,
            "session_id": state["session_id"],
            "attempt_id": attempt_id,
            "code_hash": code_hash,
            "source_code": code,
            "problem_snapshot": problem.model_dump(),
            "problem_hash": problem_hash(problem),
            "frontend_id": problem.frontend_id,
            "slug": problem.slug,
            "language": "python3",
            "state": "prepared",
            "created_at": stamp(),
            "target": None,
            "submission_id": None,
            "verdict": None,
            "receipt_id": None,
            "last_error": None,
            "binding_status": "pending",
        }
        self.save(record)
        return self.view(record)

    def claim(self, operation_id):
        record = self.load(operation_id)
        if record["state"] in {"prepared", "checking"}:
            if not self.matches_current(record):
                record.update(
                    state="cancelled", last_error="提交前代码或题面已变化，请重新保存并提交。"
                )
            else:
                record["state"] = "checking"
            self.save(record)
        # Full snapshot is sent only through the authenticated extension channel.
        return record

    def authorize_send(self, operation_id, payload):
        record = self.load(operation_id)
        if record["state"] != "checking":
            raise BridgeError("该操作已进入发送阶段或已结束，禁止再次发送。")
        target = Preflight.model_validate(payload).model_dump()
        if (
            target["frontend_id"] != record["frontend_id"]
            or target["slug"] != record["slug"]
            or not self.matches_current(record)
        ):
            raise BridgeError("提交目标或保存版本已变化，未授权发送。")
        record.update(state="sending", target=target, sent_at=stamp(), last_error=None)
        self.save(record)  # Durable intent BEFORE granting permission for the single POST.
        return {"authorized": True}

    def accepted(self, operation_id, submission_id):
        record = self.load(operation_id)
        if not isinstance(submission_id, str) or not re.fullmatch(r"[0-9]{1,30}", submission_id):
            raise BridgeError("提交标识无效，不会自动重发。")
        if record["submission_id"]:
            if record["submission_id"] != submission_id:
                raise BridgeError("提交标识与已有记录不一致。")
            return self.view(record)
        if record["state"] not in {"sending", "unknown"} or not record["target"]:
            raise BridgeError("该操作尚未授权发送。")
        record.update(state="queued", submission_id=submission_id, last_error=None)
        self.save(record)
        return self.view(record)

    def recover(self, operation_id, submission_id):
        record = self.load(operation_id)
        if not isinstance(submission_id, str) or not re.fullmatch(r"[0-9]{1,30}", submission_id):
            raise BridgeError("提交标识无效。")
        if not record["target"] or not (
            record["state"] in {"sending", "unknown"}
            or (record.get("recovered") and record["state"] in {"queued", "judging"})
        ):
            raise BridgeError("该任务无需手动恢复，已有受理标识不能替换。")
        record.update(state="queued", submission_id=submission_id, recovered=True, last_error=None)
        self.save(record)
        return self.view(record)

    def fail(self, operation_id, code):
        record = self.load(operation_id)
        if not isinstance(code, str) or code not in ERRORS:
            raise BridgeError("扩展错误类别无效。")
        if record["state"] in {"finished", "cancelled"}:
            return self.view(record)
        if record["state"] in {"prepared", "checking"}:
            record["state"] = "blocked"
        elif record["state"] == "sending":
            record["state"] = "unknown"
        record.update(last_error=ERRORS[code], error_code=code)
        self.save(record)
        return self.view(record)

    def pending(self, operation_id):
        record = self.load(operation_id)
        if record["state"] not in {"queued", "judging"}:
            raise BridgeError("该操作没有可查询的提交。")
        record.update(state="judging", last_error=None, checked_at=stamp())
        self.save(record)
        return self.view(record)

    def finish(self, operation_id, payload):
        record = self.load(operation_id)
        if record["state"] == "finished":
            return self.view(record)
        receipt = BrowserReceipt.model_validate(payload)
        detail = receipt.detail
        if record["state"] not in {"queued", "judging"} or not detail:
            raise BridgeError("提交未受理或缺少原代码证据。")
        if (
            receipt.submission_id != record["submission_id"]
            or detail.submission_id != record["submission_id"]
            or detail.frontend_id != record["frontend_id"]
            or detail.slug != record["slug"]
            or detail.question_id != record["target"]["question_id"]
            or detail.language not in {"Python3", "python3"}
            or detail.source_code != record["source_code"]
            or receipt.status_code not in VERDICTS
        ):
            raise BridgeError("判题结果与提交快照不一致，未采用该结论。")
        # First persist the receipt ID; retries after a checkpoint failure reuse this receipt.
        if not record["receipt_id"]:
            stored = self.bridge.store_receipt(receipt)
            record["receipt_id"] = stored["receipt_id"]
            self.save(record)
        elif self.bridge.load(record["receipt_id"])["status_code"] != receipt.status_code:
            raise BridgeError("重试回传的判题状态与已保存回执不一致。")
        if self.matches_current(record):
            self.coach.bind_browser_result(
                record["session_id"], record["receipt_id"], record["code_hash"]
            )
            record["binding_status"] = "current"
        else:
            record["binding_status"] = "stale"
        record.update(
            state="finished",
            verdict=VERDICTS[receipt.status_code],
            platform_status_code=receipt.status_code,
            runtime=receipt.runtime,
            memory=receipt.memory,
            failure_details=receipt.failure_details,
            finished_at=stamp(),
            last_error=None,
        )
        self.save(record)
        return self.view(record)
