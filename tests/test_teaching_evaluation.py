"""The harness is tested with explicitly labeled fixtures, not a real-model baseline."""

import copy
import json
import os
import subprocess
import sys

import pytest
from langchain_core.messages import AIMessage

from algo_coach.models import SolutionSummary, TeachingReply, Verification
from algo_coach.teaching import ModelTeacher
from algo_coach.verification import inspect_code
from evaluation.cases import Case, load_cases, select_cases
from evaluation.fixtures import CODE, GROUND_TRUTH, ROOT, check_counterexample
from evaluation.judge import REVIEW_FIELDS
from evaluation.recording import visible_result
from evaluation.reports import compare, metrics, render
from evaluation.runner import run


class StubStructured:
    def __init__(self, mode="normal", summary=False):
        self.mode, self.summary, self.calls = mode, summary, 0

    def invoke(self, messages):
        self.calls += 1
        if self.mode == "auth":
            error = RuntimeError("PRIVATE PROVIDER RESPONSE")
            error.status_code = 401
            raise error
        usage = (
            None
            if self.mode == "unknown_usage"
            else {"input_tokens": 11, "output_tokens": 5, "total_tokens": 16}
        )
        if self.summary:
            parsed = SolutionSummary(
                approach="整理现有代码。",
                correctness="这是待审阅推理。",
                complexity="请审阅复杂度。",
                limitations="没有运行证据不声称通过。",
            )
        else:
            state = json.loads(messages[1].content)
            level = state["hint_level"]
            leak = self.mode == "leak_once" and self.calls == 1
            text = "请根据当前题意继续分析你自己的尝试。"
            if state.get("request") == "answer":
                fixture = "reverse_ok" if state["problem"]["frontend_id"] == "25" else "two_sum_ok"
                text = "离线 fixture，仅验证交付契约。\n```python\n" + CODE[fixture] + "\n```"
            parsed = TeachingReply(
                reply="class Solution: pass" if leak else text,
                hint_level_used=4 if leak else level,
                stage="explore",
                full_solution=leak or level == 4,
            )
        raw = AIMessage(
            content="",
            usage_metadata=usage,
            tool_calls=[
                {
                    "name": type(parsed).__name__,
                    "args": parsed.model_dump(),
                    "id": "fixture-call",
                }
            ],
            additional_kwargs={
                "reasoning_content": "PRIVATE REASONING",
                "headers": "PRIVATE HEADERS",
            },
        )
        return {"raw": raw, "parsed": parsed, "parsing_error": None}


def teacher(mode="normal"):
    value = object.__new__(ModelTeacher)
    value.model = StubStructured(mode)
    value.summary_model = StubStructured(summary=True)
    value.last_usage = None
    return value


class FixtureVerifier:
    def verify(self, problem, code):
        initial = inspect_code(code)
        if problem.site == "local":
            return Verification(
                status="unsupported", code_hash=initial.code_hash, detail="Fixture: no adapter"
            )
        passed = code.strip() in (CODE["two_sum_ok"].strip(), CODE["reverse_ok"].strip())
        return Verification(
            status="passed" if passed else "wrong_answer",
            code_hash=initial.code_hash,
            detail="Fixture-only simulated verdict",
            executed=True,
            case_count=1,
            passed_count=int(passed),
            suite_version="test-fixture-only",
            failure=None
            if passed
            else {"inputs": {"nums": [3, 3], "target": 6}, "expected": [0, 1]},
        )


def fixture_run(tmp_path, *, ids=None, mode="normal", max_calls=200, profile="full"):
    cases, digest = load_cases()
    selected = select_cases(cases, profile, ids=ids)
    return run(
        selected,
        suite_hash=digest,
        dataset_size=len(cases),
        output_root=tmp_path,
        max_calls=max_calls,
        max_seconds=120,
        docker=True,
        teacher_factory=lambda: teacher(mode),
        backend="test_fixture",
        verifier=FixtureVerifier(),
        progress=lambda text: None,
    )


