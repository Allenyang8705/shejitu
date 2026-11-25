"""lark_auth.py 单元测试（httpx.MockTransport 模拟飞书开放平台主站）。

历史说明：曾覆盖飞书项目平台 LarkAuth（plugin_token），随任务采集功能
裁剪移除（proposal v1.1），现仅覆盖 IM 消息采集使用的 LarkTenantAuth。
"""

import httpx

from collector import lark_auth
from collector.lark_auth import LarkTenantAuth


def test_tenant_auth_token_and_bearer_header():
    """主平台 tenant_access_token：Bearer 头 + 顶层响应字段 + 缓存。"""
    token_posts = {"n": 0}
    messages_path = "/open-apis/im/v1/messages"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/open-apis/auth/v3/tenant_access_token/internal":
            token_posts["n"] += 1
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "tenant_access_token": f"t-{token_posts['n']}",
                    "expire": 7200,
                },
            )
        if request.url.path == messages_path:
            return httpx.Response(200, json={"code": 0, "data": {"items": []}})
        raise AssertionError(f"unexpected path {request.url.path}")

    auth = LarkTenantAuth(
        httpx.Client(
            base_url=lark_auth.MAIN_OPENAPI_BASE,
            transport=httpx.MockTransport(handler),
            timeout=10.0,
        ),
        "app-id",
        "app-secret",
    )
    payload = auth.api_request("GET", messages_path, params={"a": "1"})
    assert payload["data"]["items"] == []
    assert token_posts["n"] == 1
    auth.api_request("GET", messages_path, params={"a": "1"})
    assert token_posts["n"] == 1  # token 已缓存，不重复获取
    auth.close()
