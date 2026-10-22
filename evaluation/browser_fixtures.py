"""Frozen browser reports for teaching evaluation. Never contact the platform."""

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from algo_coach.browser_bridge import BrowserBridge, BrowserReceipt
from algo_coach.models import Problem, StrictModel

FIXTURES_PATH = Path(__file__).with_name("browser_reports.json")


class Provenance(StrictModel):
    kind: Literal["real_browser_evidence_replay", "synthetic_browser_report"]
    scope: str
    recorded_submission_id: str | None = None
    user_confirmed: bool = False


class BrowserFixture(StrictModel):
    code: str = Field(min_length=1, max_length=131072)
    problem: Problem
    receipt: BrowserReceipt
    provenance: Provenance

    @model_validator(mode="after")
    def exact_source(self):
        detail = self.receipt.detail
        if not detail or detail.source_code != self.code:
            raise ValueError("评估原代码与报告代码不一致")
        if detail.frontend_id != self.problem.frontend_id or detail.slug != self.problem.slug:
            raise ValueError("评估题目与报告不一致")
        if self.provenance.kind == "real_browser_evidence_replay" and (
            not self.provenance.user_confirmed
            or self.provenance.recorded_submission_id != self.receipt.submission_id
        ):
            raise ValueError("真实证据回放缺少原记录归属")
        return self


def browser_fixtures():
    return {
        name: BrowserFixture.model_validate(raw)
        for name, raw in json.loads(FIXTURES_PATH.read_text("utf-8")).items()
    }


def apply_browser_fixture(coach, sid, name):
    fixture = browser_fixtures()[name]
    state = coach.save_code(sid, fixture.code)
    bridge = BrowserBridge(coach.connection)
    receipt = bridge.store_receipt(fixture.receipt)
    return coach.bind_browser_result(sid, receipt["receipt_id"], state["code_hash"])
