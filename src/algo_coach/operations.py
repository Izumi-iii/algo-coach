"""Durable operation journal, separate from model-controlled workflow decisions."""

import json


class Operations:
    def __init__(self, connection):
        self.connection = connection
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_operations "
            "(id TEXT PRIMARY KEY, kind TEXT NOT NULL, session TEXT NOT NULL, "
            "status TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        connection.commit()

    def prepare(self, operation_id: str, kind: str, session: str, payload: dict):
        self.connection.execute(
            "INSERT OR IGNORE INTO app_operations VALUES (?, ?, ?, ?, ?)",
            (operation_id, kind, session, "prepared", json.dumps(payload, ensure_ascii=False)),
        )
        self.connection.commit()
        return self.get(operation_id, kind, session)

    def get(self, operation_id: str, kind: str, session: str):
        row = self.connection.execute(
            "SELECT status, payload FROM app_operations WHERE id=? AND kind=? AND session=?",
            (operation_id, kind, session),
        ).fetchone()
        if not row:
            raise ValueError("操作预览不存在或不属于当前会话，请重新预览")
        return {"status": row[0], "payload": json.loads(row[1])}

    def update(self, operation_id: str, status: str, **fields):
        row = self.connection.execute(
            "SELECT payload FROM app_operations WHERE id=?", (operation_id,)
        ).fetchone()
        if not row:
            raise ValueError("操作记录不存在")
        payload = {**json.loads(row[0]), **fields}
        self.connection.execute(
            "UPDATE app_operations SET status=?, payload=? WHERE id=?",
            (status, json.dumps(payload, ensure_ascii=False), operation_id),
        )
        self.connection.commit()
        return payload

    def known_commits(self, root: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT payload FROM app_operations WHERE kind='commit' AND status='committed'"
        ).fetchall()
        return {
            item["commit_oid"]
            for (data,) in rows
            if (item := json.loads(data)).get("root") == root and item.get("commit_oid")
        }