@pytest.mark.parametrize("docker", [False, True])
def test_docker_flag_controls_reference_validation_without_test_step(tmp_path, monkeypatch, docker):
    import evaluation.runner as runner_module

    constructed = []

    class ProbeRunner:
        def __init__(self, *, enabled, **kwargs):
            self.enabled = enabled
            constructed.append(self)

        def doctor(self):
            return {"ready": True}

    class ProbeVerifier(FixtureVerifier):
        def __init__(self, runner):
            self.runner = runner

        def verify(self, problem, code):
            if self.runner.enabled:
                return super().verify(problem, code)
            return inspect_code(code)

    monkeypatch.setattr(runner_module, "DockerRunner", ProbeRunner)
    monkeypatch.setattr(runner_module, "VerificationService", ProbeVerifier)
    case = Case.model_validate(
        {
            "id": "answer-without-test",
            "category": "evidence",
            "split": "dev",
            "problem": "1",
            "steps": [
                {"operation": "say", "text": "只复述题意"},
                {"operation": "say", "text": "/answer"},
            ],
            "rubric": ["reference validation"],
        }
    )
    folder, _ = run(
        [case],
        suite_hash="fixture",
        dataset_size=1,
        output_root=tmp_path,
        docker=docker,
        teacher_factory=teacher,
        progress=lambda _: None,
    )
    result = json.loads((folder / "results.json").read_text("utf-8"))
    check = result["cases"][0]["steps"][-1]["after"]["answer_checks"][-1]
    assert check["status"] == ("passed" if docker else "not_run")
    assert check["executed"] is docker
    assert constructed[0].enabled is docker


def test_dataset_is_30_multiturn_cases_with_heldout_and_checked_oracles():
    cases, _ = load_cases()
    assert len(cases) == 30 and len({c.category for c in cases}) == 10
    assert sum(c.split == "dev" for c in cases) == 20
    assert sum(c.split == "holdout" for c in cases) == 10
    assert sum(c.smoke for c in cases) == 3
    assert all(len(c.steps) >= 2 for c in cases)
    for item in GROUND_TRUTH.values():
        assert check_counterexample(item["problem"], item["inputs"], item["expected"])[
            "expected_valid"
        ]


def test_full_fixture_suite_covers_real_application_contracts_and_archives(tmp_path):
    folder, summary = fixture_run(tmp_path)
    assert summary["completed_cases"] == summary["contract_pass_cases"] == 30
    assert summary["model_invocations"] == 64
    assert summary["human_pending_steps"] == 64
    assert summary["teaching_quality"] == "pending_human_review"
    manifest = json.loads((folder / "manifest.json").read_text("utf-8"))
    assert manifest["backend"] == "test_fixture"
    assert len(manifest["case_hashes"]) == 30
    snapshot = json.loads((folder / "source.snapshot.json").read_text("utf-8"))
    from evaluation.cases import fingerprint

    assert snapshot["source_hash"] == manifest["source_hash"]
    assert (
        fingerprint({name: fingerprint(code) for name, code in snapshot["files"].items()})
        == manifest["source_hash"]
    )
    assert manifest["runtime_versions"]["python"]
    assert list(folder.rglob("artifact*"))
    assert render(folder)["contract_pass_cases"] == 30


def test_rejected_attempt_is_retained_and_retry_is_counted(tmp_path):
    folder, summary = fixture_run(tmp_path, ids=["clarify-indices"], mode="leak_once")
    assert summary["model_invocations"] == 3
    assert summary["policy_rejected_attempts"] == 1
    assert summary["contract_pass_cases"] == 1
    calls = json.loads((folder / "results.json").read_text("utf-8"))["calls"]
    assert "class Solution" in calls[0]["output"]["parsed"]["reply"]
    assert calls[0]["policy_result"] == "reply_policy_rejected"
    assert calls[1]["messages"][1]["role"] == "system"


def test_service_failure_never_falls_back_and_does_not_leak_exception(tmp_path):
    folder, summary = fixture_run(tmp_path, profile="smoke", mode="auth")
    assert summary["model_invocations"] == 1
    assert summary["contract_pass_cases"] == 0
    assert summary["incomplete_cases"] == 3
    assert summary["run_status"] == "provider_error"
    assert "PRIVATE PROVIDER RESPONSE" not in (folder / "results.json").read_text("utf-8")


def test_call_budget_includes_retry_and_unfinished_cases_are_not_passes(tmp_path):
    _, summary = fixture_run(tmp_path, profile="smoke", mode="leak_once", max_calls=2)
    assert summary["model_invocations"] == 2
    assert summary["completed_cases"] == 0 and summary["contract_pass_cases"] == 0
    assert summary["run_status"] == "budget_exhausted"


def test_missing_usage_and_cost_are_unknown_not_zero(tmp_path):
    _, summary = fixture_run(tmp_path, ids=["clarify-indices"], mode="unknown_usage")
    assert summary["known_token_sums"]["total_tokens"] is None
    assert summary["token_coverage"]["total_tokens"] == {"known_calls": 0, "all_calls": 2}
    assert summary["cost"] == "unknown"


