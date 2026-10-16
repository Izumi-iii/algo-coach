import pytest

from algo_coach.errors import TeachingOutputError
from algo_coach.models import TeachingReply
from algo_coach.service import Coach


class LightHintTeacher:
    mode = "model"

    def reply(self, state):
        return TeachingReply(
            reply="反转这一组之前，你需要保留哪几个连接位置？",
            hint_level_used=1,
            stage="explore",
            full_solution=False,
        )


@pytest.mark.parametrize(
    "text,expected",
    [
        ("我知道需要分组反转，但不知道怎么保存前后节点的连接。", 1),
        ("如何处理这个边界？", 1),
        ("我没有思路", 1),
        ("先只解释题意，不要给提示", 0),
        ("不知道怎么理解，但只复述题意，不要提示", 0),
    ],
)
def test_explicit_help_allows_only_a_light_question(tmp_path, text, expected):
    with Coach(tmp_path, LightHintTeacher()) as coach:
        session = coach.start(coach.problems.resolve("25"))["session_id"]
        state = coach.turn(session, text)
        assert state["hint_level"] == expected
        assert state["last_error"] == (None if expected else "reply_policy_rejected")
        assert not state["answer_revealed"]


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, "authentication_failed"),
        (402, "insufficient_balance"),
        (400, "request_rejected"),
        (429, "rate_limited"),
    ],
)
def test_known_service_errors_are_safe_and_not_blindly_retried(tmp_path, status, expected):
    class FailedTeacher:
        mode = "model"
        calls = 0

        def reply(self, state):
            self.calls += 1
            error = RuntimeError("SECRET-KEY and private provider response")
            error.status_code = status
            raise error

    teacher = FailedTeacher()
    with Coach(tmp_path, teacher) as coach:
        session = coach.start(coach.problems.resolve("25"))["session_id"]
        state = coach.turn(session, "请先解释题意")
        assert state["last_error"] == expected
        assert "SECRET-KEY" not in str(state)
        assert teacher.calls == 1


def test_retry_includes_correction_and_does_not_raise_permissions(tmp_path):
    class RepairTeacher(LightHintTeacher):
        calls = 0

        def reply(self, state):
            self.calls += 1
            if self.calls == 1:
                raise TeachingOutputError("private raw response")
            assert "invalid_model_output" in state["retry_feedback"]
            assert state["hint_level"] == 1
            return super().reply(state)

    with Coach(tmp_path, RepairTeacher()) as coach:
        session = coach.start(coach.problems.resolve("25"))["session_id"]
        state = coach.turn(session, "/hint")
        assert state["last_error"] is None
        assert "private raw response" not in str(state)
        assert not state["answer_revealed"]
