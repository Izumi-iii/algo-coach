"""Frozen synthetic provider contracts. No network, LeetCode text, or model charges."""

import copy
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime

import httpx
import pytest

import algo_coach.remote as remote
from algo_coach.adapters import canonical_adapter
from algo_coach.leetcode_html import parse_question, parse_statement
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher

DAY = date(2026, 10, 16)
HTML = """<p>计算两个整数之和，返回结果。</p>
<script>IGNORE_SECRET</script><style>SECRET_STYLE</style>
<p>示例 1：</p><pre><strong>输入：</strong>a = 2, b = 3
<strong>输出：</strong>5
<strong>解释：</strong>两数相加得到五。</pre>
<p><strong>提示：</strong></p><ul><li>0 &lt;= a &lt;= 10<sup>3</sup></li>
<li>0 &lt;= b &lt;= 1000</li></ul><p>进阶：能否说明复杂度？</p>"""


def question(identifier="25", slug="synthetic-sum"):
    return {
        "questionFrontendId": identifier,
        "titleSlug": slug,
        "title": "Synthetic sum",
        "translatedTitle": "合成相加题",
        "content": None,
        "translatedContent": HTML,
        "isPaidOnly": False,
        "codeSnippets": [
            {
                "langSlug": "python3",
                "code": "class Solution:\n    def solve(self, a: int, b: int) -> int:\n        ",
            }
        ],
        "metaData": json.dumps({"name": "solve", "params": [{"name": "a"}, {"name": "b"}]}),
    }


class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 16, 5, tzinfo=UTC).timestamp()

    def __call__(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(remote, "today", lambda: DAY)
    requests = []
    state = {"question": question(), "failure": None, "day": DAY.isoformat()}

    def respond(request):
        requests.append(request)
        assert str(request.url) == remote.ENDPOINT
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if state["failure"]:
            if isinstance(state["failure"], Exception):
                raise state["failure"]
            return state["failure"]
        body = json.loads(request.content)
        if "AlgoCoachLookup" in body["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "problemsetQuestionList": {
                            "total": 2,
                            "questions": [
                                {
                                    "frontendQuestionId": "1287",
                                    "titleSlug": "wrong",
                                    "paidOnly": False,
                                },
                                {
                                    "frontendQuestionId": "25",
                                    "titleSlug": "synthetic-sum",
                                    "paidOnly": False,
                                },
                            ],
                        }
                    }
                },
            )
        if "AlgoCoachDaily" in body["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "todayRecord": [
                            {
                                "date": state["day"],
                                "question": {
                                    "questionFrontendId": "25",
                                    "titleSlug": "synthetic-sum",
                                    "isPaidOnly": False,
                                },
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"data": {"question": state["question"]}})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        with Coach(tmp_path / "data", DemoTeacher(), source_client=client) as coach:
            clock = Clock()
            coach.remote.clock = clock
            coach.remote.sleep = clock.sleep
            yield coach, requests, state, clock


def test_parser_extracts_actual_examples_constraints_explanation_and_followup():
    p, language, fingerprint = parse_question(question())
    assert language == "zh-CN" and len(fingerprint) == 64
    assert p.examples[0].input == "a = 2, b = 3" and p.examples[0].output == "5"
    assert p.constraints == ["0 <= a <= 10^(3)", "0 <= b <= 1000"]
    assert "进阶" in p.statement and "两数相加得到五" in p.statement
    assert "SECRET" not in p.statement
    assert p.signature == "class Solution: def solve(self, a: int, b: int) -> int:"


def test_parser_drops_unsafe_links_and_control_chars_and_keeps_image_source():
    html = HTML.replace("计算", '<a href="javascript:evil()">计算</a>\x1b\u202e')
    html = html.replace(
        "示例 1：",
        '示例 1：<img src="https://assets.leetcode.com/example.png">'
        '<img src="http://127.0.0.1/private"><iframe>SECRET</iframe>',
    )
    statement, _, _ = parse_statement(html)
    assert "https://assets.leetcode.com/example.png" in statement
    for forbidden in ("javascript:", "127.0.0.1", "SECRET", "\x1b", "\u202e"):
        assert forbidden not in statement


@pytest.mark.parametrize(
    "markup",
    [
        "<p>没有样例和约束</p>",
        HTML.replace("提示：", "无约束标记"),
        HTML.replace("输出：", "无输出标记"),
        HTML.replace("输入：", "无输入标记"),
    ],
)
def test_parser_never_fabricates_missing_required_fields(markup):
    with pytest.raises(ValueError):
        parse_statement(markup)


