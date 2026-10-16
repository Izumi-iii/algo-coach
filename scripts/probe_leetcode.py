"""Explicit low-volume research; prints metadata, never full statements or cookies.

Run using the project venv (httpx is present in requirements-lock.txt).
No retries, login, redirects, model calls, catalogue writes, or teaching integration.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

import httpx

ENDPOINTS = {
    "cn": "https://leetcode.cn/graphql/",
    "com": "https://leetcode.com/graphql/",
}
MAX_RESPONSE_BYTES = 256 * 1024
DAILY_QUERIES = {
    "cn": """query AlgoCoachDaily {
      todayRecord { date question {
        questionId questionFrontendId title titleSlug translatedTitle isPaidOnly
      } }
    }""",
    "com": """query AlgoCoachDaily {
      activeDailyCodingChallengeQuestion { date link question {
        questionFrontendId title titleSlug isPaidOnly
      } }
    }""",
}
LOOKUP_QUERIES = {
    "cn": """query AlgoCoachLookup($term: String!) {
      problemsetQuestionList(categorySlug: "", limit: 5, skip: 0,
        filters: {searchKeywords: $term}) {
        total questions { frontendQuestionId title titleSlug paidOnly }
      }
    }""",
    "com": """query AlgoCoachLookup($term: String!) {
      problemsetQuestionListV2(searchKeyword: $term, limit: 5, skip: 0) {
        totalLength questions { questionFrontendId title titleSlug paidOnly }
      }
    }""",
}
DETAIL_QUERY = """query AlgoCoachDetail($slug: String!) {
  question(titleSlug: $slug) {
    questionId questionFrontendId title titleSlug translatedTitle
    content translatedContent isPaidOnly
    codeSnippets { langSlug code } sampleTestCase metaData
  }
}"""


class ContentInspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.images = 0
        self.scripts = 0

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images += 1
        if tag == "script":
            self.scripts += 1

    def handle_data(self, data):
        self.text.append(data)

    def summary(self, markup):
        self.feed(markup)
        text = " ".join(self.text)
        return {
            "html_chars": len(markup),
            "text_chars": len(text),
            "has_input_marker": bool(re.search(r"输入|Input", text)),
            "has_output_marker": bool(re.search(r"输出|Output", text)),
            "has_constraints_marker": bool(re.search(r"提示|约束|Constraints", text)),
            "images": self.images,
            "scripts": self.scripts,
        }


def summarize(site, name, data, variables):
    if name == "daily":
        rows = (
            data.get("todayRecord")
            if site == "cn"
            else [data.get("activeDailyCodingChallengeQuestion")]
        )
        if (
            not isinstance(rows, list)
            or not rows
            or any(
                not isinstance(row, dict)
                or not isinstance(row.get("question"), dict)
                or not row.get("date")
                for row in rows
            )
        ):
            raise ValueError("missing daily selection")
        return {"records": rows}
    if name == "lookup":
        result = data.get("problemsetQuestionList" if site == "cn" else "problemsetQuestionListV2")
        if not isinstance(result, dict):
            raise ValueError("missing lookup result")
        rows = result.get("questions")
        if not isinstance(rows, list) or len(rows) > 5:
            raise ValueError("unexpected lookup result size")
        key = "frontendQuestionId" if site == "cn" else "questionFrontendId"
        matches = [row for row in rows if row.get(key) == variables["term"]]
        return {
            "candidate_ids": [row.get(key) for row in rows],
            "exact_matches": [
                {key: row.get(key), "titleSlug": row.get("titleSlug")} for row in matches
            ],
            "unique_exact_match": len(matches) == 1,
        }
    question = data.get("question")
    if not isinstance(question, dict):
        raise ValueError("missing question")
    result = {
        key: question.get(key)
        for key in ("questionId", "questionFrontendId", "titleSlug", "isPaidOnly")
    }
    if question.get("isPaidOnly") is not False:
        result["content_inspected"] = False
        return result
    result["content"] = ContentInspector().summary(question.get("content") or "")
    result["translatedContent"] = ContentInspector().summary(
        question.get("translatedContent") or ""
    )
    snippets = question.get("codeSnippets") or []
    python = next((item["code"] for item in snippets if item.get("langSlug") == "python3"), "")
    signature = re.search(r"^\s*def\s+[^\n]+", python, re.MULTILINE)
    result["python3_signature"] = signature.group().strip() if signature else None
    result["sample_input_chars"] = len(question.get("sampleTestCase") or "")
    result["metadata"] = json.loads(question.get("metaData") or "{}")
    return result


def probe(client, site, name, variables):
    query = (
        DAILY_QUERIES[site]
        if name == "daily"
        else LOOKUP_QUERIES[site]
        if name == "lookup"
        else DETAIL_QUERY
    )
    started = time.monotonic()
    report = {
        "probe": name,
        "endpoint": ENDPOINTS[site],
        "variables": variables,
        "checked_at_utc": datetime.now(UTC).isoformat(),
    }
    try:
        client.cookies.clear()
        with client.stream(
            "POST", ENDPOINTS[site], json={"query": query, "variables": variables}
        ) as response:
            report["http_status"] = response.status_code
            report["content_type"] = response.headers.get("content-type", "")
            report["stop_site"] = response.status_code in (401, 403, 429)
            payload = bytearray()
            for chunk in response.iter_bytes():
                if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                    raise ValueError("response exceeds 256 KiB")
                payload.extend(chunk)
            report["response_bytes"] = len(payload)
            report["response_sha256"] = hashlib.sha256(payload).hexdigest()
            if 300 <= response.status_code < 400:
                report["outcome"] = "redirect_not_followed"
            elif response.status_code not in (200, 400):
                report["outcome"] = "http_error"
            elif "application/json" not in report["content_type"]:
                report["outcome"] = "unexpected_content_type"
            else:
                body = json.loads(payload)
                if not isinstance(body, dict):
                    raise ValueError("unexpected JSON root")
                errors = body.get("errors") or []
                if errors:
                    report["outcome"] = "graphql_error"
                    report["errors"] = [str(item.get("message", ""))[:250] for item in errors]
                    if any(
                        re.search(
                            r"sign in|register|login|unauth|forbidden|rate limit", message, re.I
                        )
                        for message in report["errors"]
                    ):
                        report["stop_site"] = True
                elif response.status_code != 200:
                    report["outcome"] = "http_error"
                else:
                    report["result"] = summarize(site, name, body.get("data") or {}, variables)
                    report["outcome"] = "ok"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        # Do not print response bodies or exception strings that might contain full content.
        report["outcome"] = "failed"
        report["error_type"] = type(exc).__name__
    report["seconds"] = round(time.monotonic() - started, 3)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="手动执行少量 LeetCode 匿名只读探针，不保存完整题面"
    )
    parser.add_argument("--site", choices=ENDPOINTS, default="cn")
    parser.add_argument("--probe", choices=["daily", "lookup", "detail", "all"], default="daily")
    parser.add_argument("--id", default="25")
    parser.add_argument("--slug", default="reverse-nodes-in-k-group")
    parser.add_argument("--output", type=Path, help="新建研究报告 JSON，已有文件不会覆盖")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[1-9][0-9]{0,5}", args.id) or not re.fullmatch(
        r"[a-z0-9-]{1,120}", args.slug
    ):
        parser.error("仅支持普通数字题号和小写 slug")
    if args.output and args.output.exists():
        parser.error("输出文件已存在，请选择新文件")
    names = ["daily", "detail", "lookup"] if args.probe == "all" else [args.probe]
    reports = []
    with httpx.Client(
        timeout=httpx.Timeout(15, connect=5),
        follow_redirects=False,
        headers={"User-Agent": "AlgoCoach-research/0.1", "Accept": "application/json"},
    ) as client:
        for name in names:
            variables = (
                {"term": args.id}
                if name == "lookup"
                else {"slug": args.slug}
                if name == "detail"
                else {}
            )
            report = probe(client, args.site, name, variables)
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False), flush=True)
            if report.get("stop_site"):
                break
    result = {"site": args.site, "mode": "anonymous_read_only", "reports": reports}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as target:
            json.dump(result, target, ensure_ascii=False, indent=2)
            target.write("\n")
    return 0 if all(item["outcome"] == "ok" for item in reports) else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
