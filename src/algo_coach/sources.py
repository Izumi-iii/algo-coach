"""Immutable provenance contracts shared by cache, daily selections and archives."""

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from algo_coach.adapters import problem_hash
from algo_coach.models import Problem, StrictModel


class RemoteSource(StrictModel):
    kind: Literal["leetcode_cn_graphql"] = "leetcode_cn_graphql"
    endpoint: Literal["https://leetcode.cn/graphql/"] = "https://leetcode.cn/graphql/"
    snapshot_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    fetched_at: datetime
    problem_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    raw_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    parser_version: str
    language: Literal["zh-CN", "en"]

    @model_validator(mode="after")
    def aware_timestamp(self):
        if self.fetched_at.tzinfo is None:
            raise ValueError("来源获取时间必须包含时区")
        return self


class RemoteSnapshot(StrictModel):
    problem: Problem
    source: RemoteSource

    @model_validator(mode="after")
    def validate_snapshot(self):
        if (
            self.problem.site != "leetcode_cn"
            or problem_hash(self.problem) != self.source.problem_hash
        ):
            raise ValueError("远程题目快照哈希或站点不一致")
        return self
