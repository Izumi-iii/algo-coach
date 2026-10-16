"""Explicit Git plans. No add-all, force push, automatic sync, hooks, or signing prompts."""

import os
import re
import shutil
import subprocess
from pathlib import Path

from filelock import FileLock

from algo_coach.archive import ArchivePlan, digest, json_text, matches
from algo_coach.paths import practice_root, reject_links


class GitService:
    def __init__(self, operations):
        self.operations = operations
        self.executable = shutil.which("git")
        if not self.executable:
            raise ValueError("未找到 Git 命令")

    def run(
        self, root: Path, args: list[str], *, allow_failure=False
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "--literal-pathspecs",
                    "-C",
                    str(root),
                    "-c",
                    "core.quotePath=false",
                    "-c",
                    f"core.hooksPath={os.devnull}",
                    *args,
                ],
                capture_output=True,
                timeout=45,
                shell=False,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Git 命令未完成；请检查本地 Git、网络和凭据后重试") from exc
        if result.returncode and not allow_failure:
            # Raw stderr may contain credential-bearing remote URLs; never display or persist it.
            raise ValueError("Git 操作失败；请在练习仓库检查状态、身份或凭据配置")
        return result

    def head(self, root: Path) -> str | None:
        result = self.run(root, ["rev-parse", "--verify", "--quiet", "HEAD"], allow_failure=True)
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def context(self, root: Path) -> dict:
        root = practice_root(root)
        git_dir = root / ".git"
        reject_links(git_dir)
        if not git_dir.is_dir():
            raise ValueError(
                "需要独立 Git 练习仓库；此版本不自动 git init，也不支持 worktree .git 文件"
            )
        actual = self.run(root, ["rev-parse", "--show-toplevel"]).stdout.decode("utf-8").strip()
        if not Path(actual).samefile(root):
            raise ValueError("练习目录必须是 Git 仓库根目录")
        for name in (
            "MERGE_HEAD",
            "CHERRY_PICK_HEAD",
            "REVERT_HEAD",
            "rebase-merge",
            "rebase-apply",
        ):
            if (git_dir / name).exists():
                raise ValueError("练习仓库存在未完成的合并、变基或拣选，请先手动处理")
        branch = self.run(root, ["symbolic-ref", "--quiet", "--short", "HEAD"], allow_failure=True)
        if branch.returncode:
            raise ValueError("当前为 detached HEAD，请切换到明确分支")
        return {
            "root": str(root),
            "branch": branch.stdout.decode().strip(),
            "head": self.head(root),
        }

    def staged(self, root: Path) -> set[str]:
        data = self.run(root, ["diff", "--cached", "--name-only", "-z"]).stdout
        return {item.decode("utf-8") for item in data.split(b"\0") if item}

    def check_index(self, root: Path, names: set[str]) -> None:
        for name in names:
            expected = self.run(root, ["hash-object", "--path=" + name, "--", name]).stdout.strip()
            indexed = self.run(root, ["rev-parse", ":" + name]).stdout.strip()
            if expected != indexed:
                raise ValueError("暂存内容与归档文件不一致，已停止并保留现场")

    def prepare_commit(self, session: str, archive: ArchivePlan) -> dict:
        root = practice_root(Path(archive.root))
        if not matches(archive):
            raise ValueError("归档文件已改变或缺失，请重新归档后再提交")
        context = self.context(root)
        if self.staged(root):
            raise ValueError("已有暂存内容；本工具会保留现场，请先处理后重新预览")
        payload = {
            **context,
            "archive_id": archive.plan_id,
            "message": archive.commit_message,
            "paths": sorted(f"{archive.relative}/{name}" for name in archive.contents),
        }
        operation_id = digest(json_text(payload))
        self.operations.prepare(operation_id, "commit", session, payload)
        return {
            "operation_id": operation_id,
            **payload,
            "note": "仅提交所列归档文件；本次操作禁用 hooks 和签名，不修改全局 Git 配置",
        }

    def recovered_commit(self, root: Path, payload: dict) -> str | None:
        current = self.head(root)
        if not current or not payload.get("intended_tree"):
            return None
        line = self.run(root, ["show", "-s", "--format=%T%n%P%n%B", current]).stdout.decode("utf-8")
        tree, parents, message = line.split("\n", 2)
        if (
            tree == payload["intended_tree"]
            and parents == (payload["head"] or "")
            and message.strip() == payload["message"]
        ):
            return current
        return None

    def commit(self, session: str, operation_id: str, archive: ArchivePlan) -> dict:
        record = self.operations.get(operation_id, "commit", session)
        payload = record["payload"]
        if payload["archive_id"] != archive.plan_id or payload["root"] != archive.root:
            raise ValueError("提交预览与当前归档不一致")
        root = practice_root(Path(payload["root"]))
        reject_links(root / ".algo-coach.lock")
        with FileLock(root / ".algo-coach.lock", timeout=0):
            if not matches(archive):
                raise ValueError("归档在预览后发生变化，未提交")
            context = self.context(root)
            if context["branch"] != payload["branch"]:
                raise ValueError("分支已变化，请重新预览")
            if record["status"] in ("committed", "unchanged"):
                return {**payload, "status": record["status"]}
            recovered = self.recovered_commit(root, payload)
            if recovered:
                saved = self.operations.update(operation_id, "committed", commit_oid=recovered)
                return {**saved, "status": "committed"}
            if context["head"] != payload["head"]:
                raise ValueError("HEAD 已变化，请重新预览，不会自动合并或回退")
            staged = self.staged(root)
            if staged and (
                record["status"] not in ("staging", "committing")
                or not staged <= set(payload["paths"])
            ):
                raise ValueError("出现预览之外的暂存内容，已停止并保留现场")
            self.check_index(root, staged)
            if self.run(root, ["var", "GIT_AUTHOR_IDENT"], allow_failure=True).returncode:
                raise ValueError("请先在练习仓库配置 Git user.name 和 user.email")
            for text in archive.contents.values():
                if re.search(
                    r"-----BEGIN (?:[A-Z ]+)?PRIVATE KEY-----|sk-[A-Za-z0-9_-]{24,}", text
                ):
                    raise ValueError("归档疑似包含密钥，请检查后重新生成；未执行暂存")
            self.operations.update(operation_id, "staging")
            self.run(root, ["add", "--", *payload["paths"]])
            if not self.staged(root) <= set(payload["paths"]):
                raise ValueError("暂存内容超出归档清单，已停止并保留现场")
            # Verify the exact index bytes after Git's configured clean/EOL conversion.
            self.check_index(root, self.staged(root))
            if not self.staged(root):
                saved = self.operations.update(
                    operation_id, "unchanged", commit_oid=self.head(root)
                )
                return {**saved, "status": "unchanged"}
            tree = self.run(root, ["write-tree"]).stdout.decode().strip()
            self.operations.update(operation_id, "committing", intended_tree=tree)
            self.run(root, ["-c", "commit.gpgSign=false", "commit", "-m", payload["message"]])
            saved = self.operations.get(operation_id, "commit", session)["payload"]
            commit_oid = self.recovered_commit(root, saved)
            if not commit_oid:
                raise ValueError("提交结果与预期树或父提交不一致，请手动检查仓库")
            saved = self.operations.update(operation_id, "committed", commit_oid=commit_oid)
            return {**saved, "status": "committed"}

    def remote_tip(self, root: Path, branch: str) -> tuple[str | None, str]:
        url = self.run(root, ["remote", "get-url", "origin"]).stdout.decode().strip()
        result = self.run(root, ["ls-remote", "--heads", "origin", "refs/heads/" + branch])
        lines = result.stdout.decode().splitlines()
        if len(lines) > 1:
            raise ValueError("远端分支结果不明确")
        return (lines[0].split()[0] if lines else None), digest(url)

    def prepare_push(self, session: str, root: Path) -> dict:
        context = self.context(root)
        head, branch = context["head"], context["branch"]
        if not head:
            raise ValueError("当前分支尚无提交")
        tip, fingerprint = self.remote_tip(root, branch)
        if tip == head:
            commits = []
        elif tip:
            ancestor = self.run(
                root, ["merge-base", "--is-ancestor", tip, head], allow_failure=True
            )
            if ancestor.returncode:
                raise ValueError("远端历史未同步或不是本地祖先，请先手动 fetch 并检查")
            commits = self.run(root, ["rev-list", f"{tip}..{head}"]).stdout.decode().splitlines()
        else:
            commits = self.run(root, ["rev-list", head]).stdout.decode().splitlines()
        if not set(commits) <= self.operations.known_commits(str(root)):
            raise ValueError("待推送历史包含本工具未创建的提交，请手动审阅并推送这些提交")
        payload = {
            **context,
            "remote_tip": tip,
            "remote_fingerprint": fingerprint,
            "commits": commits,
            "remote": "origin",
        }
        operation_id = digest(json_text(payload))
        self.operations.prepare(operation_id, "push", session, payload)
        return {"operation_id": operation_id, **payload}

    def push(self, session: str, operation_id: str) -> dict:
        record = self.operations.get(operation_id, "push", session)
        payload = record["payload"]
        root = practice_root(Path(payload["root"]))
        reject_links(root / ".algo-coach.lock")
        with FileLock(root / ".algo-coach.lock", timeout=0):
            context = self.context(root)
            if context["head"] != payload["head"] or context["branch"] != payload["branch"]:
                raise ValueError("分支或 HEAD 已变化，请重新生成推送预览")
            tip, fingerprint = self.remote_tip(root, payload["branch"])
            if fingerprint != payload["remote_fingerprint"]:
                raise ValueError("远程地址已变化，请重新预览")
            if tip == payload["head"]:
                self.operations.update(operation_id, "pushed")
                return {"status": "pushed", "commit_oid": tip}
            if tip != payload["remote_tip"]:
                raise ValueError("远端分支已变化，请重新预览，不执行强制推送")
            self.operations.update(operation_id, "pushing")
            try:
                self.run(
                    root,
                    [
                        "push",
                        "--porcelain",
                        "--no-verify",
                        "origin",
                        payload["head"] + ":refs/heads/" + payload["branch"],
                    ],
                )
            except ValueError:
                self.operations.update(operation_id, "unknown")
                raise ValueError("推送结果未确认；本地提交保留，可重试以核对远端状态") from None
            try:
                confirmed, _ = self.remote_tip(root, payload["branch"])
            except ValueError:
                self.operations.update(operation_id, "unknown")
                raise ValueError("推送后的远端查询失败，状态标记 unknown，请重试核对") from None
            if confirmed != payload["head"]:
                self.operations.update(operation_id, "unknown")
                raise ValueError("无法确认远端提交，状态标记 unknown，请重试核对")
            self.operations.update(operation_id, "pushed")
            return {"status": "pushed", "commit_oid": confirmed}
