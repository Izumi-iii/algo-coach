"""Opt-in checks that actually execute submissions exclusively inside Docker."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from algo_coach.problems import LocalProblems
from algo_coach.runner import DockerRunner
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher
from algo_coach.verification import VerificationService

pytestmark = pytest.mark.skipif(
    os.getenv("ALGOCOACH_TEST_DOCKER") != "1",
    reason="Set ALGOCOACH_TEST_DOCKER=1 after building the Linux runner image",
)


@pytest.fixture
def runner():
    runner = DockerRunner(enabled=True)
    probe = runner.doctor()
    assert probe["ready"], probe["reason"]
    return runner


@pytest.mark.parametrize(
    "reference,filename",
    [
        ("1", "two_sum.py"),
        ("25", "reverse_k_group.py"),
    ],
)
def test_reference_solution_in_real_container(tmp_path, runner, reference, filename):
    code = (Path(__file__).parents[1] / "examples/submissions" / filename).read_text("utf-8")
    report = VerificationService(runner).verify(LocalProblems(tmp_path).resolve(reference), code)
    assert report.status == "passed", report.model_dump()


def test_bad_indices_produce_counterexample_in_real_container(tmp_path, runner):
    code = "class Solution:\n    def twoSum(self, nums, target):\n        return [0, 0]\n"
    report = VerificationService(runner).verify(LocalProblems(tmp_path).resolve("1"), code)
    assert report.status == "wrong_answer"
    assert report.failure["actual"]["value"] == [0, 0]


def test_network_user_and_filesystem_isolation_probes(runner):
    code = """
class Solution:
    def twoSum(self, nums, target):
        import os, socket
        assert os.getuid() == 65534
        assert os.getenv("ALGOCOACH_API_KEY") is None
        assert not os.path.exists("/var/run/docker.sock")
        try:
            open("/app/isolation-probe", "w")
        except OSError:
            pass
        else:
            raise AssertionError("Root filesystem unexpectedly writable")
        sock = socket.socket()
        sock.settimeout(0.5)
        assert sock.connect_ex(("1.1.1.1", 443)) != 0
        sock.close()
        return [0, 1]
"""
    result = runner.run(
        code, "two_sum", [{"case_id": "probe", "inputs": {"nums": [1, 2], "target": 3}}]
    )
    assert result.status == "ok"
    assert result.records[0]["status"] == "ok"


def test_infinite_loop_is_terminated_in_real_container(tmp_path, runner):
    code = "while True:\n    pass\n"
    report = VerificationService(runner).verify(LocalProblems(tmp_path).resolve("1"), code)
    assert report.status == "timeout"


def test_unbounded_printing_is_terminated_in_real_container(tmp_path, runner):
    code = "while True:\n    print('x' * 4096)\n"
    report = VerificationService(runner).verify(LocalProblems(tmp_path).resolve("1"), code)
    assert report.status == "resource_limit"


def test_exported_bundle_runs_without_installed_algo_coach(tmp_path, runner):
    practice = tmp_path / "practice"
    practice.mkdir()
    code = (Path(__file__).parents[1] / "examples/submissions/two_sum.py").read_text("utf-8")
    with Coach(tmp_path / "data", DemoTeacher(), verifier=VerificationService(runner)) as coach:
        session = coach.start(coach.problems.resolve("1"))["session_id"]
        coach.turn(session, "代码", code=code)
        assert coach.test(session)["verification"]["status"] == "passed"
        coach.preview_archive(session, practice)
        target = Path(coach.apply_archive(session)["path"])
    suite = json.loads((target / "tests/cases.json").read_text("utf-8"))
    built = subprocess.run(
        [
            "docker",
            "build",
            "--file",
            str(target / "tests/Dockerfile"),
            "--tag",
            suite["image"],
            str(target / "tests"),
        ],
        capture_output=True,
        timeout=90,
    )
    assert built.returncode == 0, built.stderr
    replay = subprocess.run(
        [sys.executable, "-S", str(target / "tests/replay.py")],
        cwd=tmp_path,
        capture_output=True,
        timeout=45,
    )
    assert replay.returncode == 0, replay.stderr
    assert b"47/47" in replay.stdout
