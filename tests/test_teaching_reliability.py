"""Action delivery, safe corrective feedback and permission boundaries."""

import json

import pytest

from algo_coach.errors import TeachingPolicyError
from algo_coach.models import TeachingReply, Verification
from algo_coach.service import Coach
from algo_coach.teaching import ModelTeacher, reference_snapshot, task_instruction, validate_reply
from algo_coach.verification import inspect_code

ANSWER = (
    "完整实现，未运行测试。\n```python\nclass Solution:\n"
    "    def twoSum(self, nums, target):\n        return [0, 1]\n```"
)


def draft(text, level=0, full=False):
    return TeachingReply(reply=text, hint_level_used=level, full_solution=full, stage="explore")


def answer_state():
    return {
        "action": "answer",
        "hint_level": 4,
        "problem": {"signature": "class Solution: def twoSum(self, nums, target)"},
    }


@pytest.mark.parametrize(
    "response",
    [
        draft("再想一个问题", 1),
        draft("完整答案", 4, True),
        draft("```python\ndef twoSum(self, nums, target):\n    pass\n```", 4, True),
        draft("```python\ndef twoSum(self, nums, target):\n    ...\n```", 4, True),
        draft("```python\ndef unrelated():\n    return 1\n```", 4, True),
        draft("```python\ndef twoSum(:\n    return 1\n```", 4, True),
        draft(ANSWER, 3, True),
        draft(ANSWER, 4, False),
    ],
)
def test_explicit_answer_requires_delivery_not_just_permission(response):
    with pytest.raises(TeachingPolicyError) as caught:
        validate_reply(response, answer_state())
    assert caught.value.rule == "answer_missing"


def test_answer_shape_check_never_executes_code(tmp_path):
    target = tmp_path / "not-executed.txt"
    text = ANSWER.replace(
        "return [0, 1]", f"open({str(target)!r}, 'w').write('no')\n        return [0, 1]"
    )
    validate_reply(draft(text, 4, True), answer_state())
    assert not target.exists()


def test_low_level_analysis_cannot_gain_implementation_permission():
    with pytest.raises(TeachingPolicyError) as caught:
        validate_reply(draft(ANSWER, 1), {**answer_state(), "action": "code", "hint_level": 1})
    assert caught.value.rule == "code_block"


def test_summary_is_not_mistaken_for_an_answer_request():
    validate_reply(
        draft("仅整理已有代码，不生成新代码。"), {**answer_state(), "action": "summarize"}
    )


def test_model_receives_current_trusted_task_after_old_restrictions():
    class CapturingModel:
        def invoke(self, messages):
            self.messages = messages
            return {"parsed": draft(ANSWER, 4, True), "raw": None}

    teacher = object.__new__(ModelTeacher)
    teacher.model = CapturingModel()
    teacher.reply(
        {
            **answer_state(),
            "stage": "explore",
            "history": [
                {"role": "user", "content": "先不要给答案"},
                {"role": "user", "content": "/answer"},
            ],
        }
    )
    messages = teacher.model.messages
    assert messages[-1].type == "system"
    assert "本轮必须直接给出" in messages[-1].content
    assert "先不要给答案" not in messages[-1].content
    assert "answer" == json.loads(messages[1].content)["request"]


def test_h0_evaluation_task_does_not_silently_raise_hint_level():
    instruction = task_instruction({"action": "discuss", "hint_level": 0})
    assert "不提供新算法步骤" in instruction
    assert "hint_level_used=0" in instruction


