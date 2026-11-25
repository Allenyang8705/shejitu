"""飞书消息采集模块（design.md §2.1 / §4.1）。

获取指定飞书群在时间窗口内的消息，按关键词白名单过滤并剔除敏感词后
映射为 MessageRecord。群消息属于飞书开放平台主站（IM API），鉴权走
LarkTenantAuth。

过滤语义（proposal 2.1 / design.md §6.2）：
- 入参 keywords 为采集白名单：消息包含任一关键词才保留；为空表示该源
  不产出消息（配置即开关）；
- sensitive_keywords（config 读取）为黑名单且优先于白名单：命中即丢弃；
- 机器人 / 应用消息不属于成员讨论（且可能包含本工具自己推送的日报），
  一律跳过；图片 / 文件 / 卡片等非文本消息同样跳过。

失败语义同 github.py：失败作为数据返回（CollectResult），
CollectorError 仅用于契约违规。

端点核对点：主平台 IM 接口为通用稳定接口，置信度高于飞书项目部分；
部署前提是应用开通 im:message:readonly / im:chat:readonly /
contact:user.base:readonly 权限，且机器人已在目标群内。
"""

import json
from datetime import UTC, datetime
from typing import Any

from collector import lark_auth
from collector.lark_auth import LarkTenantAuth
from shared import logger
from shared.config import get_config
from shared.errors import CollectorError
from shared.models import CollectResult, MessageRecord

_PAGE_SIZE = 50  # 历史消息单页上限
_MESSAGE_LIST_PATH = "/open-apis/im/v1/messages"
_CHAT_INFO_PATH = "/open-apis/im/v1/chats/{chat_id}"
_USER_INFO_PATH = "/open-apis/contact/v3/users/{user_id}"


def _get_auth() -> LarkTenantAuth:
    """单例入口独立成函数，便于测试替换鉴权实现。"""
    return lark_auth.get_lark_tenant_auth()


def _ms_to_datetime(value: Any) -> datetime:
    """飞书毫秒时间戳 → aware UTC datetime（时间统一约定）。

    设计规定各采集模块互不依赖，同名实现保持独立。
    """
    return datetime.fromtimestamp(int(float(value)) / 1000, tz=UTC)


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords if keyword)


def _post_text(content: Any) -> str:
    """富文本（post）结构归一化为纯文本：标题 + 各段落文本片段。"""
    post = content.get("post") if isinstance(content, dict) else None
    if not isinstance(post, dict):
        return ""
    body = next((v for v in post.values() if isinstance(v, dict)), None)
    if body is None:
        return ""
    lines: list[str] = []
    title = body.get("title")
    if isinstance(title, str) and title.strip():
        lines.append(title)
    for paragraph in body.get("content") or []:
        if not isinstance(paragraph, list):
            continue
        segments = []
        for segment in paragraph:
            if isinstance(segment, dict) and segment.get("tag") in ("text", "a"):
                segments.append(str(segment.get("text") or ""))
        lines.append("".join(segments))
    return "\n".join(lines)


def _plain_text(raw: dict[str, Any]) -> str | None:
    """将消息体归一化为纯文本；不支持的消息类型返回 None（不进日报）。"""
    msg_type = raw.get("msg_type")
    body = raw.get("body")
    body = body if isinstance(body, dict) else {}
    raw_content = body.get("content")
    if not isinstance(raw_content, str) or not raw_content.strip():
        return None
    try:
        content = json.loads(raw_content)
    except ValueError:
        return raw_content  # 非结构化内容按原文处理
    if msg_type == "text" and isinstance(content, dict):
        text = str(content.get("text") or "").strip()
        return text or None
    if msg_type == "post":
        return _post_text(content).strip() or None
    return None  # 图片 / 文件 / 卡片等非讨论内容不进日报


def _fetch_chat_name(auth: LarkTenantAuth, chat_id: str) -> str:
    """查询群名称；MessageRecord.chat_name 不允许留空，失败即整群失败。"""
    payload = auth.api_request("GET", _CHAT_INFO_PATH.format(chat_id=chat_id))
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CollectorError("lark_msg", f"群信息缺少 name：{chat_id}")
    return name