def test_english_only_statement_is_supported():
    q = question()
    q["translatedContent"] = None
    q["content"] = (
        "<p>Add two integers.</p><p>Example 1:</p><pre>Input: a=1,b=2\nOutput: 3</pre>"
        "<p>Constraints:</p><ul><li>0 &lt;= a,b &lt;= 100</li></ul>"
    )
    p, language, _ = parse_question(q)
    assert p.title == "Synthetic sum" and language == "en"
    assert p.examples[0].output == "3"


@pytest.mark.parametrize(
    "field,value",
    [
        ("isPaidOnly", True),
        ("isPaidOnly", None),
        ("questionFrontendId", "LCP 25"),
        ("titleSlug", "UpperCase"),
        ("codeSnippets", []),
        ("metaData", "{}"),
    ],
)
def test_unsupported_question_contracts_fail(field, value):
    q = question()
    q[field] = value
    with pytest.raises((ValueError, TypeError)):
        parse_question(q)


def test_python_template_is_parsed_without_execution(tmp_path):
    q = question()
    marker = tmp_path / "must-not-exist"
    q["codeSnippets"][0]["code"] = (
        f"open({str(marker)!r}, 'w').write('bad')\n" + q["codeSnippets"][0]["code"]
    )
    parse_question(q)
    assert not marker.exists()
    q["metaData"] = '{"name":"different", "params":[]}'
    with pytest.raises(ValueError, match="不一致"):
        parse_question(q)


def test_id_lookup_uses_exact_match_and_cache_survives_restart(source):
    coach, calls, _, _ = source
    fetched = coach.remote.fetch("leetcode25")
    assert fetched.cache_status == "network"
    assert len(calls) == 2 and json.loads(calls[1].content)["variables"]["slug"] == "synthetic-sum"
    assert coach.remote.fetch("25").cache_status == "fresh_cache"
    assert (
        coach.remote.fetch("https://leetcode.cn/problems/synthetic-sum/", offline=True).cache_status
        == "offline_cache"
    )
    assert len(calls) == 2
    # A second cache service instance uses the same persisted table, not in-memory state.
    reloaded = remote.LeetCodeCN(coach.connection)
    assert reloaded.fetch("25", offline=True).snapshot == fetched.snapshot
    assert coach.problems.resolve("25").title.endswith("本地教学摘要）")


def test_refresh_versions_do_not_mutate_existing_sessions(source):
    coach, _, state, _ = source
    session = coach.start_remote("25")["session_id"]
    old = copy.deepcopy(coach.get(session))
    state["question"]["translatedTitle"] = "修订后的合成题"
    fresh = coach.remote.fetch("25", refresh=True)
    assert fresh.snapshot.problem.title == "修订后的合成题"
    assert coach.get(session)["problem"] == old["problem"]
    assert coach.get(session)["problem_source"] == old["problem_source"]
    assert coach.connection.execute("SELECT count(*) FROM app_remote_snapshots").fetchone()[0] == 2


def test_stale_fallback_is_labeled_and_failed_refresh_preserves_cache(source):
    coach, calls, state, clock = source
    original = coach.remote.fetch("25")
    clock.value += remote.CACHE_TTL + 1
    state["failure"] = httpx.ConnectError("PRIVATE PROVIDER DETAIL")
    result = coach.remote.fetch("25")
    assert result.cache_status == "stale_cache" and result.warning
    assert result.snapshot == original.snapshot
    with pytest.raises(remote.SourceError, match="连接失败"):
        coach.remote.fetch("25", refresh=True)
    assert coach.remote.fetch("25", offline=True).snapshot == original.snapshot
    assert len(calls) == 4


