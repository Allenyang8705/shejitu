"""lark_auth.py 单元测试（httpx.MockTransport 模拟飞书项目 OpenAPI）。"""

from collections.abc import Callable

import httpx
import pytest

from collector.lark_auth import LarkAuth
from shared.errors import CollectorError

TOKEN_PATH = "/open_api/auth/v3/plugin_token"


def _token_response(call_no: int) -> httpx.Response:
    return httpx.Response(
        200, json={"code": 0, "data": {"token": f"tok-{call_no}", "expire": 7200}}
    )


def _auth_with(handler: Callable[[httpx.Request], httpx.Response]) -> LarkAuth:
    return LarkAuth(
        httpx.Client(
            base_url=lark_auth_base(),
            transport=httpx.MockTransport(handler),
            timeout=10.0,
        ),
        plugin_id="pid",
        plugin_secret="psecret",
        user_headers={"X-USER-ID": "42"},
    )


def lark_auth_base() -> str:
    from collector import lark_auth  # 局部导入避免与模块级 LarkAuth 名混淆

    return lark_auth.PROJECT_OPENAPI_BASE


def test_token_cached_across_requests():
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            posts["n"] += 1
            return _token_response(posts["n"])
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = _auth_with(handler)
    headers = auth.request_headers()
    assert headers["X-PLUGIN-TOKEN"] == "tok-1"
    assert headers["X-USER-ID"] == "42"
    assert auth.request_headers()["X-PLUGIN-TOKEN"] == "tok-1"
    assert posts["n"] == 1
    auth.close()


def test_token_refresh_after_expiry():
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            posts["n"] += 1
            return _token_response(posts["n"])
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = _auth_with(handler)
    auth.request_headers()
    auth._expires_at = 0.0  # 直接置为过期，模拟时间流逝
    assert auth.request_headers()["X-PLUGIN-TOKEN"] == "tok-2"
    assert posts["n"] == 2
    auth.close()


def test_invalidate_forces_refresh():
    posts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            posts["n"] += 1
            return _token_response(posts["n"])
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = _auth_with(handler)
    auth.request_headers()
    auth.invalidate()
    assert auth.request_headers()["X-PLUGIN-TOKEN"] == "tok-2"
    assert posts["n"] == 2
    auth.close()


def test_token_endpoint_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return httpx.Response(200, json={"code": 10001, "msg": "bad credentials"})
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = _auth_with(handler)
    with pytest.raises(CollectorError, match="10001"):
        auth.request_headers()
    auth.close()


def test_timeout_retries_then_success(monkeypatch):
    posts = {"n": 0}
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            posts["n"] += 1
            if posts["n"] < 3:
                raise httpx.ReadTimeout("timeout")
            return _token_response(posts["n"])
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = _auth_with(handler)
    assert auth.request_headers()["X-PLUGIN-TOKEN"] == "tok-3"
    assert posts["n"] == 3
    auth.close()


def test_timeout_exhausted_raises(monkeypatch):
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            raise httpx.ReadTimeout("timeout")
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = _auth_with(handler)
    with pytest.raises(CollectorError):
        auth.request_headers()
    auth.close()


def test_api_request_refreshes_and_retries_once():
    token_posts = {"n": 0}
    biz_posts = {"n": 0}
    query_path = "/open_api/p1/work_item/query"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            token_posts["n"] += 1
            return _token_response(token_posts["n"])
        if request.url.path == query_path:
            biz_posts["n"] += 1
            if biz_posts["n"] == 1:
                return httpx.Response(
                    200, json={"code": 99991663, "msg": "token expired"}
                )
            return httpx.Response(200, json={"code": 0, "data": {"data": []}})
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = _auth_with(handler)
    payload = auth.api_request("POST", query_path, json_body={})
    assert payload["data"]["data"] == []
    assert token_posts["n"] == 2  # 初始获取 + 失效刷新
    assert biz_posts["n"] == 2  # 首次 + 刷新后重试 1 次（design.md §6.1）
    auth.close()


def test_api_request_nonzero_code_raises():
    query_path = "/open_api/p1/work_item/query"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == TOKEN_PATH:
            return _token_response(1)
        if request.url.path == query_path:
            return httpx.Response(200, json={"code": 40301, "msg": "no permission"})
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = _auth_with(handler)
    with pytest.raises(CollectorError, match="40301"):
        auth.api_request("POST", query_path, json_body={})
    auth.close()


def test_from_env_validates_credentials(monkeypatch):
    for key in ("LARK_APP_ID", "LARK_APP_SECRET", "LARK_USER_ID", "LARK_USER_KEY"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(CollectorError, match="LARK_APP_ID"):
        LarkAuth.from_env()

    monkeypatch.setenv("LARK_APP_ID", "app-id")
    monkeypatch.setenv("LARK_APP_SECRET", "app-secret")
    with pytest.raises(CollectorError, match="LARK_USER"):
        LarkAuth.from_env()

    monkeypatch.setenv("LARK_USER_KEY", "user-key")
    auth = LarkAuth.from_env()
    assert auth._user_headers == {"X-USER-KEY": "user-key"}
    auth.close()
