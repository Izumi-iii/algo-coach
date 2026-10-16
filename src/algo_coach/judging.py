"""Dependency-free result validators, also copied into exported practice bundles."""

from dataclasses import dataclass


@dataclass(frozen=True)
class TestCase:
    case_id: str
    inputs: dict
    expected: list
    expected_order: list[int] | None = None


def two_sum_pairs(nums: list[int], target: int) -> list[list[int]]:
    return [
        [i, j]
        for i in range(len(nums))
        for j in range(i + 1, len(nums))
        if nums[i] + nums[j] == target
    ]


def validate_output(adapter: str, case: TestCase, output: dict) -> tuple[bool, str]:
    value = output.get("value")
    if not isinstance(value, list) or any(type(item) is not int for item in value):
        return False, "返回值必须是整数列表或能转换为整数列表的有效链表"
    if adapter == "two_sum":
        if len(value) != 2 or len(set(value)) != 2:
            return False, "需要返回两个不同位置的下标"
        if not all(0 <= index < len(case.inputs["nums"]) for index in value):
            return False, "返回的下标越界"
        if sum(case.inputs["nums"][index] for index in value) != case.inputs["target"]:
            return False, "下标对应元素之和不等于目标值"
        return True, "有效下标对（顺序不限）"
    if output.get("cycle"):
        return False, "结果链表出现环或长度超过原链表"
    if value != case.expected:
        return False, "结果节点值的顺序不符合分组反转要求"
    order = output.get("node_order")
    if (
        not isinstance(order, list)
        or any(type(index) is not int for index in order)
        or order != case.expected_order
    ):
        return False, "节点连接不符合要求，可能只交换了节点值或创建了替代节点"
    return True, "节点值和原节点连接顺序均符合要求"
