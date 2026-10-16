"""Explicit dated selections. A user claim never becomes provider-verified evidence."""

import hashlib
from datetime import date
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, model_validator

from algo_coach.models import Problem, StrictModel
from algo_coach.sources import RemoteSource


class DailySelection(StrictModel):
    site: Literal["leetcode_cn", "leetcode_com", "local"]
    calendar_date: date
    kind: Literal["personal_plan", "user_reported_platform", "provider_fetched"]
    source_url: str = Field(max_length=2000)
    registered_at: str
    problem: Problem
    content_hash: str
    source: RemoteSource | None = None

    @model_validator(mode="after")
    def check_snapshot(self):
        expected = hashlib.sha256(self.problem.model_dump_json().encode()).hexdigest()
        if self.site != self.problem.site or self.content_hash != expected:
            raise ValueError("日期记录与题目快照或内容哈希不一致")
        if self.kind == "provider_fetched":
            from algo_coach.adapters import problem_hash

            if (
                not self.source
                or self.site != "leetcode_cn"
                or self.source.problem_hash != problem_hash(self.problem)
                or self.source_url != self.problem.url
            ):
                raise ValueError("远程每日题缺少有效来源或题面哈希")
        elif self.source is not None:
            raise ValueError("手动登记不能带有远程 Provider 来源")
        return self

    def render(self) -> str:
        label = {
            "personal_plan": "个人练习计划（不是平台每日题）",
            "user_reported_platform": "用户登记的平台每日题（未自动核实）",
            "provider_fetched": "中国站接口返回的每日题（按响应日期核对）",
        }[self.kind]
        return (
            f"日期：{self.calendar_date}；站点：{self.site}\n类型：{label}\n"
            f"登记时间：{self.registered_at}\n"
            f"来源：{self.source_url or '本地选择'}\n\n{self.problem.render()}"
        )


class DailySelections:
    def __init__(self, connection):
        self.connection = connection
        connection.execute(
            "CREATE TABLE IF NOT EXISTS app_daily "
            "(site TEXT NOT NULL, calendar_date TEXT NOT NULL, payload TEXT NOT NULL, "
            "PRIMARY KEY(site, calendar_date))"
        )
        connection.commit()

    def get(self, day: date, site: str) -> DailySelection:
        row = self.connection.execute(
            "SELECT payload FROM app_daily WHERE site=? AND calendar_date=?",
            (site, day.isoformat()),
        ).fetchone()
        if not row:
            raise ValueError(
                f"{site} 在 {day} 没有已登记的题目；不会以其他日期或站点的旧题代替。"
                "请先使用 daily set 登记，远程自动获取尚未接入。"
            )
        selection = DailySelection.model_validate_json(row[0])
        if selection.site != site or selection.calendar_date != day:
            raise ValueError("日期记录的站点或日期不一致")
        return selection

    def set(
        self,
        problem: Problem,
        day: date,
        registered_at: str,
        *,
        platform=False,
        source_url="",
        replace=False,
    ) -> DailySelection:
        if platform:
            parsed = urlparse(source_url)
            domain = {"leetcode_cn": "leetcode.cn", "leetcode_com": "leetcode.com"}.get(
                problem.site
            )
            if (
                not domain
                or parsed.scheme != "https"
                or parsed.hostname != domain
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
                or parsed.path.rstrip("/") != f"/problems/{problem.slug}"
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("登记平台每日题需提供与题目站点、slug 匹配的 HTTPS 原题链接")
        elif source_url:
            raise ValueError("只有 --platform 登记使用 --source-url；个人计划无需来源链接")
        selection = DailySelection(
            site=problem.site,
            calendar_date=day,
            kind="user_reported_platform" if platform else "personal_plan",
            source_url=source_url,
            registered_at=registered_at,
            problem=problem,
            content_hash=hashlib.sha256(problem.model_dump_json().encode()).hexdigest(),
        )
        row = self.connection.execute(
            "SELECT payload FROM app_daily WHERE site=? AND calendar_date=?",
            (problem.site, day.isoformat()),
        ).fetchone()
        if row:
            existing = DailySelection.model_validate_json(row[0])
            if existing.model_dump(exclude={"registered_at"}) == selection.model_dump(
                exclude={"registered_at"}
            ):
                return existing
            if not replace:
                raise ValueError("该日期已有不同题目或来源记录；明确使用 --replace 才能替换")
        with self.connection:
            self.connection.execute(
                "INSERT INTO app_daily VALUES (?, ?, ?) ON CONFLICT(site, calendar_date) "
                "DO UPDATE SET payload=excluded.payload",
                (problem.site, day.isoformat(), selection.model_dump_json()),
            )
        return selection
