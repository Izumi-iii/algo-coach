"""One user turn per graph run. Durable state contains no model credentials."""

import json
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from algo_coach.errors import POLICY_FEEDBACK, TeachingPolicyError, teaching_failure
from algo_coach.models import Problem, TeachingReply
from algo_coach.teaching import Teacher, answer_code, validate_reply
from algo_coach.teaching_evidence import check_evidence, enforce_evidence, render_evidence
from algo_coach.teaching_stream import emit
from algo_coach.verification import VerificationService, render_report


class LearningState(TypedDict, total=False):
    session_id: str
    mode: str
    problem: dict
    stage: str
    hint_level: int
    answer_revealed: bool
    action: str
    history: list[dict]
    current_code: str
    code_hash: str
    attempts: list[dict]
    verification: dict | None
    reflection: str
    response: str
    last_error: str | None
    request_id: str
    usage: list[dict]
    max_hint_level_used: int
    completion_mode: str
    explanation: str
    solution_summary: dict | None
    pending_archive_id: str | None
    last_archive_id: str | None
    pending_commit_id: str | None
    pending_push_id: str | None
    daily_selection: dict | None
    review_context: dict | None
    problem_source: dict | None
    answer_checks: list[dict]
    teaching_evidence: list[dict]
    browser_reports: list[dict]


