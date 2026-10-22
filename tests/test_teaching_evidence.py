import copy

import pytest
from pydantic import ValidationError

from algo_coach.adapters import problem_hash
from algo_coach.errors import TeachingPolicyError
from algo_coach.models import Problem, TeachingReply
from algo_coach.problems import LocalProblems
from algo_coach.service import Coach
from algo_coach.teaching import ModelTeacher
from algo_coach.teaching_evidence import (
    check_evidence,
    enforce_evidence,
    history_sources,
    render_evidence,
)
from algo_coach.verification import inspect_code


@pytest.fixture
def state(tmp_path):
    code = "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 0]\n"
    problem = LocalProblems(tmp_path).resolve("1")
    return {
        "problem": problem.model_dump(),
        "current_code": code,
        "code_hash": inspect_code(code).code_hash,
        "verification": None,
        "history": [{"role": "assistant", "content": "旧版先写后查。", "user_code_hash": "a" * 64}],
    }


def reply(**fields):
    return TeachingReply(
        reply="这只是静态分析，尚未执行代码。",
        hint_level_used=1,
        stage="debug",
        full_solution=False,
        **fields,
    )


@pytest.mark.parametrize(
    "inputs,expected,status",
    [
        ({"nums": [9, 9], "target": 18}, [1, 0], "input_expected_checked"),
        ({"nums": [0, -3, 3], "target": 0}, [1, 2], "input_expected_checked"),
        ({"nums": [1, 1, 2], "target": 3}, [0, 2], "invalid_input"),
        ({"nums": [1, 2], "target": 6}, [0, 1], "invalid_input"),
        ({"nums": [3], "target": 6}, [0, 0], "invalid_input"),
        ({"nums": [3, 3], "target": 6}, [0, 0], "invalid_expected"),
        ({"nums": [3, 3], "target": 6}, [0, 2], "invalid_expected"),
        ({"nums": [True, 3], "target": 4}, [0, 1], "invalid_input"),
        ({"nums": [3, 3], "target": True}, [0, 1], "invalid_input"),
        ({"nums": [3, 3], "target": 6, "code": "print(1)"}, [0, 1], "invalid_input"),
        ({"nums": [0] * 201, "target": 1}, [], "unsupported"),
    ],
)
def test_two_sum_proposal_constraints_and_expected(state, inputs, expected, status):
    draft = reply(counterexamples=[{"inputs": inputs, "expected": expected}])
    checks = check_evidence(draft, state)
    assert checks["counterexamples"][0]["status"] == status
    assert not checks["counterexamples"][0]["execution_evidence_matched"]
    if status.startswith("invalid_"):
        with pytest.raises(TeachingPolicyError, match="反例"):
            enforce_evidence(draft, checks)
    else:
        enforce_evidence(draft, checks)


@pytest.mark.parametrize(
    "head,k,expected,status",
    [
        ([1, 2, 3, 4, 5], 2, [2, 1, 4, 3, 5], "input_expected_checked"),
        ([1, 1, 2], 2, [1, 1, 2], "input_expected_checked"),
        ([1, 2, 3], 2, [2, 1, 3], "input_expected_checked"),
        ([1, 2, 3], 1, [1, 2, 3], "input_expected_checked"),
        ([1, 2, 3], 2, [3, 2, 1], "invalid_expected"),
        ([], 1, [], "invalid_input"),
        ([1, 2], 3, [2, 1], "invalid_input"),
        ([1, -1], 1, [1, -1], "invalid_input"),
    ],
)
def test_linked_list_expected_is_values_only(tmp_path, state, head, k, expected, status):
    state["problem"] = LocalProblems(tmp_path).resolve("25").model_dump()
    checks = check_evidence(
        reply(counterexamples=[{"inputs": {"head": head, "k": k}, "expected": expected}]), state
    )
    assert checks["counterexamples"][0]["status"] == status
    assert "不证明节点身份" in checks["counterexamples"][0]["scope"]


