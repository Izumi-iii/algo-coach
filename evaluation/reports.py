"""Re-render local reports and attach version-bound, explicit human reviews."""

import json
import math
from pathlib import Path

from evaluation.cases import fingerprint
from evaluation.judge import REVIEW_FIELDS, HumanReview


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(p * len(ordered)) - 1)], 3)


def metrics(calls):
    fields = {"input_tokens": [], "output_tokens": [], "total_tokens": []}
    for call in calls:
        usage = (call.get("output") or {}).get("usage") or {}
        for key in fields:
            value = usage.get(key)
            if type(value) is int and value >= 0:
                fields[key].append(value)
    return {
        "model_invocations": len(calls),
        "known_token_sums": {
            key: sum(values) if values else None for key, values in fields.items()
        },
        "token_coverage": {
            key: {"known_calls": len(values), "all_calls": len(calls)}
            for key, values in fields.items()
        },
        "model_latency_ms": {
            "p50": percentile([c["elapsed_ms"] for c in calls], 0.5),
            "p95": percentile([c["elapsed_ms"] for c in calls], 0.95),
        },
        "policy_rejected_attempts": sum(
            c.get("policy_result") == "reply_policy_rejected"
            or c.get("adapter_error") == "reply_policy_rejected"
            for c in calls
        ),
        "adapter_error_attempts": sum(c.get("adapter_error") is not None for c in calls),
        "cost": "unknown",
        "cost_reason": "未配置可靠的供应商定价与缓存计费规则，不能按零成本计算",
    }


def read_reviews(path, results):
    expected = {
        (case["case_id"], step["step_index"]): step
        for case in results["cases"]
        for step in case["steps"]
    }
    reviews = {}
    if not path:
        return reviews
    for line in Path(path).read_text("utf-8-sig").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        if all(raw.get(key) is None for key in REVIEW_FIELDS) and not raw.get("reviewer"):
            continue
        review = HumanReview.model_validate(raw)
        key = (review.case_id, review.step_index)
        if key in reviews:
            raise ValueError("人工复核记录重复")
        step = expected.get(key)
        if (
            not step
            or review.run_id != results["run_id"]
            or review.step_hash != step["step_hash"]
            or not step["model_call_indices"]
        ):
            raise ValueError("人工复核与本次运行、步骤或输出哈希不匹配")
        reviews[key] = review.model_dump()
    return reviews


def summarize(results, reviews):
    model_steps = [
        (case["case_id"], step)
        for case in results["cases"]
        for step in case["steps"]
        if step["model_call_indices"]
    ]
    failed = [
        key
        for key, review in reviews.items()
        if any(review[field] == "fail" for field in REVIEW_FIELDS)
    ]
    completed = [case for case in results["cases"] if case["status"] == "completed"]
    contract_pass = [
        case for case in completed if not any(step["failures"] for step in case["steps"])
    ]
    return {
        **metrics(results["calls"]),
        "dataset_cases": results["dataset_cases"],
        "selected_cases": len(results["cases"]),
        "completed_cases": len(completed),
        "contract_pass_cases": len(contract_pass),
        "contract_fail_cases": len(completed) - len(contract_pass),
        "incomplete_cases": len(results["cases"]) - len(completed),
        "planned_steps": sum(case["planned_steps"] for case in results["cases"]),
        "executed_steps": sum(len(case["steps"]) for case in results["cases"]),
        "contract_pass_rate_selected": len(contract_pass) / len(results["cases"]),
        "model_steps_for_review": len(model_steps),
        "reviewed_steps": len(reviews),
        "human_fail_steps": len(failed),
        "human_pending_steps": len(model_steps) - len(reviews),
        "teaching_quality": (
            "human_review_failed"
            if failed
            else "pending_human_review"
            if len(reviews) < len(model_steps) or not model_steps
            else "reviewed_sample_only"
        ),
        "run_status": results["status"],
    }