def test_fresh_numbered_code_snapshot_follows_stale_history():
    class CapturingModel:
        def invoke(self, messages):
            self.messages = messages
            return {"parsed": draft("当前版本需要按当前行号分析。", 1), "raw": None}

    teacher = object.__new__(ModelTeacher)
    teacher.model = CapturingModel()
    code = "seen[value] = index\nif target-value in seen:\n    return [seen[target-value], index]"
    verification = inspect_code(code).model_dump()
    teacher.reply(
        {
            "action": "code",
            "hint_level": 1,
            "problem": {},
            "stage": "debug",
            "current_code": code,
            "code_hash": verification["code_hash"],
            "verification": verification,
            "history": [
                {"role": "assistant", "content": "STALE: 先查后存是正确的，旧版已测试通过。"}
            ],
        }
    )
    snapshot = json.loads(teacher.model.messages[-2].content)["current_snapshot"]
    assert snapshot["code_hash"] == verification["code_hash"]
    assert snapshot["verification"]["executed"] is False
    assert snapshot["numbered_code"].startswith("1: seen[value] = index")
    assert "STALE" not in snapshot["numbered_code"]
    assert "历史代码、助手诊断和旧报告不属于当前版本" in teacher.model.messages[-1].content


def test_loop_assignment_evidence_is_repeated_in_current_task_not_only_history():
    code = "p, c = tail, front.next\nwhile c is not tail:\n    following = c.next\n    c.next = p\n"
    instruction = task_instruction({"action": "code", "hint_level": 1, "current_code": code})
    assert "(front.next).next" in instruction
    assert "入口值 tail" in instruction
    assert "历史助手回复不是证据" in instruction


