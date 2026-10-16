"""Copied into archive/tests; uses only Python's standard library and a local Docker CLI."""

import argparse
import hashlib
import json
import sys
from pathlib import Path


def main():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="复验 AlgoCoach 归档（候选代码只在 Docker 中执行）"
    )
    parser.add_argument("--check", action="store_true", help="只校验归档文件，不执行代码")
    args = parser.parse_args()
    folder = Path(__file__).resolve().parent
    root = folder.parent
    manifest = json.loads((root / "archive-manifest.json").read_text("utf-8"))
    for name, expected in manifest["files"].items():
        path = root / name
        if not path.resolve().is_relative_to(root) or path.is_symlink():
            raise ValueError("归档文件路径不安全")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"归档文件已改变：{name}")
    if args.check:
        print("归档文件哈希一致；未执行候选代码。")
        return 0
    # These modules are vendored alongside this script, not installed algo_coach dependencies.
    from judging import TestCase, validate_output
    from runner import DockerRunner

    suite = json.loads((folder / "cases.json").read_text("utf-8"))
    if suite["adapter"] is None:
        print("此题没有导出可执行适配器。")
        return 1
    cases = [TestCase(**case) for case in suite["cases"]]
    result = DockerRunner(enabled=True, image=suite["image"]).run(
        (root / "solution.py").read_bytes().decode("utf-8"),
        suite["adapter"],
        [{"case_id": case.case_id, "inputs": case.inputs} for case in cases],
    )
    if result.status != "ok" or len(result.records or []) != len(cases):
        print(f"未获得完整测试结果：{result.status}，{result.detail}")
        return 1
    passed = 0
    for case, record in zip(cases, result.records, strict=True):
        if not isinstance(record, dict) or record.get("case_id") != case.case_id:
            raise ValueError("用例身份不匹配")
        if record.get("status") == "ok" and isinstance(record.get("output"), dict):
            valid, reason = validate_output(suite["adapter"], case, record["output"])
        else:
            valid, reason = False, "运行异常或资源限制"
        if valid:
            passed += 1
        else:
            print(
                json.dumps(
                    {
                        "input": case.inputs,
                        "expected": case.expected,
                        "actual": record.get("output"),
                        "reason": reason,
                    },
                    ensure_ascii=False,
                )
            )
            break
    print(f"本地复验：{passed}/{len(cases)}；不是平台提交结果。")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    raise SystemExit(main())
