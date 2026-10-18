import copy
import json

import pytest
from pydantic import ValidationError
from test_teaching_evaluation import fixture_run

from evaluation.cases import load_cases
from evaluation.judge import REVIEW_FIELDS, HumanReview
from evaluation.reports import compare
from evaluation.semantic_review import AssistedReview, report, template


@pytest.fixture
def review_run(tmp_path):
    folder, _ = fixture_run(tmp_path, ids=["clarify-indices"])
    row = template(folder)[0]
    row.update(reviewer="test AI reviewer", evidence_notes="Controlled fixture, not model quality.")
    row.update({field: "pass" for field in REVIEW_FIELDS})
    path = tmp_path / "assisted.jsonl"
    path.write_text(json.dumps(row) + "\n", "utf-8")
    return folder, path, row


def test_ai_review_does_not_change_human_reports(review_run):
    folder, path, _ = review_run
    original = {name: (folder / name).read_bytes() for name in ("summary.json", "report.md")}
    summary = report(folder, path)
    assert summary["ai_reviewed_steps"] == 1
    assert summary["ai_pending_steps"] == 1
    assert summary["human_reviews_added"] == 0
    assert summary["quality_status"] == "ai_assisted_findings_require_human_review"
    assert original == {name: (folder / name).read_bytes() for name in original}


def test_ai_reviews_cannot_be_imported_as_human_ratings(review_run):
    _, _, row = review_run
    with pytest.raises(ValidationError):
        HumanReview.model_validate(row)


@pytest.mark.parametrize("change", ["run", "hash", "non_model", "duplicate", "modified_output"])
def test_assisted_review_requires_original_run_step_and_calls(review_run, change):
    folder, path, row = review_run
    if change == "run":
        row["run_id"] = "other"
    elif change == "hash":
        row["step_hash"] = "other"
    elif change == "non_model":
        row["step_index"] = 999
    elif change == "modified_output":
        result = json.loads((folder / "results.json").read_text("utf-8"))
        result["calls"][0]["output"]["parsed"]["reply"] = "altered"
        (folder / "results.json").write_text(json.dumps(result), "utf-8")
    path.write_text(
        json.dumps(row) + "\n" + (json.dumps(row) if change == "duplicate" else ""), "utf-8"
    )
    with pytest.raises(ValueError):
        report(folder, path)


def test_ai_review_retains_uncertainty_and_inapplicable_dimensions(review_run):
    folder, path, row = review_run
    row.update(correctness="fail", semantic_leakage="uncertain", counterexample_validity="na")
    path.write_text(json.dumps(row), "utf-8")
    summary = report(folder, path)
    assert summary["ai_flagged_fail_steps"] == summary["ai_uncertain_steps"] == 1
    assert summary["dimensions"]["counterexample_validity"]["na"] == 1


def test_all_na_and_blank_templates_never_become_a_quality_pass(review_run):
    folder, path, row = review_run
    row.update({field: "na" for field in REVIEW_FIELDS})
    path.write_text(json.dumps(row), "utf-8")
    assert report(folder, path)["quality_status"] == "ai_assisted_findings_require_human_review"
    blank = template(folder)[0]
    path.write_text(json.dumps(blank), "utf-8")
    assert report(folder, path)["ai_reviewed_steps"] == 0
    invalid = copy.deepcopy(row)
    invalid["reviewer"] = "  "
    with pytest.raises(ValidationError):
        AssistedReview.model_validate(invalid)


def test_compare_rejects_tampered_calls_as_well_as_render(review_run):
    folder, _, _ = review_run
    result = json.loads((folder / "results.json").read_text("utf-8"))
    result["calls"][0]["output"]["parsed"]["reply"] = "altered"
    (folder / "results.json").write_text(json.dumps(result), "utf-8")
    with pytest.raises(ValueError):
        compare(folder, folder)


def test_first_pass_challenge_is_separate_and_explicitly_held_out():
    from pathlib import Path

    original, _ = load_cases()
    challenge, _ = load_cases(Path(__file__).parents[1] / "evaluation/challenge_cases_v2.jsonl")
    assert len(challenge) == 12
    assert all(case.split == "holdout" and len(case.steps) >= 2 for case in challenge)
    assert not {case.id for case in challenge} & {case.id for case in original}
    assert sum(step.needs_model() for case in challenge for step in case.steps) == 28
