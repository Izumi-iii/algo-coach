"""Reference example for manually checking the container, not a hidden teaching input."""


class Solution:
    def twoSum(self, nums: list[int], target: int) -> list[int]:
        seen = {}
        for index, value in enumerate(nums):
            other = target - value
            if other in seen:
                return [seen[other], index]
            seen[value] = index
        raise ValueError("No valid pair")
