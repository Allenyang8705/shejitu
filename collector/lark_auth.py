"""飞书项目 OpenAPI 鉴权上下文（供 lark_task / lark_msg 复用）。

plugin_token 的获取、缓存、过期刷新与用户身份头若在两个采集模块各写
一份，必然行为漂移（proposal 3.3：token 过期自动刷新），因此收拢为
本模块；不在 design.md §2.1 模块清单内，属共享逻辑收拢。

端点与响应结构依据公开文档检索结果实现；官方文档站在本环境不可达，
未能逐字核对，故路径、请求体类型、失效错误码全部集中为常量，
真实凭据联调（main.py --check）发现出入时只需改常量。
"""

import os
import time
from typing import Any

import httpx

from shared import logger
from shared.errors import CollectorError, call_with_retry

PROJECT_OPENAPI_BASE = "https://project.feishu.cn"

_PLUGIN_TOKEN_PATH = "/open_api/auth/v3/plugin_token"
# 文档检索中亦见 /auth/v3/token 与 type: 0 的旧描述；联调 404 / 参数错误时改这两处
_TOKEN_REQUEST_TYPE = "granular"
_TOKEN_REFRESH_MARGIN = 300.0  # 提前 5 分钟判定过期，避免临界失效

# token 失效错误码（检索所得的已知超集；飞书项目实际码以联调为准，出入在此补充）
_TOKEN_INVALID_CODES = {99991661, 99991663}

_REQUEST_TIMEOUT = 10.0


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _field_of(payload: Any, key: str) -> Any:
    return payload.get(key) if isinstance(payload, dict) else None


class LarkAuth:
    """plugin_token 缓存刷新 + 用户身份头的鉴权上下文。"""

    def __init__(
        self,
        client: httpx.Client,
        plugin_id: str,
        plugin_secret: str,
        user_headers: dict[str, str],
    ) -> None:
        self._client = client
        self._plugin_id = plugin_id
        self._plugin_secret = plugin_secret
        self._user_headers = user_headers
        self._token: str | None = None
        self._expires_at = 0.0

    @classmethod
    def from_env(cls) -> "LarkAuth":
        """从环境变量构造；LARK_USER_ID 与 LARK_USER_KEY 至少配置一项。

        飞书项目 OpenAPI 的业务请求要求用户身份上下文（权限按该用户校验），
        仅凭插件身份无法调用，故用户标识也是必填凭据。
        """
        plugin_id = os.environ.get("LARK_APP_ID")
        plugin_secret = os.environ.get("LARK_APP_SECRET")
        if not plugin_id or not plugin_secret:
            raise CollectorError(
                "lark_auth", "环境变量 LARK_APP_ID / LARK_APP_SECRET 未配置"
            )
        user_id = os.environ.get("LARK_USER_ID")
        user_key = os.environ.get("LARK_USER_KEY")
        if user_id:
            user_headers = {"X-USER-ID": user_id}
        elif user_key:
            user_headers = {"X-USER-KEY": user_key}
        else:
            raise CollectorError(
                "lark_auth",
                "环境变量 LARK_USER_ID 或 LARK_USER_KEY 至少配置一项"
                "（飞书项目 OpenAPI 业务请求需要用户身份上下文）",
            )
        return cls(
            httpx.Client(base_url=PROJECT_OPENAPI_BASE, timeout=_REQUEST_TIMEOUT),
            plugin_id,
            plugin_secret,
            user_headers,
        )

    def close(self) -> None:
        self._client.close()

    def invalidate(self) -> None:
        """作废缓存 token；下一次请求将重新获取（§6.1 刷新重试的入口）。"""
        self._token = None
        self._expires_at = 0.0

    def request_headers(self) -> dict[str, str]:
        """构造带有效 plugin_token 的请求头；token 过期自动刷新。"""
        if self._token is None or time.time() >= self._expires_at:
            self._refresh()
        # _refresh 成功返回后 token 必然已就绪，断言仅用于类型收窄
        assert self._token is not None
        return {"X-PLUGIN-TOKEN": self._token, **self._user_headers}

    def _refresh(self) -> None:
        try:
            response = call_with_retry(
                lambda: self._client.post(
                    _PLUGIN_TOKEN_PATH,
                    json={
                        "plugin_id": self._plugin_id,
                        "plugin_secret": self._plugin_secret,
                        "type": _TOKEN_REQUEST_TYPE,
                    },
                ),
                retry_on=(httpx.TimeoutException,),
            )
        except httpx.TimeoutException as exc:
            raise CollectorError(
                "lark_auth", "获取 plugin_token 超时（已重试 3 次）"
            ) from exc
        payload = _safe_json(response)
        data = _field_of(payload, "data")
        token = _field_of(data, "token")
        expire = _field_of(data, "expire")
        if (
            response.is_error
            or _field_of(payload, "code") != 0
            or not token
            or not expire
        ):
            raise CollectorError(
                "lark_auth",
                f"获取 plugin_token 失败：HTTP {response.status_code}"
                f" code={_field_of(payload, 'code')} msg={_field_of(payload, 'msg')}",
            )
        self._token = str(token)
        self._expires_at = time.time() + float(expire) - _TOKEN_REFRESH_MARGIN
        logger.info("lark_token_refreshed", expire_in_seconds=float(expire))

    def _is_token_invalid(self, response: httpx.Response, payload: Any) -> bool:
        if response.status_code == 401:
            return True
        return _field_of(payload, "code") in _TOKEN_INVALID_CODES

    def api_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发起业务 API 请求并解析响应；token 失效时刷新后重试 1 次（design.md §6.1）。"""
        refreshed = False
        while True:
            try:
                response = call_with_retry(
                    lambda: self._client.request(
                        method,
                        path,
                        headers=self.request_headers(),
                        params=params,
                        json=json_body,
                    ),
                    retry_on=(httpx.TimeoutException,),
                )
            except httpx.TimeoutException as exc:
                raise CollectorError(
                    "lark_auth", f"请求超时（已重试 3 次）：{path}"
                ) from exc
            payload = _safe_json(response)
            if (
                response.status_code == 200
                and isinstance(payload, dict)
                and payload.get("code") in (0, None)
            ):
                return payload
            if not refreshed and self._is_token_invalid(response, payload):
                self.invalidate()
                refreshed = True
                continue
            raise CollectorError(
                "lark_auth",
                f"飞书项目 API 请求失败：HTTP {response.status_code}"
                f" code={_field_of(payload, 'code')} msg={_field_of(payload, 'msg')} path={path}",
            )


_auth: LarkAuth | None = None


def get_lark_auth() -> LarkAuth:
    """进程级单例：token 缓存跨 lark_task / lark_msg 复用。"""
    global _auth
    if _auth is None:
        _auth = LarkAuth.from_env()
    return _auth


def reset_lark_auth() -> None:
    """清空单例并关闭连接；测试或强制重读凭据时使用。"""
    global _auth
    if _auth is not None:
        _auth.close()
    _auth = None