def _resolve_sender(
    auth: LarkTenantAuth, user_cache: dict[str, str], sender_id: str
) -> str:
    """按 open_id 解析用户名；失败退回原始 ID，不阻断消息采集。"""
    if sender_id in user_cache:
        return user_cache[sender_id]
    resolved = sender_id
    try:
        payload = auth.api_request(
            "GET",
            _USER_INFO_PATH.format(user_id=sender_id),
            params={"user_id_type": "open_id"},
        )
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        user = data.get("user")
        user = user if isinstance(user, dict) else {}
        name = user.get("name")
        if isinstance(name, str) and name.strip():
            resolved = name
    except Exception as exc:  # noqa: BLE001 — 名称解析失败退回原始 ID
        logger.info(
            "lark_msg_sender_resolve_failed",
            sender_id=sender_id,
            error=f"{type(exc).__name__}: {exc}",
        )
    user_cache[sender_id] = resolved
    return resolved


def _iter_chat_messages(
    auth: LarkTenantAuth, chat_id: str, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """按时间窗口翻页拉取群历史消息。"""
    messages: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, str] = {
            "container_id_type": "chat",
            "container_id": chat_id,
            "start_time": str(int(since.timestamp() * 1000)),
            "end_time": str(int(until.timestamp() * 1000)),
            "sort": "ByCreateTimeAsc",
            "page_size": str(_PAGE_SIZE),
        }
        if page_token:
            params["page_token"] = page_token
        payload = auth.api_request("GET", _MESSAGE_LIST_PATH, params=params)
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        items = data.get("items") or []
        if not isinstance(items, list):
            raise CollectorError(
                "lark_msg", f"历史消息响应结构异常：{_MESSAGE_LIST_PATH}"
            )
        for entry in items:
            if isinstance(entry, dict):
                messages.append(entry)
        if not data.get("has_more"):
            break
        next_token = data.get("page_token")
        if not isinstance(next_token, str) or not next_token:
            break
        page_token = next_token
    return messages


def _to_record(
    auth: LarkTenantAuth,
    user_cache: dict[str, str],
    raw: dict[str, Any],
    chat_name: str,
    keywords: list[str],
    sensitive_keywords: list[str],
    since: datetime,
    until: datetime,
) -> MessageRecord | None:
    if raw.get("deleted"):
        return None
    sender_info = raw.get("sender")
    sender_info = sender_info if isinstance(sender_info, dict) else {}
    if sender_info.get("sender_type") != "user":
        # 应用 / 机器人消息不是成员讨论，且可能包含本工具推送的日报，跳过
        return None
    raw_time = raw.get("create_time")
    if raw_time is None:
        return None
    timestamp = _ms_to_datetime(raw_time)
    if not since <= timestamp <= until:
        # API 已按窗口过滤，此处兜底防边界语义偏差
        return None
    content = _plain_text(raw)
    if content is None:
        return None
    # 敏感词黑名单优先于关键词白名单（design.md §6.2）
    if _contains_any(content, sensitive_keywords):
        return None
    if not _contains_any(content, keywords):
        return None
    sender_id = str(sender_info.get("id") or "")
    sender = _resolve_sender(auth, user_cache, sender_id) if sender_id else "unknown"
    return MessageRecord(
        sender=sender, content=content, timestamp=timestamp, chat_name=chat_name
    )


def _collect_chat(
    auth: LarkTenantAuth,
    chat_id: str,
    keywords: list[str],
    sensitive_keywords: list[str],
    since: datetime,
    until: datetime,
) -> list[MessageRecord]:
    """单个群的完整采集流程；任一环节失败抛异常，由 collect 统一降级。"""
    chat_name = _fetch_chat_name(auth, chat_id)
    user_cache: dict[str, str] = {}
    records: list[MessageRecord] = []
    for raw in _iter_chat_messages(auth, chat_id, since, until):
        record = _to_record(
            auth, user_cache, raw, chat_name, keywords, sensitive_keywords, since, until
        )
        if record is not None:
            records.append(record)
    return records


def collect(
    chat_id: str, keywords: list[str], since: datetime, until: datetime
) -> CollectResult[MessageRecord]:
    """采集指定飞书群在窗口内的关键词相关消息（design.md §4.1 契约）。"""
    if since.tzinfo is None or until.tzinfo is None:
        raise CollectorError(
            "lark_msg", "since / until 必须是带时区的 datetime（统一 aware UTC 约定）"
        )
    try:
        records = _collect_chat(
            _get_auth(),
            chat_id,
            list(keywords),
            get_config().sensitive_keywords,
            since,
            until,
        )
    except Exception as exc:  # noqa: BLE001 — 失败作为数据返回（design.md §4.1），含凭据缺失
        logger.error(
            "lark_msg_failed", chat_id=chat_id, error=f"{type(exc).__name__}: {exc}"
        )
        return CollectResult(records=[], failed_targets=[chat_id])
    return CollectResult(records=records, failed_targets=[])
