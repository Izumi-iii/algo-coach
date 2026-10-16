"""Exercise the actual LangChain provider adapter without network access or API charges."""

import json

import httpx
import pytest
from langchain.chat_models import init_chat_model

import algo_coach.teaching as teaching
from algo_coach.service import Coach
from algo_coach.teaching import ModelTeacher


@pytest.mark.parametrize("deepseek", [False, True])
def test_real_adapter_parses_tool_output_and_accounts_for_usage(tmp_path, monkeypatch, deepseek):
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-test",
                "object": "chat.completion",
                "created": 1,
                "model": "configured-test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-test",
                                    "type": "function",
                                    "function": {
                                        "name": "TeachingReply",
                                        "arguments": json.dumps(
                                            {
                                                "reply": "末尾不足 k 个节点时，需要怎样处理？",
                                                "hint_level_used": 1,
                                                "stage": "explore",
                                                "full_solution": False,
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
            },
        )

    monkeypatch.setenv("ALGOCOACH_MODEL", "configured-test-model")
    monkeypatch.setenv("ALGOCOACH_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ALGOCOACH_MODEL_PROVIDER", "openai")
    monkeypatch.setenv(
        "ALGOCOACH_BASE_URL",
        "https://api.deepseek.com" if deepseek else "https://provider.invalid/v1",
    )
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:

        def factory(*args, **kwargs):
            return init_chat_model(*args, http_client=client, **kwargs)

        monkeypatch.setattr(teaching, "init_chat_model", factory)
        with Coach(tmp_path, ModelTeacher()) as coach:
            session = coach.start(coach.problems.resolve("25"))["session_id"]
            state = coach.turn(session, "/hint")
            assert state["last_error"] is None
            assert state["response"] == "末尾不足 k 个节点时，需要怎样处理？"
            assert state["usage"][-1]["tokens"]["total_tokens"] == 54
            assert state["usage"][-1]["cost"] == "unknown"
            assert "dummy-test-key" not in json.dumps(state)
    assert len(requests) == 1
    assert requests[0]["tools"][0]["function"]["name"] == "TeachingReply"
    if deepseek:
        assert requests[0]["thinking"] == {"type": "disabled"}
    else:
        assert "thinking" not in requests[0]
    context = json.loads(requests[0]["messages"][1]["content"])
    assert context["hint_level"] == 1
    assert "DEMO_ANSWERS" not in json.dumps(requests)


def test_provider_failure_clears_previous_usage(monkeypatch):
    class FailingModel:
        def invoke(self, messages):
            raise RuntimeError("failed")

    # Construct only the adapter wrapper: this test specifically exercises stale metadata.
    teacher = object.__new__(ModelTeacher)
    teacher.model = FailingModel()
    teacher.last_usage = {"total_tokens": 123}
    with pytest.raises(RuntimeError):
        teacher.reply({"problem": {}, "hint_level": 0, "stage": "understand", "action": "discuss"})
    assert teacher.last_usage is None


def test_real_adapter_summarizes_only_saved_code_as_versioned_draft(tmp_path, monkeypatch):
    requests = []
    draft = {
        "approach": "遍历数组，记录已访问的数值及下标。",
        "correctness": "访问当前位置时，只查找之前的位置，因此两个下标不同。",
        "complexity": "期望时间 O(n)，额外空间 O(n)。",
        "limitations": "此处仅为静态分析，请结合实际测试结果审阅。",
    }

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        tool_name = body["tools"][0]["function"]["name"]
        arguments = (
            draft
            if tool_name == "SolutionSummary"
            else {
                "reply": "你准备如何验证两个下标不同？",
                "hint_level_used": 0,
                "stage": "implement",
                "full_solution": False,
            }
        )
        return httpx.Response(
            200,
            json={
                "id": "summary-test",
                "object": "chat.completion",
                "created": 1,
                "model": "configured-test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "summary-call",
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            },
        )

    monkeypatch.setenv("ALGOCOACH_MODEL", "configured-test-model")
    monkeypatch.setenv("ALGOCOACH_API_KEY", "dummy-test-key")
    monkeypatch.setenv("ALGOCOACH_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("ALGOCOACH_BASE_URL", "https://provider.invalid/v1")
    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        monkeypatch.setattr(
            teaching,
            "init_chat_model",
            lambda *a, **kw: init_chat_model(*a, http_client=client, **kw),
        )
        with Coach(tmp_path, ModelTeacher()) as coach:
            session = coach.start(coach.problems.resolve("1"))["session_id"]
            code = "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 1]\n"
            coach.turn(session, "检查代码", code=code)
            coach.explain(session, "我先记录已经访问过的位置。")
            state = coach.summarize(session)
            assert state["solution_summary"] == {"code_hash": state["code_hash"], "content": draft}
            assert state["usage"][-1]["tokens"]["total_tokens"] == 70
            assert not state["answer_revealed"]
    assert len(requests) == 2
    assert requests[1]["tools"][0]["function"]["name"] == "SolutionSummary"
    context = json.loads(requests[1]["messages"][1]["content"])
    assert context["current_code"] == code
    assert context["explanation"] == "我先记录已经访问过的位置。"
    assert "dummy-test-key" not in json.dumps(context)
