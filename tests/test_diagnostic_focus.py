import pytest

from algo_coach.errors import TeachingPolicyError
from algo_coach.models import TeachingReply
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher, diagnostic_focus, task_instruction, validate_reply
from evaluation.browser_fixtures import apply_browser_fixture, browser_fixtures


@pytest.fixture
def state(tmp_path):
    fixture = browser_fixtures()["fixture-wa"]
    with Coach(tmp_path, DemoTeacher()) as coach:
        sid = coach.start(fixture.problem)["session_id"]
        result = apply_browser_fixture(coach, sid, "fixture-wa")
        yield {
            **result,
            "action": "discuss",
            "hint_level": 1,
            "history": [{"role": "user", "content": "请帮我定位这个错误。"}],
        }


def draft(text):
    return TeachingReply(reply=text, hint_level_used=1, stage="debug", full_solution=False)


def test_one_diagnostic_question_with_targeted_retry(state):
    validate_reply(draft("浏览器回传报告显示重复下标。这两个位置不同吗？"), state)
    with pytest.raises(TeachingPolicyError) as rejected:
        validate_reply(draft("这两个位置不同吗？准备怎么搜索？"), state)
    assert rejected.value.rule == "question_budget"
    # Literal source/error punctuation is not an extra follow-up.
    validate_reply(draft("错误文本包含 `?`。位置不同吗？"), state)


def test_direct_answer_has_no_followup_and_wider_request_is_respected(state):
    state["history"][-1]["content"] = "直接回答，不要追问。"
    assert diagnostic_focus(state)["question_limit"] == 0
    with pytest.raises(TeachingPolicyError):
        validate_reply(draft("不同吗？"), state)
    state["history"][-1]["content"] = "请列出所有问题，给我三个问题思考。"
    assert diagnostic_focus(state) is None
    validate_reply(draft("位置相同吗？返回值是什么？"), state)
    state["history"][-1]["content"] = "请解释返回值类型。"
    assert diagnostic_focus(state) is None
    state["history"][-1]["content"] = "只定位报错，不要解释返回值类型。"
    assert diagnostic_focus(state) is not None


def test_focus_does_not_leak_unrelated_algorithm_reasoning(state):
    assert "两数之和的约束推理" not in task_instruction(state)
    state["history"][-1]["content"] = "全面分析这份代码。"
    assert "两数之和的约束推理" in task_instruction(state)
    state["action"] = "answer"
    assert diagnostic_focus(state) is None


def test_stale_report_cannot_trigger_current_diagnostic_policy(state):
    state["current_code"] += "\n"
    assert diagnostic_focus(state) is None
