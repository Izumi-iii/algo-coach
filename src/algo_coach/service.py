"""Local application boundary: session ownership, validation and persistence."""

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from langgraph.checkpoint.sqlite import SqliteSaver

from algo_coach.archive import (
    ArchivePlan,
    digest,
    json_text,
    learning_snapshot,
    plan_archive,
    publish,
)
from algo_coach.calendar import today
from algo_coach.daily import DailySelections
from algo_coach.errors import teaching_failure
from algo_coach.git_ops import GitService
from algo_coach.models import Attempt, Problem
from algo_coach.operations import Operations
from algo_coach.problems import LocalProblems
from algo_coach.remote import LeetCodeCN
from algo_coach.review import Reviews
from algo_coach.teaching import Teacher
from algo_coach.verification import VerificationService, inspect_code
from algo_coach.workflow import build_workflow

MAX_CODE_BYTES = 128 * 1024


def now() -> str:
    return datetime.now(UTC).isoformat()


class Coach:
    def __init__(self, data_dir: Path, teacher: Teacher, verifier=None, *, source_client=None):
        self.data_dir = data_dir.resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(self.data_dir / "app.lock", timeout=0)
        self.lock.acquire()
        try:
            self.connection = sqlite3.connect(
                self.data_dir / "checkpoints.sqlite", check_same_thread=False
            )
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS app_sessions "
                "(id TEXT PRIMARY KEY, title TEXT NOT NULL, "
                "mode TEXT NOT NULL, created TEXT NOT NULL)"
            )
            self.connection.commit()
            self.operations = Operations(self.connection)
            self.teacher = teacher
            self.verifier = verifier
            self.problems = LocalProblems(self.data_dir)
            self.daily = DailySelections(self.connection)
            self.reviews = Reviews(self.connection)
            self.remote = LeetCodeCN(self.connection, client=source_client)
            self.graph = build_workflow(
                teacher, SqliteSaver(self.connection), answer_verifier=verifier
            )
        except Exception:
            if hasattr(self, "connection"):
                self.connection.close()
            self.lock.release()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.close()
        self.lock.release()

    @staticmethod
    def config(session_id: str) -> dict:
        return {"configurable": {"thread_id": session_id}, "recursion_limit": 8}

    def start(
        self, problem: Problem, *, daily_selection=None, review_context=None, problem_source=None
    ) -> dict:
        session_id = uuid4().hex
        state = self.graph.invoke(
            {
                "session_id": session_id,
                "mode": self.teacher.mode,
                "problem": problem.model_dump(),
                "stage": "understand",
                "hint_level": 0,
                "answer_revealed": False,
                "action": "start",
                "history": [],
                "attempts": [],
                "current_code": "",
                "code_hash": "",
                "verification": None,
                "reflection": "",
                "usage": [],
                "request_id": uuid4().hex,
                "max_hint_level_used": 0,
                "completion_mode": "unfinished",
                "explanation": "",
                "solution_summary": None,
                "daily_selection": daily_selection,
                "review_context": review_context,
                "problem_source": problem_source,
            },
            self.config(session_id),
        )
        self.connection.execute(
            "INSERT INTO app_sessions VALUES (?, ?, ?, ?)",
            (session_id, problem.title, self.teacher.mode, now()),
        )
        self.connection.commit()
        return state

    def start_daily(self, day=None, site="leetcode_cn") -> dict:
        selection = self.daily.get(day or today(), site)
        return self.start(selection.problem, daily_selection=selection.model_dump(mode="json"))

    def start_remote(self, reference, *, offline=False, refresh=False):
        result = self.remote.fetch(reference, offline=offline, refresh=refresh)
        return self.start(result.snapshot.problem, problem_source=result.record())

    def start_remote_daily(self, day=None, *, offline=False, refresh=False):
        selection, result = self.remote.daily(day, offline=offline, refresh=refresh)
        return self.start(
            selection.problem,
            daily_selection=selection.model_dump(mode="json"),
            problem_source=result.record(),
        )

    def add_review(self, session_id: str, day=None) -> dict:
        state = self.get(session_id)
        return self.reviews.add(
            Problem.model_validate(state["problem"]),
            session_id,
            day or today(),
            source=state.get("problem_source"),
        )

    def start_review(self, key: str) -> dict:
        item = self.reviews.get(key)
        return self.start(
            Problem.model_validate(item["problem"]),
            review_context={"problem_key": key, "version": item["version"]},
            problem_source={
                **item["problem_source"],
                "cache_status": "review_snapshot",
                "warning": "复习使用入队时的题面快照，未联网刷新",
            }
            if item.get("problem_source")
            else None,
        )

    def rate_review(self, session_id: str, rating: str, day=None) -> dict:
        return self.reviews.rate(self.get(session_id), rating, day or today())

    def sessions(self) -> list[tuple]:
        return self.connection.execute(
            "SELECT id, title, mode, created FROM app_sessions ORDER BY created DESC"
        ).fetchall()

    def get(self, session_id: str) -> dict:
        if not self.connection.execute(
            "SELECT 1 FROM app_sessions WHERE id = ?", (session_id,)
        ).fetchone():
            raise ValueError("会话不存在；请使用 sessions 查看完整会话 ID")
        return dict(self.graph.get_state(self.config(session_id)).values)

    def turn(self, session_id: str, text: str, *, code: str | None = None) -> dict:
        state = self.get(session_id)
        if state["mode"] != self.teacher.mode:
            raise ValueError("会话模式不匹配；演示与真实模型会话分开保存，请使用原模式恢复")
        text = text.strip()
        if not text or len(text) > 16000:
            raise ValueError("消息长度应为 1～16000 字符")
        action = "discuss"
        retry = text == "/retry"
        if retry:
            if not state.get("last_error"):
                raise ValueError("当前没有失败的教学请求需要重试")
            action = state["action"] if state["action"] in {"code", "answer"} else "discuss"
        elif text in ("/hint", "给一点提示", "再具体一点", "给我一点提示"):
            action = "hint"
        elif text in ("/answer", "直接给答案", "直接给我答案", "直接看答案"):
            action = "answer"
        elif text in ("/finish", "完成复盘"):
            action = "finish"
        elif text.startswith("/") and code is None:
            raise ValueError("未知会话命令；使用 /help 查看支持的命令")
        update = {
            "action": action,
            "history": state["history"]
            if retry
            else (state["history"] + [{"role": "user", "content": text}]),
            "request_id": uuid4().hex,
        }
        if code is not None:
            if not code.strip() or len(code.encode("utf-8")) > MAX_CODE_BYTES:
                raise ValueError("代码必须非空且不超过 128 KiB")
            verification = inspect_code(code)
            attempt = Attempt(
                attempt_id=uuid4().hex,
                code=code,
                code_hash=verification.code_hash,
                created_at=now(),
                verification=verification,
            )
            update.update(
                {
                    "action": "code",
                    "current_code": code,
                    "code_hash": attempt.code_hash,
                    "attempts": state["attempts"] + [attempt.model_dump()],
                    "verification": verification.model_dump(),
                    "completion_mode": "unfinished",
                    "solution_summary": None,
                    "explanation": state.get("explanation", "")
                    if state.get("code_hash") == attempt.code_hash
                    else "",
                }
            )
        return self.graph.invoke(update, self.config(session_id))

    def note(self, session_id: str, text: str) -> dict:
        state = self.get(session_id)
        if not text.strip() or len(text) > 16000:
            raise ValueError("反思应为 1～16000 字符")
        self.graph.update_state(self.config(session_id), {"reflection": text})
        return {**state, "reflection": text}

    def test(self, session_id: str) -> dict:
        """Explicit execution request: no model call and no extra attempt or hint escalation."""
        state = self.get(session_id)
        if not state.get("current_code"):
            raise ValueError("尚未保存代码，请先使用 /code 提交代码")
        verifier = self.verifier if self.verifier is not None else VerificationService()
        report = verifier.verify(Problem.model_validate(state["problem"]), state["current_code"])
        if report.code_hash != state["code_hash"]:
            raise ValueError("验证结果与当前代码版本不匹配，结果未写入")
        attempts = [dict(attempt) for attempt in state["attempts"]]
        target = attempts[-1]
        history = list(target.get("verification_history", []))
        if not history:
            history.append(target["verification"])
        history.append(report.model_dump())
        target.update(verification=report.model_dump(), verification_history=history)
        return self.graph.invoke(
            {
                "action": "test",
                "verification": report.model_dump(),
                "attempts": attempts,
                "history": state["history"] + [{"role": "user", "content": "/test"}],
                "request_id": uuid4().hex,
            },
            self.config(session_id),
        )

    def explain(self, session_id: str, text: str) -> dict:
        state = self.get(session_id)
        if not state.get("current_code"):
            raise ValueError("请先保存代码，再记录与代码版本对应的思路")
        if not text.strip() or len(text) > 16000:
            raise ValueError("思路应为 1～16000 字符")
        self.graph.update_state(self.config(session_id), {"explanation": text})
        return {**state, "explanation": text}

    def complete(self, session_id: str, mode: str) -> dict:
        state = self.get(session_id)
        if mode not in ("independent", "hint_assisted", "answer_assisted", "unfinished"):
            raise ValueError(
                "完成程度只能是 independent / hint_assisted / answer_assisted / unfinished"
            )
        if mode != "unfinished" and not state.get("current_code"):
            raise ValueError("请先保存代码")
        if state.get("answer_revealed") and mode not in ("answer_assisted", "unfinished"):
            raise ValueError("本会话已展示完整答案，不能标为独立或仅提示后完成")
        if mode == "independent" and state.get("max_hint_level_used", state["hint_level"]) > 0:
            raise ValueError("本会话已有提示记录，不能标为无提示独立完成")
        self.graph.update_state(self.config(session_id), {"completion_mode": mode})
        return {**state, "completion_mode": mode}

    def summarize(self, session_id: str) -> dict:
        state = self.get(session_id)
        if not state.get("current_code"):
            raise ValueError("请先保存代码")
        if self.teacher.mode != "model" or not hasattr(self.teacher, "summarize"):
            raise ValueError("当前模式没有模型整理能力；可以用 /explain 保存自己的思路")
        usage = list(state.get("usage", []))
        summary = None
        message = "模型整理失败"
        for _ in range(2):
            try:
                from algo_coach.models import SolutionSummary

                summary = SolutionSummary.model_validate(self.teacher.summarize(state))
                break
            except Exception as exc:
                _, message, retryable = teaching_failure(exc)
                if not retryable:
                    break
            finally:
                usage.append(
                    {"tokens": getattr(self.teacher, "last_usage", None), "cost": "unknown"}
                )
        update = {"usage": usage}
        if summary:
            update["solution_summary"] = {
                "code_hash": state["code_hash"],
                "content": summary.model_dump(),
            }
        self.graph.update_state(self.config(session_id), update)
        if summary is None:
            raise ValueError(message + "；原记录保留，没有生成虚假草稿")
        return self.get(session_id)

    def snapshot(self, session_id: str) -> str:
        state = self.get(session_id)
        return json.dumps(
            {
                "session_id": session_id,
                "mode": state["mode"],
                "problem": Problem.model_validate(state["problem"]).key,
                "stage": state["stage"],
                "hint_level": state["hint_level"],
                "answer_revealed": state["answer_revealed"],
                "attempts": len(state["attempts"]),
                "verification": state["verification"],
                "reflection": state["reflection"],
                "last_error": state.get("last_error"),
                "completion_mode": state.get("completion_mode", "unfinished"),
                "daily_selection": state.get("daily_selection"),
                "review_context": state.get("review_context"),
                "problem_source": state.get("problem_source"),
                "pending_archive_id": state.get("pending_archive_id"),
                "last_archive_id": state.get("last_archive_id"),
            },
            ensure_ascii=False,
            indent=2,
        )

    def preview_archive(self, session_id: str, root: Path) -> ArchivePlan:
        state = self.get(session_id)
        plan = plan_archive(state, root)
        self.operations.prepare(plan.plan_id, "archive", session_id, plan.model_dump())
        self.graph.update_state(self.config(session_id), {"pending_archive_id": plan.plan_id})
        return plan

    def archive_plan(self, session_id: str, operation_id: str | None = None) -> ArchivePlan:
        state = self.get(session_id)
        operation_id = operation_id or state.get("pending_archive_id")
        if not operation_id:
            raise ValueError("请先生成 /archive 预览")
        record = self.operations.get(operation_id, "archive", session_id)
        return ArchivePlan.model_validate(record["payload"])

    def apply_archive(self, session_id: str, operation_id: str | None = None) -> dict:
        plan = self.archive_plan(session_id, operation_id)
        if digest(json_text(learning_snapshot(self.get(session_id)))) != plan.snapshot_hash:
            raise ValueError("代码、验证或学习记录在预览后已变化，请重新生成 /archive 预览")
        self.operations.update(plan.plan_id, "writing")
        result = publish(plan)
        self.operations.update(plan.plan_id, "published")
        self.graph.update_state(self.config(session_id), {"last_archive_id": plan.plan_id})
        return result

    def published_archive(self, session_id: str) -> ArchivePlan:
        state = self.get(session_id)
        operation_id = state.get("last_archive_id")
        if not operation_id:
            raise ValueError("请先应用归档计划")
        plan = self.archive_plan(session_id, operation_id)
        if digest(json_text(learning_snapshot(state))) != plan.snapshot_hash:
            raise ValueError("学习记录已变化，请重新预览并归档后再执行 Git 操作")
        return plan

    def preview_commit(self, session_id: str) -> dict:
        plan = GitService(self.operations).prepare_commit(
            session_id, self.published_archive(session_id)
        )
        self.graph.update_state(
            self.config(session_id), {"pending_commit_id": plan["operation_id"]}
        )
        return plan

    @staticmethod
    def git_policy(*, push=False):
        policy = os.getenv("ALGOCOACH_GIT_POLICY", "export_only").strip()
        if policy not in ("export_only", "commit_on_request", "push_on_request"):
            raise ValueError("ALGOCOACH_GIT_POLICY 配置无效")
        if policy == "export_only" or (push and policy != "push_on_request"):
            raise ValueError("当前策略禁止该 Git 写操作；请先在 .env 选择相应的 on_request 策略")

    def apply_commit(self, session_id: str, operation_id: str | None = None) -> dict:
        self.git_policy()
        operation_id = operation_id or self.get(session_id).get("pending_commit_id")
        if not operation_id:
            raise ValueError("请先使用 /commit 生成提交预览")
        return GitService(self.operations).commit(
            session_id, operation_id, self.published_archive(session_id)
        )

    def preview_push(self, session_id: str) -> dict:
        archive = self.published_archive(session_id)
        plan = GitService(self.operations).prepare_push(session_id, Path(archive.root))
        self.graph.update_state(self.config(session_id), {"pending_push_id": plan["operation_id"]})
        return plan

    def apply_push(self, session_id: str, operation_id: str | None = None) -> dict:
        self.git_policy(push=True)
        self.published_archive(session_id)
        operation_id = operation_id or self.get(session_id).get("pending_push_id")
        if not operation_id:
            raise ValueError("请先使用 /push 生成远端推送预览")
        return GitService(self.operations).push(session_id, operation_id)
