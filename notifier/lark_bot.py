"""飞书机器人推送模块（design.md §2.3 / §4.3）。

通过飞书自定义机器人 webhook 把 Markdown 日报发送到指定群。

实现要点与核对点：
- 自定义机器人不走 tenant_access_token 鉴权（与 collector/lark_auth.py 的
  开放平台鉴权体系无关），webhook 地址本身即凭据：从环境变量
  LARK_BOT_WEBHOOK_URL 读取；LARK_BOT_WEBHOOK_SECRET 可选，配置了则按官方
  签名算法附加 timestamp 与 sign。
- 签名算法（官方文档「自定义机器人使用指南 → 签名校验」，2026-11 检索核实）：
  签名字符串为 timestamp + "\\n" + secret，整体作为 HmacSHA256 的 key
  （消息为空，与常规 HMAC 用法相反），结果 Base64 编码即 sign；
  timestamp 为秒级字符串，与服务端相差不得超过 1 小时。
- 自定义机器人没有纯 markdown 消息类型，用 interactive 卡片的 markdown
  元素承载 report.markdown，卡片内容不改写。核对点：卡片文本长度上限
  未经官方文档逐字核实，先不截断；若因超长被拒，会在响应业务码中体现
  并按推送失败处理。
- 成功判定以响应 StatusCode == 0 为准（自定义机器人响应字段为首字母
  大写形态，与开放平台 code 字段不同）。核对点：社区反馈该端点在消息
  投递失败（业务码 9499 等）时 StatusCode 仍可能为 0，故响应含 code
  字段时一并校验，两者均为 0 才算成功。
- 契约参数 chat_id（design.md §4.3）：自定义机器人 webhook 本身已绑定群，
  chat_id 仅作日志标识，为未来多群映射预留。
"""

import base64
import hashlib
import hmac
import os
import time
from typing import Any

import httpx

from shared import logger
from shared.errors import call_with_retry
from shared.models import DailyReport

WEBHOOK_URL_ENV = "LARK_BOT_WEBHOOK_URL"
WEBHOOK_SECRET_ENV = "LARK_BOT_WEBHOOK_SECRET"

_REQUEST_TIMEOUT = 10.0
_PUSH_RETRIES = 2  # design.md §6.1：推送失败重试 2 次


def _build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """构造 webhook 客户端；transport 仅供测试注入 MockTransport，生产调用不传。"""
    return httpx.Client(timeout=_REQUEST_TIMEOUT, transport=transport)


def _gen_sign(timestamp: str, secret: str) -> str:
    """按官方算法计算签名：拼接串整体作 HMAC key，消息为空，结果 Base64 编码。"""
    string_to_sign = f"{timestamp}\n{secret}"
    hmac_code = hmac.new(
        string_to_sign.encode("utf-8"), digestmod=hashlib.sha256
    ).digest()
    return base64.b64encode(hmac_code).decode("utf-8")


def _build_payload(report: DailyReport) -> dict[str, Any]:
    """构造请求体；配置了签名密钥时附加 timestamp 与 sign。"""
    payload: dict[str, Any] = {
        "msg_type": "interactive",
        "card": {"elements": [{"tag": "markdown", "content": report.markdown}]},
    }
    secret = os.environ.get(WEBHOOK_SECRET_ENV)
    if secret:
        timestamp = str(int(time.time()))
        payload["timestamp"] = timestamp
        payload["sign"] = _gen_sign(timestamp, secret)
    return payload


def _is_accepted(body: Any) -> bool:
    """判定 webhook 业务响应是否成功；结构异常一律视为失败。

    以 StatusCode == 0 为准；code 字段存在时一并校验（见模块 docstring 核对点）。
    """
    if not isinstance(body, dict) or body.get("StatusCode") != 0:
        return False
    return body.get("code", 0) == 0


def send(report: DailyReport, chat_id: str) -> bool:
    """发送 Markdown 日报到飞书群（design.md §4.3 契约），成功返回 True。

    webhook 未配置时记录 ERROR 日志并返回 False；网络与 HTTP 层失败按 §6.1
    重试 2 次，耗尽后记录 ERROR 日志并返回 False；业务码非 0 不重试，同样
    返回 False。raise_for_status 置于重试范围内：网关 5xx 等瞬时错误值得重试。
    """
    webhook_url = os.environ.get(WEBHOOK_URL_ENV)
    if not webhook_url:
        logger.error(
            "lark_bot_send_failed",
            chat_id=chat_id,
            reason=f"webhook 未配置（缺少环境变量 {WEBHOOK_URL_ENV}）",
        )
        return False

    payload = _build_payload(report)
    try:
        with _build_client() as client:

            def _post_once() -> httpx.Response:
                resp = client.post(webhook_url, json=payload)
                # HTTP 层错误（网关 5xx 等瞬时故障）也按 §6.1 纳入重试范围
                resp.raise_for_status()
                return resp

            response = call_with_retry(
                _post_once,
                retries=_PUSH_RETRIES,
                retry_on=(httpx.HTTPError,),
            )
    except httpx.HTTPError as exc:
        logger.error(
            "lark_bot_send_failed",
            chat_id=chat_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return False

    try:
        body: Any = response.json()
    # 响应非 JSON（如代理返回错误页）时无法判定业务结果，按推送失败降级
    except ValueError as exc:
        logger.error(
            "lark_bot_send_failed",
            chat_id=chat_id,
            error=f"响应不是有效 JSON：{type(exc).__name__}: {exc}",
        )
        return False

    if not _is_accepted(body):
        logger.error(
            "lark_bot_send_failed",
            chat_id=chat_id,
            http_status=response.status_code,
            response=body,
        )
        return False

    logger.info("lark_bot_send_success", chat_id=chat_id)
    return True
