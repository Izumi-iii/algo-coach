"""Run fixed teaching scenarios through the real application; no model fallback."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from dotenv import load_dotenv

from algo_coach.adapters import problem_hash
from algo_coach.errors import teaching_failure
from algo_coach.problems import LocalProblems
from algo_coach.runner import DockerRunner
from algo_coach.service import Coach
from algo_coach.teaching import SYSTEM_PROMPT, ModelTeacher
from algo_coach.verification import VerificationService
from evaluation.browser_fixtures import apply_browser_fixture, browser_fixtures
from evaluation.cases import DEFAULT_CASES, fingerprint, load_cases, problem_for, select_cases
from evaluation.fixtures import CODE, GROUND_TRUTH, ROOT, check_counterexample
from evaluation.judge import check_step, facts, review_template
from evaluation.recording import Budget, RecordingTeacher
from evaluation.reports import compare, render

FATAL_SERVICE_ERRORS = {
    "authentication_failed",
    "insufficient_balance",
    "permission_denied",
    "model_or_endpoint_not_found",
    "rate_limited",
    "request_rejected",
    "model_timeout",
    "model_service_error",
}


class Writer:
    def __init__(self, folder, secret=""):
        self.folder, self.secret = folder, secret

    def clean(self, value):
        text = json.dumps(value, ensure_ascii=False)
        if self.secret and len(self.secret) > 8:
            text = text.replace(self.secret, "[REDACTED_API_KEY]")
        return json.loads(text)

    def json(self, name, value):
        temporary = self.folder / (name + ".tmp")
        temporary.write_text(
            json.dumps(self.clean(value), ensure_ascii=False, indent=2) + "\n", "utf-8"
        )
        temporary.replace(self.folder / name)

    def line(self, name, value):
        with (self.folder / name).open("a", encoding="utf-8") as target:
            target.write(json.dumps(self.clean(value), ensure_ascii=False) + "\n")
            target.flush()


def source_files():
    paths = sorted((ROOT / "src/algo_coach").rglob("*.py")) + sorted(
        (ROOT / "evaluation").glob("*.py")
    )
    return {str(p.relative_to(ROOT)): p.read_text("utf-8") for p in paths}


def source_fingerprint():
    return fingerprint({name: fingerprint(content) for name, content in source_files().items()})


def runtime_versions():
    versions = {"python": sys.version}
    for package in ("langchain", "langchain-core", "langchain-openai", "langgraph", "pydantic"):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = "not_installed"
    return versions


def print_progress(message):
    print(message, flush=True)


def model_config():
    endpoint = urlparse(os.getenv("ALGOCOACH_BASE_URL", ""))
    return {
        "requested_model": os.getenv("ALGOCOACH_MODEL", ""),
        "provider": os.getenv("ALGOCOACH_MODEL_PROVIDER", "openai"),
        "endpoint_origin": f"{endpoint.scheme}://{endpoint.hostname}"
        if endpoint.hostname
        else "provider_default",
        "temperature": "provider_default",
        "sdk_max_retries": 0,
        "timeout_seconds": 45,
        "thinking": "disabled" if endpoint.hostname == "api.deepseek.com" else "provider_default",
    }


def execute_step(coach, sid, step, case_dir):
    operation = step.operation
    artifact, output = None, None
    if operation == "say":
        state = coach.turn(sid, step.text)
        output = state["response"]
    elif operation == "code":
        state = coach.turn(
            sid, "请分析这段提交的代码，不要替我改写完整答案。", code=CODE[step.fixture]
        )
        output = state["response"]
    elif operation == "save_code":
        state = coach.save_code(sid, CODE[step.fixture])
        output = "已保存代码版本；未调用模型、未执行代码。"
    elif operation == "browser_report":
        state = apply_browser_fixture(coach, sid, step.fixture)
        output = "评估证据已回放并绑定；无平台请求。"
    elif operation == "test":
        state = coach.test(sid)
        output = state["response"]
    elif operation == "note":
        state = coach.note(sid, step.text)
    elif operation == "explain":
        state = coach.explain(sid, step.text)
    elif operation == "complete":
        state = coach.complete(sid, step.text)
    elif operation == "finish":
        state = coach.turn(sid, "/finish")
        output = state["response"]
    elif operation == "summarize":
        state = coach.summarize(sid)
        output = json.dumps(state["solution_summary"]["content"], ensure_ascii=False)
    elif operation == "archive":
        # Exercise the application archive policy outside the source repository.
        # Retain only a run-owned copy after checking actual publication and idempotence.
        with tempfile.TemporaryDirectory(prefix="algo-coach-eval-archive-") as temporary:
            practice = Path(temporary) / "practice"
            practice.mkdir()
            plan = coach.preview_archive(sid, practice)
            result = coach.apply_archive(sid)
            repeated = coach.apply_archive(sid)
            if repeated["status"] != "unchanged" or repeated["path"] != result["path"]:
                raise ValueError("归档重复执行没有保持幂等")
            destination = case_dir / "artifacts" / plan.plan_id
            destination.parent.mkdir(exist_ok=True)
            shutil.copytree(result["path"], destination)
            artifact = (destination, plan)
        state = coach.get(sid)
        output = "归档已实际写入；重复应用返回 unchanged。"
    else:
        raise ValueError("resume 由运行器管理")
    return state, output, artifact


def run(
    cases,
    *,
    suite_hash,
    dataset_size,
    output_root,
    max_calls=12,
    max_seconds=300,
    docker=False,
    teacher_factory=ModelTeacher,
    backend="real_model",
    verifier=None,
    progress=print_progress,
):
    if backend not in {"real_model", "test_fixture"}:
        raise ValueError("未知后端")
    if max_calls < 1 or max_seconds < 1:
        raise ValueError("评估预算必须为正数")
    if any(case.needs_docker() for case in cases) and not docker:
        raise ValueError("所选场景包含动态测试，需显式 --docker；不能把未执行当通过")
    # /answer also verifies generated code, even when no step explicitly invokes /test.
    if docker and backend == "real_model":
        runner = DockerRunner(
            enabled=True, image=os.getenv("ALGOCOACH_RUNNER_IMAGE", "algo-coach-runner:0.1")
        )
        ready = runner.doctor()
        if not ready["ready"]:
            raise ValueError("Docker 或执行镜像不可用，请先完成 doctor 检查")
        verifier = VerificationService(runner)
    if verifier is None:
        # Evaluation flags govern execution even if the user's CLI enables Docker in .env.
        verifier = VerificationService(DockerRunner(enabled=False))
    delegate = teacher_factory()
    if backend == "real_model" and not isinstance(delegate, ModelTeacher):
        raise ValueError("真实后端必须使用 ModelTeacher，不接受演示或规则替代")
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    folder = Path(output_root).resolve() / run_id
    folder.mkdir(parents=True, exist_ok=False)
    writer = Writer(folder, os.getenv("ALGOCOACH_API_KEY", ""))
    (folder / "calls.jsonl").touch()
    (folder / "reviews.template.jsonl").touch()
    budget = Budget(max_calls, max_seconds)
    teacher = RecordingTeacher(delegate, budget, lambda event: writer.line("calls.jsonl", event))
    problems = {}
    catalogue = LocalProblems(folder / "frozen-catalogue")
    for case in cases:
        problems[case.id] = problem_for(case, catalogue)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "backend": backend,
        "started_at": datetime.now(UTC).isoformat(),
        "suite_hash": suite_hash,
        "selected_case_ids": [case.id for case in cases],
        "dataset_cases": dataset_size,
        "case_hashes": {case.id: fingerprint(case.model_dump()) for case in cases},
        "fixture_hashes": {name: fingerprint(code) for name, code in CODE.items()},
        "browser_fixture_hashes": {
            name: fingerprint(f.model_dump(mode="json")) for name, f in browser_fixtures().items()
        },
        "problem_hashes": {key: problem_hash(p) for key, p in problems.items()},
        "prompt_hash": fingerprint(SYSTEM_PROMPT),
        "source_hash": source_fingerprint(),
        "runtime_versions": runtime_versions(),
        "git_revision": revision.stdout.strip() if revision.returncode == 0 else "unknown",
        "git_worktree_dirty": bool(dirty.stdout.strip()),
        "model_config": model_config(),
        "budget": {"model_invocations": max_calls, "wall_seconds": max_seconds},
        "docker_enabled": docker,
        "cases": [case.model_dump(mode="json") for case in cases],
    }
    writer.json("manifest.json", manifest)
    writer.json(
        "source.snapshot.json", {"source_hash": manifest["source_hash"], "files": source_files()}
    )
    writer.json(
        "fixtures.snapshot.json",
        {
            "code": CODE,
            "known_defects": GROUND_TRUTH,
            "browser_reports": {
                name: f.model_dump(mode="json") for name, f in browser_fixtures().items()
            },
        },
    )
    writer.json("problems.snapshot.json", {key: p.model_dump() for key, p in problems.items()})
    results = {
        "run_id": run_id,
        "dataset_cases": dataset_size,
        "status": "running",
        "calls": [],
        "cases": [
            {
                "case_id": case.id,
                "category": case.category,
                "split": case.split,
                "status": "not_run",
                "planned_steps": len(case.steps),
                "steps": [],
            }
            for case in cases
        ],
    }
    review_lines = [
        "# 逐轮人工复核",
        "",
        "这是固定模拟用户场景，不代表真实学习效果。",
        "请同时查看可见回复、状态证据及被拦截的模型尝试。不要仅凭模型自报提示等级评分。",
        "",
    ]
    abort_reason = None
    started = time.monotonic()
    writer.json("results.json", results)
    try:
        for case, entry in zip(cases, results["cases"], strict=True):
            if abort_reason:
                entry["status"] = "not_run:" + abort_reason
                continue
            case_dir = folder / "sessions" / case.id
            case_dir.mkdir(parents=True)
            coach = Coach(case_dir / "state", teacher, verifier=verifier)
            try:
                sid = coach.start(problems[case.id])["session_id"]
                entry["session_id"] = sid
                entry["status"] = "running"
                progress(f"{case.id}: started")
                for index, step in enumerate(case.steps):
                    # Reserve both possible application attempts before a model-dependent step.
                    if not budget.available(2 if step.needs_model() else 0):
                        abort_reason = "budget_exhausted"
                        entry["status"] = "incomplete:budget_exhausted"
                        break
                    teacher.context = {"case_id": case.id, "step_index": index}
                    before = facts(coach.get(sid))
                    calls_before = len(teacher.events)
                    tick = time.monotonic()
                    error, output, artifact = None, None, None
                    try:
                        if step.operation == "resume":
                            coach.__exit__()
                            coach = None
                            coach = Coach(case_dir / "state", teacher, verifier=verifier)
                        else:
                            _, output, artifact = execute_step(coach, sid, step, case_dir)
                    except ValueError as exc:
                        # Application errors are controlled; provider bodies are never printed.
                        error = str(exc)[:1000]
                    after = facts(coach.get(sid))
                    failures = check_step(step, before, after, error=error, artifact=artifact)
                    row = writer.clean(
                        {
                            "step_index": index,
                            "operation": step.operation,
                            "input": step.model_dump(mode="json"),
                            "response": output,
                            "before": before,
                            "after": after,
                            "action_error": error,
                            "failures": failures,
                            "elapsed_ms": round((time.monotonic() - tick) * 1000, 3),
                            "model_call_indices": [
                                e["call_index"] for e in teacher.events[calls_before:]
                            ],
                            "artifact": str(artifact[0].relative_to(folder)) if artifact else None,
                            "calls_hash": fingerprint(writer.clean(teacher.events[calls_before:])),
                        }
                    )
                    row["step_hash"] = fingerprint(row)
                    entry["steps"].append(row)
                    if row["model_call_indices"]:
                        writer.line("reviews.template.jsonl", review_template(run_id, case, row))
                        review_lines += [
                            f"## {case.id} / step {index}",
                            "",
                            "判定要点：",
                            "",
                            *["- " + text for text in case.rubric],
                            "",
                            "本轮输入与可见回复：",
                            "",
                            "```json",
                            json.dumps(
                                {"input": row["input"], "response": row["response"]},
                                ensure_ascii=False,
                            ),
                            "```",
                            "",
                            f"模型尝试编号：{row['model_call_indices']}；步骤哈希：`{row['step_hash']}`。",
                            "",
                            "状态证据与完整上下文分别见 results.json、calls.jsonl。",
                            "",
                        ]
                    results["calls"] = teacher.events
                    writer.json("results.json", results)
                    progress(
                        f"{case.id}: step {index + 1}/{len(case.steps)}, "
                        f"calls={budget.calls}, contract_failures={len(failures)}"
                    )
                    if budget.exhausted:
                        abort_reason = "budget_exhausted"
                    elif after.get("last_error") in FATAL_SERVICE_ERRORS:
                        abort_reason = "provider_error"
                    if abort_reason:
                        entry["status"] = "incomplete:" + abort_reason
                        break
                else:
                    entry["status"] = "completed"
            finally:
                if coach:
                    coach.__exit__()
    except KeyboardInterrupt:
        abort_reason = "interrupted"
    except Exception as exc:
        abort_reason = "harness_error: " + type(exc).__name__
        results["harness_error_category"] = teaching_failure(exc)[0]
    finally:
        for entry in results["cases"]:
            if entry["status"] in {"not_run", "running"}:
                entry["status"] = "incomplete:" + (abort_reason or "unknown")
        results["status"] = abort_reason or "completed"
        results["calls"] = teacher.events
        results["elapsed_seconds"] = round(time.monotonic() - started, 3)
        writer.json("results.json", results)
        (folder / "review.md").write_text("\n".join(review_lines) + "\n", "utf-8")
    summary = render(folder)
    return folder, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="AlgoCoach 固定场景真实模型教学评估")
    actions = parser.add_subparsers(dest="action", required=True)
    validate = actions.add_parser("validate", help="离线校验场景，不调用模型")
    validate.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    item = actions.add_parser("run", help="调用已配置的真实模型，不提供 demo 降级")
    item.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    item.add_argument("--profile", choices=["smoke", "full"], default="smoke")
    item.add_argument("--split", choices=["all", "dev", "holdout"], default="all")
    item.add_argument("--ids", nargs="+")
    item.add_argument("--output-root", type=Path, default=Path(".algo-coach/evaluations"))
    item.add_argument("--max-model-calls", type=int, default=12)
    item.add_argument("--max-seconds", type=int, default=300)
    item.add_argument("--docker", action="store_true")
    item = actions.add_parser("report", help="离线重建报告，可附人工评分")
    item.add_argument("run_dir", type=Path)
    item.add_argument("--reviews", type=Path)
    item = actions.add_parser("compare", help="比较同一组场景的两次运行")
    item.add_argument("first", type=Path)
    item.add_argument("second", type=Path)
    item = actions.add_parser("counterexample", help="独立核对反例的输入及期望，不执行候选代码")
    item.add_argument("problem", choices=["1", "25"])
    item.add_argument("--inputs", required=True)
    item.add_argument("--expected", required=True)
    args = parser.parse_args(argv)
    try:
        if args.action == "validate":
            cases, digest = load_cases(args.cases)
            for item in GROUND_TRUTH.values():
                if not check_counterexample(item["problem"], item["inputs"], item["expected"])[
                    "expected_valid"
                ]:
                    raise ValueError("预置反例的输入或期望不符合可信参考判定")
            print(
                json.dumps(
                    {
                        "cases": len(cases),
                        "suite_hash": digest,
                        "dev": sum(c.split == "dev" for c in cases),
                        "holdout": sum(c.split == "holdout" for c in cases),
                        "smoke": sum(c.smoke for c in cases),
                        "steps": sum(len(c.steps) for c in cases),
                        "planned_model_turns": sum(s.needs_model() for c in cases for s in c.steps),
                        "status": "dataset_validated_not_model_evaluated",
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        elif args.action == "run":
            load_dotenv(Path.cwd() / ".env", override=False)
            cases, digest = load_cases(args.cases)
            selected = select_cases(cases, args.profile, args.split, args.ids)
            folder, summary = run(
                selected,
                suite_hash=digest,
                dataset_size=len(cases),
                output_root=args.output_root,
                max_calls=args.max_model_calls,
                max_seconds=args.max_seconds,
                docker=args.docker,
            )
            print(
                json.dumps(
                    {"run_dir": str(folder), "summary": summary}, ensure_ascii=False, indent=2
                )
            )
            return (
                0 if summary["incomplete_cases"] == 0 and summary["contract_fail_cases"] == 0 else 1
            )
        elif args.action == "report":
            print(
                json.dumps(
                    render(args.run_dir, reviews_path=args.reviews), ensure_ascii=False, indent=2
                )
            )
        elif args.action == "compare":
            print(json.dumps(compare(args.first, args.second), ensure_ascii=False, indent=2))
        else:
            result = check_counterexample(
                args.problem, json.loads(args.inputs), json.loads(args.expected)
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result["expected_valid"] else 1
        return 0
    except (ValueError, OSError) as exc:
        # Never print arbitrary exception payloads from providers or validation contexts.
        message = str(exc) if type(exc) is ValueError else type(exc).__name__
        print("评估未完成：" + message)
        return 2


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
