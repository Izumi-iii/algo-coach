"""Explicit LeetCode CN reads, exact-date cache and controlled error messages."""

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx

from algo_coach.adapters import problem_hash
from algo_coach.calendar import today
from algo_coach.daily import DailySelection
from algo_coach.leetcode_html import PARSER_VERSION, parse_question
from algo_coach.sources import RemoteSnapshot, RemoteSource

ENDPOINT = "https://leetcode.cn/graphql/"
MAX_RESPONSE = 512 * 1024
CACHE_TTL = 6 * 3600
DAILY_QUERY = """query AlgoCoachDaily {
  todayRecord { date question { questionFrontendId titleSlug isPaidOnly } }
}"""
LOOKUP_QUERY = """query AlgoCoachLookup($term: String!, $skip: Int!) {
  problemsetQuestionList(categorySlug: "", limit: 20, skip: $skip,
    filters: {searchKeywords: $term}) {
    total questions { frontendQuestionId titleSlug paidOnly }
  }
}"""
DETAIL_QUERY = """query AlgoCoachDetail($slug: String!) {
  question(titleSlug: $slug) {
    questionFrontendId title titleSlug translatedTitle content translatedContent
    isPaidOnly codeSnippets { langSlug code } metaData
  }
}"""


class SourceError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Fetched:
    snapshot: RemoteSnapshot
    cache_status: str
    warning: str = ""

    def record(self):
        return {
            **self.snapshot.source.model_dump(mode="json"),
            "cache_status": self.cache_status,
            "warning": self.warning,
        }

    def render(self):
        return (
            f"远程来源：LeetCode 中国站；缓存状态：{self.cache_status}\n"
            f"题面获取时间：{self.snapshot.source.fetched_at.isoformat()}\n"
            + (f"提示：{self.warning}\n" if self.warning else "")
        )


