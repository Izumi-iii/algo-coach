"""Independent state/artifact assertions and human review contracts."""

import hashlib
import json
from typing import Literal

from pydantic import Field

from algo_coach.adapters import problem_hash
from algo_coach.models import Problem, StrictModel
from evaluation.cases import fingerprint

REVIEW_FIELDS = (
    "semantic_leakage",
    "correctness",
    "counterexample_validity",
    "pedagogical_progress",
    "factual_fidelity",
)


class HumanReview(StrictModel):
    run_id: str
    case_id: str
    step_index: int = Field(ge=0)
    step_hash: str
    reviewer: str = Field(min_length=1)
    semantic_leakage: Literal["pass", "fail", "na"]
    correctness: Literal["pass", "fail", "na"]
    counterexample_validity: Literal["pass", "fail", "na"]
    pedagogical_progress: Literal["pass", "fail", "na"]
    factual_fidelity: Literal["pass", "fail", "na"]
    evidence_notes: str = Field(min_length=1)


def facts(state):
    return {
        "answer_checks": state.get("answer_checks", []),
        "stage": state["stage"],
        "history_hash": fingerprint(state.get("history", [])),
        "pending_archive_id": state.get("pending_archive_id"),
        "last_archive_id": state.get("last_archive_id"),
        "problem_key": Problem.model_validate(state["problem"]).key,
        "problem_hash": problem_hash(Problem.model_validate(state["problem"])),
        "hint_level": state["hint_level"],
        "answer_revealed": state["answer_revealed"],
        "max_hint_level_used": state.get("max_hint_level_used", 0),
        "code_hash": state.get("code_hash", ""),
        "current_code": state.get("current_code", ""),
        "verification": state.get("verification"),
        "reflection": state.get("reflection", ""),
        "completion_mode": state.get("completion_mode", "unfinished"),
        "attempts": len(state.get("attempts", [])),
        "explanation": state.get("explanation", ""),
        "solution_summary": state.get("solution_summary"),
        "last_error": state.get("last_error"),
    }


def check_step(step, before, after, *, error=None, artifact=None):
    failures = []
    expected = step.expect
    if expected.expected_error:
        if not error or expected.expected_error not in error:
            failures.append("expected_error_missing")
    elif error:
        failures.append("unexpected_action_error")
    if step.needs_model() and after.get("last_error"):
        failures.append("teaching_request_failed:" + after["last_error"])
    if expected.hint_cap is not None and after["hint_level"] > expected.hint_cap:
        failures.append("hint_cap_exceeded")
    for key in ("answer_revealed", "reflection", "completion_mode"):
        if getattr(expected, key) is not None and after[key] != getattr(expected, key):
            failures.append(key + "_mismatch")
    report = after.get("verification") or {}
    if expected.verification_status and report.get("status") != expected.verification_status:
        failures.append("verification_status_mismatch")
    if after.get("current_code"):
        actual = hashlib.sha256(after["current_code"].encode()).hexdigest()
        if actual != after["code_hash"] or report.get("code_hash") != actual:
            failures.append("code_evidence_hash_mismatch")
        if report.get("status") == "passed" and (
            not report.get("executed")
            or not report.get("case_count")
            or report.get("passed_count") != report["case_count"]
        ):
            failures.append("passed_without_complete_execution")
    if step.operation == "resume" and before != after:
        failures.append("recovery_state_changed")
    if step.operation == "code" and report.get("executed"):
        failures.append("submission_executed_implicitly")
    if step.operation == "summarize" and not error:
        summary = after.get("solution_summary") or {}
        if summary.get("code_hash") != after.get("code_hash"):
            failures.append("summary_wrong_code_version")
    if step.operation == "archive" and not error:
        folder, plan = artifact
        if (folder / "solution.py").read_bytes() != after["current_code"].encode():
            failures.append("archive_code_changed")
        metadata = json.loads((folder / "metadata.json").read_text("utf-8"))
        if (
            metadata["code_hash"] != after["code_hash"]
            or metadata["completion_mode"] != after["completion_mode"]
        ):
            failures.append("archive_metadata_mismatch")
        if after["reflection"] and after["reflection"] not in (folder / "reflection.md").read_text(
            "utf-8"
        ):
            failures.append("archive_reflection_changed")
        for name, text in plan.contents.items():
            if (folder / name).read_bytes() != text.encode():
                failures.append("archive_manifest_content_mismatch:" + name)
    return failures


def review_template(run_id, case, step):
    return {
        "run_id": run_id,
        "case_id": case.id,
        "step_index": step["step_index"],
        "step_hash": step["step_hash"],
        "reviewer": "",
        **{key: None for key in REVIEW_FIELDS},
        "evidence_notes": "",
    }
