"""lark_bot.py 单元测试（httpx.MockTransport 模拟飞书自定义机器人 webhook）。"""

import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest

from notifier import lark_bot
from shared.models import DailyReport

WEBHOOK_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/test-token-placeholder"
SECRET = "test-sign-secret-placeholder"

_OK_BODY = {"StatusCode": 0, "StatusMessage": "success", "code": 0}


def _report(markdown: str = "# 团队日报\n- 张三：修复登录 bug") -> DailyReport:
    return DailyReport(
        date=date(2026, 11, 20),
        team_name="研发团队",
        members=[],
        generated_at=datetime(2026, 11, 20, 18, 0, tzinfo=UTC),
        markdown=markdown,
    )


def _use_client(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    """让真实 _build_client 走 MockTransport，保留其超时等生产配置。"""
    real_build = lark_bot._build_client
    monkeypatch.setattr(
        lark_bot,
        "_build_client",
        lambda: real_build(httpx.MockTransport(handler)),
    )


def _capture_and_respond(
    captured: dict[str, Any], response: httpx.Response
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return response

    return handler


def test_send_success(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _use_client(
        monkeypatch, _capture_and_respond(captured, httpx.Response(200, json=_OK_BODY))
    )
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.delenv("LARK_BOT_WEBHOOK_SECRET", raising=False)

    report = _report()
    assert lark_bot.send(report, "group-a") is True

    assert captured["url"] == WEBHOOK_URL
    body = captured["body"]
    assert body["msg_type"] == "interactive"
    # 卡片内容只来自 report.markdown，不改写
    assert body["card"]["elements"] == [{"tag": "markdown", "content": report.markdown}]
    assert "timestamp" not in body
    assert "sign" not in body


def test_no_sign_when_secret_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _use_client(
        monkeypatch, _capture_and_respond(captured, httpx.Response(200, json=_OK_BODY))
    )
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.delenv("LARK_BOT_WEBHOOK_SECRET", raising=False)

    assert lark_bot.send(_report(), "group-a") is True
    assert "timestamp" not in captured["body"]
    assert "sign" not in captured["body"]


def test_sign_attached_when_secret_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    _use_client(
        monkeypatch, _capture_and_respond(captured, httpx.Response(200, json=_OK_BODY))
    )
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)
    monkeypatch.setenv("LARK_BOT_WEBHOOK_SECRET", SECRET)

    assert lark_bot.send(_report(), "group-a") is True

    body = captured["body"]
    timestamp = body["timestamp"]
    assert isinstance(timestamp, str) and timestamp.isdigit()  # 秒级时间戳字符串
    # 官方算法独立复算：拼接串整体作 HMAC key、消息为空、Base64 编码
    string_to_sign = f"{timestamp}\n{SECRET}"
    expected_sign = base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")
    assert body["sign"] == expected_sign


def test_retry_exhausted_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        raise httpx.ConnectError(f"connection refused on {request.url.path}")

    _use_client(monkeypatch, handler)
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)

    assert lark_bot.send(_report(), "group-a") is False
    assert attempts["n"] == 3  # 首次 + §6.1 重试 2 次


def test_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ReadTimeout(f"timeout on {request.url.path}")
        return httpx.Response(200, json=_OK_BODY)

    _use_client(monkeypatch, handler)
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)

    assert lark_bot.send(_report(), "group-a") is True
    assert attempts["n"] == 3


def test_http_error_retried_then_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(502, text="bad gateway")

    _use_client(monkeypatch, handler)
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)

    assert lark_bot.send(_report(), "group-a") is False
    assert attempts["n"] == 3


def test_nonzero_status_code_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    body = {
        "StatusCode": 19024,
        "StatusMessage": "Key Words Not Hit",
        "code": 19024,
    }
    _use_client(
        monkeypatch, _capture_and_respond(captured, httpx.Response(200, json=body))
    )
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)

    assert lark_bot.send(_report(), "group-a") is False


def test_status_code_zero_but_code_nonzero_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """网关放行（StatusCode 0）但投递失败（code 9499）时必须判为失败。"""
    body = {
        "StatusCode": 0,
        "StatusMessage": "success",
        "code": 9499,
        "msg": "sign match error or timestamp is not within one hour from current time",
    }
    _use_client(monkeypatch, _capture_and_respond({}, httpx.Response(200, json=body)))
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)

    assert lark_bot.send(_report(), "group-a") is False


def test_business_failure_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """业务码非 0 是确定性失败，重试无意义，只应请求一次。"""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200, json={"StatusCode": 19024, "code": 19024})

    _use_client(monkeypatch, handler)
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)

    assert lark_bot.send(_report(), "group-a") is False
    assert attempts["n"] == 1


def test_non_json_response_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _use_client(
        monkeypatch,
        _capture_and_respond({}, httpx.Response(200, text="<html>err</html>")),
    )
    monkeypatch.setenv("LARK_BOT_WEBHOOK_URL", WEBHOOK_URL)

    assert lark_bot.send(_report(), "group-a") is False


def test_webhook_missing_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("webhook 未配置时不应发起任何请求")

    _use_client(monkeypatch, handler)
    monkeypatch.delenv("LARK_BOT_WEBHOOK_URL", raising=False)

    assert lark_bot.send(_report(), "group-a") is False
