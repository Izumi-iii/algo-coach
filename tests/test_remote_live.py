"""Explicit network + Docker acceptance. Ordinary pytest never calls LeetCode."""

import json
import os
from pathlib import Path

import pytest

from algo_coach.adapters import canonical_adapter, is_builtin_summary
from algo_coach.calendar import today
from algo_coach.runner import DockerRunner
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher
from algo_coach.verification import VerificationService

pytestmark = pytest.mark.skipif(
    os.getenv("ALGOCOACH_TEST_LEETCODE") != "1" or os.getenv("ALGOCOACH_TEST_DOCKER") != "1",
    reason="Set both ALGOCOACH_TEST_LEETCODE=1 and ALGOCOACH_TEST_DOCKER=1 for live acceptance",
)


def test_cn_fetch_parse_cache_teach_verify_and_archive(tmp_path):
    practice = tmp_path / "practice"
    practice.mkdir()
    runner = DockerRunner(enabled=True)
    assert runner.doctor()["ready"]
    data = tmp_path / "data"
    with Coach(data, DemoTeacher(), verifier=VerificationService(runner)) as coach:
        for identifier, filename, adapter in [
            ("25", "reverse_k_group.py", "reverse_k_group"),
            ("1", "two_sum.py", "two_sum"),
        ]:
            fetched = coach.remote.fetch(identifier)
            assert fetched.cache_status == "network"
            p = fetched.snapshot.problem
            assert p.frontend_id == identifier and canonical_adapter(p) == adapter
            assert not is_builtin_summary(p)
            # Even one content edit must invalidate the reviewed test binding.
            assert (
                canonical_adapter(p.model_copy(update={"statement": p.statement + " changed"}))
                is None
            )
            session = coach.start_remote(identifier, offline=True)["session_id"]
            code = (Path(__file__).parents[1] / "examples/submissions" / filename).read_text(
                "utf-8"
            )
            coach.turn(session, "代码", code=code)
            result = coach.test(session)
            assert result["verification"]["status"] == "passed"
            assert result["verification"]["executed"]
            plan = coach.preview_archive(session, practice)
            assert p.statement not in plan.contents["problem.md"]
            assert p.url in plan.contents["problem.md"]
            metadata = json.loads(plan.contents["metadata.json"])
            assert (
                metadata["problem_source"]["problem_hash"] == fetched.snapshot.source.problem_hash
            )
            coach.apply_archive(session)
        selection, fetched = coach.remote.daily(today())
        assert selection.calendar_date == today() and selection.kind == "provider_fetched"
        assert selection.source == fetched.snapshot.source
        sid = coach.start_remote_daily(today(), offline=True)["session_id"]
    with Coach(data, DemoTeacher()) as coach:
        assert coach.get(sid)["problem_source"]["cache_status"] == "offline_cache"
        assert coach.remote.daily(today(), offline=True)[0] == selection
