"""github.py 单元测试（httpx.MockTransport 模拟 GitHub API）。"""

import time
from collections.abc import Callable
from datetime import UTC, datetime

import httpx
import pytest

from collector import github
from shared.errors import CollectorError

SINCE = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)
UNTIL = datetime(2026, 11, 20, 23, 59, tzinfo=UTC)


def _commit_item(
    sha: str, date: str = "2026-11-20T06:00:00Z", login: str = "zhangsan"
) -> dict:
    return {
        "sha": sha,
        "author": {"login": login},
        "commit": {
            "message": f"feat: {sha}",
            "author": {"name": "张三"},
            "committer": {"date": date},
        },
    }


def _detail(additions: int = 10, deletions: int = 4, files: int = 2) -> dict:
    return {
        "stats": {"additions": additions, "deletions": deletions},
        "files": [{}] * files,
    }


def _use_client(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """让真实 _build_client 走 MockTransport，保留其凭据头部逻辑以便测试。"""
    real_build = github._build_client  # 先捕获原函数，避免 lambda 内自引用
    monkeypatch.setattr(
        github,
        "_build_client",
        lambda: real_build(httpx.MockTransport(handler)),
    )


def test_collect_single_commit(monkeypatch):
    detail_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[_commit_item("abc123")])
        detail_calls["n"] += 1
        return httpx.Response(200, json=_detail())

    _use_client(monkeypatch, handler)
    records = github.collect(["org/repo"], SINCE, UNTIL)

    assert len(records) == 1
    assert detail_calls["n"] == 1
    record = records[0]
    assert record.author == "zhangsan"
    assert record.message == "feat: abc123"
    assert record.timestamp == datetime(2026, 11, 20, 6, 0, tzinfo=UTC)
    assert record.timestamp.tzinfo is not None
    assert record.repo == "org/repo"
    assert record.additions == 10
    assert record.deletions == 4
    assert record.files_changed == 2


def test_pagination(monkeypatch):
    seen_pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            page = int(request.url.params.get("page", "0"))
            seen_pages.append(page)
            if page == 1:
                return httpx.Response(
                    200, json=[_commit_item("s1"), _commit_item("s2")]
                )
            return httpx.Response(200, json=[_commit_item("s3")])
        return httpx.Response(200, json=_detail())

    _use_client(monkeypatch, handler)
    monkeypatch.setattr(github, "_PER_PAGE", 2)
    records = github.collect(["org/repo"], SINCE, UNTIL)

    assert seen_pages == [1, 2]
    assert len(records) == 3


def test_timeout_retry_then_success(monkeypatch):
    attempts = {"n": 0}
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ReadTimeout(f"timeout on {request.url.path}")
            return httpx.Response(200, json=[])
        raise AssertionError("unreachable")

    _use_client(monkeypatch, handler)
    assert github.collect(["org/repo"], SINCE, UNTIL) == []
    assert attempts["n"] == 3


def test_timeout_exhausted_raises(monkeypatch):
    monkeypatch.setattr("shared.errors.time.sleep", lambda _: None)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timeout on {request.url.path}")

    _use_client(monkeypatch, handler)
    with pytest.raises(CollectorError):
        github.collect(["org/repo"], SINCE, UNTIL)


def test_rate_limit_waits_then_success(monkeypatch):
    attempts = {"n": 0}
    monkeypatch.setattr(github.time, "sleep", lambda _: None)
    reset_at = str(int(time.time()) + 5)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            attempts["n"] += 1
            if attempts["n"] <= 2:
                return httpx.Response(403, headers={"X-RateLimit-Reset": reset_at})
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=_detail())

    _use_client(monkeypatch, handler)
    assert github.collect(["org/repo"], SINCE, UNTIL) == []
    assert attempts["n"] == 3


def test_rate_limit_exhausted_raises(monkeypatch):
    requests_count = {"n": 0}
    monkeypatch.setattr(github.time, "sleep", lambda _: None)
    reset_at = str(int(time.time()) + 5)

    def handler(request: httpx.Request) -> httpx.Response:
        requests_count["n"] += 1
        if request.url.path.endswith("/commits"):
            return httpx.Response(403, headers={"X-RateLimit-Reset": reset_at})
        return httpx.Response(200, json=_detail())

    _use_client(monkeypatch, handler)
    with pytest.raises(CollectorError):
        github.collect(["org/repo"], SINCE, UNTIL)
    assert requests_count["n"] == 1 + github._RATE_LIMIT_MAX_RETRIES


def test_single_repo_failure_does_not_block_others(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/repos/org/broken"):
            return httpx.Response(500)
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[_commit_item("ok1")])
        return httpx.Response(200, json=_detail())

    _use_client(monkeypatch, handler)
    records = github.collect(["org/broken", "org/good"], SINCE, UNTIL)
    assert [record.repo for record in records] == ["org/good"]


def test_all_repos_failed_raises(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    _use_client(monkeypatch, handler)
    with pytest.raises(CollectorError) as exc_info:
        github.collect(["org/a", "org/b"], SINCE, UNTIL)
    assert exc_info.value.source == "github"


def test_window_filter_excludes_out_of_range(monkeypatch):
    detail_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            items = [
                _commit_item("in-window", date="2026-11-20T08:00:00Z"),
                _commit_item("before-window", date="2026-11-19T08:00:00Z"),
                _commit_item("after-window", date="2026-11-21T08:00:00Z"),
            ]
            return httpx.Response(200, json=items)
        detail_calls["n"] += 1
        return httpx.Response(200, json=_detail())

    _use_client(monkeypatch, handler)
    records = github.collect(["org/repo"], SINCE, UNTIL)
    assert [record.message for record in records] == ["feat: in-window"]
    assert detail_calls["n"] == 1


def test_author_fallback_when_login_missing(monkeypatch):
    item = _commit_item("abc")
    item["author"] = None

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/commits"):
            return httpx.Response(200, json=[item])
        return httpx.Response(200, json=_detail())

    _use_client(monkeypatch, handler)
    records = github.collect(["org/repo"], SINCE, UNTIL)
    assert records[0].author == "张三"


def test_token_sent_when_configured(monkeypatch):
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    _use_client(monkeypatch, handler)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token-placeholder")
    github.collect(["org/repo"], SINCE, UNTIL)
    assert captured["auth"] == "Bearer test-token-placeholder"


def test_anonymous_when_token_missing(monkeypatch):
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    _use_client(monkeypatch, handler)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    github.collect(["org/repo"], SINCE, UNTIL)
    assert captured["auth"] is None


def test_naive_datetime_rejected():
    with pytest.raises(CollectorError):
        github.collect(["org/repo"], datetime(2026, 11, 20), UNTIL)
