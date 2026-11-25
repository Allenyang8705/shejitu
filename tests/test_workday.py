"""workday.py 单元测试。"""

from datetime import date

from shared.config import Config
from shared.workday import is_workday


def _config(
    holidays: list[date] | None = None, extra: list[date] | None = None
) -> Config:
    return Config(
        team_name="t",
        repos=[],
        chats=[],
        message_keywords=[],
        sensitive_keywords=[],
        members=[],
        holidays=holidays or [],
        extra_workdays=extra or [],
    )


def test_friday_is_workday():
    assert is_workday(date(2026, 11, 20), _config()) is True  # 周五


def test_weekend_not_workday():
    assert is_workday(date(2026, 11, 21), _config()) is False  # 周六
    assert is_workday(date(2026, 11, 22), _config()) is False  # 周日


def test_holiday_overrides_weekday():
    # 2026-11-20 是周五，但列入节假日表
    day = date(2026, 11, 20)
    assert is_workday(day, _config(holidays=[day])) is False


def test_extra_workday_overrides_weekend():
    # 周六调休补班
    day = date(2026, 11, 21)
    assert is_workday(day, _config(extra=[day])) is True


def test_extra_workday_wins_over_holiday():
    """同日既列节假日又列补班：补班优先，宁可多跑一次也不静默缺报。"""
    day = date(2026, 11, 20)
    assert is_workday(day, _config(holidays=[day], extra=[day])) is True
