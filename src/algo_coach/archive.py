"""Deterministic reviewable bundles; publication never overwrites existing files."""

import hashlib
import json
import re
import shutil
from dataclasses import asdict
from importlib.resources import files
from pathlib import Path
from uuid import uuid4

from filelock import FileLock
from pydantic import BaseModel, ConfigDict

from algo_coach.adapters import (
    SEED,
    SUITE_VERSION,
    build_cases,
    canonical_adapter,
    is_builtin_summary,
)
from algo_coach.models import Attempt, Problem, Verification
from algo_coach.paths import practice_root, reject_links, safe_path


def digest(data: str | bytes) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def json_text(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def learning_snapshot(state: dict) -> dict:
    if not state.get("current_code") or not state.get("attempts"):
        raise ValueError("请先使用 /code 保存要归档的代码")
    if not re.fullmatch(r"[0-9a-f]{32}", state["session_id"]):
        raise ValueError("会话标识不合法")
    problem = Problem.model_validate(state["problem"])
    attempts = [Attempt.model_validate(attempt).model_dump() for attempt in state["attempts"]]
    code_hash = digest(state["current_code"])
    if code_hash != state["code_hash"] or attempts[-1]["code_hash"] != code_hash:
        raise ValueError("当前代码与尝试记录不一致")
    verification = Verification.model_validate(state["verification"]).model_dump()
    if verification["code_hash"] != code_hash:
        raise ValueError("验证结果属于其他代码版本")
    summary = state.get("solution_summary")
    if summary and summary.get("code_hash") != code_hash:
        summary = None
    snapshot = {
        "schema_version": 1,
        "session_id": state["session_id"],
        "mode": state["mode"],
        "problem": problem.model_dump(),
        "attempts": attempts,
        "code": state["current_code"],
        "code_hash": code_hash,
        "verification": verification,
        "reflection": state.get("reflection", ""),
        "explanation": state.get("explanation", ""),
        "solution_summary": summary,
        "answer_revealed": state.get("answer_revealed", False),
        "max_hint_level_used": state.get("max_hint_level_used", state.get("hint_level", 0)),
        "completion_mode": state.get("completion_mode", "unfinished"),
    }
    for name in ("daily_selection", "review_context", "problem_source"):
        if state.get(name):
            snapshot[name] = state[name]
    return snapshot


class ArchivePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    plan_id: str
    root: str
    relative: str
    snapshot_hash: str
    bundle_hash: str
    contents: dict[str, str]
    commit_message: str

    def preview(self) -> str:
        rows = [
            f"归档计划：{self.plan_id}",
            f"目标：{Path(self.root) / self.relative}",
            f"建议提交信息：{self.commit_message}",
            "文件清单：",
        ]
        rows += [
            f"  {name} ({len(content.encode('utf-8'))} bytes)"
            for name, content in sorted(self.contents.items())
        ]
        rows.append("使用 /archive show 文件名 查看内容；确认后 /archive apply 写入。")
        return "\n".join(rows)


def render_bundle(snapshot: dict) -> dict[str, str]:
    problem = Problem.model_validate(snapshot["problem"])
    report = snapshot["verification"]
    adapter = canonical_adapter(problem)
    problem_md = (
        f"# {problem.frontend_id}. {problem.title}\n\n来源：{problem.url or '用户本地题目'}\n\n"
    )
    if is_builtin_summary(problem):
        problem_md += problem.render() + "\n"
    else:
        problem_md += (
            "远程或导入题面未自动再分发；请通过来源或原始本地文件查看，并自行填写有权使用的摘要。\n"
        )
    summary = snapshot["solution_summary"]
    explanation = "# 解题思路\n\n"
    if snapshot["explanation"]:
        explanation += "## 用户提供的思路\n\n" + snapshot["explanation"] + "\n\n"
    if summary:
        explanation += "## 模型整理草稿（待用户审阅，不作为正确性证明）\n\n"
        for label, key in [
            ("思路", "approach"),
            ("正确性分析", "correctness"),
            ("复杂度分析", "complexity"),
            ("限制", "limitations"),
        ]:
            explanation += f"### {label}\n\n{summary['content'][key]}\n\n"
    if not summary and not snapshot["explanation"]:
        explanation += (
            "用户尚未填写思路。可用 /explain 提供自己的分析，或 /summarize 请求模型整理代码。\n\n"
        )
    explanation += f"## 验证证据\n\n状态：`{report['status']}`。{report['detail']}\n"
    reflection = (
        "# 反思\n\n## 用户原始记录\n\n"
        + (snapshot["reflection"] or "用户尚未填写，未自动补写学习经历。")
        + "\n\n## 复习问题（Agent 建议）\n\n- 关键不变量是什么？\n"
        "- 哪个边界最容易漏掉？\n- 能否不看旧代码重新实现？\n"
    )
    metadata = {
        key: snapshot[key]
        for key in (
            "schema_version",
            "session_id",
            "mode",
            "code_hash",
            "completion_mode",
            "answer_revealed",
            "max_hint_level_used",
        )
    }
    metadata.update(
        {
            "problem_key": problem.key,
            "platform_result": "not_verified",
            "code_origin": "user_submitted_authorship_not_verified",
            "summary_status": "model_draft" if summary else "user_notes_or_missing",
        }
    )
    metadata["problem_hash"] = digest(json_text(snapshot["problem"]))
    for name in ("daily_selection", "review_context", "problem_source"):
        if snapshot.get(name):
            # Keep selection provenance without redistributing another full statement.
            metadata[name] = {
                key: value for key, value in snapshot[name].items() if key != "problem"
            }
    metadata["attempts"] = [
        {
            key: attempt[key]
            for key in (
                "attempt_id",
                "code_hash",
                "created_at",
                "origin",
            )
        }
        for attempt in snapshot["attempts"]
    ]
    result = {
        "problem.md": problem_md,
        "solution.py": snapshot["code"],
        "explanation.md": explanation,
        "reflection.md": reflection,
        "metadata.json": json_text(metadata),
        "verification/report.json": json_text(report),
    }
    for index, attempt in enumerate(snapshot["attempts"], 1):
        result[f"attempts/attempt-{index:03}.py"] = attempt["code"]
        result[f"verification/attempt-{index:03}.json"] = json_text(
            {
                "latest": attempt["verification"],
                "history": attempt["verification_history"],
            }
        )
    package = files("algo_coach")
    worker = package.joinpath("sandbox_worker.py").read_text("utf-8")
    image = "algo-coach-replay:" + digest(worker)[:12]
    suite = {
        "adapter": adapter,
        "random_seed": SEED,
        "suite_version": SUITE_VERSION,
        "image": image,
        "cases": [asdict(case) for case in build_cases(adapter)] if adapter else [],
    }
    result["tests/cases.json"] = json_text(suite)
    for name in ("runner.py", "judging.py", "sandbox_worker.py", "replay.py"):
        result[f"tests/{name}"] = package.joinpath(name).read_text("utf-8")
    result["tests/Dockerfile"] = (
        "FROM python:3.12-slim\nWORKDIR /app\nCOPY sandbox_worker.py /app/worker.py\n"
        'USER 65534:65534\nENTRYPOINT ["python", "-I", "-B", "/app/worker.py"]\n'
    )
    result["tests/README.md"] = (
        "# 归档复验\n\n在此归档根目录运行，所需 Python >= 3.11，无第三方 Python 依赖。\n\n"
        "```powershell\npython tests/replay.py --check\n"
        f"docker build --file tests/Dockerfile --tag {image} tests\n"
        "python tests/replay.py\n```\n\n需要已配置的 Linux Docker Engine。候选代码只在容器中执行。"
        "`--check` 只核对文件哈希；哈希不构成作者身份或防篡改签名。"
        "代码修改后需重新归档，不能沿用本报告。测试只是有限本地验证，不是平台 Accepted。\n"
    )
    return result


def plan_archive(state: dict, directory: Path) -> ArchivePlan:
    root = practice_root(directory)
    snapshot = learning_snapshot(state)
    contents = render_bundle(snapshot)
    hashes = {name: digest(text) for name, text in sorted(contents.items())}
    bundle_hash = digest(json_text(hashes))
    problem = Problem.model_validate(snapshot["problem"])
    identifier = (
        problem.frontend_id.zfill(4) if problem.frontend_id.isdigit() else problem.frontend_id
    )
    base = f"problems/{problem.site.replace('_', '-')}/{identifier}-{problem.slug}"

    def assemble(relative):
        plan_id = digest(json_text({"root": str(root), "relative": relative, "files": hashes}))
        manifest = {
            "schema_version": 1,
            "plan_id": plan_id,
            "bundle_hash": bundle_hash,
            "files": hashes,
        }
        return ArchivePlan(
            plan_id=plan_id,
            root=str(root),
            relative=relative,
            snapshot_hash=digest(json_text(snapshot)),
            bundle_hash=bundle_hash,
            contents={**contents, "archive-manifest.json": json_text(manifest)},
            commit_message=(
                f"practice({problem.site.replace('_', '-')}): "
                f"archive {identifier} solution and reflection"
            ),
        )

    initial = assemble(base)
    target = safe_path(root, base)
    if not target.exists() or matches(initial):
        return initial
    return assemble(f"{base}/reviews/{snapshot['session_id']}-{bundle_hash[:12]}")


def matches(plan: ArchivePlan) -> bool:
    root = practice_root(Path(plan.root))
    for relative, text in plan.contents.items():
        path = safe_path(root, f"{plan.relative}/{relative}")
        if not path.is_file() or digest(path.read_bytes()) != digest(text):
            return False
    return True


def publish(plan: ArchivePlan) -> dict:
    root = practice_root(Path(plan.root))
    reject_links(root / ".algo-coach.lock")
    with FileLock(root / ".algo-coach.lock", timeout=0):
        target = safe_path(root, plan.relative)
        if target.exists():
            if matches(plan):
                return {"plan_id": plan.plan_id, "path": str(target), "status": "unchanged"}
            raise ValueError("预览目标已存在且内容不同；未覆盖，请重新生成归档预览")
        stage = root / (".algo-coach-stage-" + uuid4().hex)
        stage.mkdir()
        try:
            for name, text in plan.contents.items():
                path = safe_path(stage, name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(text.encode("utf-8"))
                if digest(path.read_bytes()) != digest(text):
                    raise ValueError("暂存文件校验失败")
            reject_links(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise ValueError("目标在发布前发生变化；请重新预览")
            stage.rename(target)
        finally:
            if stage.exists():
                reject_links(stage)
                if not stage.resolve().is_relative_to(root) or not stage.name.startswith(
                    ".algo-coach-stage-"
                ):
                    raise ValueError("拒绝清理异常暂存路径")
                shutil.rmtree(stage)
        return {"plan_id": plan.plan_id, "path": str(target), "status": "written"}