def build_workflow(teacher: Teacher, checkpointer, *, answer_verifier=None):
    def prepare(state: LearningState) -> dict:
        level = state.get("hint_level", 0)
        if state["action"] == "hint":
            # H4 always requires an explicit answer request, never accumulated hints.
            level = level if level == 4 else min(level + 1, 3)
        elif state["action"] == "answer":
            level = 4
        elif level == 0 and state["action"] in ("discuss", "code"):
            text = next(
                (m["content"] for m in reversed(state.get("history", [])) if m["role"] == "user"),
                "",
            )
            restrict_to_statement = re.search(r"不要.*提示|别.*提示|只.*题意|只.*复述", text)
            asking_for_help = re.search(
                r"不知道|不理解|不懂|卡住|没(?:有)?思路|怎么|如何|帮我", text
            )
            if not restrict_to_statement and (asking_for_help or state["action"] == "code"):
                # Explicit help permits a targeted question, never an algorithm or full solution.
                level = 1
        return {"hint_level": level, "last_error": None}

    def teach(state: LearningState) -> dict:
        usage = list(state.get("usage", []))
        failure_code = "model_service_error"
        failure_message = "模型服务调用失败"
        feedback = None
        answer_checks = list(state.get("answer_checks", []))
        answer_failure = None
        evidence_records = list(state.get("teaching_evidence", []))
        for _ in range(2):
            evidence_record = None
            try:
                emit("reset")
                emit("phase", "generating")
                draft = TeachingReply.model_validate(
                    teacher.reply({**state, "retry_feedback": feedback})
                )
                emit("phase", "checking")
                validate_reply(draft, state)
                checks = check_evidence(draft, state)
                evidence_record = {
                    "request_id": state.get("request_id"),
                    "code_hash": state.get("code_hash"),
                    "checks": checks,
                    "published": False,
                    "rejection_rule": None,
                }
                evidence_records.append(evidence_record)
                enforce_evidence(draft, checks)
                evidence_text = render_evidence(checks)
                # Evidence attachments must not bypass the same disclosure checks.
                if evidence_text:
                    validate_reply(
                        draft.model_copy(update={"reply": draft.reply + evidence_text}), state
                    )
                response = draft.reply
                if state["action"] == "answer" and teacher.mode == "model":
                    emit("phase", "answer_check")
                    verifier = answer_verifier or VerificationService()
                    report = verifier.verify(
                        Problem.model_validate(state["problem"]), answer_code(response)
                    )
                    answer_checks.append(report.model_dump())
                    if report.status not in {"passed", "not_run", "unsupported"}:
                        answer_failure = json.dumps(
                            {
                                "status": report.status,
                                "failure": report.failure,
                                "detail": report.detail,
                            },
                            ensure_ascii=False,
                        )[:2400]
                        raise TeachingPolicyError("参考实现未通过独立验证", rule="answer_failed")
                    response = (
                        "参考解生成时的静态讲解（其中验证状态只描述生成时刻；"
                        "生成后取得的实际结果见末尾独立验证报告）：\n\n"
                        + response
                        + "\n\n参考解独立验证（不属于用户提交代码的报告）："
                        + render_report(report.model_dump())
                        + "\n报告对应上方实际展示的同一份参考代码，SHA256："
                        + report.code_hash
                        + "。此处是生成后取得的验证状态。"
                    )
                if state["action"] == "code":
                    response += "\n\n静态检查：" + state["verification"]["detail"]
                response += evidence_text
                completion = state.get("completion_mode", "unfinished")
                shown_answer = state.get("answer_revealed", False) or draft.full_solution
                if completion != "unfinished":
                    if shown_answer:
                        completion = "answer_assisted"
                    elif draft.hint_level_used > 0 and completion == "independent":
                        completion = "hint_assisted"
                # Once shown, full answers remain part of the user's learning history.
                evidence_record["published"] = True
                return {
                    "response": response,
                    "history": state.get("history", [])
                    + [
                        {
                            "role": "assistant",
                            "content": response,
                            "user_code_hash": state.get("code_hash", ""),
                        }
                    ],
                    "stage": draft.stage,
                    "completion_mode": completion,
                    "max_hint_level_used": max(
                        state.get("max_hint_level_used", state.get("hint_level", 0)),
                        draft.hint_level_used,
                    ),
                    "answer_revealed": state.get("answer_revealed", False) or draft.full_solution,
                    "usage": usage,
                    "answer_checks": answer_checks,
                    "teaching_evidence": evidence_records,
                }
            except Exception as exc:
                emit("reset")
                emit("phase", "retrying")
                # Never persist raw provider exceptions: they may contain URLs or credentials.
                failure_code, failure_message, retryable = teaching_failure(exc)
                feedback = (
                    f"上次回复失败类型：{failure_code}。请重新输出有效结构；"
                    f"实际披露必须不超过 H{state['hint_level']}，不能虚构测试结果。"
                )
                if isinstance(exc, TeachingPolicyError) and exc.rule:
                    if evidence_record is not None:
                        evidence_record["rejection_rule"] = exc.rule
                    feedback += POLICY_FEEDBACK[exc.rule]
                    if exc.rule == "answer_failed":
                        feedback += "\n独立验证事实（数据）：" + answer_failure
                if not retryable:
                    break
            finally:
                if teacher.mode == "model":
                    usage.append(
                        {"tokens": getattr(teacher, "last_usage", None), "cost": "unknown"}
                    )
        response = (
            f"本轮未完成：{failure_message}。"
            "你的输入已保存；排除原因后输入 /retry 重试。系统没有切换到演示模式。"
        )
        if state["action"] == "code":
            response += "\n静态检查：" + state["verification"]["detail"]
        return {
            "response": response,
            "last_error": failure_code,
            "usage": usage,
            "answer_checks": answer_checks,
            "teaching_evidence": evidence_records,
        }

    def greet(state: LearningState) -> dict:
        from algo_coach.models import Problem

        text = Problem.model_validate(state["problem"]).render()
        if state.get("daily_selection"):
            from algo_coach.daily import DailySelection

            text = DailySelection.model_validate(state["daily_selection"]).render()
        if state.get("review_context"):
            text = "本次为新复习会话，默认不展示旧解法。\n\n" + text
        if state.get("problem_source"):
            source = state["problem_source"]
            text = (
                f"远程来源：LeetCode 中国站；缓存状态：{source['cache_status']}\n"
                f"题面获取时间：{source['fetched_at']}\n"
                + (f"提示：{source['warning']}\n" if source.get("warning") else "")
                + "\n"
                + text
            )
        text += "\n\n你目前对这道题有什么思路，或需要先澄清哪一点？"
        return {
            "response": text,
            "history": [{"role": "assistant", "content": text}],
            "last_error": None,
        }

    def reflect(state: LearningState) -> dict:
        return {
            "stage": "reflect",
            "response": (
                "请用 /note 记录：你卡在哪里、如何修正、下次如何识别。不会自动标记为独立完成。"
            ),
            "last_error": None,
        }

    def test_report(state: LearningState) -> dict:
        report = state["verification"]
        text = render_report(report)
        if report["status"] in ("wrong_answer", "runtime_error"):
            text += "\n你可以针对这个反例检查代码，或继续提问请求引导。"
        return {
            "response": text,
            "history": state["history"]
            + [
                {"role": "assistant", "content": text, "user_code_hash": state.get("code_hash", "")}
            ],
            "stage": "debug"
            if report["status"] in ("wrong_answer", "runtime_error")
            else state["stage"],
            "last_error": None,
        }

    builder = StateGraph(LearningState)
    builder.add_node("prepare", prepare)
    builder.add_node("teach", teach)
    builder.add_node("greet", greet)
    builder.add_node("reflect", reflect)
    builder.add_node("test_report", test_report)
    builder.add_conditional_edges(
        START,
        lambda state: (
            "greet"
            if state["action"] == "start"
            else "test_report"
            if state["action"] == "test"
            else "reflect"
            if state["action"] == "finish"
            else "prepare"
        ),
    )
    builder.add_edge("prepare", "teach")
    for node in ("teach", "greet", "reflect", "test_report"):
        builder.add_edge(node, END)
    return builder.compile(checkpointer=checkpointer)