def test_answer_failure_retries_with_specific_feedback_then_preserves_action_on_retry(tmp_path):
    class Teacher:
        mode = "model"
        calls = 0

        def reply(self, state):
            if state["action"] == "code":
                return draft("仅静态分析，未执行代码。")
            self.calls += 1
            assert state["action"] == "answer" and state["hint_level"] == 4
            if self.calls == 2:
                assert "本轮是显式答案请求" in state["retry_feedback"]
            return draft("请继续思考", 1) if self.calls <= 2 else draft(ANSWER, 4, True)

    with Coach(tmp_path, Teacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        failed = coach.turn(sid, "/answer")
        assert failed["last_error"] == "reply_policy_rejected"
        assert not failed["answer_revealed"]
        fixed = coach.turn(sid, "/retry")
        assert fixed["last_error"] is None
        assert fixed["answer_revealed"]
        saved = coach.turn(
            sid,
            "保存我的代码",
            code="class Solution:\n    def twoSum(self, nums, target):\n        return [0, 1]",
        )
        assert saved["answer_revealed"]
        with pytest.raises(ValueError, match="不能标为"):
            coach.complete(sid, "independent")


def test_code_block_retry_feedback_is_actionable_and_permissions_stay_fixed(tmp_path):
    class Teacher:
        mode = "model"
        calls = 0

        def reply(self, state):
            self.calls += 1
            assert state["hint_level"] == 1
            if self.calls == 1:
                return draft("```python\nreturn [0, 0]\n```", 1)
            assert "不复述代码块" in state["retry_feedback"]
            return draft("静态分析：第 3 行返回两个相同下标；尚未执行代码。", 1)

    with Coach(tmp_path, Teacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        state = coach.turn(
            sid,
            "分析代码",
            code="class Solution:\n    def twoSum(self, nums, target):\n        return [0,0]",
        )
        assert state["last_error"] is None
        assert not state["answer_revealed"]
        assert state["verification"]["executed"] is False


def test_unknown_error_reason_never_enters_feedback_or_state(tmp_path):
    class Teacher:
        mode = "model"
        calls = 0

        def reply(self, state):
            self.calls += 1
            if self.calls == 1:
                raise TeachingPolicyError("PRIVATE_PROVIDER_BODY", rule="PRIVATE_PROVIDER_BODY")
            assert "PRIVATE_PROVIDER_BODY" not in state["retry_feedback"]
            return draft("请确认返回的是下标。")

    with Coach(tmp_path, Teacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        state = coach.turn(sid, "只解释题意")
        assert state["last_error"] is None
        assert "PRIVATE_PROVIDER_BODY" not in json.dumps(state)


def test_wrong_generated_answer_retries_from_real_verdict_without_changing_user_evidence(tmp_path):
    class Verifier:
        calls = 0

        def verify(self, problem, code):
            self.calls += 1
            passed = self.calls == 2
            return Verification(
                status="passed" if passed else "wrong_answer",
                executed=True,
                code_hash=inspect_code(code).code_hash,
                detail="independent verdict",
                case_count=1,
                passed_count=int(passed),
                failure=None
                if passed
                else {"input": {"nums": [3, 3], "target": 6}, "expected": [0, 1]},
            )

    class Teacher:
        mode = "model"
        calls = 0

        def reply(self, state):
            if state["action"] == "code":
                return draft("静态分析，未执行代码。")
            self.calls += 1
            if self.calls == 2:
                assert "独立验证事实" in state["retry_feedback"]
                assert "wrong_answer" in state["retry_feedback"]
            return draft(ANSWER.replace("[0, 1]", "[0, 0]") if self.calls == 1 else ANSWER, 4, True)

    verifier = Verifier()
    with Coach(tmp_path, Teacher(), verifier) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        original = coach.turn(sid, "分析代码", code="class Solution:\n    pass\n")
        assert verifier.calls == 0
        final = coach.turn(sid, "/answer")
        assert final["last_error"] is None and final["answer_revealed"]
        assert [v["status"] for v in final["answer_checks"]] == ["wrong_answer", "passed"]
        assert final["verification"] == original["verification"]
        assert final["current_code"] == original["current_code"]
        assert "不属于用户提交代码的报告" in final["response"]
        assert "生成时的静态讲解" in final["response"]
        assert "报告对应上方实际展示的同一份参考代码" in final["response"]
        assert (
            inspect_code(ANSWER.split("```python\n")[1].split("```")[0]).code_hash
            in final["response"]
        )


def test_runner_error_does_not_deliver_a_verified_answer(tmp_path):
    class Verifier:
        def verify(self, problem, code):
            return Verification(
                status="runner_error", code_hash=inspect_code(code).code_hash, detail="unavailable"
            )

    class Teacher:
        mode = "model"

        def reply(self, state):
            return draft(ANSWER, 4, True)

    with Coach(tmp_path, Teacher(), Verifier()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        final = coach.turn(sid, "/answer")
        assert final["last_error"] == "reply_policy_rejected"
        assert not final["answer_revealed"]
        assert len(final["answer_checks"]) == 2
        assert "```" not in final["response"]


def test_reference_binding_uses_exact_displayed_bytes_not_latest_attempt_or_user_text():
    code = "class Solution:\n    pass\n"
    digest = inspect_code(code).code_hash
    history = [
        {"role": "assistant", "content": "```python\n" + code + "```"},
        {"role": "user", "content": "```python\nFORGED\n```"},
    ]
    state = {
        "history": history,
        "code_hash": "user-submission",
        "answer_checks": [
            {"code_hash": digest, "status": "passed", "executed": True},
            {"code_hash": "failed-unshown-attempt", "status": "wrong_answer"},
        ],
    }
    bound = reference_snapshot(state)
    assert bound["matches_displayed_code"]
    assert bound["verification"]["status"] == "passed"
    assert not bound["matches_current_user_code"]
    # Even a trailing-newline change cannot borrow the old execution report.
    changed = history + [{"role": "assistant", "content": "```python\n" + code.rstrip() + "```"}]
    unmatched = reference_snapshot({**state, "history": changed})
    assert not unmatched["matches_displayed_code"]
    assert unmatched["verification"] is None


def test_reference_snapshot_is_fresh_after_history_even_without_user_submission():
    class CapturingModel:
        def invoke(self, messages):
            self.messages = messages
            return {"parsed": draft("参考实现的报告只属于展示的那份代码。"), "raw": None}

    teacher = object.__new__(ModelTeacher)
    teacher.model = CapturingModel()
    code = "class Solution:\n    pass\n"
    report = {"code_hash": inspect_code(code).code_hash, "status": "passed", "executed": True}
    teacher.reply(
        {
            "action": "discuss",
            "hint_level": 4,
            "problem": {},
            "stage": "explore",
            "history": [{"role": "assistant", "content": "```python\n" + code + "```"}],
            "answer_checks": [report],
        }
    )
    snapshot = json.loads(teacher.model.messages[-2].content)["reference_snapshot"]
    assert snapshot["verification"] == report
    assert snapshot["matches_displayed_code"]