def verified_run(run_dir):
    """Read and validate frozen step/call bindings without changing any reports."""
    folder = Path(run_dir)
    manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
    results = json.loads((folder / "results.json").read_text("utf-8"))
    for case in results["cases"]:
        for step in case["steps"]:
            if (
                fingerprint({k: v for k, v in step.items() if k != "step_hash"})
                != step["step_hash"]
            ):
                raise ValueError("步骤报告内容已改变，不能沿用旧人工评分")
            calls = [
                call
                for call in results["calls"]
                if call["call_index"] in step["model_call_indices"]
            ]
            if fingerprint(calls) != step["calls_hash"]:
                raise ValueError("模型调用或结构化输出已改变，不能沿用旧人工评分")
    return manifest, results


def render(run_dir, *, reviews_path=None):
    folder = Path(run_dir)
    manifest, results = verified_run(folder)
    reviews = read_reviews(reviews_path, results)
    summary = summarize(results, reviews)
    summary["review_file"] = str(Path(reviews_path).resolve()) if reviews_path else None
    (folder / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", "utf-8"
    )
    lines = [
        "# AlgoCoach 教学评估报告",
        "",
        f"运行：`{results['run_id']}`；后端：`{manifest['backend']}`。",
        f"场景集：`{manifest['suite_hash']}`；状态：`{results['status']}`。",
        "",
        f"完整场景 {summary['completed_cases']}/{summary['selected_cases']}；"
        f"数据集共 {summary['dataset_cases']} 个。",
        f"自动契约通过 {summary['contract_pass_cases']}，失败 {summary['contract_fail_cases']}，"
        f"未完成 {summary['incomplete_cases']}。",
        "自动契约结果不是教学质量分数；原始模型声明不是独立正确性证明。",
        "",
        f"人工复核：{summary['reviewed_steps']}/{summary['model_steps_for_review']}；状态：`{summary['teaching_quality']}`。",
        f"模型 invoke 调用 {summary['model_invocations']}；成本 unknown（缺少可靠计费配置）。",
        f"已知 token 小计：`{summary['known_token_sums']}`；覆盖率见 summary.json。",
        f"模型耗时 ms：`{summary['model_latency_ms']}`；重试和被拦截尝试均计入。",
        "",
        "| 场景 | 分类 | 完成状态 | 自动契约失败 |",
        "| --- | --- | --- | --- |",
    ]
    for case in results["cases"]:
        failures = sorted({failure for step in case["steps"] for failure in step["failures"]})
        lines.append(
            f"| {case['case_id']} | {case['category']} | {case['status']} | "
            f"{', '.join(failures) or '无'} |"
        )
    lines += [
        "",
        "完整输入/模型尝试见 calls.jsonl；步骤与证据见 results.json；逐轮阅读见 review.md。",
        "在 reviews.template.jsonl 的副本中人工评分后，"
        "使用 report --reviews 绑定本次输出重建报告。",
        "未运行场景、缺少模型用量和未复核质量均不会自动计为通过。",
    ]
    (folder / "report.md").write_text("\n".join(lines) + "\n", "utf-8")
    return summary


def compare(first, second):
    ma, ra = verified_run(first)
    mb, rb = verified_run(second)
    if (
        ma["suite_hash"] != mb["suite_hash"]
        or ma["fixture_hashes"] != mb["fixture_hashes"]
        or ma["problem_hashes"] != mb["problem_hashes"]
        or [c["case_id"] for c in ra["cases"]] != [c["case_id"] for c in rb["cases"]]
        or ma["backend"] != mb["backend"]
    ):
        raise ValueError("场景集、题目、筛选或后端不同，不能作为同一批场景比较")
    sa, sb = summarize(ra, {}), summarize(rb, {})
    return {
        "first_run": ra["run_id"],
        "second_run": rb["run_id"],
        "configuration_changes": {
            key: [ma.get(key), mb.get(key)]
            for key in ("model_config", "prompt_hash", "source_hash", "git_revision")
            if ma.get(key) != mb.get(key)
        },
        "first": sa,
        "second": sb,
        "interpretation": "仅比较同场景自动契约和资源数据；未附人工复核，不推断教学质量提升",
    }
