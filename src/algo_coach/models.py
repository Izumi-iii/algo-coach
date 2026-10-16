"""Validated domain contracts; model output cannot change application permissions."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Stage = Literal["understand", "explore", "implement", "debug", "reflect"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Example(StrictModel):
    input: str = Field(min_length=1, max_length=8000)
    output: str = Field(min_length=1, max_length=8000)


class Problem(StrictModel):
    site: Literal["leetcode_cn", "leetcode_com", "local"]
    frontend_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,40}$")
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,119}$")
    title: str = Field(min_length=1, max_length=200)
    statement: str = Field(min_length=1, max_length=40000)
    constraints: list[str] = Field(min_length=1, max_length=30)
    examples: list[Example] = Field(min_length=1, max_length=10)
    signature: str = Field(min_length=1, max_length=1000)
    url: str = Field(default="", max_length=2000)
    attribution: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def check_url(self) -> "Problem":
        from urllib.parse import urlparse

        if self.url:
            parsed = urlparse(self.url)
            domains = {"leetcode.cn", "leetcode.com"}
            if parsed.scheme != "https" or parsed.hostname not in domains:
                raise ValueError("题目来源链接只允许 HTTPS LeetCode 域名")
            if parsed.username or parsed.password or parsed.port not in (None, 443):
                raise ValueError("题目链接不能包含凭据或非标准端口")
            expected = {"leetcode_cn": "leetcode.cn", "leetcode_com": "leetcode.com"}
            if self.site in expected and parsed.hostname != expected[self.site]:
                raise ValueError("题目站点与来源链接不一致")
        return self

    @property
    def key(self) -> str:
        return f"{self.site}:{self.frontend_id}"

    def render(self) -> str:
        lines = [f"{self.frontend_id}. {self.title}", self.statement, "约束："]
        lines.extend(f"- {item}" for item in self.constraints)
        for index, example in enumerate(self.examples, 1):
            lines.extend([f"样例 {index}：{example.input}", f"输出：{example.output}"])
        lines.extend([f"Python 签名：{self.signature}", f"来源说明：{self.attribution}"])
        if self.url:
            lines.append(f"来源链接：{self.url}")
        return "\n".join(lines)


class TeachingReply(StrictModel):
    reply: str = Field(min_length=1, max_length=16000)
    hint_level_used: int = Field(ge=0, le=4)
    stage: Stage
    full_solution: bool


class SolutionSummary(StrictModel):
    approach: str = Field(min_length=1, max_length=4000)
    correctness: str = Field(min_length=1, max_length=4000)
    complexity: str = Field(min_length=1, max_length=2000)
    limitations: str = Field(min_length=1, max_length=2000)


class Verification(StrictModel):
    status: Literal[
        "not_run",
        "syntax_error",
        "passed",
        "wrong_answer",
        "runtime_error",
        "timeout",
        "resource_limit",
        "unsupported",
        "runner_error",
        "cancelled",
    ]
    code_hash: str
    detail: str
    executed: bool = False
    case_count: int = Field(default=0, ge=0)
    passed_count: int = Field(default=0, ge=0)
    suite_version: str | None = None
    adapter_version: str | None = None
    problem_hash: str | None = None
    runner_image: str | None = None
    random_seed: int | None = None
    failure: dict | None = None

    @model_validator(mode="after")
    def check_evidence(self) -> "Verification":
        if self.passed_count > self.case_count:
            raise ValueError("通过用例数不能超过总用例数")
        if self.status in ("wrong_answer", "runtime_error") and not self.executed:
            raise ValueError("判定代码错误必须有执行证据")
        if self.status == "passed" and (
            not self.executed or not self.case_count or self.passed_count != self.case_count
        ):
            raise ValueError("通过状态必须有实际执行的完整本地测试证据")
        return self


class Attempt(StrictModel):
    attempt_id: str
    code: str
    code_hash: str
    created_at: str
    origin: Literal["user"] = "user"
    verification: Verification
    verification_history: list[Verification] = Field(default_factory=list)
