"""lark_msg.py 单元测试（httpx.MockTransport 模拟飞书主平台 IM API）。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from collector import lark_auth, lark_msg
from collector.lark_auth import LarkTenantAuth
from shared.config import Config
from shared.errors import CollectorError

CHAT_ID = "oc-1"
TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
CHAT_INFO_PATH = "/open-apis/im/v1/chats/oc-1"
MESSAGE_LIST_PATH = "/open-apis/im/v1/messages"
USER_INFO_PREFIX = "/open-apis/contact/v3/users/"
SINCE = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_auth_singleton():
    lark_auth.reset_lark_auth()
    yield
    lark_auth.reset_lark_auth()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _config(sensitive: list[str]) -> Config:
    return Config(
        team_name="t",
        repos=[],
        chats=[],
        message_keywords=[],
        sensitive_keywords=sensitive,
        members=[],
    )


def _msg(
    message_id: str,
    text: str,
    at: datetime,
    *,
    msg_type: str = "text",
    sender_type: str = "user",
    deleted: bool = False,
    sender_id: str = "ou-1",
    content_dict: dict | None = None,
) -> dict:
    body_content = (
        json.dumps(content_dict, ensure_ascii=False)
        if content_dict is not None
        else json.dumps({"text": text}, ensure_ascii=False)
    )
    return {
        "message_id": message_id,
        "msg_type": msg_type,
        "create_time": str(_ms(at)),
        "deleted": deleted,
        "sender": {"id": sender_id, "id_type": "open_id", "sender_type": sender_type},
        "body": {"content": body_content},
    }


def _base_handler(
    state: dict[str, int], messages: list[dict]
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TENANT_TOKEN_PATH:
            state["token"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{state['token']}",
                    "expire": 7200,
                },
            )
        if request.url.path == CHAT_INFO_PATH:
            return httpx.Response(200, json={"code": 0, "data": {"name": "项目群"}})
        if request.url.path == MESSAGE_LIST_PATH:
            state["list"] += 1
            return httpx.Response(
                200, json={"code": 0, "data": {"items": messages, "has_more": False}}
            )
        if request.url.path.startswith(USER_INFO_PREFIX):
            return httpx.Response(
                200, json={"code": 0, "data": {"user": {"name": "张三"}}}
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    return handler


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    keywords: list[str],
    sensitive: list[str] | None = None,
) -> LarkTenantAuth:
    monkeypatch.setattr(lark_msg, "get_config", lambda: _config(sensitive or ["薪资"]))
    auth = LarkTenantAuth(
        httpx.Client(
            base_url=lark_auth.MAIN_OPENAPI_BASE,
            transport=httpx.MockTransport(handler),
            timeout=10.0,
        ),
        "app-id",
        "app-secret",
    )
    monkeypatch.setattr(lark_msg, "_get_auth", lambda: auth)
    return auth


def test_collect_text_message_happy_path(monkeypatch):
    state: dict[str, int] = {"token": 0, "list": 0}
    messages = [_msg("m-1", "今天同步了进度", datetime(2026, 11, 20, 8, 0, tzinfo=UTC))]
    _setup(monkeypatch, _base_handler(state, messages), keywords=["进度"])

    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert result.failed_targets == []
    assert len(result.records) == 1
    record = result.records[0]
    assert record.sender == "张三"
    assert record.content == "今天同步了进度"
    assert record.timestamp == datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    assert record.timestamp.tzinfo is not None
    assert record.chat_name == "项目群"


def test_whitelist_keeps_only_matching_messages(monkeypatch):
    state: dict[str, int] = {"token": 0, "list": 0}
    messages = [
        _msg("m-1", "进度已同步", datetime(2026, 11, 20, 8, 0, tzinfo=UTC)),
        _msg("m-2", "中午吃什么", datetime(2026, 11, 20, 12, 0, tzinfo=UTC)),
    ]
    _setup(monkeypatch, _base_handler(state, messages), keywords=["进度"])

    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert [record.content for record in result.records] == ["进度已同步"]


def test_sensitive_keyword_overrides_whitelist(monkeypatch):
    """既含采集关键词又含敏感词的消息必须被过滤（design.md §6.2）。"""
    state: dict[str, int] = {"token": 0, "list": 0}
    messages = [
        _msg("m-1", "进度同步，顺带聊了薪资", datetime(2026, 11, 20, 8, 0, tzinfo=UTC))
    ]
    _setup(
        monkeypatch,
        _base_handler(state, messages),
        keywords=["进度"],
        sensitive=["薪资"],
    )

    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == []


def test_empty_whitelist_yields_nothing(monkeypatch):
    """白名单为空表示关键词采集关闭，不产出任何消息。"""
    state: dict[str, int] = {"token": 0, "list": 0}
    messages = [_msg("m-1", "任意消息", datetime(2026, 11, 20, 8, 0, tzinfo=UTC))]
    _setup(monkeypatch, _base_handler(state, messages), keywords=[])

    result = lark_msg.collect(CHAT_ID, [], SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == []


def test_bot_deleted_and_non_text_skipped(monkeypatch):
    state: dict[str, int] = {"token": 0, "list": 0}
    at = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    messages = [
        _msg("m-1", "机器人播报进度", at, sender_type="app"),
        _msg("m-2", "已删除的进度", at, deleted=True),
        _msg("m-3", "图片消息", at, msg_type="image"),
    ]
    _setup(monkeypatch, _base_handler(state, messages), keywords=["进度"])

    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert result.records == []


def test_post_message_normalized(monkeypatch):
    state: dict[str, int] = {"token": 0, "list": 0}
    post_content = {
        "post": {
            "zh_cn": {
                "title": "周报",
                "content": [
                    [
                        {"tag": "text", "text": "完成"},
                        {"tag": "a", "text": "链接", "href": "x"},
                    ],
                    [{"tag": "text", "text": "第二行"}],
                ],
            }
        }
    }
    messages = [
        _msg(
            "m-1",
            "",
            datetime(2026, 11, 20, 8, 0, tzinfo=UTC),
            msg_type="post",
            content_dict=post_content,
        )
    ]
    _setup(monkeypatch, _base_handler(state, messages), keywords=["完成"])

    result = lark_msg.collect(CHAT_ID, ["完成"], SINCE, UNTIL)

    assert [record.content for record in result.records] == ["周报\n完成链接\n第二行"]


def test_pagination(monkeypatch):
    state: dict[str, int] = {"token": 0, "list": 0}
    at = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TENANT_TOKEN_PATH:
            state["token"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{state['token']}",
                    "expire": 7200,
                },
            )
        if request.url.path == CHAT_INFO_PATH:
            return httpx.Response(200, json={"code": 0, "data": {"name": "项目群"}})
        if request.url.path == MESSAGE_LIST_PATH:
            state["list"] += 1
            if state["list"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "items": [
                                _msg("m-1", "进度一", at),
                                _msg("m-2", "进度二", at),
                            ],
                            "has_more": True,
                            "page_token": "p2",
                        },
                    },
                )
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"items": [_msg("m-3", "进度三", at)], "has_more": False},
                },
            )
        if request.url.path.startswith(USER_INFO_PREFIX):
            return httpx.Response(
                200, json={"code": 0, "data": {"user": {"name": "张三"}}}
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler, keywords=["进度"])
    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert len(result.records) == 3
    assert state["list"] == 2


def test_chat_info_failure_marks_failed(monkeypatch):
    state: dict[str, int] = {"token": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TENANT_TOKEN_PATH:
            state["token"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{state['token']}",
                    "expire": 7200,
                },
            )
        if request.url.path == CHAT_INFO_PATH:
            return httpx.Response(500)
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler, keywords=["进度"])
    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == [CHAT_ID]
    assert state["list"] == 0  # 群名拿不到就不该继续拉消息


def test_token_expired_refresh_retry_once(monkeypatch):
    state: dict[str, int] = {"token": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TENANT_TOKEN_PATH:
            state["token"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{state['token']}",
                    "expire": 7200,
                },
            )
        if request.url.path == CHAT_INFO_PATH:
            return httpx.Response(200, json={"code": 0, "data": {"name": "项目群"}})
        if request.url.path == MESSAGE_LIST_PATH:
            state["list"] += 1
            if state["list"] == 1:
                return httpx.Response(
                    200, json={"code": 99991663, "msg": "token expired"}
                )
            return httpx.Response(
                200, json={"code": 0, "data": {"items": [], "has_more": False}}
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler, keywords=["进度"])
    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == []
    assert state["token"] == 2  # 初始获取 + 失效刷新
    assert state["list"] == 2  # 首次 + 刷新后重试 1 次（design.md §6.1）


def test_timeout_exhausted_marks_failed(monkeypatch):
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)
    state: dict[str, int] = {"token": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TENANT_TOKEN_PATH:
            state["token"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{state['token']}",
                    "expire": 7200,
                },
            )
        if request.url.path == CHAT_INFO_PATH:
            return httpx.Response(200, json={"code": 0, "data": {"name": "项目群"}})
        if request.url.path == MESSAGE_LIST_PATH:
            raise httpx.ReadTimeout("timeout")
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler, keywords=["进度"])
    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == [CHAT_ID]


def test_missing_credentials_marks_failed(monkeypatch):
    # 消息采集走主站 tenant 鉴权，只需 LARK_APP_ID / LARK_APP_SECRET
    for key in ("LARK_APP_ID", "LARK_APP_SECRET"):
        monkeypatch.delenv(key, raising=False)
    # 不替换 _get_auth，走真实单例构造路径验证凭据缺失的降级行为

    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == [CHAT_ID]


def test_sender_resolution_fallback(monkeypatch):
    state: dict[str, int] = {"token": 0, "list": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TENANT_TOKEN_PATH:
            state["token"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{state['token']}",
                    "expire": 7200,
                },
            )
        if request.url.path == CHAT_INFO_PATH:
            return httpx.Response(200, json={"code": 0, "data": {"name": "项目群"}})
        if request.url.path == MESSAGE_LIST_PATH:
            state["list"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "items": [
                            _msg(
                                "m-1", "进度", datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
                            )
                        ],
                        "has_more": False,
                    },
                },
            )
        if request.url.path.startswith(USER_INFO_PREFIX):
            return httpx.Response(500)  # 通讯录不可用时退回原始 ID
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler, keywords=["进度"])
    result = lark_msg.collect(CHAT_ID, ["进度"], SINCE, UNTIL)

    assert [record.sender for record in result.records] == ["ou-1"]


def test_naive_datetime_rejected():
    with pytest.raises(CollectorError):
        lark_msg.collect(CHAT_ID, ["进度"], datetime(2026, 11, 20), UNTIL)
