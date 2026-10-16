"""Docker-only execution boundary with bounded I/O and external container cleanup."""

import json
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from uuid import uuid4

DEFAULT_IMAGE = "algo-coach-runner:0.1"
MAX_OUTPUT = 64 * 1024


@dataclass(frozen=True)
class ProcessResult:
    status: str
    returncode: int | None = None
    stdout: bytes = b""


def bounded_process(command: list[str], payload: bytes = b"", timeout: float = 10) -> ProcessResult:
    """Bound stdout AND stderr while running a trusted CLI, not candidate code on the host."""
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
    output = bytearray()
    lock = threading.Lock()
    overflow = threading.Event()
    total = 0

    def drain(stream, keep):
        nonlocal total
        try:
            while chunk := stream.read(4096):
                with lock:
                    total += len(chunk)
                    if keep:
                        output.extend(chunk[: max(0, MAX_OUTPUT - len(output))])
                    if total > MAX_OUTPUT:
                        overflow.set()
                        try:
                            process.kill()
                        except OSError:
                            pass
        finally:
            stream.close()

    def feed():
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    threads = [
        threading.Thread(target=drain, args=(process.stdout, True), daemon=True),
        threading.Thread(target=drain, args=(process.stderr, False), daemon=True),
        threading.Thread(target=feed, daemon=True),
    ]
    for thread in threads:
        thread.start()
    status = "ok"
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        status = "timeout"
        process.kill()
        process.wait(timeout=3)
    except BaseException:
        process.kill()
        process.wait(timeout=3)
        raise
    finally:
        for thread in threads:
            thread.join(timeout=1)
    if overflow.is_set():
        status = "resource_limit"
    return ProcessResult(status, process.returncode, bytes(output))


@dataclass(frozen=True)
class RunnerResult:
    status: str
    detail: str
    records: list[dict] | None = None
    image_id: str | None = None


class DockerRunner:
    def __init__(self, *, enabled: bool = False, image: str = DEFAULT_IMAGE):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,240}", image):
            raise ValueError("执行器镜像名称无效")
        self.enabled = enabled
        self.image = image

    @classmethod
    def from_env(cls):
        flag = os.getenv("ALGOCOACH_RUNNER_ENABLED", "false").strip().lower()
        if flag not in ("true", "false"):
            raise ValueError("ALGOCOACH_RUNNER_ENABLED 只能是 true 或 false")
        return cls(enabled=flag == "true", image=os.getenv("ALGOCOACH_RUNNER_IMAGE", DEFAULT_IMAGE))

    def doctor(self) -> dict:
        executable = shutil.which("docker")
        result = {"enabled": self.enabled, "image": self.image, "ready": False}
        if not executable:
            return {**result, "reason": "未找到 Docker 命令；没有使用宿主 Python 作为替代"}
        try:
            info = bounded_process([executable, "info", "--format", "{{.OSType}}"], timeout=5)
            if info.status != "ok" or info.returncode != 0:
                return {**result, "reason": "Docker 服务不可用或检查超时，请启动 Docker Engine"}
            if info.stdout.strip() == b"windows":
                return {**result, "reason": "需要 Linux 容器模式"}
            if info.stdout.strip() != b"linux":
                return {
                    **result,
                    "reason": "Docker 引擎未返回有效状态，请启动或重启 Docker Desktop",
                }
            image = bounded_process(
                [executable, "image", "inspect", "--format", "{{.Id}}", self.image], timeout=5
            )
            image_id = image.stdout.decode("utf-8", errors="replace").strip()
            if (
                image.status != "ok"
                or image.returncode != 0
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id)
            ):
                return {**result, "reason": "找不到已构建的执行器镜像，请参考 Docs/RUNNER.md"}
            return {
                **result,
                "ready": True,
                "executable": executable,
                "image_id": image_id,
                "reason": "Linux 引擎与镜像存在；隔离探针仍需另行验证",
            }
        except OSError:
            return {**result, "reason": "无法访问 Docker CLI"}

    @staticmethod
    def command(executable: str, name: str, image_id: str) -> list[str]:
        return [
            executable,
            "run",
            "--name",
            name,
            "--pull",
            "never",
            "--interactive",
            "--network",
            "none",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--ulimit",
            "nofile=64:64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=16m",
            "--log-driver",
            "none",
            image_id,
        ]

    def run(self, code: str, adapter: str, cases: list[dict]) -> RunnerResult:
        if not self.enabled:
            return RunnerResult("not_run", "动态验证未启用；请配置执行器并运行 doctor 检查")
        probe = self.doctor()
        if not probe["ready"]:
            return RunnerResult("not_run", probe["reason"])
        name = "algo-coach-" + uuid4().hex
        executable, image_id = probe["executable"], probe["image_id"]
        payload = json.dumps(
            {"code": code, "adapter": adapter, "cases": cases}, ensure_ascii=False
        ).encode("utf-8")
        if len(payload) > 256 * 1024:
            return RunnerResult("runner_error", "运行输入超过协议大小限制")
        try:
            outcome = self._execute(executable, name, image_id, payload)
        except KeyboardInterrupt:
            outcome = RunnerResult("cancelled", "用户取消了验证", image_id=image_id)
        except OSError:
            outcome = RunnerResult("runner_error", "执行 Docker 命令失败", image_id=image_id)
        finally:
            # Only our generated container name is removed. Never delete user containers.
            try:
                cleanup = bounded_process([executable, "rm", "--force", name], timeout=5)
                cleanup_ok = cleanup.status == "ok" and cleanup.returncode == 0
            except OSError:
                cleanup_ok = False
        if not cleanup_ok:
            return RunnerResult(
                "runner_error", f"无法确认容器清理，请检查本次容器 {name}", image_id=image_id
            )
        return outcome

    def _execute(self, executable: str, name: str, image_id: str, payload: bytes) -> RunnerResult:
        try:
            result = bounded_process(self.command(executable, name, image_id), payload)
            if result.status != "ok":
                detail = {
                    "timeout": "容器启动或执行超过本地 10 秒墙钟限制",
                    "resource_limit": "容器输出超过本地 64 KiB 限制",
                }[result.status]
                return RunnerResult(result.status, detail, image_id=image_id)
            if result.returncode != 0:
                inspection = bounded_process(
                    [executable, "inspect", "--format", "{{.State.OOMKilled}}", name], timeout=3
                )
                if inspection.status == "ok" and inspection.stdout.strip() == b"true":
                    return RunnerResult(
                        "resource_limit", "Docker 确认任务因内存限制被终止", image_id=image_id
                    )
                return RunnerResult(
                    "runner_error", "容器异常退出，未获得有效执行证据", image_id=image_id
                )
            try:
                response = json.loads(result.stdout)
                if (
                    not isinstance(response, dict)
                    or response.get("protocol") != 1
                    or not isinstance(response.get("results"), list)
                ):
                    raise ValueError("Invalid protocol")
            except (ValueError, UnicodeError, RecursionError):
                return RunnerResult("runner_error", "容器返回内容不符合执行协议", image_id=image_id)
            return RunnerResult("ok", "已获得容器观测结果", response["results"], image_id)
        except UnicodeError:
            return RunnerResult("runner_error", "容器返回非法编码", image_id=image_id)
