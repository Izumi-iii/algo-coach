"""Signature explanations must not open a bypass for solution implementations."""

import pytest

from algo_coach.errors import TeachingPolicyError
from algo_coach.models import TeachingReply
from algo_coach.service import Coach
from algo_coach.teaching import validate_reply

SIGNATURE = "def twoSum(self, nums: list[int], target: int) -> list[int]"


def state(signature="class Solution: " + SIGNATURE):
    return {"hint_level": 0, "problem": {"signature": signature}}


def reply(text):
    return TeachingReply(reply=text, hint_level_used=0, full_solution=False, stage="understand")


@pytest.mark.parametrize(
    "text",
    [
        SIGNATURE,
        "对应到签名：" + SIGNATURE + "，返回两个下标。",
        "`" + SIGNATURE + "`",
        "`class Solution: " + SIGNATURE + "`",
    ],
)
def test_exact_declared_bare_signature_is_not_an_implementation(text):
    validate_reply(reply(text), state())


@pytest.mark.parametrize(
    "text",
    [
        SIGNATURE + ": return [0, 1]",
        SIGNATURE + " :\n    return [0, 1]",
        SIGNATURE + ":\n    pass",
        "class Solution:",
        "class Solution: " + SIGNATURE + ": return [0, 1]",
        "class Solution:\n    " + SIGNATURE + ":\n        return [0, 1]",
        "```python\n" + SIGNATURE + "\n```",
        "def twoSum(self, nums, target): return [0, 1]",
        SIGNATURE + "\ndef helper(): return [0, 1]",
        SIGNATURE + "[0]",
        SIGNATURE + "Other",
    ],
)
def test_implementations_and_unrecognized_declarations_stay_blocked(text):
    with pytest.raises(TeachingPolicyError):
        validate_reply(reply(text), state())


@pytest.mark.parametrize(
    "signature",
    ["", "unrelated", SIGNATURE + ":\n    return [0, 1]", SIGNATURE + ": pass; return [0, 1]"],
)
def test_missing_or_body_containing_problem_signature_cannot_grant_exception(signature):
    with pytest.raises(TeachingPolicyError):
        validate_reply(reply(SIGNATURE), state(signature))


def test_signature_explanation_is_delivered_without_retry_or_answer_upgrade(tmp_path):
    class SignatureTeacher:
        mode = "model"
        calls = 0

        def reply(self, current):
            self.calls += 1
            return reply("对应到签名：" + SIGNATURE + "，即输入 nums、target，输出两个下标。")

    teacher = SignatureTeacher()
    with Coach(tmp_path, teacher) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        current = coach.turn(sid, "只澄清题意，不要提示。")
        assert current["last_error"] is None
        assert current["hint_level"] == 0
        assert not current["answer_revealed"]
        assert teacher.calls == 1
