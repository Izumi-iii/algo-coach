"""Static checks and host-side verdicts; dynamic execution uses Docker exclusively."""

import ast
import hashlib
import json
import re

from algo_coach.adapters import (
    ADAPTER_VERSION,
    SEED,
    SUITE_VERSION,
    build_cases,
    canonical_adapter,
    problem_hash,
    validate_output,
)
from algo_coach.models import Problem, Verification
from algo_coach.runner import DockerRunner


def inspect_code(code: str) -> Verification:
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    try:
        # Preserve the original code and hash, while accepting a UTF-8 BOM from Windows files.
        ast.parse(code.removeprefix("\ufeff"))
    except (SyntaxError, ValueError, RecursionError) as exc:
        if isinstance(exc, SyntaxError):
            detail = f"第 {exc.lineno} 行：{exc.msg}"
        else:
            detail = "代码无法进行语法解析，可能包含非法字符或嵌套过深。"
        return Verification(status="syntax_error", code_hash=code_hash, detail=detail)
    return Verification(
        status="not_run",
        code_hash=code_hash,
        detail="语法解析成功；未执行代码、未运行测试，不能据此判断算法正确。",
    )


class VerificationService:
    def __init__(self, runner=None):
        self.runner = runner if runner is not None else DockerRunner.from_env()

    def verify(self, problem: Problem, code: str) -> Verification:
        initial = inspect_code(code)
        if initial.status == "syntax_error":
            return initial
        adapter = canonical_adapter(problem)
        base = {
            "code_hash": initial.code_hash,
            "problem_hash": problem_hash(problem),
            "suite_version": SUITE_VERSION,
            "adapter_version": ADAPTER_VERSION,
            "random_seed": SEED,
        }
        if adapter is None:
            return Verification(
                **base,
                status="unsupported",
                detail=(
                    "仅内置摘要和已审核的第 1、25 题远程快照有可信适配器；此题或题面版本尚未支持"
                ),
            )
        cases = build_cases(adapter)
        result = self.runner.run(
            code, adapter, [{"case_id": case.case_id, "inputs": case.inputs} for case in cases]
        )
        base["runner_image"] = result.image_id
        if result.status != "ok":
            return Verification(**base, status=result.status, detail=result.detail)
        records = result.records
        if not isinstance(records, list) or len(records) != len(cases):
            return Verification(
                **base, status="runner_error", detail="执行结果缺失或用例数量不匹配"
            )
        for case, record in zip(cases, records, strict=True):
            if (
                not isinstance(record, dict)
                or record.get("case_id") != case.case_id
                or record.get("status") not in ("ok", "runtime_error", "resource_limit")
                or (record["status"] == "ok" and not isinstance(record.get("output"), dict))
            ):
                return Verification(
                    **base, status="runner_error", detail="执行协议字段不合法或用例身份不匹配"
                )
        passed = 0
        failure = None
        failure_status = "wrong_answer"
        for case, record in zip(cases, records, strict=True):
            status = record["status"]
            if status == "ok":
                correct, reason = validate_output(adapter, case, record["output"])
            else:
                correct = False
                reason = "执行时发生异常" if status == "runtime_error" else "执行时超过内存限制"
            if correct:
                passed += 1
            elif failure is None:
                failure_status = "wrong_answer" if status == "ok" else status
                failure = {
                    "case_id": case.case_id,
                    "input": case.inputs,
                    "expected": case.expected,
                    "actual": record.get("output"),
                    "reason": reason,
                }
                error = record.get("error")
                if status == "runtime_error" and isinstance(error, dict):
                    name, line = error.get("type"), error.get("line")
                    if isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", name):
                        failure["error_type"] = name
                    if type(line) is int and 0 < line <= len(code.splitlines()):
                        failure["code_line"] = line
        detail = f"本地测试 {passed}/{len(cases)} 通过；不代表 LeetCode 隐藏测试或平台提交结果。"
        return Verification(
            **base,
            status=failure_status if failure else "passed",
            executed=True,
            case_count=len(cases),
            passed_count=passed,
            failure=failure,
            detail=detail,
        )


def render_report(report: dict) -> str:
    heading = "本地验证" if report.get("executed") else "检查结果"
    text = f"{heading} [{report['status']}]：{report['detail']}"
    if report.get("failure"):
        text += "\n首个失败用例：\n" + json.dumps(report["failure"], ensure_ascii=False, indent=2)
    return text
