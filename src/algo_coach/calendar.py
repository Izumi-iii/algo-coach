"""Learning dates use the user's calendar, not a guessed platform update boundary."""

import re
from datetime import date, datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")


def today() -> date:
    return datetime.now(SHANGHAI).date()


def parse_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise ValueError("日期请使用 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError("日期不存在，请使用有效的 YYYY-MM-DD") from None
