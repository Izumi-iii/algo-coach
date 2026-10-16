"""Synthetic research contracts, never external network calls."""

import json

import httpx
import pytest

from scripts.probe_leetcode import probe, summarize


def test_lookup_uses_exact_id_instead_of_first_fuzzy_hit():
    result = summarize(
        "cn",
        "lookup",
        {
            "problemsetQuestionList": {
                "questions": [
                    {"frontendQuestionId": "1287", "titleSlug": "contains-25"},
                    {"frontendQuestionId": "25", "titleSlug": "exact-match"},
                ]
            }
        },
        {"term": "25"},
    )
    assert result["unique_exact_match"]
    assert result["exact_matches"] == [{"frontendQuestionId": "25", "titleSlug": "exact-match"}]


@pytest.mark.parametrize(
    "rows",
    [
        [],
        [{"frontendQuestionId": "125"}],
        [
            {"frontendQuestionId": "25"},
            {"frontendQuestionId": "25"},
        ],
    ],
)
def test_missing_or_ambiguous_id_is_not_unique(rows):
    result = summarize(
        "cn", "lookup", {"problemsetQuestionList": {"questions": rows}}, {"term": "25"}
    )
    assert not result["unique_exact_match"]


def test_http_200_login_error_is_not_success():
    def respond(request):
        assert "cookie" not in request.headers
        return httpx.Response(
            200, json={"data": None, "errors": [{"message": "Please Register or Sign in"}]}
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = probe(client, "com", "lookup", {"term": "25"})
    assert result["http_status"] == 200
    assert result["outcome"] == "graphql_error" and result["stop_site"]


@pytest.mark.parametrize("status", [401, 403, 429])
def test_denial_stops_and_does_not_export_body(status):
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="SECRET RESPONSE BODY")
        )
    ) as client:
        result = probe(client, "cn", "daily", {})
    assert result["stop_site"] and result["outcome"] == "http_error"
    assert "SECRET RESPONSE BODY" not in json.dumps(result)


def test_redirect_does_not_trigger_another_request():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://example.invalid/"})

    with httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False) as client:
        result = probe(client, "cn", "daily", {})
    assert result["outcome"] == "redirect_not_followed" and len(calls) == 1


def test_response_is_bounded_and_not_retried(monkeypatch):
    import scripts.probe_leetcode as research

    monkeypatch.setattr(research, "MAX_RESPONSE_BYTES", 32)
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, content=b"x" * 33)

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        result = probe(client, "cn", "daily", {})
    assert result["outcome"] == "failed" and len(calls) == 1
    assert result["error_type"] == "ValueError"


def test_detail_reports_presence_without_exporting_statement():
    result = summarize(
        "cn",
        "detail",
        {
            "question": {
                "questionFrontendId": "25",
                "isPaidOnly": False,
                "content": "<p>PRIVATE STATEMENT Input Output Constraints</p><img src='x'>",
                "translatedContent": "<p>输入 输出 提示</p>",
                "codeSnippets": [
                    {
                        "langSlug": "python3",
                        "code": "class Solution:\n    def solve(self, x):\n        ",
                    }
                ],
                "metaData": '{"name":"solve"}',
            }
        },
        {},
    )
    assert result["content"]["has_constraints_marker"]
    assert result["content"]["images"] == 1
    assert result["python3_signature"] == "def solve(self, x):"
    assert "PRIVATE STATEMENT" not in json.dumps(result)


def test_paid_content_not_inspected():
    result = summarize(
        "cn",
        "detail",
        {
            "question": {
                "isPaidOnly": True,
                "content": "PAYWALLED CONTENT",
            }
        },
        {},
    )
    assert result["content_inspected"] is False
    assert "PAYWALLED CONTENT" not in json.dumps(result)


def test_missing_daily_data_is_not_success():
    with httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": {}}))
    ) as client:
        result = probe(client, "cn", "daily", {})
    assert result["outcome"] == "failed"
