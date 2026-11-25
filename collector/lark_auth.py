"""飞书开放平台主站鉴权（tenant_access_token，供 lark_msg 使用）。

飞书群消息等 IM 接口走 open.feishu.cn 的 tenant_access_token 鉴权体系；
token 的获取、缓存、过期刷新若在各采集模块各写一份，必然行为漂移
（proposal 3.3：token 过期自动刷新），因此收拢为本模块；不在
design.md §2.1 模块清单内，属共享逻辑收拢。

历史说明：本模块曾同时收拢飞书项目平台（project.feishu.cn）的
plugin_token 鉴权，随飞书任务采集功能一并裁剪（proposal v1.1）；
将来接入任务源时从对应提交找回。
"""

import os
import time
from typing import Any

import httpx

from shared import logger
from shared.errors import CollectorError, call_with_retry

MAIN_OPENAPI_BASE = "https://open.feishu.cn"

_TENANT_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_TOKEN_REFRESH_MARGIN = 300.0  # 提前 5 分钟判定过期，避免临界失效

# token 失效错误码（检索所得的已知超集；实际码以联调为准，出入在此补充）
_TOKEN_INVALID_CODES = {99991661, 99991663}

_REQUEST_TIMEOUT = 10.0


def _safe_json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _field_of(payload: Any, key: str) -> Any:
    return payload.get(key) if isinstance(payload, dict) else None


def _status_of(payload: Any) -> tuple[Any, Any]:
    """兼容顶层 code/msg 与 error.{code,msg} 两种错误包裹。"""
    code = _field_of(payload, "code")
    msg = _field_of(payload, "msg")
    error = _field_of(payload, "error")
    if isinstance(error, dict):
        if code is None:
            code = error.get("code")
        if msg is None:
            msg = error.get("msg")
    return code, msg


class _LarkApiBase:
    """token 缓存刷新 + 失效重试 1 次 + 超时重试的通用骨架。

    子类实现 _headers()（请求头组装）与 _fetch_token()（换取新 token），
    缓存与重试语义在此统一。
    """

    def __init__(self, client: httpx.Client, source: str) -> None:
        self._client = client
        self._source = source
        self._token: str | None = None
        self._expires_at = 0.0

    def close(self) -> None:
        self._client.close()

    def invalidate(self) -> None:
        """作废缓存 token；下一次请求将重新获取（§6.1 刷新重试的入口）。"""
        self._token = None
        self._expires_at = 0.0

    def request_headers(self) -> dict[str, str]:
        """构造带有效 token 的请求头；token 过期自动刷新。"""
        return self._headers()

    def _headers(self) -> dict[str, str]:
        raise NotImplementedError

    def _fetch_token(self) -> tuple[str, float]:
        raise NotImplementedError

    def _ensure_token(self) -> str:
        if self._token is None or time.time() >= self._expires_at:
            token, expire = self._fetch_token()
            self._token = token
            self._expires_at = time.time() + expire - _TOKEN_REFRESH_MARGIN
            logger.info(
                "lark_token_refreshed", source=self._source, expire_in_seconds=expire
            )
        # _fetch_token 成功返回后 token 必然已就绪，断言仅用于类型收窄
        assert self._token is not None
        return self._token

    def _is_token_invalid(self, response: httpx.Response, payload: Any) -> bool:
        if response.status_code == 401:
            return True
        code, _msg = _status_of(payload)
        return code in _TOKEN_INVALID_CODES

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
                        headers=self._headers(),
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
            code, msg = _status_of(payload)
            if (
                response.status_code == 200
                and isinstance(payload, dict)
                and code in (0, None)
            ):
                return payload
            if not refreshed and self._is_token_invalid(response, payload):
                self.invalidate()
                refreshed = True
                continue
            raise CollectorError(
                "lark_auth",
                f"飞书 API 请求失败（{self._source}）：HTTP {response.status_code}"
                f" code={code} msg={msg} path={path}",
            )


class LarkTenantAuth(_LarkApiBase):
    """飞书开放平台主站鉴权：tenant_access_token（IM 消息等接口）。

    token / expire 在响应顶层而非 data 内，请求头为 Authorization: Bearer，
    业务接口不需要用户身份头。
    """

    def __init__(self, client: httpx.Client, app_id: str, app_secret: str) -> None:
        super().__init__(client, source="lark_tenant")
        self._app_id = app_id
        self._app_secret = app_secret

    @classmethod
    def from_env(cls) -> "LarkTenantAuth":
        app_id = os.environ.get("LARK_APP_ID")
        app_secret = os.environ.get("LARK_APP_SECRET")
        if not app_id or not app_secret:
            raise CollectorError(
                "lark_auth", "环境变量 LARK_APP_ID / LARK_APP_SECRET 未配置"
            )
        return cls(
            httpx.Client(base_url=MAIN_OPENAPI_BASE, timeout=_REQUEST_TIMEOUT),
            app_id,
            app_secret,
        )

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    def _fetch_token(self) -> tuple[str, float]:
        try:
            response = call_with_retry(
                lambda: self._client.post(
                    _TENANT_TOKEN_PATH,
                    json={"app_id": self._app_id, "app_secret": self._app_secret},
                ),
                retry_on=(httpx.TimeoutException,),
            )
        except httpx.TimeoutException as exc:
            raise CollectorError(
                "lark_auth", "获取 tenant_access_token 超时（已重试 3 次）"
            ) from exc
        payload = _safe_json(response)
        token = _field_of(payload, "tenant_access_token")
        expire = _field_of(payload, "expire")
        if (
            response.is_error
            or _field_of(payload, "code") != 0
            or not token
            or not expire
        ):
            raise CollectorError(
                "lark_auth",
                f"获取 tenant_access_token 失败：HTTP {response.status_code}"
                f" code={_field_of(payload, 'code')} msg={_field_of(payload, 'msg')}",
            )
        return str(token), float(expire)


_tenant_auth: LarkTenantAuth | None = None


def get_lark_tenant_auth() -> LarkTenantAuth:
    """进程级单例：IM 消息采集使用，token 缓存跨调用复用。"""
    global _tenant_auth
    if _tenant_auth is None:
        _tenant_auth = LarkTenantAuth.from_env()
    return _tenant_auth


def reset_lark_auth() -> None:
    """清空单例并关闭连接；测试或强制重读凭据时使用。

    函数名沿用双鉴权时期的命名，避免改动全部测试 fixture。
    """
    global _tenant_auth
    if _tenant_auth is not None:
        _tenant_auth.close()
    _tenant_auth = None