def test_changed_problem_snapshot_is_not_trusted_by_number(state):
    state["problem"]["statement"] += " changed"
    checks = check_evidence(
        reply(counterexamples=[{"inputs": {"nums": [3, 3], "target": 6}, "expected": [0, 1]}]),
        state,
    )
    assert checks["counterexamples"][0]["status"] == "unsupported"


def test_constraint_discussion_is_not_confused_with_a_legal_counterexample(state):
    draft = reply(
        counterexamples=[
            {
                "kind": "constraint_example",
                "inputs": {"nums": [1, 1, 2], "target": 3},
                "expected": [],
            }
        ]
    )
    checks = check_evidence(draft, state)
    enforce_evidence(draft, checks)
    assert checks["counterexamples"][0]["status"] == "constraint_violation_checked"
    assert "不核验此输入的预期" in render_evidence(checks)
    legal = reply(
        counterexamples=[
            {
                "kind": "constraint_example",
                "inputs": {"nums": [3, 3], "target": 6},
                "expected": [0, 1],
            }
        ]
    )
    with pytest.raises(TeachingPolicyError):
        enforce_evidence(legal, check_evidence(legal, state))


@pytest.mark.parametrize("damage", [None, "code", "problem", "executed", "input", "expected"])
def test_only_exact_existing_execution_can_confirm_failure(state, damage):
    state["verification"] = {
        "status": "wrong_answer",
        "executed": True,
        "code_hash": state["code_hash"],
        "problem_hash": problem_hash(Problem.model_validate(state["problem"])),
        "failure": {"input": {"nums": [3, 3], "target": 6}, "expected": [0, 1]},
    }
    if damage in {"code", "problem"}:
        state["verification"][damage + "_hash"] = "stale"
    elif damage == "executed":
        state["verification"]["executed"] = False
    elif damage == "input":
        state["verification"]["failure"]["input"]["target"] = 7
    elif damage == "expected":
        state["verification"]["failure"]["expected"] = [0, 0]
    checks = check_evidence(
        reply(counterexamples=[{"inputs": {"nums": [3, 3], "target": 6}, "expected": [1, 0]}]),
        state,
    )
    assert checks["counterexamples"][0]["execution_evidence_matched"] is (damage is None)


def test_history_citation_requires_exact_source_text_and_binding(state):
    source = history_sources(state)[0]
    citation = {"message_id": source["message_id"], "quote": "先写后查", "user_code_hash": "a" * 64}
    draft = reply(history_citations=[citation])
    checks = check_evidence(draft, state)
    enforce_evidence(draft, checks)
    assert checks["history_citations"][0]["status"] == "source_matched"
    assert "先写后查" in render_evidence(checks)
    for key, value in [
        ("message_id", "missing"),
        ("quote", "上一轮误判"),
        ("user_code_hash", state["code_hash"]),
    ]:
        broken = reply(history_citations=[{**citation, key: value}])
        with pytest.raises(TeachingPolicyError, match="历史"):
            enforce_evidence(broken, check_evidence(broken, state))
    assert state["history"][0]["content"] == "旧版先写后查。"


def test_sources_are_stable_on_append_and_unknown_binding_is_not_inferred(state):
    before = history_sources(state)
    state["history"].append({"role": "assistant", "content": "旧记录"})
    assert history_sources(state)[0] == before[0]
    assert history_sources(state)[-1]["user_code_hash"] is None
    state["history"] += [{"role": "user", "content": "next"}] * 20
    assert before[0]["message_id"] not in {s["message_id"] for s in history_sources(state)}


