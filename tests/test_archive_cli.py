import json
import os
import subprocess
import sys

from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher


def environment():
    return {key: value for key, value in os.environ.items() if not key.startswith("ALGOCOACH_")}


def test_archive_flow_in_cli_with_unicode_practice_path(tmp_path):
    root = tmp_path / "练习 目录"
    root.mkdir()
    data = tmp_path / "data"
    command = [
        sys.executable,
        "-m",
        "algo_coach",
        "--data-dir",
        str(data),
        "--practice-dir",
        str(root),
        "--demo",
        "chat",
        "1",
    ]
    result = subprocess.run(
        command,
        input="/code\nclass Solution:\n    pass\n.end\n/explain 代码尚未实现\n"
        "/note 需要继续练习\n/archive\n/archive show metadata.json\n/archive apply\n/quit\n",
        cwd=tmp_path,
        env=environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "归档计划" in result.stdout
    assert '"status": "written"' in result.stdout
    assert (root / "problems/leetcode-cn/0001-two-sum/solution.py").exists()


def test_standalone_archive_of_model_session_does_not_need_model_keys(tmp_path):
    data = tmp_path / "data"
    root = tmp_path / "practice"
    root.mkdir()
    teacher = DemoTeacher()
    teacher.mode = "model"
    with Coach(data, teacher) as coach:
        session = coach.start(coach.problems.resolve("1"))["session_id"]
        coach.turn(session, "代码", code="pass")
    command = [
        sys.executable,
        "-m",
        "algo_coach",
        "--data-dir",
        str(data),
        "archive",
        session,
        str(root),
    ]
    preview = subprocess.run(
        command,
        cwd=tmp_path,
        env=environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert preview.returncode == 0, preview.stderr
    with Coach(data, DemoTeacher()) as coach:
        plan_id = coach.get(session)["pending_archive_id"]
    applied = subprocess.run(
        command + ["--apply", plan_id],
        cwd=tmp_path,
        env=environment(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["status"] == "written"
