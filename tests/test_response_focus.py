import json

import pytest

from algo_coach.errors import TeachingPolicyError
from algo_coach.models import TeachingReply
from algo_coach.service import Coach
from algo_coach.teaching import (
    ModelTeacher,
    direct_check_requested,
    submission_context,
    task_instruction,
    validate_reply,
)
from algo_coach.verification import inspect_code
from evaluation.fixtures import CODE


def state_for(*codes):
    attempts = [{"code": c, "code_hash": inspect_code(c).code_hash} for c in codes]
    return {
        "action": "code",
        "hint_level": 1,
        "stage": "debug",
        "problem": {},
        "attempts": attempts,
        "current_code": codes[-1],
        "code_hash": attempts[-1]["code_hash"],
    }


def test_changed_versions_are_compared_without_transferring_old_diagnosis():
    state = state_for(CODE["two_sum_self"], CODE["two_sum_ok"])
    context = submission_context(state)
    assert context["relation"] == "changed_code"
    assert context["previous_mapping_scans"][0]["order"] == "write_before_lookup"
    assert context["current_mapping_scans"][0]["order"] == "lookup_before_write"
    assert "换版不证明旧诊断有误" in context["scope"]
    instruction = task_instruction(state)
    assert "版本改变、诊断各自绑定" in instruction
    assert "旧版确有此问题" not in instruction
    reverse = task_instruction(state_for(CODE["two_sum_ok"], CODE["two_sum_self"]))
    assert "不能预设哪一版有问题" in reverse
    assert "不能仅因换版就说上一轮看错" in instruction


def test_identical_resubmission_and_text_only_change_do_not_prove_a_fix():
    code = CODE["two_sum_ok"]
    assert submission_context(state_for(code))["relation"] == "first_submission"
    assert submission_context(state_for(code, code))["relation"] == "same_code"
    context = submission_context(state_for(code, "# new comment\n" + code))
    assert context["relation"] == "changed_code"
    assert (
        context["previous_mapping_scans"][0]["order"]
        == context["current_mapping_scans"][0]["order"]
    )
    assert "哈希变化不证明行为变化" in context["scope"]


@pytest.mark.parametrize("damage", ["previous_hash", "current_hash", "missing_code", "no_attempts"])
def test_corrupted_or_legacy_state_does_not_create_a_version_story(damage):
    state = state_for(CODE["two_sum_self"], CODE["two_sum_ok"])
    if damage == "previous_hash":
        state["attempts"][0]["code_hash"] = "tampered"
    elif damage == "current_hash":
        state["code_hash"] = "tampered"
    elif damage == "missing_code":
        del state["attempts"][0]["code"]
    else:
        del state["attempts"]
    assert submission_context(state)["relation"] == "unknown"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("直接回答：命中后还会写入吗？", True),
        ("请直接给出结论，不要反问。", True),
        ("不必提问，只核对下标。", True),
        ("不要直接回答，请给一个引导问题。", False),
        ("别直接给结论，我想自己推演。", False),
        ("如何想到这个解法？", False),
    ],
)
def test_latest_explicit_style_preference_does_not_become_a_hint_permission(text, expected):
    state = {
        **state_for(CODE["two_sum_ok"]),
        "action": "discuss",
        "history": [
            {"role": "user", "content": "不要追问"},
            {"role": "assistant", "content": "直接回答"},
            {"role": "user", "content": text},
        ],
    }
    assert direct_check_requested(state) is expected
    instruction = task_instruction(state)
    assert "披露上限 H1" in instruction
    assert ("用户明确要求直接核对" in instruction) is expected
    with pytest.raises(TeachingPolicyError):
        validate_reply(
            TeachingReply(
                reply="```python\npass\n```", hint_level_used=1, stage="debug", full_solution=False
            ),
            state,
        )


def test_fresh_version_comparison_follows_stale_assistant_history():
    class Capture:
        def invoke(self, messages):
            self.messages = messages
            return {
                "parsed": TeachingReply(
                    reply="旧版先写后查；当前版先查后写。",
                    hint_level_used=1,
                    stage="debug",
                    full_solution=False,
                ),
                "raw": None,
            }

    teacher = object.__new__(ModelTeacher)
    teacher.model = Capture()
    state = state_for(CODE["two_sum_self"], CODE["two_sum_ok"])
    state["history"] = [{"role": "assistant", "content": "STALE 上一轮诊断错了"}]
    teacher.reply(state)
    context = json.loads(teacher.model.messages[-2].content)["submission_context"]
    assert context["relation"] == "changed_code"
    assert context["current_code_hash"] == state["code_hash"]
    assert "STALE" not in json.dumps(context)


def test_saved_replies_keep_their_original_submission_binding_across_version_changes(tmp_path):
    class Teacher:
        mode = "demo"

        def reply(self, state):
            return TeachingReply(
                reply="仅静态核对本次代码。", hint_level_used=1, stage="debug", full_solution=False
            )

    with Coach(tmp_path, Teacher()) as coach:
        sid = coach.start(coach.problems.resolve("1"))["session_id"]
        old = coach.turn(sid, "旧版", code=CODE["two_sum_self"])
        current = coach.turn(sid, "新版", code=CODE["two_sum_ok"])
        bound = [m for m in current["history"] if m.get("user_code_hash")]
        assert [m["user_code_hash"] for m in bound] == [old["code_hash"], current["code_hash"]]
        assert old["code_hash"] != current["code_hash"]


def test_model_history_labels_legacy_as_unknown_without_changing_saved_messages():
    class Capture:
        def invoke(self, messages):
            self.messages = messages
            return {
                "parsed": TeachingReply(
                    reply="以当前版本为准。", hint_level_used=1, stage="debug", full_solution=False
                ),
                "raw": None,
            }

    teacher = object.__new__(ModelTeacher)
    teacher.model = Capture()
    state = state_for(CODE["two_sum_self"], CODE["two_sum_ok"])
    old_hash = state["attempts"][0]["code_hash"]
    state["history"] = [
        {"role": "assistant", "content": "LEGACY"},
        {"role": "assistant", "content": "OLD", "user_code_hash": old_hash},
    ]
    teacher.reply(state)
    messages = [m.content for m in teacher.model.messages if m.type == "ai"]
    assert "未知或当时尚未提交代码" in messages[0]
    assert old_hash in messages[1]
    assert "user_code_hash" not in state["history"][0]
    assert state["history"][1]["content"] == "OLD"
