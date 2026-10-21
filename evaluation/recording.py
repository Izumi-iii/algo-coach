"""Instrument the real structured LangChain calls without altering application decisions."""

import time
from datetime import UTC, datetime

from algo_coach.errors import POLICY_FEEDBACK, teaching_failure
from algo_coach.teaching import validate_reply
from algo_coach.teaching_evidence import check_evidence, enforce_evidence, render_evidence


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, calls, seconds):
        self.max_calls = calls
        self.deadline = time.monotonic() + seconds
        self.calls = 0
        self.exhausted = False

    def available(self, reserve=1):
        return self.calls + reserve <= self.max_calls and time.monotonic() < self.deadline

    def acquire(self):
        if not self.available():
            self.exhausted = True
            raise BudgetExceeded("评估调用次数或时间预算已用尽")
        self.calls += 1


def visible_result(result):
    """Whitelist visible tool outputs; exclude reasoning, headers and provider extras."""
    if not isinstance(result, dict):
        return {"shape": type(result).__name__}
    raw = result.get("raw")
    parsed = result.get("parsed")
    output = {
        "parsed": parsed.model_dump() if hasattr(parsed, "model_dump") else None,
        "parsing_error": type(result["parsing_error"]).__name__
        if result.get("parsing_error")
        else None,
        "usage": getattr(raw, "usage_metadata", None),
        "tool_calls": [],
    }
    allowed = {
        "TeachingReply": {
            "reply",
            "hint_level_used",
            "stage",
            "full_solution",
            "counterexamples",
            "history_citations",
        },
        "SolutionSummary": {"approach", "correctness", "complexity", "limitations"},
    }
    for call in getattr(raw, "tool_calls", []) or []:
        name, args = call.get("name"), call.get("args")
        if name in allowed and isinstance(args, dict):
            output["tool_calls"].append(
                {
                    "name": name,
                    "args": {
                        key: (value[:24000] if isinstance(value, str) else value)
                        for key, value in args.items()
                        if key in allowed[name]
                    },
                }
            )
    content = getattr(raw, "content", None)
    output["visible_text"] = content[:24000] if isinstance(content, str) else None
    meta = getattr(raw, "response_metadata", {}) or {}
    output["response_metadata"] = {
        key: meta[key]
        for key in ("model_name", "model", "finish_reason", "model_provider")
        if key in meta
    }
    return output


class CapturingModel:
    def __init__(self, wrapped, recorder, phase):
        self.wrapped, self.recorder, self.phase = wrapped, recorder, phase

    def invoke(self, messages):
        self.recorder.budget.acquire()
        event = {
            "call_index": self.recorder.budget.calls,
            **self.recorder.context,
            "phase": self.phase,
            "started_at": datetime.now(UTC).isoformat(),
            "messages": [
                {"role": message.type, "content": message.content} for message in messages
            ],
            "adapter_error": None,
            "policy_result": None,
        }
        started = time.monotonic()
        try:
            result = self.wrapped.invoke(messages)
            event["output"] = visible_result(result)
            return result
        except Exception as exc:
            event["adapter_error"] = teaching_failure(exc)[0]
            raise
        finally:
            event["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.recorder.events.append(event)
            self.recorder.emit(event)


class RecordingTeacher:
    mode = "model"

    def __init__(self, delegate, budget, emit):
        if delegate.mode != "model":
            raise ValueError("真实模型评估不接受演示模型")
        self.delegate, self.budget, self.emit = delegate, budget, emit
        self.context = {}
        self.events = []
        delegate.model = CapturingModel(delegate.model, self, "teaching")
        delegate.summary_model = CapturingModel(delegate.summary_model, self, "summary")

    @property
    def last_usage(self):
        return self.delegate.last_usage

    def _call(self, method, state):
        self.context.update(
            {
                "allowed_hint_level": state["hint_level"],
                "action": state.get("action"),
                "code_hash": state.get("code_hash"),
                "verification": state.get("verification"),
            }
        )
        start = len(self.events)
        try:
            result = method(state)
            if method.__name__ == "reply":
                try:
                    validate_reply(result, state)
                    checks = check_evidence(result, state)
                    if len(self.events) > start:
                        self.events[-1]["teaching_evidence"] = checks
                    enforce_evidence(result, checks)
                    attachment = render_evidence(checks)
                    if attachment:
                        validate_reply(
                            result.model_copy(update={"reply": result.reply + attachment}), state
                        )
                    policy = "accepted_by_application_filter"
                except Exception as exc:
                    policy = teaching_failure(exc)[0]
                    rule = getattr(exc, "rule", None)
                    if len(self.events) > start and rule in POLICY_FEEDBACK:
                        self.events[-1]["policy_rule"] = rule
                if len(self.events) > start:
                    self.events[-1]["policy_result"] = policy
            return result
        except Exception as exc:
            if len(self.events) > start:
                self.events[-1]["adapter_error"] = teaching_failure(exc)[0]
            raise
        finally:
            # Append a final event version after adapter parsing and policy observation.
            if len(self.events) > start:
                self.emit({"event_update": self.events[-1]})

    def reply(self, state):
        return self._call(self.delegate.reply, state)

    def summarize(self, state):
        return self._call(self.delegate.summarize, state)
