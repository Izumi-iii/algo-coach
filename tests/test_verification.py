import pytest
from pydantic import ValidationError

from algo_coach.adapters import build_cases, canonical_adapter, two_sum_pairs, validate_output
from algo_coach.models import TeachingReply, Verification
from algo_coach.problems import LocalProblems
from algo_coach.runner import DockerRunner, RunnerResult
from algo_coach.sandbox_worker import ListNode, observe_linked_list
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher, validate_reply
from algo_coach.verification import VerificationService, inspect_code


class ObservationRunner:
    """Inject observations without executing any candidate code on the host."""

    def __init__(self, mutate=None):
        self.mutate = mutate
        self.calls = 0

    def run(self, code, adapter, cases):
        self.calls += 1
        assert all(set(case) == {"case_id", "inputs"} for case in cases)
        expected = build_cases(adapter)
        records = [
            {
                "case_id": case.case_id,
                "status": "ok",
                "output": {"value": list(case.expected), "node_order": case.expected_order},
            }
            for case in expected
        ]
        if self.mutate:
            self.mutate(records)
        return RunnerResult("ok", "simulated", records, "sha256:" + "a" * 64)


@pytest.mark.parametrize("adapter,count", [("two_sum", 47), ("reverse_k_group", 46)])
def test_generated_cases_are_reproducible_and_valid(adapter, count):
    cases = build_cases(adapter)
    assert cases == build_cases(adapter)
    assert len(cases) == count
    for case in cases:
        if adapter == "two_sum":
            assert len(two_sum_pairs(case.inputs["nums"], case.inputs["target"])) == 1
            assert validate_output(adapter, case, {"value": case.expected[::-1]})[0]
        else:
            assert sorted(case.expected_order) == list(range(len(case.inputs["head"])))


@pytest.mark.parametrize("value", [[0, 0], [-1, 1], [True, 1], [0, 99], [1], [1, 2], None])
def test_two_sum_rejects_invalid_indices(value):
    case = build_cases("two_sum")[0]
    assert not validate_output("two_sum", case, {"value": value})[0]


def test_linked_list_requires_original_node_connections_not_only_values():
    case = build_cases("reverse_k_group")[0]
    valid = {"value": case.expected, "node_order": case.expected_order, "cycle": False}
    assert validate_output("reverse_k_group", case, valid)[0]
    for order in (list(range(5)), [-1] * 5):
        assert not validate_output("reverse_k_group", case, {**valid, "node_order": order})[0]
    assert not validate_output("reverse_k_group", case, {**valid, "cycle": True})[0]


def test_linked_list_observation_detects_cycles_and_preserves_identity():
    # Trusted adapter objects only; no candidate code is compiled or called here.
    a, b = ListNode(1), ListNode(2)
    b.next = a
    assert observe_linked_list(b, [a, b])["node_order"] == [1, 0]
    a.next = b
    assert observe_linked_list(b, [a, b])["cycle"]


@pytest.mark.parametrize("reference", ["1", "25"])
def test_host_verdict_records_versions_hash_and_evidence(tmp_path, reference):
    problem = LocalProblems(tmp_path).resolve(reference)
    report = VerificationService(ObservationRunner()).verify(problem, "pass")
    assert report.status == "passed"
    assert report.executed
    assert report.case_count == report.passed_count
    assert report.problem_hash and report.suite_version and report.adapter_version
    assert report.code_hash == inspect_code("pass").code_hash


def test_report_contains_reproducible_counterexample(tmp_path):
    def wrong(records):
        records[0]["output"] = {"value": [0, 0]}

    report = VerificationService(ObservationRunner(wrong)).verify(
        LocalProblems(tmp_path).resolve("1"), "pass"
    )
    assert report.status == "wrong_answer"
    assert report.failure["input"] == {"nums": [2, 7, 11, 15], "target": 9}
    assert report.failure["expected"] == [0, 1]
    assert report.failure["actual"] == {"value": [0, 0]}
    assert report.passed_count == report.case_count - 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows.pop(),
        lambda rows: rows[0].update(case_id="forged"),
        lambda rows: rows[0].update(status="passed"),
        lambda rows: rows[0].update(output=None),
    ],
)
def test_malformed_worker_evidence_is_a_runner_error(tmp_path, mutation):
    report = VerificationService(ObservationRunner(mutation)).verify(
        LocalProblems(tmp_path).resolve("1"), "pass"
    )
    assert report.status == "runner_error"
    assert not report.executed


