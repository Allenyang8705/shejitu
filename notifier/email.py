"""邮件推送模块（design.md §2.3 / §4.3）。

将 DailyReport 的 HTML 日报通过 SMTP 发送给 Leader；Markdown 作为
multipart/alternative 中的纯文本备用部分，不支持 HTML 的客户端降级显示。
正文只取 report.html / report.markdown 原文，不做改写。

凭据一律来自环境变量（design.md §6.2），缺失或端口非法时返回 False 并记录
ERROR 日志，不抛异常（tasks.md Task 7）。连接 / 认证 / 发送失败按 §6.1
重试 2 次（间隔 5s），仍失败记录 ERROR 日志并返回 False；成功返回 True。
recipients 为空属于编排层契约违规，抛 NotifierError（参照 collector 对
参数契约违规抛 CollectorError 的既有模式）。
"""

import os
import smtplib
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate

from shared import logger
from shared.errors import NotifierError, call_with_retry
from shared.models import DailyReport

_SMTP_TIMEOUT = 10.0
# design.md §6.1：推送失败重试 2 次（不含首次，即最多 3 次尝试）
_RETRIES = 2
_SSL_PORT = 465  # 隐式 TLS 端口：连接建立即 TLS，无需 STARTTLS 升级

# 全部必填（tasks.md Task 7 约定）：缺失任意一项视为凭据不完整
_CREDENTIAL_KEYS = (
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "SMTP_FROM",
)


def _load_credentials() -> dict[str, str] | None:
    """读取 SMTP 凭据；缺失或端口非法时记录 ERROR 日志并返回 None。

    返回 None 表示配置不可用，调用方直接返回 False（cron 一次性进程，
    配置错误不应当以异常中断整个日报流程）。
    """
    values = {key: os.environ.get(key, "").strip() for key in _CREDENTIAL_KEYS}
    missing = [key for key, value in values.items() if not value]
    if missing:
        logger.error("email_config_missing", missing=missing)
        return None
    if not values["SMTP_PORT"].isdigit():
        logger.error(
            "email_config_invalid",
            key="SMTP_PORT",
            value=values["SMTP_PORT"],
            hint="SMTP_PORT 必须是十进制端口号",
        )
        return None
    return values


def _build_message(
    subject: str, report: DailyReport, recipients: list[str], smtp_from: str
) -> MIMEMultipart:
    """组装 multipart/alternative 邮件：纯文本（Markdown）在前、HTML 在后。

    RFC 2046 规定 alternative 各部分按保真度递增排列，客户端取最后一个
    可渲染的部分，因此 HTML 作为主正文放在最后。
    """
    message = MIMEMultipart("alternative")
    # 中文主题按 RFC 2047 编码为单个 encoded word，避免网关 / 客户端乱码
    message["Subject"] = Header(subject, "utf-8").encode()
    message["From"] = smtp_from
    message["To"] = ", ".join(recipients)
    message["Date"] = formatdate(localtime=True)
    message.attach(MIMEText(report.markdown, "plain", "utf-8"))
    message.attach(MIMEText(report.html, "html", "utf-8"))
    return message


def _deliver(
    message: MIMEMultipart, creds: dict[str, str], recipients: list[str]
) -> None:
    """建立连接、认证并发送；任何一步失败抛 SMTPException / OSError 交给重试。

    每次调用都建立全新连接，失败的连接不跨重试复用。
    """
    host = creds["SMTP_HOST"]
    port = int(creds["SMTP_PORT"])
    # 465 为隐式 TLS 端口（握手即 TLS），其余端口明文连接后升级 STARTTLS
    factory = smtplib.SMTP_SSL if port == _SSL_PORT else smtplib.SMTP
    with factory(host, port, timeout=_SMTP_TIMEOUT) as client:
        if port != _SSL_PORT:
            client.starttls()
        client.login(creds["SMTP_USER"], creds["SMTP_PASSWORD"])
        client.sendmail(
            from_addr=creds["SMTP_FROM"],
            to_addrs=recipients,
            msg=message.as_string(),
        )


def send(report: DailyReport, recipients: list[str]) -> bool:
    """发送日报邮件（design.md §4.3 契约）：成功 True，失败 False。

    465 端口走 SMTP_SSL（隐式 TLS），其余端口走 STARTTLS。发送失败按
    §6.1 重试 2 次，仍失败记录 ERROR 日志并返回 False，不抛异常。
    """
    creds = _load_credentials()
    if creds is None:
        return False
    if not recipients:
        raise NotifierError("email", "recipients 不能为空，请检查收件人配置")

    subject = f"{report.team_name} 日报 {report.date.isoformat()}"
    message = _build_message(subject, report, recipients, creds["SMTP_FROM"])
    try:
        call_with_retry(
            lambda: _deliver(message, creds, recipients),
            retries=_RETRIES,
            retry_on=(smtplib.SMTPException, OSError),
        )
    except (smtplib.SMTPException, OSError) as exc:
        logger.error(
            "email_send_failed",
            error=f"{type(exc).__name__}: {exc}",
            retries=_RETRIES,
            host=creds["SMTP_HOST"],
            port=creds["SMTP_PORT"],
        )
        return False
    logger.info("email_sent", recipients=recipients, subject=subject)
    return True
