"""Trusted, host-side test generation and result validators. Never executes submissions."""

import hashlib
import json
import random
from importlib.resources import files

from algo_coach.judging import TestCase, two_sum_pairs
from algo_coach.judging import validate_output as validate_output
from algo_coach.models import Problem

SEED = 20261015
SUITE_VERSION = "small-differential-v1"
ADAPTER_VERSION = "python-functions-v1"


def problem_hash(problem: Problem) -> str:
    data = json.dumps(problem.model_dump(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def is_builtin_summary(problem: Problem) -> bool:
    entries = json.loads(files("algo_coach").joinpath("data/problems.json").read_text("utf-8"))
    return any(problem_hash(problem) == problem_hash(Problem.model_validate(p)) for p in entries)


def canonical_adapter(problem: Problem) -> str | None:
    """Only exact built-in or reviewed remote snapshots bind to a trusted suite."""
    entries = json.loads(files("algo_coach").joinpath("data/problems.json").read_text("utf-8"))
    for entry in entries:
        known = Problem.model_validate(entry)
        if problem_hash(problem) == problem_hash(known):
            return {"1": "two_sum", "25": "reverse_k_group"}.get(known.frontend_id)
    approved = json.loads(
        files("algo_coach").joinpath("data/remote_adapters.json").read_text("utf-8")
    )
    for entry in approved:
        if (
            problem.site == entry["site"]
            and problem.frontend_id == entry["frontend_id"]
            and problem.slug == entry["slug"]
            and problem_hash(problem) == entry["problem_hash"]
            and entry["adapter"] in {"two_sum", "reverse_k_group"}
        ):
            return entry["adapter"]
    return None


def build_cases(adapter: str) -> list[TestCase]:
    rng = random.Random(SEED)
    if adapter == "two_sum":
        inputs = [
            ([2, 7, 11, 15], 9),
            ([3, 2, 4], 6),
            ([3, 3], 6),
            ([0, 4, 3, 0], 0),
            ([-3, 4, 3, 90], 0),
            ([1, 2], 3),
            ([10**9, -(10**9), 2], 0),
        ]
        for _ in range(2000):
            nums = [rng.randint(-20, 20) for _ in range(rng.randint(2, 12))]
            a, b = rng.sample(range(len(nums)), 2)
            target = nums[a] + nums[b]
            if len(two_sum_pairs(nums, target)) == 1:
                inputs.append((nums, target))
            if len(inputs) >= 47:
                break
        return [
            TestCase(
                f"two-sum-{i:03}", {"nums": nums, "target": target}, two_sum_pairs(nums, target)[0]
            )
            for i, (nums, target) in enumerate(inputs)
        ]
    if adapter == "reverse_k_group":
        inputs = [
            ([1, 2, 3, 4, 5], 2),
            ([1, 2, 3, 4, 5], 3),
            ([1], 1),
            ([1, 2, 3], 1),
            ([1, 2, 3], 3),
            ([1, 1, 2, 1, 2], 2),
        ]
        for _ in range(40):
            nums = [rng.randint(0, 20) for _ in range(rng.randint(1, 20))]
            inputs.append((nums, rng.randint(1, len(nums))))
        cases = []
        for i, (nums, k) in enumerate(inputs):
            order = []
            for start in range(0, len(nums), k):
                group = list(range(start, min(start + k, len(nums))))
                order.extend(reversed(group) if len(group) == k else group)
            cases.append(
                TestCase(
                    f"reverse-k-{i:03}",
                    {"head": nums, "k": k},
                    [nums[index] for index in order],
                    order,
                )
            )
        return cases
    raise ValueError("未知适配器")
