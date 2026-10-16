import json
import os
import subprocess
import sys


def test_standalone_verification_needs_no_model_and_never_falls_back(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    candidate = tmp_path / "candidate.py"
    candidate.write_text(
        f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('wrong')\n",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALGOCOACH_")}
    env["ALGOCOACH_RUNNER_ENABLED"] = "false"
    result = subprocess.run(
        [sys.executable, "-m", "algo_coach", "test", "1", str(candidate)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 1
    assert "not_run" in result.stdout
    assert not sentinel.exists()
    assert "API" not in result.stderr


def test_doctor_reports_missing_docker_without_creating_a_session(tmp_path):
    env = {key: value for key, value in os.environ.items() if not key.startswith("ALGOCOACH_")}
    env["PATH"] = ""
    result = subprocess.run(
        [sys.executable, "-m", "algo_coach", "doctor"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0
    assert not json.loads(result.stdout)["ready"]
    assert not (tmp_path / ".algo-coach").exists()
