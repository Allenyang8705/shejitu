"""GitHub 采集模块（design.md §2.1 / §4.1）。

获取指定仓库在时间窗口内的 commit 记录，并逐条补齐变更统计（列表接口
不含 additions / deletions / files_changed，需请求单提交详情）。

失败语义（对 tasks.md T3「仍失败返回空列表」字面行为的修正）：
- 单仓库失败：记录 ERROR 日志并跳过，不阻断同批其他仓库（proposal 3.2）；
- 全部仓库失败：抛出 CollectorError。若此时仍返回空列表，编排层将无法
  区分「采集失败」与「采集成功但无提交」，proposal 3.2 要求的
  「日报中标注数据获取失败」就不可能实现。
"""

import os
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from shared import logger
from shared.errors import CollectorError, call_with_retry
from shared.models import CommitRecord

GITHUB_API_BASE = "https://api.github.com"

_PER_PAGE = 100  # 分页每页上限（GitHub 最大 100），减少请求轮次
_REQUEST_TIMEOUT = 10.0
_RATE_LIMIT_MAX_RETRIES = 3  # proposal 3.3：限流等待后重试，最多 3 次


def _build_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """构造 API 客户端；GITHUB_TOKEN 可选（匿名调用限流低且私有仓库不可见）。

    transport 仅供测试注入 MockTransport，生产调用不传。
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    else:
        logger.info(
            "github_token_missing",
            hint="未配置 GITHUB_TOKEN：匿名调用限流 60 次/小时，私有仓库不可见",
        )
    return httpx.Client(
        base_url=GITHUB_API_BASE,
        headers=headers,
        timeout=_REQUEST_TIMEOUT,
        transport=transport,
    )


def _parse_timestamp(value: str) -> datetime:
    """解析 GitHub 的 ISO 8601 时间；缺时区时按 UTC 处理（统一 aware UTC 约定）。"""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _get_json(
    client: httpx.Client, url: str, params: dict[str, str] | None = None
) -> Any:
    """GET 并解析 JSON；超时按 §6.1 重试，限流按 proposal 3.3 等待 reset 后重试。"""
    rate_limit_waits = 0
    while True:
        try:
            response = call_with_retry(
                lambda: client.get(url, params=params),
                retry_on=(httpx.TimeoutException,),
            )
        except httpx.TimeoutException as exc:
            raise CollectorError("github", f"请求超时（已重试 3 次）：{url}") from exc

        if response.status_code == 403 and "X-RateLimit-Reset" in response.headers:
            if rate_limit_waits >= _RATE_LIMIT_MAX_RETRIES:
                raise CollectorError("github", f"API 限流且等待重试耗尽：{url}")
            reset_at = int(response.headers["X-RateLimit-Reset"])
            wait_seconds = max(reset_at - time.time(), 0.0) + 1.0
            logger.info(
                "github_rate_limited", url=url, wait_seconds=round(wait_seconds)
            )
            time.sleep(wait_seconds)
            rate_limit_waits += 1
            continue

        if response.is_error:
            raise CollectorError(
                "github", f"GitHub API 返回 {response.status_code}：{url}"
            )
        return response.json()


def _list_commits(
    client: httpx.Client, repo: str, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """按窗口与分页拉取 commit 列表元数据（不含统计字段）。"""
    commits: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _get_json(
            client,
            f"/repos/{repo}/commits",
            params={
                "per_page": str(_PER_PAGE),
                "page": str(page),
                "since": since.isoformat(),
                "until": until.isoformat(),
            },
        )
        if not isinstance(batch, list) or not batch:
            break
        commits.extend(batch)
        if len(batch) < _PER_PAGE:
            break
        page += 1
    return commits


def _to_record(
    client: httpx.Client, repo: str, item: dict[str, Any], timestamp: datetime
) -> CommitRecord:
    """从列表项 + 单提交详情构造 CommitRecord。"""
    detail = _get_json(client, f"/repos/{repo}/commits/{item['sha']}")
    stats = detail.get("stats") or {}
    # 未映射 GitHub 账号的提交没有 author.login，退回 git 作者名
    author = (
        (item.get("author") or {}).get("login")
        or item["commit"]["author"]["name"]
        or "unknown"
    )
    return CommitRecord(
        author=author,
        message=item["commit"]["message"],
        timestamp=timestamp,
        repo=repo,
        additions=int(stats.get("additions", 0)),
        deletions=int(stats.get("deletions", 0)),
        files_changed=len(detail.get("files") or []),
    )


def _collect_repo(
    client: httpx.Client, repo: str, since: datetime, until: datetime
) -> list[CommitRecord]:
    records: list[CommitRecord] = []
    for item in _list_commits(client, repo, since, until):
        timestamp = _parse_timestamp(item["commit"]["committer"]["date"])
        if not since <= timestamp <= until:
            # 服务端已按窗口过滤，此处兜底防 API 边界语义偏差
            continue
        records.append(_to_record(client, repo, item, timestamp))
    return records


def collect(repos: list[str], since: datetime, until: datetime) -> list[CommitRecord]:
    """采集时间窗口内全部仓库的 commit 记录（design.md §4.1 契约）。"""
    if since.tzinfo is None or until.tzinfo is None:
        raise CollectorError(
            "github", "since / until 必须是带时区的 datetime（统一 aware UTC 约定）"
        )
    records: list[CommitRecord] = []
    failed: list[str] = []
    with _build_client() as client:
        for repo in repos:
            try:
                records.extend(_collect_repo(client, repo, since, until))
            # 单仓库任何失败（含网络错误、畸形响应）都不得阻断其他仓库（proposal 3.2），
            # 此处盲捕是刻意的隔离边界，故豁免 BLE001
            except Exception as exc:  # noqa: BLE001
                failed.append(repo)
                logger.error(
                    "github_repo_failed",
                    repo=repo,
                    error=f"{type(exc).__name__}: {exc}",
                )
    if repos and len(failed) == len(repos):
        raise CollectorError("github", f"全部仓库采集失败：{', '.join(failed)}")
    return records
