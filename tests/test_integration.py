"""main.py 集成测试：进程内运行真入口（走 argparse 与退出码），外部边界全部 mock。

边界替换约定：所有 monkeypatch 都打在 main 的导入名上（main.github.collect 等），
不发生任何真实网络调用，也不写真实 data/autodaily.db（Storage 指向 tmp_path）。
"""

import io
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import main
from shared import logger
from shared.config import ChatConfig, Config, MemberConfig
from shared.models import (
    CollectResult,
    CommitRecord,
    MessageRecord,
)

T1 = datetime(2026, 11, 20, 8, 30, tzinfo=UTC)


def _config(email_recipients: list[str] | None = None) -> Config:
    return Config(
        team_name="研发团队",
        repos=["org/repo"],
        chats=[ChatConfig(chat_id="oc-1", name="项目群")],
        message_keywords=["进度"],
        sensitive_keywords=["薪资"],
        members=[
            MemberConfig(name="张三", github="zhangsan", lark="zhangsan@company.com"),
            MemberConfig(name="李四", github="lisi", lark="lisi@company.com"),
        ],
        email_recipients=email_recipients or [],
    )


def _commit(author: str) -> CommitRecord:
    return CommitRecord(
        author=author,
        message="feat: 新增导出",
        timestamp=T1,
        repo="org/repo",
        additions=10,
        deletions=4,
        files_changed=2,
    )


def _message(sender: str) -> MessageRecord:
    return MessageRecord(
        sender=sender, content="讨论了联调进度", timestamp=T1, chat_name="项目群"
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> SimpleNamespace:
    """替换 main 的全部外部边界；返回 db_path 供断言落库结果。"""
    real_storage = main.Storage
    db_path = tmp_path / "it.db"
    monkeypatch.setattr(main, "Storage", lambda: real_storage(db_path))
    monkeypatch.setattr(main, "get_config", lambda: _config())
    monkeypatch.setattr(main, "is_workday", lambda *args, **kwargs: True)
    return SimpleNamespace(db_path=db_path)


def _patch_collectors(
    monkeypatch: pytest.MonkeyPatch,
    github_result: CollectResult[Any],
    msg_result: CollectResult[Any],
) -> None:
    monkeypatch.setattr(main.github, "collect", lambda *args, **kwargs: github_result)
    monkeypatch.setattr(main.lark_msg, "collect", lambda *args, **kwargs: msg_result)


def _saved_rows(db_path: Any) -> list[tuple]:
    # 全失败场景下 Storage 未构造，库文件 / reports 表可能不存在，均为「未落库」
    if not Path(db_path).exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT report_date, team_name, markdown, html FROM reports"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


def test_full_pipeline_success(monkeypatch, env):
    """tasks.md 场景 1：两源可用，日报生成、落库并推送成功；耗时 < 60 秒。"""
    _patch_collectors(
        monkeypatch,
        CollectResult(records=[_commit("zhangsan")], failed_targets=[]),
        CollectResult(records=[_message("张三")], failed_targets=[]),
    )
    monkeypatch.setattr(
        main, "get_config", lambda: _config(email_recipients=["leader@example.com"])
    )
    monkeypatch.setenv(
        "LARK_BOT_WEBHOOK_URL", "https://open.feishu.cn/hook/placeholder"
    )
    calls: list[str] = []

    def fake_email_send(report, recipients):
        calls.append("email")
        return True

    def fake_lark_send(report, chat_id):
        calls.append("lark")
        return True

    monkeypatch.setattr(main.email, "send", fake_email_send)
    monkeypatch.setattr(main.lark_bot, "send", fake_lark_send)

    started = time.perf_counter()
    exit_code = main.main([])
    elapsed = time.perf_counter() - started

    assert exit_code == 0
    assert elapsed < 60  # tasks.md：mock 环境下执行耗时 < 60 秒
    assert calls == ["email", "lark"]  # 两个渠道均被推送
    rows = _saved_rows(env.db_path)
    assert len(rows) == 1
    assert rows[0][1] == "研发团队"
    assert "张三" in rows[0][2]
    assert "- `org/repo`" in rows[0][2]


def test_degraded_source_annotated_dry_run(monkeypatch, env):
    """tasks.md 场景 2：仅 GitHub 源超时失败，日报标注失败，其余部分正常。"""
    _patch_collectors(
        monkeypatch,
        CollectResult(records=[], failed_targets=["org/broken"]),
        CollectResult(records=[_message("张三")], failed_targets=[]),
    )
    exit_code = main.main(["--dry-run"])

    assert exit_code == 0
    rows = _saved_rows(env.db_path)
    assert len(rows) == 1
    assert "GitHub 数据获取失败" in rows[0][2]
    assert "讨论了联调进度" in rows[0][2]  # 其余源数据正常呈现


def test_empty_data_still_reports(monkeypatch, env):
    """tasks.md 场景 3：成员无任何记录 → 显示「今日无记录」，报告仍生成。"""
    _patch_collectors(
        monkeypatch,
        CollectResult(records=[], failed_targets=[]),
        CollectResult(records=[], failed_targets=[]),
    )
    exit_code = main.main(["--dry-run"])

    assert exit_code == 0
    rows = _saved_rows(env.db_path)
    assert len(rows) == 1
    assert "今日无记录" in rows[0][2]
    assert "张三" in rows[0][2] and "李四" in rows[0][2]


def test_all_sources_failed_no_report(monkeypatch, env):
    """tasks.md 场景 4：全部数据源不可用 → 不生成日报，记录错误日志。"""
    stream = io.StringIO()
    logger.set_output(stream)
    _patch_collectors(
        monkeypatch,
        CollectResult(records=[], failed_targets=["org/a"]),
        CollectResult(records=[], failed_targets=["oc-1"]),
    )
    try:
        exit_code = main.main(["--dry-run"])
        output = stream.getvalue()
    finally:
        logger.set_output(sys.stdout)

    assert exit_code == 1
    assert "pipeline_abort_all_sources_failed" in output
    assert _saved_rows(env.db_path) == []  # 不落库


def test_push_failure_exit_code(monkeypatch, env):
    """tasks.md 场景 5 延伸：邮件推送失败 → 退出码 1，但飞书渠道仍被尝试（§6.1 备用渠道）。"""
    _patch_collectors(
        monkeypatch,
        CollectResult(records=[_commit("zhangsan")], failed_targets=[]),
        CollectResult(records=[], failed_targets=[]),
    )
    monkeypatch.setattr(
        main, "get_config", lambda: _config(email_recipients=["leader@example.com"])
    )
    monkeypatch.setenv(
        "LARK_BOT_WEBHOOK_URL", "https://open.feishu.cn/hook/placeholder"
    )
    calls: list[str] = []

    def fake_email_send(report, recipients):
        calls.append("email")
        return False

    def fake_lark_send(report, chat_id):
        calls.append("lark")
        return True

    monkeypatch.setattr(main.email, "send", fake_email_send)
    monkeypatch.setattr(main.lark_bot, "send", fake_lark_send)

    assert main.main([]) == 1
    assert calls == ["email", "lark"]
