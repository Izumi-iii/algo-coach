import pytest

from algo_coach.code_facts import code_facts


def test_length_subscript_distinguishes_exception_from_counterfactual_value():
    facts = code_facts("def f(items):\n    return items[len(items)]\n")
    item = facts["length_subscripts"][0]
    assert item["line"] == 2 and item["container"] == "items"
    assert "不会返回" in item["conditional_meaning"]
    assert "元素值，不是位置编号" in item["conditional_meaning"]
    assert "len未被重定义" in item["scope"]


@pytest.mark.parametrize(
    "expression", ["items[0]", "items[len(other)]", "items[len(items)-1]", "len(items)"]
)
def test_length_subscript_does_not_generalize_other_expressions(expression):
    assert code_facts("def f(items):\n    return " + expression)["length_subscripts"] == []


def test_first_pointer_write_resolves_entry_alias_without_running_code():
    facts = code_facts(
        "previous, current = after, before.next\nwhile current is not after:\n"
        "    following = current.next\n    current.next = previous\n"
        "    previous, current = current, following\n"
    )
    write = facts["loops"][0]["first_attribute_write"]
    assert write["line"] == 4
    assert write["target_at_entry"] == "(before.next).next"
    assert write["value_at_entry"] == "after"
    assert "不证明循环次数" in facts["loops"][0]["scope"]


@pytest.mark.parametrize(
    "prefix",
    [
        "if condition:\n        pass",
        "work()",
        "current = other",
        "after = other",
        "obj.attr = other",
    ],
)
def test_branch_side_effect_or_rebinding_prevents_alias_claim(prefix):
    code = (
        "previous, current = after, before.next\nwhile current is not after:\n    "
        + prefix
        + "\n    current.next = previous\n"
    )
    assert code_facts(code)["loops"] == []


def test_renamed_variables_use_same_syntactic_rule():
    facts = code_facts(
        "p, c = tail, front.next\nwhile c is not tail:\n    following = c.next\n    c.next = p\n"
    )
    assert facts["loops"][0]["first_attribute_write"]["value_at_entry"] == "tail"


def test_syntax_error_is_not_dynamic_execution():
    assert code_facts("def broken(:")["syntax_valid"] is False


def test_ast_analysis_does_not_execute_top_level_side_effects(tmp_path):
    target = tmp_path / "never-created"
    facts = code_facts(f"open({str(target)!r}, 'w').write('no')")
    assert facts["syntax_valid"]
    assert not target.exists()


def test_empty_local_facts_are_not_a_claim_that_for_loop_is_absent():
    facts = code_facts("for item in values:\n    print(item)\n")
    assert facts["loops"] == []
    assert "不表示代码没有 for/while" in facts["coverage"]