def test_capture_whitelists_visible_fields_without_reasoning_or_headers():
    data = StubStructured().invoke(
        [
            type("Message", (), {"content": "unused"})(),
            type("Message", (), {"content": '{"hint_level":0}'})(),
        ]
    )
    captured = json.dumps(visible_result(data))
    assert "PRIVATE REASONING" not in captured and "PRIVATE HEADERS" not in captured


def test_manual_review_requires_exact_run_and_output_hash(tmp_path):
    folder, _ = fixture_run(tmp_path, ids=["clarify-indices"])
    rows = [
        json.loads(line)
        for line in (folder / "reviews.template.jsonl").read_text("utf-8").splitlines()
    ]
    for row in rows:
        row.update({key: "pass" for key in REVIEW_FIELDS})
        row.update(reviewer="test-rater", evidence_notes="Fixture ratings test only.")
    path = tmp_path / "reviews.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), "utf-8")
    assert render(folder, reviews_path=path)["human_pending_steps"] == 0
    rows[0]["step_hash"] = "incorrect"
    path.write_text("\n".join(json.dumps(row) for row in rows), "utf-8")
    with pytest.raises(ValueError, match="不匹配"):
        render(folder, reviews_path=path)


def test_modified_model_output_cannot_reuse_review_or_report(tmp_path):
    folder, _ = fixture_run(tmp_path, ids=["clarify-indices"])
    path = folder / "results.json"
    result = json.loads(path.read_text("utf-8"))
    result["calls"][0]["output"]["parsed"]["reply"] = "rewritten"
    path.write_text(json.dumps(result), "utf-8")
    with pytest.raises(ValueError, match="输出已改变"):
        render(folder)


def test_compare_checks_scenario_and_fixture_identity(tmp_path):
    first, _ = fixture_run(tmp_path, ids=["clarify-indices"])
    second, _ = fixture_run(tmp_path, ids=["clarify-indices"])
    assert compare(first, second)["first"]["contract_pass_cases"] == 1
    path = second / "manifest.json"
    data = json.loads(path.read_text("utf-8"))
    data["fixture_hashes"]["two_sum_ok"] = "changed"
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(ValueError):
        compare(first, second)


def test_real_backend_rejects_non_model_delegate_and_missing_docker(tmp_path):
    cases, digest = load_cases()
    with pytest.raises(ValueError, match="ModelTeacher"):
        run(
            [cases[0]],
            suite_hash=digest,
            dataset_size=30,
            output_root=tmp_path,
            teacher_factory=lambda: type("Fake", (), {"mode": "model"})(),
        )
    docker_case = next(c for c in cases if c.needs_docker())
    with pytest.raises(ValueError, match="--docker"):
        run([docker_case], suite_hash=digest, dataset_size=30, output_root=tmp_path)


def test_cases_reject_arbitrary_commands_and_unknown_code():
    cases, _ = load_cases()
    raw = copy.deepcopy(cases[0].model_dump())
    raw["steps"][0]["text"] = "/push apply"
    with pytest.raises(ValueError):
        Case.model_validate(raw)
    raw["steps"][0] = {"operation": "code", "fixture": "../../private"}
    with pytest.raises(ValueError):
        Case.model_validate(raw)


def test_cli_validation_is_offline_and_real_run_needs_credentials(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALGOCOACH_")}
    env["PYTHONPATH"] = str(ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    command = [sys.executable, "-m", "evaluation.runner"]
    validated = subprocess.run(
        command + ["validate"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert validated.returncode == 0 and '"cases": 30' in validated.stdout
    run_result = subprocess.run(
        command + ["run"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert run_result.returncode == 2
    assert "ALGOCOACH_API_KEY" in run_result.stdout
    assert not (tmp_path / ".algo-coach").exists()


@pytest.mark.parametrize(
    "inputs,expected,valid",
    [
        ({"nums": [3, 3], "target": 6}, [0, 1], True),
        ({"nums": [3, 3], "target": 6}, [0, 0], False),
        ({"nums": [1, 2, 3, 4], "target": 5}, [0, 3], False),
    ],
)
def test_counterexample_checks_constraint_uniqueness(inputs, expected, valid):
    assert check_counterexample("1", inputs, expected)["expected_valid"] is valid


def test_metrics_count_each_attempt_once_and_unknown_coverage():
    result = metrics(
        [
            {
                "elapsed_ms": 3,
                "output": {"usage": {"input_tokens": 2, "output_tokens": 4, "total_tokens": 6}},
            },
            {"elapsed_ms": 5},
        ]
    )
    assert result["model_invocations"] == 2
    assert result["known_token_sums"]["total_tokens"] == 6
    assert result["token_coverage"]["total_tokens"] == {"known_calls": 1, "all_calls": 2}
    assert result["model_latency_ms"]["p95"] == 5
