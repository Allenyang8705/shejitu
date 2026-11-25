"""lark_task.py 单元测试（httpx.MockTransport 模拟飞书项目 OpenAPI）。"""

import json
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from collector import lark_auth, lark_task
from collector.lark_auth import LarkAuth
from shared.config import Config
from shared.errors import CollectorError

PROJECT = "proj-1"
TOKEN_PATH = "/open_api/authen/plugin_token"
QUERY_PATH = "/open_api/proj-1/work_item/query"
SINCE = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_auth_singleton():
    lark_auth.reset_lark_auth()
    yield
    lark_auth.reset_lark_auth()


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _stub_config(types: list[str]) -> Config:
    return Config(
        team_name="t",
        lark_project_id=PROJECT,
        repos=[],
        chats=[],
        message_keywords=[],
        sensitive_keywords=[],
        members=[],
        lark_work_item_types=types,
    )


def _item(work_item_id: str, title: str = "导出功能") -> dict:
    return {"id": work_item_id, "title": title, "owner": {"name": "张三"}}


def _transition(from_state: object, to_state: object, at_ms: int) -> dict:
    return {"from_state": from_state, "to_state": to_state, "created_at": at_ms}


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    types: list[str] | None = None,
) -> LarkAuth:
    monkeypatch.setattr(
        lark_task, "get_config", lambda: _stub_config(types or ["task"])
    )
    auth = LarkAuth(
        httpx.Client(
            base_url=lark_auth.PROJECT_OPENAPI_BASE,
            transport=httpx.MockTransport(handler),
            timeout=10.0,
        ),
        plugin_id="pid",
        plugin_secret="psecret",
        user_headers={"X-USER-ID": "42"},
    )
    monkeypatch.setattr(lark_task, "_get_auth", lambda: auth)
    return auth


