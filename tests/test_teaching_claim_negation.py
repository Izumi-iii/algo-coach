import pytest

from algo_coach.errors import TeachingPolicyError
from algo_coach.models import TeachingReply
from algo_coach.teaching import validate_reply


def validate(text):
    validate_reply(
        TeachingReply(reply=text, hint_level_used=0, full_solution=False, stage="reflect"),
        {"hint_level": 0},
    )


@pytest.mark.parametrize(
    "text",
    [
        "我不会声称你的代码通过了测试。",
        "既不能声称它通过了本地测试，更不能声称平台已通过。",
        "不能声称 LeetCode Accepted。",
        "没有测试全部通过的执行记录。",
        "现在不能断言测试全部通过。",
    ],
)
def test_explicit_negated_claims_do_not_require_positive_execution_evidence(text):
    validate(text)


@pytest.mark.parametrize(
    "text",
    [
        "代码通过了测试。",
        "不能声称平台已通过，但平台已通过。",
        "我不会声称代码通过了测试，不过它测试全部通过。",
        "不能忽视代码通过了测试。",
        "没有错误，所以测试全部通过。",
        "不能保证别的代码正确；这份已通过全部测试。",
    ],
)
def test_negation_does_not_authorize_an_unrelated_positive_claim(text):
    with pytest.raises(TeachingPolicyError):
        validate(text)
