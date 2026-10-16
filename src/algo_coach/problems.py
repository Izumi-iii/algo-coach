"""Local-only problem catalogue. No network access or inferred problem statements."""

import json
import re
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlparse

from algo_coach.models import Problem

MAX_IMPORT_BYTES = 128 * 1024


def read_problem(path: Path) -> Problem:
    if path.stat().st_size > MAX_IMPORT_BYTES:
        raise ValueError("题目文件不能超过 128 KiB")
    return Problem.model_validate_json(path.read_text(encoding="utf-8-sig"))


class LocalProblems:
    def __init__(self, data_dir: Path):
        self.directory = data_dir / "problems"
        self.directory.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[Problem]:
        builtin = files("algo_coach").joinpath("data/problems.json").read_text(encoding="utf-8")
        problems = {p.key: p for p in map(Problem.model_validate, json.loads(builtin))}
        for path in sorted(self.directory.glob("*.json")):
            problem = read_problem(path)
            problems[problem.key] = problem
        return sorted(problems.values(), key=lambda p: (p.site, p.frontend_id))

    def import_file(self, path: Path) -> Problem:
        problem = read_problem(path)
        target = self.directory / f"{problem.site}-{problem.frontend_id}.json"
        # Atomic publication of one local catalogue entry, not a multi-file transaction.
        temporary = target.with_suffix(".tmp")
        temporary.write_text(problem.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)
        return problem

    def resolve(self, reference: str, site: str = "leetcode_cn") -> Problem:
        reference = reference.strip()
        if reference.startswith(("https://", "http://")):
            parsed = urlparse(reference)
            domains = {"leetcode.cn": "leetcode_cn", "leetcode.com": "leetcode_com"}
            if (
                parsed.scheme != "https"
                or parsed.hostname not in domains
                or parsed.username
                or parsed.password
                or parsed.port not in (None, 443)
            ):
                raise ValueError("只支持 HTTPS LeetCode 题目链接；不会请求网络")
            match = re.fullmatch(r"/problems/([a-z0-9-]+)(?:/.*)?", parsed.path)
            if not match:
                raise ValueError("无法识别题目链接")
            candidates = [
                p
                for p in self.all()
                if p.site == domains[parsed.hostname] and p.slug == match.group(1)
            ]
        else:
            identifier = re.sub(r"^leetcode\s*", "", reference, flags=re.IGNORECASE)
            if not identifier or not re.fullmatch(r"[A-Za-z0-9._-]{1,40}", identifier):
                raise ValueError("请输入题号、leetcode25 或已导入题目的链接")
            if identifier.isdigit():
                identifier = str(int(identifier))
            candidates = [p for p in self.all() if p.frontend_id == identifier and p.site == site]
        if len(candidates) != 1:
            raise ValueError("本地题库未找到该题；请先使用 import 导入。当前不联网获取题目。")
        return candidates[0]