def test_runtime_error_is_not_wrong_answer(tmp_path):
    def runtime_error(rows):
        rows[0].update(status="runtime_error", output=None)

    report = VerificationService(ObservationRunner(runtime_error)).verify(
        LocalProblems(tmp_path).resolve("1"), "pass"
    )
    assert report.status == "runtime_error"


def test_changed_statement_cannot_reuse_builtin_adapter(tmp_path):
    problem = LocalProblems(tmp_path).resolve("1")
    changed = problem.model_copy(update={"statement": "返回两个元素的值，不返回下标"})
    runner = ObservationRunner()
    report = VerificationService(runner).verify(changed, "pass")
    assert canonical_adapter(changed) is None
    assert report.status == "unsupported"
    assert runner.calls == 0


def test_syntax_error_prevents_execution(tmp_path):
    runner = ObservationRunner()
    report = VerificationService(runner).verify(LocalProblems(tmp_path).resolve("1"), "def f(:")
    assert report.status == "syntax_error"
    assert runner.calls == 0


def test_disabled_runner_is_not_a_pass_or_host_fallback(tmp_path):
    report = VerificationService(DockerRunner(enabled=False)).verify(
        LocalProblems(tmp_path).resolve("1"), "raise RuntimeError('must not run')"
    )
    assert report.status == "not_run"
    assert not report.executed
    assert report.case_count == 0


def test_pass_requires_actual_complete_evidence():
    with pytest.raises(ValidationError):
        Verification(status="passed", code_hash="abc", detail="fake")


def test_testing_saves_evidence_and_new_code_invalidates_it(tmp_path):
    class CountingTeacher(DemoTeacher):
        calls = 0

        def reply(self, state):
            self.calls += 1
            return super().reply(state)

    teacher = CountingTeacher()
    with Coach(tmp_path, teacher, verifier=VerificationService(ObservationRunner())) as coach:
        session = coach.start(coach.problems.resolve("1"))["session_id"]
        coach.turn(session, "检查代码", code="pass")
        before = teacher.calls
        state = coach.test(session)
        assert teacher.calls == before  # Verification itself never calls a paid model.
        assert state["verification"]["status"] == "passed"
        assert len(state["attempts"]) == 1
        assert len(state["attempts"][0]["verification_history"]) == 2
        state = coach.turn(session, "检查新版本", code="x = 1")
        assert state["verification"]["status"] == "not_run"
        assert state["attempts"][0]["verification"]["status"] == "passed"
    with Coach(tmp_path, DemoTeacher()) as coach:
        state = coach.get(session)
        assert len(state["attempts"]) == 2
        assert state["verification"]["code_hash"] == state["code_hash"]
        assert "not_run" in coach.snapshot(session)


def test_teacher_can_only_cite_pass_for_matching_code_version():
    report = {"status": "passed", "executed": True, "code_hash": "current"}
    draft = TeachingReply(
        reply="本地代码已通过这些测试，仍需进一步分析。",
        hint_level_used=1,
        stage="implement",
        full_solution=False,
    )
    state = {"hint_level": 1, "code_hash": "current", "verification": report}
    validate_reply(draft, state)
    with pytest.raises(ValueError):
        validate_reply(draft, {**state, "code_hash": "changed"})


def test_hash_mismatch_is_never_saved(tmp_path):
    class WrongVerifier:
        def verify(self, problem, code):
            return Verification(status="not_run", code_hash="wrong", detail="wrong")

    with Coach(tmp_path, DemoTeacher(), verifier=WrongVerifier()) as coach:
        session = coach.start(coach.problems.resolve("1"))["session_id"]
        old = coach.turn(session, "代码", code="pass")
        with pytest.raises(ValueError, match="版本不匹配"):
            coach.test(session)
        assert coach.get(session)["verification"] == old["verification"]
