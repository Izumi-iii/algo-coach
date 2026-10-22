"""Bounded checks of model-proposed data. Never execute submitted/model code."""

import hashlib
import json
import re

from algo_coach.adapters import canonical_adapter, problem_hash
from algo_coach.browser_bridge import current_browser_feedback
from algo_coach.errors import TeachingPolicyError
from algo_coach.judging import two_sum_pairs
from algo_coach.models import Problem, TeachingReply


def history_sources(state: dict) -> list[dict]:
    """IDs derive from the full history index and exact saved message, including binding.

    Old messages without a binding stay unknown; do not infer it from current code.
    Match the model's last-16-message context rather than allowing unseen citations.
    """
    history = state.get("history", [])
    sources = []
    for index in range(max(0, len(history) - 16), len(history)):
        message = history[index]
        payload = {
            "role": message["role"],
            "content": message["content"],
            "user_code_hash": message.get("user_code_hash"),
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        sources.append(
            {"message_id": f"m{index}-{digest}", **payload, "content": payload["content"][:16000]}
        )
    return sources


def _integers(values, low, high):
    return isinstance(values, list) and all(type(v) is int and low <= v <= high for v in values)


def _check_case(adapter, inputs, expected):
    # A deliberately small bounded subset for teaching examples, not all legal inputs.
    if adapter == "two_sum":
        nums, target = inputs.get("nums"), inputs.get("target")
        if (
            set(inputs) != {"nums", "target"}
            or not _integers(nums, -(10**9), 10**9)
            or not 2 <= len(nums) <= 10000
            or type(target) is not int
            or not -(10**9) <= target <= 10**9
        ):
            return "invalid_input", "输入必须是 2～10000 个范围内整数和整数 target。"
        if len(nums) > 200:
            return "unsupported", "超过本轮反例核验的 200 元素上限，未核验预期输出。"
        pairs = two_sum_pairs(nums, target)
        if len(pairs) != 1:
            return "invalid_input", "题面要求恰有一个有效下标对，当前输入不满足。"
        if sorted(expected) != pairs[0]:
            return "invalid_expected", "预期输出不是唯一有效的两个不同下标。"
    else:
        head, k = inputs.get("head"), inputs.get("k")
        if (
            set(inputs) != {"head", "k"}
            or not _integers(head, 0, 1000)
            or not 1 <= len(head) <= 5000
            or type(k) is not int
            or not 1 <= k <= len(head)
        ):
            return "invalid_input", "head 必须含 1～5000 个 0～1000 整数，且 1≤k≤长度。"
        if len(head) > 200:
            return "unsupported", "超过本轮反例核验的 200 元素上限，未核验预期输出。"
        result = []
        for start in range(0, len(head), k):
            group = head[start : start + k]
            result.extend(reversed(group) if len(group) == k else group)
        if expected != result:
            return "invalid_expected", "预期节点值序列不符合完整分组反转、尾组保留规则。"
    return "input_expected_checked", "输入约束与预期输出核验通过；未执行用户代码。"


def browser_failure_facts(state: dict) -> dict | None:
    """Check bounded report arithmetic; never run code or attest the browser source."""
    report = current_browser_feedback(state)
    if not report or report.get("status_code") not in {11, 15}:
        return None
    if canonical_adapter(Problem.model_validate(state["problem"])) != "two_sum":
        return None
    details = report.get("failure_details") or {}
    try:
        lines = details.get("last_testcase", "").strip().splitlines()
        if len(lines) != 2:
            return None
        nums, target = (json.loads(line) for line in lines)
        if (
            not _integers(nums, -(10**9), 10**9)
            or not 2 <= len(nums) <= 200
            or type(target) is not int
            or not -(10**9) <= target <= 10**9
        ):
            return None
        expected_text = details.get("expected_output")
        if expected_text:
            expected = json.loads(expected_text)
        elif report["status_code"] == 15:
            pairs = two_sum_pairs(nums, target)
            if len(pairs) != 1:
                return None
            expected = pairs[0]
        else:
            return None
        output_text = details.get("code_output")
        output = json.loads(output_text) if output_text else None
        if report["status_code"] == 11 and output is None:
            return None
        if not _integers(expected, 0, 199) or len(expected) != 2:
            return None
        inputs = {"nums": nums, "target": target}
        if _check_case("two_sum", inputs, expected)[0] != "input_expected_checked":
            return None
    except (ValueError, TypeError, AttributeError, RecursionError):
        return None
    in_bounds = _integers(output, 0, len(nums) - 1) and len(output) == 2
    values = [nums[index] for index in output] if in_bounds else None
    return {
        "inputs": inputs,
        "expected": expected,
        "expected_source": "browser_report" if expected_text else "application_oracle",
        "reported_output": output,
        "indices_in_bounds": in_bounds if output is not None else None,
        "distinct_indices": output[0] != output[1] if in_bounds else None,
        "selected_values": values,
        "selected_sum": sum(values) if values is not None else None,
        "sum_matches_target": sum(values) == target if values is not None else None,
        "scope": (
            "输入及reported_output仅取自当前浏览器报告；缺少的实际输出保持null。"
            "expected_source区分报告预期与应用独立计算的预期，后者不是执行记录。"
            "应用仅核对约束、预期和已报告下标的算术，未执行代码或独立认证平台。"
        ),
    }


def check_evidence(reply: TeachingReply, state: dict) -> dict:
    """Return application-owned records, with no inferred runtime or semantic verdict."""
    result = {
        "counterexamples": [],
        "history_citations": [],
        "browser_failure": browser_failure_facts(state),
    }
    sources = {s["message_id"]: s for s in history_sources(state)}
    for citation in reply.history_citations:
        source = sources.get(citation.message_id)
        valid = (
            source is not None
            and citation.quote.strip()
            and citation.quote in source["content"][:16000]
            and citation.user_code_hash == source["user_code_hash"]
        )
        result["history_citations"].append(
            {
                **citation.model_dump(),
                "source_role": source["role"] if source else None,
                "status": "source_matched" if valid else "invalid_citation",
                "scope": "仅核对原文和版本归属，不证明原诊断正确或当前引用语义恰当。",
            }
        )
    adapter, digest = None, None
    if reply.counterexamples:
        try:
            problem = Problem.model_validate(state.get("problem", {}))
            adapter, digest = canonical_adapter(problem), problem_hash(problem)
        except ValueError:
            pass
    for case in reply.counterexamples:
        status, detail = (
            _check_case(adapter, case.inputs, case.expected)
            if adapter
            else ("unsupported", "当前题面无已审核适配器，输入与预期无法独立核验。")
        )
        if case.kind == "constraint_example":
            if status == "invalid_input":
                status = "constraint_violation_checked"
                detail = "已核对该输入违反约束，不能作为合法反例；不核验此输入的预期输出。"
            elif status != "unsupported":
                status = "invalid_claim"
                detail = "该输入没有违反已检查的约束，不能标记为约束违反示例。"
        report = state.get("verification") or {}
        code = state.get("current_code", "")
        bound = (
            bool(code)
            and hashlib.sha256(code.encode("utf-8")).hexdigest() == state.get("code_hash")
            and report.get("code_hash") == state.get("code_hash")
            and report.get("problem_hash") == digest
        )
        failure = report.get("failure") or {}
        matched = (
            status == "input_expected_checked"
            and bound
            and report.get("executed") is True
            and report.get("status") == "wrong_answer"
            and failure.get("input") == case.inputs
            and (
                sorted(failure.get("expected", [])) == sorted(case.expected)
                if adapter == "two_sum"
                else failure.get("expected") == case.expected
            )
        )
        result["counterexamples"].append(
            {
                **case.model_dump(),
                "status": status,
                "detail": detail,
                "problem_hash": digest,
                "code_hash": state.get("code_hash"),
                "execution_evidence_matched": bool(matched),
                "scope": (
                    "匹配已有当前版本本地失败报告；没有新增执行。"
                    if matched
                    else "未证实当前代码在此输入上失败。"
                    + (
                        "链表预期仅核对值序列，不证明节点身份。"
                        if adapter == "reverse_k_group"
                        else ""
                    )
                ),
            }
        )
    return result


def enforce_evidence(reply: TeachingReply, checks: dict):
    if any(c["status"] == "invalid_citation" for c in checks["history_citations"]):
        raise TeachingPolicyError("历史引用原文或版本不匹配", rule="history_evidence")
    if any(c["status"].startswith("invalid_") for c in checks["counterexamples"]):
        raise TeachingPolicyError("反例输入或预期不成立", rule="counterexample_evidence")
    # Narrow recognizable direct quotations only. Free-text paraphrases are not proved.
    quote_claims = re.finditer(
        r"(?:你|我|我们|上轮|上一轮|之前|此前)[^。！？\n]{0,24}"
        r"(?:说过|说|判断|认为|提到)[：:]?[“\"]([^”\"\n]{1,500})[”\"]",
        reply.reply,
    )
    for quote_claim in quote_claims:
        if not any(quote_claim[1] in c["quote"] for c in checks["history_citations"]):
            raise TeachingPolicyError("直接历史引文缺少匹配来源", rule="history_evidence")
    # Restrict this guard to prose examples: implementation literals are not proposals.
    prose = re.sub(r"```[^\n]*\n[\s\S]*?```", "", reply.reply)
    for match in re.finditer(r"\b(nums|head)\s*[:=：]\s*(\[[^\]\n]{0,2000}\])", prose):
        try:
            values = json.loads(match[2])
        except ValueError:
            raise TeachingPolicyError(
                "样例字面量需要结构化核验", rule="counterexample_evidence"
            ) from None
        tail = re.match(r"\s*[，,;；]?\s*(target|k)\s*[:=：]\s*(-?\d+)", prose[match.end() :])
        registered = list(checks["counterexamples"])
        if checks.get("browser_failure"):
            registered.append(checks["browser_failure"])
        if not any(
            c["inputs"].get(match[1]) == values
            and (not tail or c["inputs"].get(tail[1]) == int(tail[2]))
            for c in registered
        ):
            raise TeachingPolicyError("具体样例缺少对应结构化数据", rule="counterexample_evidence")


def render_evidence(checks: dict) -> str:
    lines = []
    for index, case in enumerate(checks["counterexamples"], 1):
        lines.append(f"样例 {index}：{case['detail']} {case['scope']}")
        if case["status"] == "input_expected_checked":
            lines.append(
                "输入："
                + json.dumps(case["inputs"], ensure_ascii=False)
                + "；预期："
                + json.dumps(case["expected"])
            )
    for citation in checks["history_citations"]:
        binding = citation["user_code_hash"] or "未知或当时无代码"
        lines.append(
            f"历史来源 {citation['message_id']}（角色：{citation['source_role']}；"
            f"代码版本：{binding}）："
            f"「{citation['quote']}」；{citation['scope']}"
        )
    return "\n\n教学证据检查（应用核验）：\n" + "\n".join(lines) if lines else ""
