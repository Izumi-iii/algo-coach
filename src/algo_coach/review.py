"""Opt-in review queue. Ratings are self reports, never inferred platform acceptance."""

import json
from datetime import date, timedelta

from algo_coach.models import Problem

INTERVALS = (1, 3, 7, 14)


class Reviews:
    def __init__(self, connection):
        self.connection = connection
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS app_reviews ("
            "problem_key TEXT PRIMARY KEY, problem TEXT NOT NULL, source_session TEXT NOT NULL, "
            "due_date TEXT NOT NULL, step INTEGER NOT NULL, version INTEGER NOT NULL);"
            "CREATE TABLE IF NOT EXISTS app_review_events (session TEXT PRIMARY KEY, "
            "rating TEXT NOT NULL, payload TEXT NOT NULL);"
        )
        connection.commit()
        if "source" not in {row[1] for row in connection.execute("PRAGMA table_info(app_reviews)")}:
            connection.execute("ALTER TABLE app_reviews ADD COLUMN source TEXT")
            connection.commit()

    def get(self, key: str) -> dict:
        row = self.connection.execute(
            "SELECT problem, source_session, due_date, step, version, source "
            "FROM app_reviews WHERE problem_key=?",
            (key,),
        ).fetchone()
        if not row:
            raise ValueError("复习队列中没有该题，请先使用 /review add")
        return {
            "problem_key": key,
            "problem": Problem.model_validate_json(row[0]).model_dump(),
            "source_session": row[1],
            "due_date": row[2],
            "step": row[3],
            "version": row[4],
            "problem_source": json.loads(row[5]) if row[5] else None,
        }

    def add(self, problem: Problem, session: str, day: date, *, source=None) -> dict:
        existing = self.connection.execute(
            "SELECT problem FROM app_reviews WHERE problem_key=?",
            (problem.key,),
        ).fetchone()
        if existing and Problem.model_validate_json(existing[0]) != problem:
            raise ValueError("复习队列已有该题号的不同题面快照，暂不自动替换或合并进度")
        with self.connection:
            self.connection.execute(
                "INSERT OR IGNORE INTO app_reviews "
                "(problem_key, problem, source_session, due_date, step, version, source) "
                "VALUES (?, ?, ?, ?, 0, 1, ?)",
                (
                    problem.key,
                    problem.model_dump_json(),
                    session,
                    (day + timedelta(days=INTERVALS[0])).isoformat(),
                    json.dumps(source, ensure_ascii=False) if source else None,
                ),
            )
        return self.get(problem.key)

    def due(self, day: date) -> list[dict]:
        rows = self.connection.execute(
            "SELECT problem_key FROM app_reviews WHERE due_date <= ? "
            "ORDER BY due_date, problem_key",
            (day.isoformat(),),
        ).fetchall()
        # Public queue includes neither original solutions nor reflections.
        return [
            {
                "problem_key": item["problem_key"],
                "title": item["problem"]["title"],
                "due_date": item["due_date"],
            }
            for (key,) in rows
            if (item := self.get(key))
        ]

    def rate(self, state: dict, rating: str, day: date) -> dict:
        if rating not in ("again", "hard", "good"):
            raise ValueError("复习结果只能是 again / hard / good")
        context = state.get("review_context")
        if not context:
            raise ValueError("请先使用 /review start 题目标识 创建本次复习会话")
        if Problem.model_validate(state["problem"]).key != context["problem_key"]:
            raise ValueError("复习题目标识与会话不一致")
        existing = self.connection.execute(
            "SELECT rating, payload FROM app_review_events WHERE session=?",
            (state["session_id"],),
        ).fetchone()
        if existing:
            if existing[0] != rating:
                raise ValueError("本次复习已记录其他结果；如需再次练习，请创建新复习会话")
            return json.loads(existing[1])
        if rating != "again" and not state.get("current_code"):
            raise ValueError("请先保存本次复习代码；尚未完成可记录 again")
        item = self.get(context["problem_key"])
        if item["version"] != context["version"]:
            raise ValueError("另一复习会话已更新计划；本次结果未覆盖，请重新开始复习")
        assisted = (
            state.get("answer_revealed", False)
            or state.get("max_hint_level_used", state.get("hint_level", 0)) > 0
        )
        effective = "hard" if rating == "good" and assisted else rating
        status = (state.get("verification") or {}).get("status", "not_run")
        if status in ("syntax_error", "wrong_answer", "runtime_error", "timeout", "resource_limit"):
            effective = "again"
        step = min(item["step"] + 1, len(INTERVALS) - 1) if effective == "good" else 0
        result = {
            "problem_key": item["problem_key"],
            "session_id": state["session_id"],
            "rating": rating,
            "effective_rating": effective,
            "self_reported": True,
            "assisted": assisted,
            "code_hash": state.get("code_hash", ""),
            "verification_status": status,
            "rated_date": day.isoformat(),
            "due_date": (day + timedelta(days=INTERVALS[step])).isoformat(),
            "step": step,
        }
        with self.connection:
            changed = self.connection.execute(
                "UPDATE app_reviews SET due_date=?, step=?, version=version+1 "
                "WHERE problem_key=? AND version=?",
                (result["due_date"], step, item["problem_key"], context["version"]),
            )
            if changed.rowcount != 1:
                raise ValueError("复习计划已变化，本次没有覆盖")
            self.connection.execute(
                "INSERT INTO app_review_events VALUES (?, ?, ?)",
                (state["session_id"], rating, json.dumps(result, ensure_ascii=False)),
            )
        return result
