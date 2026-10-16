import subprocess

import pytest

from algo_coach.git_ops import GitService
from algo_coach.service import Coach
from algo_coach.teaching import DemoTeacher


def git(root, *args):
    return (
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
        .stdout.decode("utf-8")
        .strip()
    )


@pytest.fixture
def practice(tmp_path):
    root = tmp_path / "practice"
    root.mkdir()
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "AlgoCoach Test")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    return root


def archive(coach, practice):
    session = coach.start(coach.problems.resolve("1"))["session_id"]
    coach.turn(session, "代码", code="class Solution:\r\n    pass\r\n")
    coach.explain(session, "当前尚未实现。")
    plan = coach.preview_archive(session, practice)
    coach.apply_archive(session)
    return session, plan


def commit(coach, session):
    plan = coach.preview_commit(session)
    return coach.apply_commit(session, plan["operation_id"])


def test_commit_includes_only_archive_and_is_idempotent(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "commit_on_request")
    sentinel = practice / "unrelated.txt"
    sentinel.write_text("not part of archive")
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, exported = archive(coach, practice)
        plan = coach.preview_commit(session)
        assert not git(practice, "diff", "--cached", "--name-only")
        first = coach.apply_commit(session)
        second = coach.apply_commit(session)
        assert first["commit_oid"] == second["commit_oid"]
        assert git(practice, "rev-list", "--count", "HEAD") == "1"
        committed = set(git(practice, "ls-tree", "-r", "--name-only", "HEAD").splitlines())
        assert committed == set(plan["paths"])
        assert "unrelated.txt" not in committed
        assert "archive-manifest.json" in exported.contents
        assert sentinel.read_text() == "not part of archive"


def test_preexisting_staging_is_preserved(tmp_path, practice):
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        (practice / "other.txt").write_text("staged by user")
        git(practice, "add", "other.txt")
        with pytest.raises(ValueError, match="已有暂存"):
            coach.preview_commit(session)
        assert git(practice, "diff", "--cached", "--name-only") == "other.txt"


def test_staging_changes_after_preview_prevent_commit(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "commit_on_request")
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        coach.preview_commit(session)
        (practice / "other.txt").write_text("staged by user")
        git(practice, "add", "other.txt")
        with pytest.raises(ValueError, match="暂存内容"):
            coach.apply_commit(session)
        assert git(practice, "diff", "--cached", "--name-only") == "other.txt"


def test_changed_archive_and_changed_head_are_not_committed(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "commit_on_request")
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, plan = archive(coach, practice)
        coach.preview_commit(session)
        path = practice / plan.relative / "solution.py"
        path.write_text("user modification")
        with pytest.raises(ValueError, match="发生变化"):
            coach.apply_commit(session)
        path.write_bytes(plan.contents["solution.py"].encode("utf-8"))
        (practice / "other.txt").write_text("external commit")
        git(practice, "add", "other.txt")
        git(practice, "commit", "-m", "external")
        with pytest.raises(ValueError, match="HEAD 已变化"):
            coach.apply_commit(session)


def test_commit_respects_git_eol_conversion(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "commit_on_request")
    git(practice, "config", "core.autocrlf", "true")
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        assert commit(coach, session)["status"] == "committed"


def test_recovery_never_overwrites_a_user_modified_index_blob(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "commit_on_request")
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, exported = archive(coach, practice)
        pending = coach.preview_commit(session)
        coach.operations.update(pending["operation_id"], "staging")
        name = exported.relative + "/solution.py"
        target = practice / name
        target.write_text("user staged content", encoding="utf-8")
        git(practice, "add", "--", name)
        target.write_bytes(exported.contents["solution.py"].encode("utf-8"))
        with pytest.raises(ValueError, match="暂存内容与归档"):
            coach.apply_commit(session)
        assert git(practice, "show", ":" + name) == "user staged content"


def test_commit_recovery_after_git_succeeded_but_journal_update_failed(
    tmp_path, practice, monkeypatch
):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "commit_on_request")
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        plan = coach.preview_commit(session)
        update = coach.operations.update

        def fail_once(operation_id, status, **fields):
            if status == "committed":
                raise RuntimeError("simulated crash after commit")
            return update(operation_id, status, **fields)

        monkeypatch.setattr(coach.operations, "update", fail_once)
        with pytest.raises(RuntimeError, match="simulated crash"):
            coach.apply_commit(session)
        head = git(practice, "rev-parse", "HEAD")
        monkeypatch.setattr(coach.operations, "update", update)
        assert coach.apply_commit(session, plan["operation_id"])["commit_oid"] == head
        assert git(practice, "rev-list", "--count", "HEAD") == "1"


def test_export_only_policy_blocks_commit_and_push(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "export_only")
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        coach.preview_commit(session)
        with pytest.raises(ValueError, match="策略禁止"):
            coach.apply_commit(session)
        with pytest.raises(ValueError, match="策略禁止"):
            coach.apply_push(session)


def test_local_bare_remote_push_and_retry_are_safe(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "push_on_request")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(practice, "remote", "add", "origin", str(remote))
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        local = commit(coach, session)
        plan = coach.preview_push(session)
        assert plan["commits"] == [local["commit_oid"]]
        result = coach.apply_push(session)
        assert result["status"] == "pushed"
        assert coach.apply_push(session)["commit_oid"] == local["commit_oid"]
        assert git(remote, "rev-parse", "refs/heads/main") == local["commit_oid"]


def test_push_refuses_unreviewed_external_commits(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "push_on_request")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(practice, "remote", "add", "origin", str(remote))
    (practice / "README.md").write_text("user commit")
    git(practice, "add", "README.md")
    git(practice, "commit", "-m", "outside tool")
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        commit(coach, session)
        with pytest.raises(ValueError, match="工具未创建"):
            coach.preview_push(session)
        assert not git(remote, "for-each-ref", "--format=%(refname)", "refs/heads")


def test_push_detects_remote_change_and_does_not_leak_url(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "push_on_request")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(practice, "remote", "add", "origin", str(remote))
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        commit(coach, session)
        coach.preview_push(session)
        replacement = tmp_path / "replacement.git"
        subprocess.run(["git", "init", "--bare", str(replacement)], check=True, capture_output=True)
        git(practice, "remote", "set-url", "origin", str(replacement))
        with pytest.raises(ValueError, match="远程地址已变化"):
            coach.apply_push(session)


def test_push_recovers_unknown_result_without_new_commit(tmp_path, practice, monkeypatch):
    monkeypatch.setenv("ALGOCOACH_GIT_POLICY", "push_on_request")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    git(practice, "remote", "add", "origin", str(remote))
    with Coach(tmp_path / "data", DemoTeacher()) as coach:
        session, _ = archive(coach, practice)
        local = commit(coach, session)
        coach.preview_push(session)
        execute = GitService.run

        def lost_response(self, root, args, **options):
            result = execute(self, root, args, **options)
            if args[0] == "push":
                raise ValueError("simulated connection loss")
            return result

        monkeypatch.setattr(GitService, "run", lost_response)
        with pytest.raises(ValueError, match="结果未确认"):
            coach.apply_push(session)
        monkeypatch.setattr(GitService, "run", execute)
        assert coach.apply_push(session)["commit_oid"] == local["commit_oid"]
        assert git(practice, "rev-list", "--count", "HEAD") == "1"