def test_bold_only_quotation_and_empty_no_code_binding_are_not_false_rejections(state):
    state["history"] = [
        {
            "role": "assistant",
            "content": "所以**能排序当且仅当**：序列是循环移位。",
            "user_code_hash": "",
        }
    ]
    source = history_sources(state)[0]
    assert source["user_code_hash"] is None
    assert state["history"][0]["user_code_hash"] == ""
    citation = {
        "message_id": source["message_id"],
        "quote": "能排序当且仅当：序列是循环移位。",
        "user_code_hash": None,
    }
    checks = check_evidence(reply(history_citations=[citation]), state)
    enforce_evidence(reply(history_citations=[citation]), checks)
    assert checks["history_citations"][0]["match_kind"] == "paired_bold_removed"
    for fields in [
        {"quote": "能排序当且仅当：序列不是循环移位。"},
        {"quote": "能排序当且仅当:序列是循环移位。"},
        {"user_code_hash": "a" * 64},
        {"message_id": "missing"},
    ]:
        bad = reply(history_citations=[{**citation, **fields}])
        with pytest.raises(TeachingPolicyError):
            enforce_evidence(bad, check_evidence(bad, state))


def test_authorized_full_answer_survives_display_only_history_quote(tmp_path):
    from algo_coach.models import Verification

    class Teacher:
        mode = "model"

        def reply(self, state):
            assert state["action"] == "answer" and state["hint_level"] == 4
            source = history_sources(state)[0]
            return TeachingReply(
                reply="使用双循环核对两个不同位置，最坏时间O(n²)，额外空间O(1)。\n"
                "```python\nclass Solution:\n    def twoSum(self, nums, target):\n"
                "        for i in range(len(nums)):\n"
                "            for j in range(i+1, len(nums)):\n"
                "                if nums[i]+nums[j] == target:\n"
                "                    return [i,j]\n```",
                hint_level_used=4,
                stage="debug",
                full_solution=True,
                history_citations=[
                    {
                        "message_id": source["message_id"],
                        "quote": "两个不同位置",
                        "user_code_hash": None,
                    }
                ],
            )

    class NoExecution:
        def verify(self, problem, code):
            return Verification(
                status="not_run", code_hash=inspect_code(code).code_hash, detail="Fixture: 未执行。"
            )

    with Coach(tmp_path, Teacher(), verifier=NoExecution()) as coach:
        initial = coach.start(coach.problems.resolve("1"))
        sid = initial["session_id"]
        coach.graph.update_state(
            coach.config(sid),
            {
                "history": [
                    {
                        "role": "assistant",
                        "content": "核对**两个不同位置**。",
                        "user_code_hash": "",
                    }
                ]
            },
        )
        result = coach.turn(sid, "/answer")
        assert result["last_error"] is None and result["answer_revealed"] is True
        assert result.get("current_code", "") == ""
        assert result["teaching_evidence"][-1]["published"] is True
        assert result["answer_checks"][-1]["executed"] is False


@pytest.mark.parametrize(
    "content,quote",
    [
        ("指数 `a ** b ** c`。", "a  b  c"),
        ("```python\nx = a**b**c\n```", "x = abc"),
        ("a**b**c", "abc"),
    ],
)
def test_quote_format_normalization_never_rewrites_code_or_exponentiation(state, content, quote):
    state["history"] = [{"role": "assistant", "content": content}]
    source = history_sources(state)[0]
    draft = reply(history_citations=[{"message_id": source["message_id"], "quote": quote}])
    with pytest.raises(TeachingPolicyError):
        enforce_evidence(draft, check_evidence(draft, state))


def test_explicit_fabricated_quote_and_unstructured_example_are_rejected(state):
    for text in ["我之前说过：“已修复”。", "例如 nums=[1,1,2]，target=3，是反例。"]:
        draft = reply().model_copy(update={"reply": text})
        with pytest.raises(TeachingPolicyError):
            enforce_evidence(draft, check_evidence(draft, state))


def test_expected_cannot_coerce_boolean_or_string():
    for value in [True, "1"]:
        with pytest.raises(ValidationError):
            reply(
                counterexamples=[{"inputs": {"nums": [3, 3], "target": 6}, "expected": [0, value]}]
            )


