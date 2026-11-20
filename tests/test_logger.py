"""logger.py 单元测试。"""

import io
import json
import sys

import pytest

from shared import logger


@pytest.fixture(autouse=True)
def _restore_output():
    """用例结束后恢复默认输出流，避免重定向泄漏到其他测试。"""
    yield
    logger.set_output(sys.stdout)


def _single_record(stream: io.StringIO) -> dict:
    lines = stream.getvalue().strip().splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_info_writes_single_json_line():
    stream = io.StringIO()
    logger.set_output(stream)
    logger.info("collect_done", source="github", count=3)
    record = _single_record(stream)
    assert record["level"] == "INFO"
    assert record["event"] == "collect_done"
    assert record["source"] == "github"
    assert record["count"] == 3
    assert "ts" in record


def test_error_writes_error_level():
    stream = io.StringIO()
    logger.set_output(stream)
    logger.error("push_failed", channel="email")
    record = _single_record(stream)
    assert record["level"] == "ERROR"
    assert record["channel"] == "email"


def test_output_is_unescaped_utf8_json_lines():
    """中文原样输出（ensure_ascii=False），便于人肉排查 cron 日志。"""
    stream = io.StringIO()
    logger.set_output(stream)
    logger.info("采集完成", 数据源="飞书任务")
    value = stream.getvalue()
    assert "采集完成" in value
    assert "飞书任务" in value
    assert "\\u" not in value
