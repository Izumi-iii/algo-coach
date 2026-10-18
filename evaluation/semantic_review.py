"""Separate, hash-bound AI-assisted review. Never fills or counts human ratings."""

import argparse
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from algo_coach.models import StrictModel
from evaluation.judge import REVIEW_FIELDS
from evaluation.reports import verified_run


class AssistedReview(StrictModel):
    review_kind: Literal["ai_assisted"]
    run_id: str
    case_id: str
    step_index: int = Field(ge=0)
    step_hash: str
    reviewer: str = Field(pattern=r"\S", max_length=120)
    semantic_leakage: Literal["pass", "fail", "uncertain", "na"]
    correctness: Literal["pass", "fail", "uncertain", "na"]
    counterexample_validity: Literal["pass", "fail", "uncertain", "na"]
    pedagogical_progress: Literal["pass", "fail", "uncertain", "na"]
    factual_fidelity: Literal["pass", "fail", "uncertain", "na"]
    evidence_notes: str = Field(pattern=r"\S", max_length=4000)


def model_steps(results):
    return {
        (case["case_id"], step["step_index"]): step
        for case in results["cases"]
        for step in case["steps"]
        if step["model_call_indices"]
    }


def template(run_dir):
    _, results = verified_run(run_dir)
    return [
        {
            "review_kind": "ai_assisted",
            "run_id": results["run_id"],
            "case_id": key[0],
            "step_index": key[1],
            "step_hash": step["step_hash"],
            "reviewer": "",
            **{field: None for field in REVIEW_FIELDS},
            "evidence_notes": "",
        }
        for key, step in model_steps(results).items()
    ]


def report(run_dir, reviews_path):
    _, results = verified_run(run_dir)
    expected, reviewed = model_steps(results), {}
    for line in Path(reviews_path).read_text("utf-8-sig").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if all(raw.get(field) is None for field in REVIEW_FIELDS) and not raw.get("reviewer"):
            continue
        review = AssistedReview.model_validate(raw)
        key = (review.case_id, review.step_index)
        if key in reviewed:
            raise ValueError("AI 辅助复核记录重复")
        if (
            review.run_id != results["run_id"]
            or key not in expected
            or review.step_hash != expected[key]["step_hash"]
        ):
            raise ValueError("辅助复核的运行、步骤或输出版本不匹配")
        reviewed[key] = review.model_dump()
    rows = list(reviewed.values())
    return {
        "review_kind": "ai_assisted",
        "run_id": results["run_id"],
        "model_steps": len(expected),
        "ai_reviewed_steps": len(rows),
        "ai_pending_steps": len(expected) - len(rows),
        "ai_flagged_fail_steps": sum(any(row[f] == "fail" for f in REVIEW_FIELDS) for row in rows),
        "ai_uncertain_steps": sum(
            any(row[f] == "uncertain" for f in REVIEW_FIELDS) for row in rows
        ),
        "dimensions": {
            f: {v: sum(row[f] == v for row in rows) for v in ("pass", "fail", "uncertain", "na")}
            for f in REVIEW_FIELDS
        },
        "findings": [
            row for row in rows if any(row[f] in {"fail", "uncertain"} for f in REVIEW_FIELDS)
        ],
        "human_reviews_added": 0,
        "quality_status": "ai_assisted_findings_require_human_review",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线辅助语义复核；不调用模型，不填写人工评分")
    sub = parser.add_subparsers(dest="action", required=True)
    item = sub.add_parser("template")
    item.add_argument("run_dir", type=Path)
    item.add_argument("--output", type=Path, required=True)
    item = sub.add_parser("report")
    item.add_argument("run_dir", type=Path)
    item.add_argument("--reviews", type=Path, required=True)
    item.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = (
            template(args.run_dir)
            if args.action == "template"
            else report(args.run_dir, args.reviews)
        )
        # Do not overwrite another review/report by accident.
        with args.output.open("x", encoding="utf-8") as target:
            if args.action == "template":
                for row in value:
                    target.write(json.dumps(row, ensure_ascii=False) + "\n")
            else:
                target.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
        print("已写入：" + str(args.output))
        return 0
    except (ValueError, OSError) as exc:
        print("辅助复核未完成：" + type(exc).__name__)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
