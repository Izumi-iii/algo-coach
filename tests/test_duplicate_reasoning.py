"""Constraint reasoning must stay scoped and cannot certify arbitrary code."""

from itertools import product

from algo_coach.judging import two_sum_pairs
from algo_coach.problems import LocalProblems
from algo_coach.teaching import problem_reasoning, task_instruction


def test_reasoning_binds_to_reviewed_problem_not_frontend_number(tmp_path):
    problem = LocalProblems(tmp_path).resolve("1").model_dump()
    state = {"problem": problem, "current_code": "unparsed input", "action": "code"}
    text = problem_reasoning(state)
    assert "本身不是缺陷" in text
    assert "可能命中 i 本身" in text
    assert "不是当前代码正确性或执行结论" in text
    changed = {**problem, "constraints": ["必须返回最早的下标"]}
    assert problem_reasoning({**state, "problem": changed}) == ""
    assert problem_reasoning({**state, "problem": {"frontend_id": "1"}}) == ""
    assert (
        problem_reasoning({**state, "problem": LocalProblems(tmp_path).resolve("25").model_dump()})
        == ""
    )


def test_no_new_algorithm_context_for_statement_only_turn(tmp_path):
    state = {"problem": LocalProblems(tmp_path).resolve("1").model_dump(), "action": "discuss"}
    assert problem_reasoning(state) == ""
    instruction = task_instruction({**state, "hint_level": 0})
    assert "等值元素" not in instruction
    assert "不提供新算法步骤" in instruction
    assert problem_reasoning({**state, "action": "answer"})


def test_equal_processed_values_preserve_pair_validity_independently_of_candidate_code():
    # Check the stated lemma, not an implementation of twoSum. No submitted code executes.
    checked = 0
    for nums in product((-1, 0, 1), repeat=4):
        for i in range(2, 4):
            for j in range(i):
                for k in range(j + 1, i):
                    if nums[j] == nums[k]:
                        assert j != i and k != i
                        assert nums[j] + nums[i] == nums[k] + nums[i]
                        checked += 1
    assert checked > 0
    assert two_sum_pairs([1, 1, 2, 3], 5) == [[2, 3]]
    assert two_sum_pairs([1, 1, 4], 5) == [[0, 2], [1, 2]]
    assert two_sum_pairs([3, 3], 6) == [[0, 1]]