def test_collect_status_transition(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"code": 0, "data": {"token": "tok-1", "expire": 7200}}
            )
        if request.url.path == QUERY_PATH:
            return httpx.Response(
                200, json={"code": 0, "data": {"data": [_item("wi-1")]}}
            )
        if request.url.path.endswith("/logs"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "logs": [
                            _transition(
                                {"name": "进行中"},
                                {"name": "已完成"},
                                _ms(datetime(2026, 11, 20, 8, 0, tzinfo=UTC)),
                            )
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler)
    result = lark_task.collect(PROJECT, SINCE, UNTIL)

    assert result.failed_targets == []
    assert len(result.records) == 1
    record = result.records[0]
    assert record.assignee == "张三"
    assert record.title == "导出功能"
    assert record.status_from == "进行中"
    assert record.status_to == "已完成"
    assert record.updated_at == datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    assert record.updated_at.tzinfo is not None


def test_window_filter_and_creation_transition(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"code": 0, "data": {"token": "tok-1", "expire": 7200}}
            )
        if request.url.path == QUERY_PATH:
            return httpx.Response(
                200, json={"code": 0, "data": {"data": [_item("wi-1")]}}
            )
        if request.url.path.endswith("/logs"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "logs": [
                            # 新建流转：无 from_state，status_from 应为空串
                            _transition(
                                None,
                                "待处理",
                                _ms(datetime(2026, 11, 20, 6, 0, tzinfo=UTC)),
                            ),
                            _transition(
                                "待处理",
                                "已完成",
                                _ms(datetime(2026, 11, 19, 8, 0, tzinfo=UTC)),
                            ),
                            _transition(
                                "已完成",
                                "关闭",
                                _ms(datetime(2026, 11, 21, 8, 0, tzinfo=UTC)),
                            ),
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler)
    result = lark_task.collect(PROJECT, SINCE, UNTIL)

    assert result.failed_targets == []
    assert len(result.records) == 1
    assert result.records[0].status_from == ""
    assert result.records[0].status_to == "待处理"


def test_token_expired_refresh_retry_once(monkeypatch):
    token_posts = {"n": 0}
    biz_posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_posts["n"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"token": f"tok-{token_posts['n']}", "expire": 7200},
                },
            )
        if request.url.path == QUERY_PATH:
            biz_posts["n"] += 1
            if biz_posts["n"] == 1:
                return httpx.Response(
                    200, json={"code": 99991663, "msg": "token expired"}
                )
            return httpx.Response(200, json={"code": 0, "data": {"data": []}})
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler)
    result = lark_task.collect(PROJECT, SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == []
    assert token_posts["n"] == 2  # 初始获取 + 失效刷新
    assert biz_posts["n"] == 2  # 首次 + 刷新后重试 1 次（design.md §6.1）


def test_token_still_invalid_after_refresh_marks_failed(monkeypatch):
    token_posts = {"n": 0}
    biz_posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_posts["n"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {"token": f"tok-{token_posts['n']}", "expire": 7200},
                },
            )
        if request.url.path == QUERY_PATH:
            biz_posts["n"] += 1
            return httpx.Response(200, json={"code": 99991663, "msg": "token expired"})
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler)
    result = lark_task.collect(PROJECT, SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == [PROJECT]
    assert biz_posts["n"] == 2  # 刷新后重试 1 次即放弃，不死循环
    assert token_posts["n"] == 2


def test_timeout_exhausted_marks_failed(monkeypatch):
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"code": 0, "data": {"token": "tok-1", "expire": 7200}}
            )
        if request.url.path == QUERY_PATH:
            raise httpx.ReadTimeout("timeout")
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler)
    result = lark_task.collect(PROJECT, SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == [PROJECT]


def test_type_failure_isolates_others(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"code": 0, "data": {"token": "tok-1", "expire": 7200}}
            )
        if request.url.path == QUERY_PATH:
            body = json.loads(request.content)
            if body["work_item_type_key"] == "task":
                return httpx.Response(500)
            return httpx.Response(
                200, json={"code": 0, "data": {"data": [_item("wi-9")]}}
            )
        if request.url.path.endswith("/logs"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "logs": [
                            _transition(
                                "进行中",
                                "已完成",
                                _ms(datetime(2026, 11, 20, 9, 0, tzinfo=UTC)),
                            )
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler, types=["task", "bug"])
    result = lark_task.collect(PROJECT, SINCE, UNTIL)

    # task 类型失败不丢 bug 类型的数据，但源级失败标记必须给出
    assert [record.title for record in result.records] == ["导出功能"]
    assert result.failed_targets == [PROJECT]


def test_missing_credentials_marks_failed(monkeypatch):
    monkeypatch.setattr(lark_task, "get_config", lambda: _stub_config(["task"]))
    for key in ("LARK_APP_ID", "LARK_APP_SECRET", "LARK_USER_ID", "LARK_USER_KEY"):
        monkeypatch.delenv(key, raising=False)
    # 不替换 _get_auth，走真实单例构造路径验证凭据缺失的降级行为

    result = lark_task.collect(PROJECT, SINCE, UNTIL)

    assert result.records == []
    assert result.failed_targets == [PROJECT]


def test_pagination(monkeypatch):
    query_posts = {"n": 0}
    monkeypatch.setattr(lark_task, "_PAGE_SIZE", 2)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(
                200, json={"code": 0, "data": {"token": "tok-1", "expire": 7200}}
            )
        if request.url.path == QUERY_PATH:
            query_posts["n"] += 1
            if query_posts["n"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "data": [_item("wi-1"), _item("wi-2")],
                            "has_more": True,
                        },
                    },
                )
            return httpx.Response(
                200, json={"code": 0, "data": {"data": [_item("wi-3")]}}
            )
        if request.url.path.endswith("/logs"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "logs": [
                            _transition(
                                "进行中",
                                "已完成",
                                _ms(datetime(2026, 11, 20, 10, 0, tzinfo=UTC)),
                            )
                        ]
                    },
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    _setup(monkeypatch, handler)
    result = lark_task.collect(PROJECT, SINCE, UNTIL)

    assert len(result.records) == 3
    assert query_posts["n"] == 2


def test_naive_datetime_rejected():
    with pytest.raises(CollectorError):
        lark_task.collect(PROJECT, datetime(2026, 11, 20), UNTIL)
