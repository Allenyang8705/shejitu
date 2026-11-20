"""email.py 单元测试（unittest.mock 替换 smtplib，不触真实网络）。"""

import email as email_lib
import smtplib
from datetime import UTC, date, datetime
from email.header import decode_header, make_header
from unittest.mock import patch

import pytest

from notifier import email
from shared.errors import NotifierError
from shared.models import DailyReport

SUBJECT = "研发团队 日报 2026-11-20"


def _report() -> DailyReport:
    return DailyReport(
        date=date(2026, 11, 20),
        team_name="研发团队",
        members=[],
        generated_at=datetime(2026, 11, 20, 18, 0, tzinfo=UTC),
        markdown="# 团队日报\n- 张三：修复登录 bug",
        html="<h1>团队日报</h1>",
    )


def _setup_env(monkeypatch: pytest.MonkeyPatch, port: str = "465") -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", port)
    monkeypatch.setenv("SMTP_USER", "report-bot")
    monkeypatch.setenv("SMTP_PASSWORD", "secret-placeholder")
    monkeypatch.setenv("SMTP_FROM", "report-bot@example.com")


def _parsed(message_str: str) -> email_lib.message.Message:
    return email_lib.message_from_string(message_str)


def _part_payload(parsed: email_lib.message.Message, content_type: str) -> str:
    for part in parsed.walk():
        if part.get_content_type() == content_type:
            payload = part.get_payload(decode=True)
            assert isinstance(payload, bytes), f"缺少 {content_type} 部分"
            return payload.decode("utf-8")
    raise AssertionError(f"缺少 {content_type} 部分")


def test_send_success_via_ssl(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(monkeypatch, port="465")
    report = _report()
    with (
        patch("smtplib.SMTP_SSL") as ssl_cls,
        patch("smtplib.SMTP") as plain_cls,
    ):
        client = ssl_cls.return_value.__enter__.return_value
        assert email.send(report, ["leader@example.com"]) is True

    ssl_cls.assert_called_once_with("smtp.example.com", 465, timeout=10.0)
    plain_cls.assert_not_called()  # 465 端口不应走明文连接
    client.starttls.assert_not_called()  # 隐式 TLS 无需升级
    client.login.assert_called_once_with("report-bot", "secret-placeholder")

    kwargs = client.sendmail.call_args.kwargs
    assert kwargs["from_addr"] == "report-bot@example.com"
    assert kwargs["to_addrs"] == ["leader@example.com"]

    parsed = _parsed(kwargs["msg"])
    # 中文主题 RFC 2047 编码后需能无损还原（tasks.md：主题含团队名与日期）
    assert str(make_header(decode_header(parsed["Subject"]))) == SUBJECT
    assert _part_payload(parsed, "text/plain") == report.markdown
    assert _part_payload(parsed, "text/html") == report.html


def test_send_success_via_starttls(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(monkeypatch, port="587")
    with (
        patch("smtplib.SMTP_SSL") as ssl_cls,
        patch("smtplib.SMTP") as plain_cls,
    ):
        client = plain_cls.return_value.__enter__.return_value
        assert email.send(_report(), ["leader@example.com"]) is True

    ssl_cls.assert_not_called()
    plain_cls.assert_called_once_with("smtp.example.com", 587, timeout=10.0)
    client.starttls.assert_called_once()  # 明文连接后必须升级 TLS


def test_retry_exhausted_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)
    _setup_env(monkeypatch, port="465")
    with patch("smtplib.SMTP_SSL") as ssl_cls:
        client = ssl_cls.return_value.__enter__.return_value
        client.sendmail.side_effect = smtplib.SMTPException("relay denied")
        assert email.send(_report(), ["leader@example.com"]) is False
    assert client.sendmail.call_count == 3  # 首次 + §6.1 重试 2 次


def test_retry_then_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)
    _setup_env(monkeypatch, port="465")
    with patch("smtplib.SMTP_SSL") as ssl_cls:
        client = ssl_cls.return_value.__enter__.return_value
        client.sendmail.side_effect = [smtplib.SMTPException("temp fail"), None]
        assert email.send(_report(), ["leader@example.com"]) is True
    assert client.sendmail.call_count == 2


def test_missing_credentials_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(key, raising=False)
    with patch("smtplib.SMTP_SSL") as ssl_cls:
        assert email.send(_report(), ["leader@example.com"]) is False
    ssl_cls.assert_not_called()


def test_invalid_port_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_env(monkeypatch, port="not-a-port")
    with patch("smtplib.SMTP_SSL") as ssl_cls:
        assert email.send(_report(), ["leader@example.com"]) is False
    ssl_cls.assert_not_called()


def test_empty_recipients_raises_contract_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _setup_env(monkeypatch)
    with patch("smtplib.SMTP_SSL"), pytest.raises(NotifierError, match="recipients"):
        email.send(_report(), [])
