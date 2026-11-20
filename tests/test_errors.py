"""errors.py 单元测试。"""

import pytest

from shared.errors import (
    AutoDailyError,
    CollectorError,
    GeneratorError,
    NotifierError,
    call_with_retry,
)


def test_three_required_exceptions_defined():
    """tasks.md Task 2：定义 CollectorError / GeneratorError / NotifierError。"""
    assert issubclass(AutoDailyError, Exception)
    for exc_type in (CollectorError, GeneratorError, NotifierError):
        assert issubclass(exc_type, AutoDailyError)


def test_error_carries_source_and_message():
    err = CollectorError("github", "API 超时")
    assert err.source == "github"
    assert err.message == "API 超时"
    assert "[github]" in str(err)
    assert "API 超时" in str(err)


class _Flaky:
    """按预设次数抛错的可调用对象，用于验证重试语义。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ValueError("boom")
        return 42


def test_retry_success_first_try():
    op = _Flaky(0)
    assert call_with_retry(op, retries=3, interval=0, retry_on=(ValueError,)) == 42
    assert op.calls == 1


def test_retry_recovers_within_budget():
    op = _Flaky(2)
    assert call_with_retry(op, retries=3, interval=0, retry_on=(ValueError,)) == 42
    assert op.calls == 3


def test_retry_exhausted_reraises_last_error():
    op = _Flaky(99)
    with pytest.raises(ValueError, match="boom"):
        call_with_retry(op, retries=3, interval=0, retry_on=(ValueError,))
    assert op.calls == 4  # 首次 1 次 + 重试 3 次（design.md §6.1）


def test_retry_passes_through_unlisted_exceptions():
    def op() -> int:
        raise KeyError("nope")

    with pytest.raises(KeyError):
        call_with_retry(op, retries=3, interval=0, retry_on=(ValueError,))