def test_workflow_corrects_rejected_example_and_persists_checks_without_execution(tmp_path):
    class Teacher:
        mode = "model"
        calls = []

        def reply(self, state):
            self.calls.append(copy.deepcopy(state))
            expected = [0, 0] if len(self.calls) == 1 else [0, 1]
            return reply(
                counterexamples=[{"inputs": {"nums": [3, 3], "target": 6}, "expected": expected}]
            )

    teacher = Teacher()
    with Coach(tmp_path, teacher) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        result = coach.turn(
            sid,
            "帮我检查",
            code="class Solution:\n    def twoSum(self, nums, target):\n        return [0, 0]\n",
        )
        assert result["last_error"] is None
        assert len(teacher.calls) == 2
        assert "counterexamples" in teacher.calls[1]["retry_feedback"]
        assert len(result["teaching_evidence"]) == 2
        assert result["teaching_evidence"][0]["rejection_rule"] == "counterexample_evidence"
        assert not result["teaching_evidence"][0]["published"]
        assert result["teaching_evidence"][1]["published"]
        assert result["verification"]["status"] == "not_run"
        assert not result["verification"]["executed"]
        assert "未证实当前代码" in result["response"]
        assert "教学证据检查" in result["history"][-1]["content"]
    with Coach(tmp_path, teacher) as coach:
        assert len(coach.get(sid)["teaching_evidence"]) == 2


def test_model_prompt_has_citable_sources_and_structured_proposals(state):
    class Capture:
        def invoke(self, messages):
            self.messages = messages
            return {"parsed": reply(), "raw": None}

    teacher = object.__new__(ModelTeacher)
    teacher.model = Capture()
    state.update(hint_level=1, stage="debug", action="discuss")
    teacher.reply(state)
    content = "\n".join(m.content for m in teacher.model.messages)
    assert history_sources(state)[0]["message_id"] in content
    assert "counterexamples" in content and "history_citations" in content


def test_second_fabricated_quote_is_not_hidden_by_first_valid_quote(state):
    source = history_sources(state)[0]
    draft = reply(
        history_citations=[
            {"message_id": source["message_id"], "quote": "先写后查", "user_code_hash": "a" * 64}
        ]
    ).model_copy(update={"reply": "我之前说：“先写后查”。你之前说：“已修复”。"})
    with pytest.raises(TeachingPolicyError):
        enforce_evidence(draft, check_evidence(draft, state))


def test_prose_input_must_match_structured_target_but_code_literals_are_not_examples(state):
    draft = reply(
        counterexamples=[{"inputs": {"nums": [3, 3], "target": 6}, "expected": [0, 1]}]
    ).model_copy(update={"reply": "例如 nums=[3,3], target=7。"})
    with pytest.raises(TeachingPolicyError):
        enforce_evidence(draft, check_evidence(draft, state))
    code = reply().model_copy(update={"reply": "```python\nnums = []\n```"})
    enforce_evidence(code, check_evidence(code, state))


def test_evidence_citation_does_not_bypass_hint_permissions(tmp_path):
    class Teacher:
        mode = "model"

        def reply(self, state):
            source = history_sources(state)[-1]
            return reply(
                history_citations=[
                    {
                        "message_id": source["message_id"],
                        "quote": "class Solution: pass",
                        "user_code_hash": None,
                    }
                ]
            )

    with Coach(tmp_path, Teacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        result = coach.turn(sid, "帮我分析 class Solution: pass")
        assert result["last_error"] == "reply_policy_rejected"
        assert "class Solution" not in result["response"]
        assert len(result["teaching_evidence"]) == 2
        assert all(not r["published"] for r in result["teaching_evidence"])


def test_twice_invalid_counterexample_is_not_published(tmp_path):
    class Teacher:
        mode = "model"

        def reply(self, state):
            return reply(
                counterexamples=[{"inputs": {"nums": [1, 1, 2], "target": 3}, "expected": [0, 2]}]
            )

    with Coach(tmp_path, Teacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        result = coach.turn(sid, "帮我理解")
        assert result["last_error"] == "reply_policy_rejected"
        assert "教学证据检查" not in result["response"]
        assert len(result["history"]) == 2  # greeting and user, no rejected assistant draft
        assert len(result["teaching_evidence"]) == 2
