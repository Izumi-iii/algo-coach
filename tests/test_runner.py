import json
import sys

import pytest

import algo_coach.runner as module
from algo_coach.runner import MAX_OUTPUT, DockerRunner, ProcessResult, bounded_process

IMAGE_ID = "sha256:" + "a" * 64


def ready_runner(monkeypatch):
    runner = DockerRunner(enabled=True)
    monkeypatch.setattr(
        runner,
        "doctor",
        lambda: {
            "ready": True,
            "executable": "docker-test",
            "image_id": IMAGE_ID,
        },
    )
    return runner


def test_docker_command_limits_permissions_and_never_mounts_host_data():
    args = DockerRunner.command("docker", "algo-coach-test", IMAGE_ID)
    for option, value in [
        ("--network", "none"),
        ("--user", "65534:65534"),
        ("--cap-drop", "ALL"),
        ("--memory", "256m"),
        ("--memory-swap", "256m"),
        ("--cpus", "1"),
        ("--pids-limit", "64"),
        ("--pull", "never"),
        ("--log-driver", "none"),
    ]:
        assert args[args.index(option) + 1] == value
    assert "--read-only" in args
    assert args[-1] == IMAGE_ID
    assert not set(args).intersection({"--privileged", "--volume", "--mount", "--env", "-e"})


def test_missing_docker_never_starts_a_host_candidate(monkeypatch):
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    monkeypatch.setattr(module, "bounded_process", lambda *a, **k: pytest.fail("must not spawn"))
    result = DockerRunner(enabled=True).run("pass", "two_sum", [])
    assert result.status == "not_run"


@pytest.mark.parametrize(
    "info,image,ready",
    [
        (ProcessResult("timeout"), ProcessResult("ok", 0, IMAGE_ID.encode()), False),
        (ProcessResult("ok", 0, b"windows"), ProcessResult("ok", 0, IMAGE_ID.encode()), False),
        (ProcessResult("ok", 0, b""), ProcessResult("ok", 0, IMAGE_ID.encode()), False),
        (ProcessResult("ok", 0, b"linux"), ProcessResult("ok", 1), False),
        (ProcessResult("ok", 0, b"linux"), ProcessResult("ok", 0, IMAGE_ID.encode()), True),
    ],
)
def test_doctor_checks_engine_mode_and_resolves_immutable_image(monkeypatch, info, image, ready):
    monkeypatch.setattr(module.shutil, "which", lambda name: "docker-test")
    monkeypatch.setattr(
        module, "bounded_process", lambda command, **kw: info if command[1] == "info" else image
    )
    probe = DockerRunner().doctor()
    assert probe["ready"] is ready
    if ready:
        assert probe["image_id"] == IMAGE_ID
    elif info.status == "ok" and info.returncode == 0 and not info.stdout:
        assert "引擎未返回有效状态" in probe["reason"]


@pytest.mark.parametrize(
    "result,expected",
    [
        (ProcessResult("timeout"), "timeout"),
        (ProcessResult("resource_limit"), "resource_limit"),
        (ProcessResult("ok", 0, b"not-json"), "runner_error"),
        (ProcessResult("ok", 0, b'{"protocol":99,"results":[]}'), "runner_error"),
        (ProcessResult("ok", 0, b'{"protocol":1,"results":[]}'), "ok"),
    ],
)
def test_every_execution_path_cleans_its_own_container(monkeypatch, result, expected):
    calls = []

    def run(command, payload=b"", **kwargs):
        calls.append(command)
        if command[1] == "rm":
            return ProcessResult("ok", 0)
        assert json.loads(payload)["code"] == "pass"
        assert command[-1] == IMAGE_ID
        return result

    runner = ready_runner(monkeypatch)
    monkeypatch.setattr(module, "bounded_process", run)
    result = runner.run("pass", "two_sum", [])
    assert result.status == expected
    assert calls[-1][1:3] == ["rm", "--force"]
    assert calls[-1][-1].startswith("algo-coach-")
    assert calls[0][calls[0].index("--name") + 1] == calls[-1][-1]


def test_cleanup_failure_cannot_be_reported_as_success(monkeypatch):
    runner = ready_runner(monkeypatch)
    monkeypatch.setattr(
        module,
        "bounded_process",
        lambda command, *a, **k: (
            ProcessResult("ok", 1)
            if command[1] == "rm"
            else ProcessResult("ok", 0, b'{"protocol":1,"results":[]}')
        ),
    )
    result = runner.run("pass", "two_sum", [])
    assert result.status == "runner_error"
    assert "清理" in result.detail


def test_confirmed_oom_is_distinguished_from_generic_exit(monkeypatch):
    runner = ready_runner(monkeypatch)

    def run(command, *args, **kwargs):
        if command[1] == "rm":
            return ProcessResult("ok", 0)
        if command[1] == "inspect":
            return ProcessResult("ok", 0, b"true")
        return ProcessResult("ok", 137)

    monkeypatch.setattr(module, "bounded_process", run)
    assert runner.run("pass", "two_sum", []).status == "resource_limit"


def test_cancel_also_attempts_container_cleanup(monkeypatch):
    runner = ready_runner(monkeypatch)
    cleaned = []

    def run(command, *args, **kwargs):
        if command[1] == "rm":
            cleaned.append(command[-1])
            return ProcessResult("ok", 0)
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "bounded_process", run)
    assert runner.run("pass", "two_sum", []).status == "cancelled"
    assert len(cleaned) == 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_supervisor_bounds_both_streams(stream):
    # Trusted fixed infrastructure probe; never uses a submitted program on the host.
    code = f"import sys; sys.{stream}.write('x' * 1000000); sys.{stream}.flush()"
    result = bounded_process([sys.executable, "-c", code])
    assert result.status == "resource_limit"
    assert len(result.stdout) <= MAX_OUTPUT


def test_supervisor_wall_clock_timeout():
    result = bounded_process([sys.executable, "-c", "import time; time.sleep(10)"], timeout=0.1)
    assert result.status == "timeout"


def test_supervisor_can_send_protocol_input():
    result = bounded_process(
        [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"],
        b"trusted protocol probe",
    )
    assert result.status == "ok"
    assert result.stdout == b"trusted protocol probe"


def test_invalid_runner_flag_is_not_silently_enabled(monkeypatch):
    monkeypatch.setenv("ALGOCOACH_RUNNER_ENABLED", "maybe")
    with pytest.raises(ValueError):
        DockerRunner.from_env()
