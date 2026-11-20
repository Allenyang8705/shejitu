"""errors.py 单元测试。"""

from shared.errors import AutoDailyError, CollectorError, GeneratorError, NotifierError


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
