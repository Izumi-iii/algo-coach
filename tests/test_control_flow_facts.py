"""The extractor recognizes only straight-line dictionary scans, without execution."""

import pytest

from algo_coach.code_facts import code_facts
from algo_coach.teaching import task_instruction
from evaluation.fixtures import CODE


def scans(code):
    return code_facts(code)["mapping_scans"]


def test_current_version_reverses_order_and_line_evidence():
    old = scans(CODE["two_sum_self"])[0]
    new = scans(CODE["two_sum_ok"])[0]
    assert old["order"] == "write_before_lookup"
    assert (old["write_line"], old["lookup_line"], old["return_line"]) == (5, 6, 7)
    assert "返回[0,0]" in old["first_iteration"]
    assert "不会进入第二轮" in old["first_iteration"]
    assert new["order"] == "lookup_before_write"
    assert (new["lookup_line"], new["write_line"], new["return_line"]) == (9, 11, 10)
    assert [s["kind"] for s in new["sequence"]] == ["lookup_then_return_if_hit", "write"]
    assert "本轮后面的写入不执行" in new["first_iteration"]


@pytest.mark.parametrize("bom", ["", "\ufeff"])
def test_renaming_and_comments_preserve_conditional_local_facts(bom):
    code = CODE["two_sum_self"].replace("seen", "table").replace("value", "item")
    fact = scans(bom + "# comment\n" + code)[0]
    assert fact["mapping"] == "table" and fact["value"] == "item"
    assert fact["lookup_line"] == 7
    assert "不执行提交" in fact["scope"]


@pytest.mark.parametrize(
    "old,new",
    [
        ("for i, value in enumerate(nums):", "for i, value in enumerate(nums, 1):"),
        ("seen = {}", "seen = {0: 0}"),
        ("seen = {}", "seen = custom_dict()"),
        ("seen[value] = i", "seen[value] = i\n            work()"),
        ("seen[value] = i", "seen[value] = i\n            i = 0"),
        ("seen[value] = i", "seen[value] = i\n            if flag:\n                continue"),
        ("return [seen[target - value], i]", "yield [seen[target - value], i]"),
        ("return [seen[target - value], i]", "result = [seen[target - value], i]"),
        ("seen[value] = i", "seen[value] = i + 1"),
        ("if target - value in seen:", "if target - value not in seen:"),
        ("for i, value in enumerate(nums):", "for value, value in enumerate(nums):"),
        ("seen = {}", "seen = {}\n        callback()"),
    ],
)
def test_nearby_but_unsupported_patterns_do_not_get_flow_conclusions(old, new):
    assert scans(CODE["two_sum_self"].replace(old, new)) == []


def test_try_finally_and_else_cannot_borrow_return_termination_claim():
    original = CODE["two_sum_self"]
    wrapped = original.replace(
        "            return [seen[target - value], i]",
        "            try:\n                return [seen[target - value], i]\n"
        "            finally:\n                keep_working()",
    )
    assert scans(wrapped) == []
    assert scans(original + "            else:\n                return []\n") == []


def test_assignment_aliasing_and_large_ast_are_rejected():
    assert scans(CODE["two_sum_self"].replace("seen", "nums")) == []
    huge = "\n".join("x = 0" for _ in range(1500)) + "\n" + CODE["two_sum_self"]
    facts = code_facts(huge)
    assert facts["mapping_scans"] == [] and "analysis_skipped" in facts


def test_extractor_never_executes_top_level_or_loop_calls(tmp_path):
    target = tmp_path / "not-created"
    code = f"open({str(target)!r}, 'w').write('no')\n" + CODE["two_sum_self"]
    facts = code_facts(code)
    assert facts["syntax_valid"]
    assert not target.exists()


def test_current_task_repeats_flow_after_stale_diagnosis_without_full_solution():
    task = task_instruction(
        {
            "action": "code",
            "hint_level": 1,
            "current_code": CODE["two_sum_ok"],
            "history": [{"role": "assistant", "content": "STALE 先写后查"}],
        }
    )
    assert "第9行查询，第11行写入" in task
    assert "lookup_before_write" in task
    assert "后续语句和下一轮均不再执行" in task
    assert "STALE" not in task
    assert "不据此提供完整修复实现" in task