@pytest.mark.parametrize(
    "response,code",
    [
        (httpx.Response(403, text="PRIVATE"), "auth"),
        (httpx.Response(429, headers={"retry-after": "600"}), "rate_limit"),
        (httpx.Response(200, json={"errors": [{"message": "Please Sign in PRIVATE"}]}), "auth"),
        (httpx.Response(200, json={"errors": [{"message": "rate limit PRIVATE"}]}), "rate_limit"),
    ],
)
def test_denial_or_throttle_is_not_retried_and_cooldown_is_persisted(source, response, code):
    coach, calls, state, _ = source
    state["failure"] = response
    with pytest.raises(remote.SourceError) as failure:
        coach.remote.fetch("25")
    assert failure.value.code == code and "PRIVATE" not in str(failure.value)
    with pytest.raises(remote.SourceError, match="冷却"):
        coach.remote.fetch("25")
    assert len(calls) == 1
    assert coach.connection.execute("SELECT count(*) FROM app_remote_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "http://127.0.0.1"}),
        httpx.Response(200, text="<html>login</html>"),
        httpx.Response(200, json={"data": {}, "errors": [{"message": "unknown field"}]}),
        httpx.Response(200, json=[1, 2]),
    ],
)
def test_bad_protocol_does_not_create_cache(source, response):
    coach, calls, state, _ = source
    state["failure"] = response
    with pytest.raises(remote.SourceError):
        coach.remote.fetch("25")
    assert len(calls) == 1
    assert coach.connection.execute("SELECT count(*) FROM app_remote_snapshots").fetchone()[0] == 0


def test_response_size_is_bounded(source, monkeypatch):
    coach, calls, state, _ = source
    monkeypatch.setattr(remote, "MAX_RESPONSE", 10)
    state["failure"] = httpx.Response(
        200, content=b"x" * 11, headers={"content-type": "application/json"}
    )
    with pytest.raises(remote.SourceError, match="512 KiB"):
        coach.remote.fetch("25")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "reference",
    [
        "https://leetcode.com/problems/two-sum/",
        "http://leetcode.cn/problems/two-sum/",
        "https://leetcode.cn.evil.test/problems/two-sum/",
        "https://user:secret@leetcode.cn/problems/two-sum/",
        "https://leetcode.cn/problems/two-sum/?secret=x",
        "https://127.0.0.1/problems/two-sum/",
        "https://leetcode.cn:8443/problems/two-sum/",
        "LCP25",
        "0",
        "../private",
    ],
)
def test_untrusted_references_are_rejected_before_network(source, reference):
    coach, calls, _, _ = source
    with pytest.raises(ValueError):
        coach.remote.fetch(reference)
    assert calls == []


def test_offline_miss_and_conflicting_options_do_not_network(source):
    coach, calls, _, _ = source
    with pytest.raises(remote.SourceError):
        coach.remote.fetch("25", offline=True)
    with pytest.raises(remote.SourceError):
        coach.remote.fetch("25", offline=True, refresh=True)
    assert calls == []


def test_remote_daily_has_provider_provenance_and_never_overwrites_manual_selection(source):
    coach, calls, _, _ = source
    manual = coach.daily.set(coach.problems.resolve("1"), DAY, "2026-10-16T01:00:00+00:00")
    selection, result = coach.remote.daily(DAY)
    assert selection.kind == "provider_fetched" and selection.source == result.snapshot.source
    assert selection.calendar_date == DAY and selection.problem.frontend_id == "25"
    assert coach.daily.get(DAY, "leetcode_cn") == manual
    session = coach.start_remote_daily(DAY)["session_id"]
    assert coach.get(session)["daily_selection"]["kind"] == "provider_fetched"
    assert coach.get(session)["problem_source"]["cache_status"] == "fresh_cache"
    assert len(calls) == 2


def test_wrong_daily_date_is_not_replaced_with_yesterday(source):
    coach, calls, state, _ = source
    state["day"] = "2026-10-15"
    with pytest.raises(remote.SourceError, match="请求日期"):
        coach.remote.daily(DAY)
    assert len(calls) == 1
    assert coach.connection.execute("SELECT count(*) FROM app_remote_daily").fetchone()[0] == 0


def test_daily_offline_is_exact_date_only_and_historical_miss_does_not_network(source):
    coach, calls, _, _ = source
    selection, _ = coach.remote.daily(DAY)
    assert coach.remote.daily(DAY, offline=True)[0] == selection
    for other in (date(2026, 10, 15), date(2026, 10, 17)):
        with pytest.raises(remote.SourceError):
            coach.remote.daily(other)
    assert len(calls) == 2


def test_incomplete_or_mismatched_detail_never_publishes_snapshot(source):
    coach, _, state, _ = source
    state["question"]["questionFrontendId"] = "999"
    with pytest.raises(remote.SourceError, match="不一致"):
        coach.remote.fetch("25")
    state["question"] = question()
    state["question"]["translatedContent"] = "<p>没有可靠样例</p>"
    with pytest.raises(remote.SourceError, match="可靠解析"):
        coach.remote.daily(DAY)
    assert coach.connection.execute("SELECT count(*) FROM app_remote_snapshots").fetchone()[0] == 0
    assert coach.connection.execute("SELECT count(*) FROM app_remote_daily").fetchone()[0] == 0


