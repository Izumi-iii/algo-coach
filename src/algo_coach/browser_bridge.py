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

from algo_coach.models import StrictModel


class BridgeError(ValueError):
    pass


class BrowserReceipt(StrictModel):
    submission_id: str = Field(pattern=r"^[0-9]{1,30}$")
    state: Literal["SUCCESS"]
    status_code: StrictInt = Field(ge=0, le=10000)
    question_id: str | None = Field(default=None, pattern=r"^[0-9]{1,30}$")
    runtime: str = Field(default="", max_length=200)
    memory: str = Field(default="", max_length=200)


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

    def pair(self, extension_id):
        if not isinstance(extension_id, str) or not re.fullmatch(r"[a-p]{32}", extension_id):
            raise BridgeError("扩展标识无效。")
        with self.lock:
            self.pairs = {k: v for k, v in self.pairs.items() if v["expires"] > self.clock()}
            if len(self.pairs) >= 16:
                raise BridgeError("连接数量已满，请稍后重连。")
            token = secrets.token_urlsafe(32)
            self.pairs[token] = {
                "origin": "chrome-extension://" + extension_id,
                "expires": self.clock() + 900,
                "digest": None,
                "receipt": None,
            }
            return {"token": token, "expires_in": 900}

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
            self.db.execute(
                "INSERT INTO app_browser_receipts VALUES (?, ?)",
                (record["receipt_id"], json.dumps(record, ensure_ascii=False)),
            )
            self.db.commit()
            pair.update(digest=digest, receipt=record)
            return record

    def recent(self):
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT payload FROM app_browser_receipts ORDER BY rowid DESC LIMIT 10"
            )
        ]
