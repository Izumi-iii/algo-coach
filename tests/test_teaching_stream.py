import json
from types import SimpleNamespace

import httpx
import pytest
from langchain.chat_models import init_chat_model

import algo_coach.teaching as teaching
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher, ModelTeacher
from algo_coach.teaching_stream import (
    _string_prefix,
    consume_stream,
    paragraph_candidate,
    stream_to,
)


def arguments(text, **fields):
    return json.dumps(
        {
            "hint_level_used": 0,
            "stage": "understand",
            "full_solution": False,
            "counterexamples": [],
            "history_citations": [],
            **fields,
            "reply": text,
        },
        ensure_ascii=False,
    )


def chunks(raw):
    cut = raw.index("\\n\\n") + 4
    return raw[:cut], raw[cut:]


@pytest.mark.parametrize("text", ['中文与"引号"。\n\n第二段', "反斜线\\与emoji😀。\n\n后续"])
def test_partial_json_withholds_incomplete_escapes_and_decodes_paragraph(text):
    raw = arguments(text)
    first, _ = chunks(raw)
    assert paragraph_candidate(first).reply == text.split("\n\n")[0]
    assert _string_prefix('"\\uD83D') == ""
    assert _string_prefix('"\\uD83D\\uDE00') == "😀"
    assert _string_prefix('"abc\\') == "abc"
    assert paragraph_candidate('{"reply":"提前文本。\\n\\n') is None


def test_checked_paragraph_arrives_before_provider_finishes_and_rejected_text_never_does(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        state = coach.start(coach.problems.resolve("1"))
        state = {**state, "action": "discuss", "hint_level": 0}
        events = []

        class Stream:
            def __init__(self, text, allow_preview):
                self.raw, self.allow_preview = arguments(text), allow_preview

            def stream(self, messages):
                first, tail = chunks(self.raw)
                yield SimpleNamespace(
                    tool_call_chunks=[{"index": 0, "name": "StreamTeachingReply", "args": first}]
                )
                assert bool([v for k, v in events if k == "preview"]) is self.allow_preview
                yield SimpleNamespace(tool_call_chunks=[{"index": 0, "args": tail}])

        with stream_to(lambda kind, value: events.append((kind, value))):
            reply = consume_stream(
                Stream("先确认题意。\n\n不必急着写代码。", True), [], state, lambda _: None
            )
        assert reply.reply.endswith("不必急着写代码。")
        events.clear()
        with stream_to(lambda kind, value: events.append((kind, value))):
            consume_stream(
                Stream("class Solution:\n    pass\n\n不应预览。", False), [], state, lambda _: None
            )
        assert not any(kind == "preview" for kind, _ in events)


def test_answer_is_buffered_until_full_validation(tmp_path):
    with Coach(tmp_path, DemoTeacher()) as coach:
        state = {**coach.start(coach.problems.resolve("1")), "action": "answer", "hint_level": 4}
        raw = arguments("解答第一段。\n\n后续解答。", hint_level_used=4, full_solution=True)
        model = SimpleNamespace(
            stream=lambda _: iter(
                [
                    SimpleNamespace(
                        tool_call_chunks=[{"index": 0, "name": "StreamTeachingReply", "args": raw}]
                    )
                ]
            )
        )
        events = []
        with stream_to(lambda k, v: events.append((k, v))):
            consume_stream(model, [], state, lambda _: None)
        assert not any(kind == "preview" for kind, _ in events)


def test_native_langchain_stream_adapter_commits_reply_and_accounts_usage(tmp_path, monkeypatch):
    raw = arguments("先确认返回的是位置。\n\n输入可以有相同数值。")
    parts = chunks(raw)
    requests, events = [], []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        packets = []
        for index, part in enumerate(parts):
            call = {"index": 0, "function": {"arguments": part}}
            if index == 0:
                call.update(id="stream-test", type="function")
                call["function"]["name"] = "StreamTeachingReply"
            packets.append(
                {
                    "id": "test",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "configured-test-model",
                    "choices": [
                        {"index": 0, "delta": {"tool_calls": [call]}, "finish_reason": None}
                    ],
                }
            )
        packets.append(
            {
                "id": "test",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "configured-test-model",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
                "usage": {"prompt_tokens": 42, "completion_tokens": 12, "total_tokens": 54},
            }
        )
        data = "".join("data: " + json.dumps(p) + "\n\n" for p in packets) + "data: [DONE]\n\n"
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, content=data.encode()
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
            sid = coach.start(coach.problems.resolve("1"))["session_id"]
            with stream_to(lambda k, v: events.append((k, v))):
                state = coach.turn(sid, "请澄清题意。")
            assert state["last_error"] is None
            assert state["history"][-1]["content"] == json.loads(raw)["reply"]
            assert state["usage"][-1]["tokens"]["total_tokens"] == 54
    assert len(requests) == 1 and requests[0]["stream"] is True
    assert any(kind == "preview" for kind, _ in events)