def test_remote_provenance_survives_archive_and_review_without_statement_redistribution(
    source, tmp_path
):
    coach, _, _, _ = source
    session = coach.start_remote("25")["session_id"]
    coach.turn(session, "代码", code="pass")
    practice = tmp_path / "practice"
    practice.mkdir()
    plan = coach.preview_archive(session, practice)
    metadata = json.loads(plan.contents["metadata.json"])
    assert metadata["problem_source"]["kind"] == "leetcode_cn_graphql"
    assert "计算两个整数" not in plan.contents["problem.md"]
    assert canonical_adapter(coach.remote.fetch("25").snapshot.problem) is None
    coach.add_review(session, DAY)
    review = coach.start_review("leetcode_cn:25")
    assert review["problem_source"]["cache_status"] == "review_snapshot"
    assert review["current_code"] == "" and review["attempts"] == []


def test_real_cli_can_read_remote_cache_without_model_keys_or_network(source, tmp_path):
    coach, _, _, _ = source
    coach.remote.fetch("25")
    data = coach.data_dir
    coach.__exit__()  # Release the application lock for an independent CLI process.
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALGOCOACH_")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "algo_coach",
            "--data-dir",
            str(data),
            "fetch",
            "25",
            "--offline",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "offline_cache" in result.stdout and "合成相加题" in result.stdout


def test_daily_stale_fallback_and_failed_refresh_keep_exact_mapping(source):
    coach, _, state, clock = source
    original, _ = coach.remote.daily(DAY)
    clock.value += remote.CACHE_TTL + 1
    state["failure"] = httpx.ConnectError("unavailable")
    selection, fetched = coach.remote.daily(DAY)
    assert selection == original and fetched.cache_status == "stale_cache"
    with pytest.raises(remote.SourceError):
        coach.remote.daily(DAY, refresh=True)
    assert coach.remote.daily(DAY, offline=True)[0] == original


def test_lookup_pagination_is_bounded_when_no_exact_id_exists(source):
    coach, calls, state, _ = source
    state["failure"] = httpx.Response(
        200,
        json={
            "data": {
                "problemsetQuestionList": {
                    "total": 900,
                    "questions": [
                        {
                            "frontendQuestionId": str(i + 100),
                            "titleSlug": "no-match",
                            "paidOnly": False,
                        }
                        for i in range(20)
                    ],
                }
            }
        },
    )
    with pytest.raises(remote.SourceError, match="60 个"):
        coach.remote.fetch("25")
    assert len(calls) == 3
    assert [json.loads(call.content)["variables"]["skip"] for call in calls] == [0, 20, 40]


def test_failed_refresh_parse_does_not_destroy_prior_daily_snapshot(source):
    coach, _, state, _ = source
    original, _ = coach.remote.daily(DAY)
    state["question"]["translatedContent"] = "<p>unparseable</p>"
    with pytest.raises(remote.SourceError):
        coach.remote.daily(DAY, refresh=True)
    assert coach.remote.daily(DAY, offline=True)[0] == original
    assert coach.connection.execute("SELECT count(*) FROM app_remote_snapshots").fetchone()[0] == 1


def test_legacy_review_rows_survive_column_migration(tmp_path):
    import sqlite3

    from algo_coach.problems import LocalProblems

    data = tmp_path / "legacy"
    data.mkdir()
    problem = LocalProblems(data).resolve("25")
    with sqlite3.connect(data / "checkpoints.sqlite") as connection:
        connection.execute(
            "CREATE TABLE app_reviews (problem_key TEXT PRIMARY KEY, "
            "problem TEXT NOT NULL, source_session TEXT NOT NULL, "
            "due_date TEXT NOT NULL, step INTEGER NOT NULL, version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO app_reviews VALUES (?, ?, ?, ?, 2, 7)",
            (problem.key, problem.model_dump_json(), "legacy-session", DAY.isoformat()),
        )
    with Coach(data, DemoTeacher()) as coach:
        old = coach.reviews.get("leetcode_cn:25")
        assert old["version"] == 7 and old["step"] == 2 and old["problem_source"] is None
        assert old["problem"] == problem.model_dump()
        state = coach.start(coach.problems.resolve("1"))
        entry = coach.add_review(state["session_id"], DAY)
        assert entry["problem_source"] is None
        assert entry["due_date"] == "2026-10-17"