class LeetCodeCN:
    def __init__(self, connection, *, client=None, clock=time.time, sleep=time.sleep):
        self.connection, self.client, self.clock, self.sleep = connection, client, clock, sleep
        connection.executescript(
            "CREATE TABLE IF NOT EXISTS app_remote_snapshots (id TEXT PRIMARY KEY, "
            "frontend_id TEXT NOT NULL, slug TEXT NOT NULL, payload TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS app_remote_heads (frontend_id TEXT PRIMARY KEY, "
            "snapshot_id TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS app_remote_daily (calendar_date TEXT PRIMARY KEY, "
            "payload TEXT NOT NULL);"
            "CREATE TABLE IF NOT EXISTS app_remote_network (id INTEGER PRIMARY KEY CHECK(id=1), "
            "next_request REAL NOT NULL);"
        )
        connection.commit()

    def _request(self, query, variables):
        row = self.connection.execute(
            "SELECT next_request FROM app_remote_network WHERE id=1"
        ).fetchone()
        delay = (row[0] - self.clock()) if row else 0
        if delay > 1.1:
            raise SourceError("rate_limit", "题源处于访问冷却期，请稍后重试或使用 --offline")
        if delay > 0:
            self.sleep(delay)
        self._cooldown(1)
        owned = self.client is None
        client = self.client or httpx.Client(
            timeout=httpx.Timeout(15, connect=5),
            follow_redirects=False,
            headers={"User-Agent": "AlgoCoach/0.1", "Accept": "application/json"},
        )
        try:
            client.cookies.clear()
            with client.stream(
                "POST",
                ENDPOINT,
                json={"query": query, "variables": variables},
                follow_redirects=False,
            ) as response:
                status = response.status_code
                if status in (401, 403):
                    self._cooldown(60)
                    raise SourceError(
                        "auth", "题源拒绝匿名访问，已停止；不会尝试绕过登录或访问限制"
                    )
                if status == 429:
                    retry = response.headers.get("retry-after", "60")
                    seconds = min(max(int(retry), 60), 3600) if retry.isdigit() else 60
                    self._cooldown(seconds)
                    raise SourceError(
                        "rate_limit", "题源限流，已进入冷却期；可使用 --offline 读取已有缓存"
                    )
                if status != 200:
                    raise SourceError("http", f"题源 HTTP {status}；未跟随重定向或自动重试")
                if "application/json" not in response.headers.get("content-type", ""):
                    raise SourceError("schema", "题源没有返回 JSON，未保存响应正文")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    if len(payload) + len(chunk) > MAX_RESPONSE:
                        raise SourceError("schema", "题源响应超过 512 KiB，已停止读取")
                    payload.extend(chunk)
                body = json.loads(payload)
                if not isinstance(body, dict):
                    raise SourceError("schema", "题源 JSON 结构不符合预期")
                if body.get("errors"):
                    errors = body["errors"]
                    messages = (
                        [str(e.get("message", "")) for e in errors if isinstance(e, dict)]
                        if isinstance(errors, list)
                        else []
                    )
                    if any(
                        re.search(r"sign in|login|register|unauth|forbidden", m, re.I)
                        for m in messages
                    ):
                        self._cooldown(60)
                        raise SourceError("auth", "题源要求登录或拒绝访问，未保存原始服务错误")
                    if any(re.search(r"rate limit|too many|throttl", m, re.I) for m in messages):
                        self._cooldown(60)
                        raise SourceError("rate_limit", "题源返回限流错误，已停止请求并进入冷却期")
                    raise SourceError(
                        "schema", "题源返回 GraphQL 错误，可能是字段变化；未使用部分响应"
                    )
                if not isinstance(body.get("data"), dict):
                    raise SourceError("schema", "题源缺少 data 对象")
                return body["data"]
        except httpx.HTTPError:
            raise SourceError(
                "network", "题源连接失败或超时；可用 --offline 读取已有缓存"
            ) from None
        except (json.JSONDecodeError, UnicodeError):
            raise SourceError("schema", "题源 JSON 无法解析") from None
        finally:
            if owned:
                client.close()

    def _cooldown(self, seconds):
        with self.connection:
            self.connection.execute(
                "INSERT INTO app_remote_network VALUES (1, ?) ON CONFLICT(id) "
                "DO UPDATE SET next_request=excluded.next_request",
                (self.clock() + seconds,),
            )

    @staticmethod
    def reference(value):
        value = value.strip()
        if value.startswith(("http://", "https://")):
            parsed = urlparse(value)
            if (
                parsed.scheme != "https"
                or parsed.hostname != "leetcode.cn"
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or parsed.query
                or parsed.fragment
            ):
                raise SourceError("reference", "远程仅支持不带凭据或参数的中国站 HTTPS 原题链接")
            match = re.fullmatch(
                r"/problems/([a-z0-9][a-z0-9-]{0,119})/(?:description/)?",
                parsed.path + ("/" if not parsed.path.endswith("/") else ""),
            )
            if not match:
                raise SourceError("reference", "无法识别中国站题目链接")
            return "slug", match[1]
        identifier = re.sub(r"^leetcode\s*", "", value, flags=re.I)
        if not re.fullmatch(r"[0-9]{1,6}", identifier) or int(identifier) == 0:
            raise SourceError("reference", "远程首版支持普通数字题号、leetcode25 或中国站原题链接")
        return "id", str(int(identifier))

    def _cached(self, kind, value):
        query = (
            "SELECT s.payload FROM app_remote_snapshots s JOIN app_remote_heads h "
            "ON h.snapshot_id=s.id WHERE " + ("h.frontend_id=?" if kind == "id" else "s.slug=?")
        )
        row = self.connection.execute(query, (value,)).fetchone()
        if not row:
            return None
        snapshot = RemoteSnapshot.model_validate_json(row[0])
        if snapshot.source.parser_version != PARSER_VERSION:
            return None
        if (kind == "id" and snapshot.problem.frontend_id != value) or (
            kind == "slug" and snapshot.problem.slug != value
        ):
            raise SourceError("cache", "缓存索引与题目快照不一致")
        return snapshot

    def _fresh(self, fetched_at):
        age = self.clock() - fetched_at.timestamp()
        return 0 <= age <= CACHE_TTL

    def _save(self, snapshot):
        p, source = snapshot.problem, snapshot.source
        self.connection.execute(
            "INSERT OR IGNORE INTO app_remote_snapshots VALUES (?, ?, ?, ?)",
            (source.snapshot_id, p.frontend_id, p.slug, snapshot.model_dump_json()),
        )
        self.connection.execute(
            "INSERT INTO app_remote_heads VALUES (?, ?) ON CONFLICT(frontend_id) "
            "DO UPDATE SET snapshot_id=excluded.snapshot_id",
            (p.frontend_id, source.snapshot_id),
        )

    def _lookup(self, identifier):
        for page in range(3):
            data = self._request(LOOKUP_QUERY, {"term": identifier, "skip": page * 20})
            result = data.get("problemsetQuestionList")
            rows = result.get("questions") if isinstance(result, dict) else None
            if (
                not isinstance(rows, list)
                or len(rows) > 20
                or not all(isinstance(r, dict) for r in rows)
            ):
                raise SourceError("schema", "题号搜索结果结构异常")
            matches = [r for r in rows if r.get("frontendQuestionId") == identifier]
            if len(matches) > 1:
                raise SourceError("schema", "题号搜索返回多个精确结果，未自动选择")
            if matches:
                match = matches[0]
                if match.get("paidOnly") is not False:
                    raise SourceError("paid", "该搜索结果并非明确公开题目，未读取详情")
                slug = match.get("titleSlug")
                if not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", slug):
                    raise SourceError("schema", "搜索结果 slug 不受支持")
                return slug
            if len(rows) < 20:
                break
        raise SourceError(
            "not_found", "在最多 60 个搜索候选中未找到唯一精确题号；可改用中国站原题链接"
        )

    def _detail(self, slug, expected_id=None):
        data = self._request(DETAIL_QUERY, {"slug": slug})
        question = data.get("question")
        if not isinstance(question, dict):
            raise SourceError("not_found", "题目不存在或匿名不可见")
        if question.get("titleSlug") != slug or (
            expected_id and question.get("questionFrontendId") != expected_id
        ):
            raise SourceError("schema", "详情题号或 slug 与请求不一致，未保存")
        try:
            problem, language, raw_hash = parse_question(question)
        except (ValueError, KeyError, TypeError, SyntaxError, RecursionError):
            raise SourceError(
                "parse", "题面、样例、约束或 Python3 签名无法可靠解析；未编造字段或写入缓存"
            ) from None
        stamp = datetime.fromtimestamp(self.clock(), UTC)
        fingerprint = problem_hash(problem)
        snapshot_id = hashlib.sha256(
            f"{fingerprint}:{raw_hash}:{stamp.isoformat()}".encode()
        ).hexdigest()
        return RemoteSnapshot(
            problem=problem,
            source=RemoteSource(
                snapshot_id=snapshot_id,
                fetched_at=stamp,
                problem_hash=fingerprint,
                raw_hash=raw_hash,
                parser_version=PARSER_VERSION,
                language=language,
            ),
        )

    def fetch(self, reference, *, offline=False, refresh=False):
        if offline and refresh:
            raise SourceError("options", "--offline 和 --refresh 不能同时使用")
        kind, value = self.reference(reference)
        cached = self._cached(kind, value)
        if offline:
            if not cached:
                raise SourceError("cache", "没有该题的可用远程缓存；离线模式不会联网")
            return Fetched(cached, "offline_cache")
        if cached and not refresh and self._fresh(cached.source.fetched_at):
            return Fetched(cached, "fresh_cache")
        try:
            slug = value if kind == "slug" else self._lookup(value)
            snapshot = self._detail(slug, value if kind == "id" else None)
            with self.connection:
                self._save(snapshot)
            return Fetched(snapshot, "network")
        except SourceError as exc:
            if cached and not refresh and exc.code in {"network", "http", "auth", "rate_limit"}:
                return Fetched(
                    cached, "stale_cache", "当前题源不可用；仅展示这道题的旧缓存，未重新核实"
                )
            raise

    def daily(self, day=None, *, offline=False, refresh=False):
        day = day or today()
        if offline and refresh:
            raise SourceError("options", "--offline 和 --refresh 不能同时使用")
        row = self.connection.execute(
            "SELECT payload FROM app_remote_daily WHERE calendar_date=?", (day.isoformat(),)
        ).fetchone()
        cached = DailySelection.model_validate_json(row[0]) if row else None
        if cached and (cached.calendar_date != day or not cached.source):
            raise SourceError("cache", "每日题缓存日期或解析版本不一致")
        if cached and cached.source.parser_version != PARSER_VERSION:
            cached = None
        if offline and not cached:
            raise SourceError("cache", "没有该日期的可用远程缓存；离线模式不会请求题源")
        if offline or day != today():
            if not cached or refresh:
                raise SourceError(
                    "date", "历史日期只支持已有的精确日期缓存；当前接口不提供任意历史日期查询"
                )
            status = "offline_cache" if offline else "historical_cache"
            return cached, Fetched(
                RemoteSnapshot(problem=cached.problem, source=cached.source), status
            )
        if cached and not refresh and self._fresh(datetime.fromisoformat(cached.registered_at)):
            return cached, Fetched(
                RemoteSnapshot(problem=cached.problem, source=cached.source), "fresh_cache"
            )
        try:
            data = self._request(DAILY_QUERY, {})
            rows = data.get("todayRecord")
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise SourceError("schema", "每日题响应结构异常")
            matches = [r for r in rows if r.get("date") == day.isoformat()]
            if len(matches) != 1:
                raise SourceError("date", "题源未返回请求日期的唯一每日题；不会以其他日期代替")
            brief = matches[0].get("question")
            if not isinstance(brief, dict) or brief.get("isPaidOnly") is not False:
                raise SourceError("paid", "每日题没有明确的公开题目标识")
            identifier, slug = brief.get("questionFrontendId"), brief.get("titleSlug")
            if (
                not isinstance(identifier, str)
                or not re.fullmatch(r"[1-9][0-9]{0,5}", identifier)
                or not isinstance(slug, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,119}", slug)
            ):
                raise SourceError("schema", "每日题标识不受支持")
            snapshot = self._detail(slug, identifier)
            selection = DailySelection(
                site="leetcode_cn",
                calendar_date=day,
                kind="provider_fetched",
                source_url=snapshot.problem.url,
                registered_at=datetime.fromtimestamp(self.clock(), UTC).isoformat(),
                problem=snapshot.problem,
                content_hash=hashlib.sha256(
                    snapshot.problem.model_dump_json().encode()
                ).hexdigest(),
                source=snapshot.source,
            )
            with self.connection:
                self._save(snapshot)
                self.connection.execute(
                    "INSERT INTO app_remote_daily VALUES (?, ?) ON CONFLICT(calendar_date) "
                    "DO UPDATE SET payload=excluded.payload",
                    (day.isoformat(), selection.model_dump_json()),
                )
            return selection, Fetched(snapshot, "network")
        except SourceError as exc:
            if cached and not refresh and exc.code in {"network", "http", "auth", "rate_limit"}:
                return cached, Fetched(
                    RemoteSnapshot(problem=cached.problem, source=cached.source),
                    "stale_cache",
                    "当前题源不可用；仅展示所请求日期的旧缓存，未重新核实",
                )
            raise
