"""Web-only incremental tool arguments. Only checked, complete paragraphs are previewed."""

import json
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import Field

from algo_coach.errors import TeachingOutputError
from algo_coach.models import (
    CounterexampleProposal,
    HistoryCitation,
    Stage,
    StrictModel,
    TeachingReply,
)

_sink = ContextVar("teaching_stream_sink", default=None)


class StreamTeachingReply(StrictModel):
    """Emit all metadata first, then the reply string, so paragraphs can be checked."""

    hint_level_used: int = Field(ge=0, le=4)
    stage: Stage
    full_solution: bool
    counterexamples: list[CounterexampleProposal] = Field(default_factory=list, max_length=2)
    history_citations: list[HistoryCitation] = Field(default_factory=list, max_length=3)
    reply: str = Field(min_length=1, max_length=16000)


@contextmanager
def stream_to(sink):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def streaming_enabled():
    return _sink.get() is not None


def emit(kind, value=""):
    callback = _sink.get()
    if callback:
        callback(kind, value)


def _string_prefix(raw):
    """Decode a JSON string prefix, withholding unfinished escapes and surrogate pairs."""
    if not raw.startswith('"'):
        return None
    output, i = [], 1
    escapes = {
        '"': '"',
        "\\": "\\",
        "/": "/",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "b": "\b",
        "f": "\f",
    }
    while i < len(raw):
        char = raw[i]
        if char == '"':
            break
        if char != "\\":
            if ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF:
                return None
            output.append(char)
            i += 1
            continue
        if i + 1 >= len(raw):
            break
        escape = raw[i + 1]
        if escape != "u":
            if escape not in escapes:
                return None
            output.append(escapes[escape])
            i += 2
            continue
        if i + 6 > len(raw):
            break
        try:
            value = int(raw[i + 2 : i + 6], 16)
            if 0xD800 <= value <= 0xDBFF:
                if i + 12 > len(raw):
                    break
                if raw[i + 6 : i + 8] != "\\u":
                    return None
                low = int(raw[i + 8 : i + 12], 16)
                if not 0xDC00 <= low <= 0xDFFF:
                    return None
                output.append(chr(0x10000 + ((value - 0xD800) << 10) + low - 0xDC00))
                i += 12
            elif 0xDC00 <= value <= 0xDFFF:
                return None
            else:
                output.append(chr(value))
                i += 6
        except ValueError:
            return None
    return "".join(output)


def paragraph_candidate(raw):
    """No preview until every policy/evidence field precedes the reply and is complete."""
    decoder = json.JSONDecoder()
    metadata, i = {}, 1
    if not raw.lstrip().startswith("{"):
        return None
    raw = raw.lstrip()
    try:
        while i < len(raw):
            while i < len(raw) and raw[i] in " \r\n\t,":
                i += 1
            key, i = decoder.raw_decode(raw, i)
            if not isinstance(key, str) or key in metadata:
                return None
            while i < len(raw) and raw[i].isspace():
                i += 1
            if i == len(raw) or raw[i] != ":":
                return None
            i += 1
            while i < len(raw) and raw[i].isspace():
                i += 1
            if key == "reply":
                if set(metadata) != {
                    "hint_level_used",
                    "stage",
                    "full_solution",
                    "counterexamples",
                    "history_citations",
                }:
                    return None
                text = _string_prefix(raw[i:])
                if text is None or "\n\n" not in text:
                    return None
                text = text[: text.rfind("\n\n")].rstrip()
                return TeachingReply.model_validate({**metadata, "reply": text}) if text else None
            metadata[key], i = decoder.raw_decode(raw, i)
    except (ValueError, TypeError, RecursionError):
        return None
    return None


def consume_stream(model, messages, state, usage):
    # Import here to keep teaching's construction and validators on one policy path.
    from algo_coach.teaching import validate_reply
    from algo_coach.teaching_evidence import check_evidence, enforce_evidence, render_evidence

    raw, shown, name = "", "", ""
    emit("phase", "generating")
    for chunk in model.stream(messages):
        if getattr(chunk, "usage_metadata", None):
            usage(chunk.usage_metadata)
        for call in getattr(chunk, "tool_call_chunks", []):
            if call.get("index", 0) != 0:
                raise TeachingOutputError("流式回复包含额外工具调用")
            if call.get("name"):
                name = call["name"]
            raw += call.get("args") or ""
            if len(raw) > 131072:
                raise TeachingOutputError("流式回复超过大小限制")
        if name != "StreamTeachingReply" or state.get("hint_level", 0) >= 3:
            continue
        candidate = paragraph_candidate(raw)
        if not candidate or candidate.full_solution or len(candidate.reply) <= len(shown):
            continue
        try:
            validate_reply(candidate, state)
            checks = check_evidence(candidate, state)
            enforce_evidence(candidate, checks)
            validate_reply(
                candidate.model_copy(update={"reply": candidate.reply + render_evidence(checks)}),
                state,
            )
        except ValueError:
            # A prefix may not yet satisfy the complete contract. Never publish it unchecked.
            continue
        shown = candidate.reply
        emit("preview", shown)
    if name != "StreamTeachingReply":
        raise TeachingOutputError("模型未返回流式教学结构")
    return TeachingReply.model_validate_json(raw)
