import json

import pytest

from algo_coach.browser_bridge import current_browser_feedback
from algo_coach.errors import TeachingPolicyError
from algo_coach.models import TeachingReply
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher, ModelTeacher
from algo_coach.teaching_evidence import browser_failure_facts, check_evidence, enforce_evidence
from evaluation.browser_fixtures import BrowserFixture, apply_browser_fixture, browser_fixtures
from evaluation.cases import load_cases
from evaluation.runner import run


class NoTeacher(DemoTeacher):
    def reply(self, state):
        raise AssertionError("Saving/replaying must not call a teacher")


def test_report_sample_arithmetic_and_literal_registration(tmp_path):
    fixture = browser_fixtures()["fixture-wa"]
    with Coach(tmp_path, NoTeacher()) as coach:
        sid = coach.start(fixture.problem)["session_id"]
        state = apply_browser_fixture(coach, sid, "fixture-wa")
        facts = browser_failure_facts(state)
        assert facts["selected_values"] == [2, 2]
        assert facts["selected_sum"] == 4
        assert facts["distinct_indices"] is False
        assert facts["sum_matches_target"] is False
        draft = TeachingReply(
            reply="浏览器回传报告 nums=[2,7], target=9；重复下标。",
            hint_level_used=1,
            stage="debug",
            full_solution=False,
        )
        enforce_evidence(draft, check_evidence(draft, state))
        unrelated = draft.model_copy(update={"reply": "nums=[2,7], target=10"})
        with pytest.raises(TeachingPolicyError):
            enforce_evidence(unrelated, check_evidence(unrelated, state))
        changed = coach.save_code(sid, fixture.code + "\n")
        assert browser_failure_facts(changed) is None
        with pytest.raises(TeachingPolicyError):
            enforce_evidence(draft, check_evidence(draft, changed))
        state["browser_reports"][-1]["failure_details"]["expected_output"] = "[0,0]"
        assert browser_failure_facts(state) is None
        state["browser_reports"][-1]["failure_details"]["last_testcase"] = (
            "[" * 1100 + "0" + "]" * 1100 + "\n9"
        )
        assert browser_failure_facts(state) is None


def test_saving_and_replaying_do_not_call_model_or_execute_code(tmp_path):
    fixture = browser_fixtures()["live-ac"]
    with Coach(tmp_path, NoTeacher()) as coach:
        sid = coach.start(fixture.problem)["session_id"]
        bound = apply_browser_fixture(coach, sid, "live-ac")
        assert current_browser_feedback(bound)["status_code"] == 10
        assert bound["verification"]["executed"] is False
        changed = coach.save_code(sid, fixture.code + "\n")
        assert current_browser_feedback(changed) is None
        assert len(changed["browser_reports"]) == 1
        assert len(changed["attempts"]) == 2
        with pytest.raises(ValueError):
            coach.save_code(sid, " ")


def test_browser_fixture_rejects_unrelated_source():
    raw = browser_fixtures()["fixture-wa"].model_dump()
    raw["code"] += "\n"
    with pytest.raises(ValueError):
        BrowserFixture.model_validate(raw)


def test_runtime_report_input_is_citable_without_inventing_report_output(tmp_path):
    fixture = browser_fixtures()["fixture-runtime"]
    with Coach(tmp_path, NoTeacher()) as coach:
        sid = coach.start(fixture.problem)["session_id"]
        state = apply_browser_fixture(coach, sid, "fixture-runtime")
        facts = browser_failure_facts(state)
        assert facts["expected_source"] == "application_oracle"
        assert facts["expected"] == [0, 1]
        for field in ("reported_output", "indices_in_bounds", "selected_values", "selected_sum"):
            assert facts[field] is None
        draft = TeachingReply(
            reply="浏览器回传报告 nums=[2,7], target=9，长度为2；未提供实际返回值。",
            hint_level_used=1,
            stage="debug",
            full_solution=False,
        )
        enforce_evidence(draft, check_evidence(draft, state))
        state["browser_reports"][-1]["failure_details"]["last_testcase"] = "[1,2]\n9"
        assert browser_failure_facts(state) is None


@pytest.mark.parametrize("version,planned_calls", [(7, 6), (8, 11)])
def test_new_suite_runs_through_real_adapter_with_fixture_model(tmp_path, version, planned_calls):
    class Structured:
        def invoke(self, messages):
            context = json.loads(messages[1].content)
            browser = context["browser_feedback"]
            if context["diagnostic_focus"] and browser["status_code"] in {15, 20}:
                assert "本轮错误诊断聚焦" in messages[-1].content
            elif browser and browser["status_code"] in {15, 20}:
                assert "本轮错误诊断聚焦" not in messages[-1].content
            if browser and browser["status_code"] == 11:
                assert context["browser_failure_facts"]["selected_sum"] == 4
            if not browser and context["hint_level"] == 0:
                assert "H0任务范围" in messages[-1].content
            return {
                "parsed": TeachingReply(
                    reply="评估占位回复，不进行语义验收。",
                    hint_level_used=context["hint_level"],
                    stage="debug",
                    full_solution=False,
                )
            }

    def factory():
        teacher = ModelTeacher.__new__(ModelTeacher)
        teacher.model = Structured()
        teacher.summary_model = Structured()
        teacher.last_usage = None
        return teacher

    cases, digest = load_cases(f"evaluation/browser_cases_v{version}.jsonl")
    folder, summary = run(
        cases,
        suite_hash=digest,
        dataset_size=len(cases),
        output_root=tmp_path,
        max_calls=12,
        max_seconds=60,
        teacher_factory=factory,
        backend="test_fixture",
        progress=lambda _: None,
    )
    assert summary["contract_fail_cases"] == 0
    assert summary["incomplete_cases"] == 0
    results = json.loads((folder / "results.json").read_text("utf-8"))
    assert len(results["calls"]) == planned_calls
    stale = next(c for c in results["cases"] if c["case_id"] == "browser-stale-result-isolation")
    final = stale["steps"][-1]["after"]
    assert final["browser_feedback"] is None and final["browser_report_count"] == 1
    snapshot = json.loads((folder / "fixtures.snapshot.json").read_text("utf-8"))
    assert (
        snapshot["browser_reports"]["live-ac"]["provenance"]["kind"]
        == "real_browser_evidence_replay"
    )
    assert (
        snapshot["browser_reports"]["fixture-wa"]["provenance"]["kind"]
        == "synthetic_browser_report"
    )
