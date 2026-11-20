"""主编排入口（design.md §2.5）：采集 → 聚合 → 生成 → 推送。

cron 一次性进程。日志事件覆盖 tasks.md Task 9 的执行记录要求：
pipeline_start（开始时间）→ collect_done（各数据源条数与失败目标）→
generate_done → push_email / push_lark（推送结果）→ pipeline_end
（结束时间与总耗时）。

退出码：0 = 全流程成功；1 = 未生成日报（全源失败 / 配置错误）或存在
推送失败；--check 有任一失败项。
"""

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta, timezone

import httpx

from collector import github, lark_auth, lark_msg, lark_task
from generator.formatter import generate
from notifier import email, lark_bot
from shared import logger
from shared.config import Config, get_config
from shared.errors import AutoDailyError
from shared.models import (
    CollectResult,
    CommitRecord,
    MemberReport,
    MessageRecord,
    TaskRecord,
)
from shared.storage import Storage
from shared.workday import is_workday

_VIEW_TZ = timezone(timedelta(hours=8))  # 与 formatter 一致：受众为中国团队
_GITHUB_CHECK_URL = "https://api.github.com/rate_limit"
_SMTP_ENV_KEYS = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")


def _collect_window(now_utc: datetime) -> tuple[datetime, datetime]:
    """采集窗口：UTC+8 当天 00:00 → 执行时刻（proposal 采集窗口定义）。"""
    local_now = now_utc.astimezone(_VIEW_TZ)
    local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return local_start.astimezone(UTC), now_utc


def _aggregate(
    cfg: Config,
    github_result: CollectResult[CommitRecord],
    task_result: CollectResult[TaskRecord],
    messages: list[MessageRecord],
    msg_failed: list[str],
) -> tuple[list[MemberReport], bool]:
    """按身份映射聚合到成员；返回 (成员列表, 是否三源全部失败)。

    GitHub 按 members[].github 匹配；飞书侧 assignee / sender 为显示名，
    按 members[].name 精确匹配（部署要求 config 姓名与飞书显示名一致）。
    聚合不到的记录丢弃并记日志，避免未知账号撑爆日报。
    """
    by_github = {member.github: member for member in cfg.members}
    by_name = {member.name: member for member in cfg.members}
    reports = {
        member.name: MemberReport(name=member.name, github_username=member.github)
        for member in cfg.members
    }

    for record in github_result.records:
        member = by_github.get(record.author)
        if member is None:
            logger.info("aggregate_unmatched", source="github", key=record.author)
            continue
        reports[member.name].commits.append(record)
    for task in task_result.records:
        member = by_name.get(task.assignee)
        if member is None:
            logger.info("aggregate_unmatched", source="lark_task", key=task.assignee)
            continue
        reports[member.name].tasks.append(task)
    for message in messages:
        member = by_name.get(message.sender)
        if member is None:
            logger.info("aggregate_unmatched", source="lark_msg", key=message.sender)
            continue
        reports[member.name].messages.append(message)

    failed_sources = []
    if github_result.failed_targets:
        failed_sources.append("github")
    if task_result.failed_targets:
        failed_sources.append("lark_task")
    if msg_failed:
        failed_sources.append("lark_msg")
    for report in reports.values():
        report.failed_sources = list(failed_sources)
    return list(reports.values()), len(failed_sources) == 3


def _log_end(started: datetime, exit_code: int) -> None:
    duration = (datetime.now(UTC) - started).total_seconds()
    logger.info(
        "pipeline_end", exit_code=exit_code, duration_seconds=round(duration, 2)
    )


def run_pipeline(dry_run: bool) -> int:
    """完整流程：采集 → 聚合 → 生成 → 落库 →（可选）推送。"""
    started = datetime.now(UTC)
    logger.info(
        "pipeline_start",
        dry_run=dry_run,
        started_at=started.isoformat(timespec="seconds"),
    )
    cfg = get_config()
    since, until = _collect_window(datetime.now(UTC))

    github_result = github.collect(cfg.repos, since, until)
    logger.info(
        "collect_done",
        source="github",
        count=len(github_result.records),
        failed=github_result.failed_targets,
    )

    task_result = lark_task.collect(cfg.lark_project_id, since, until)
    logger.info(
        "collect_done",
        source="lark_task",
        count=len(task_result.records),
        failed=task_result.failed_targets,
    )

    messages: list[MessageRecord] = []
    msg_failed: list[str] = []
    for chat in cfg.chats:
        result = lark_msg.collect(chat.chat_id, cfg.message_keywords, since, until)
        messages.extend(result.records)
        msg_failed.extend(result.failed_targets)
    logger.info(
        "collect_done", source="lark_msg", count=len(messages), failed=msg_failed
    )

    members, all_failed = _aggregate(
        cfg, github_result, task_result, messages, msg_failed
    )
    if all_failed:
        logger.error(
            "pipeline_abort_all_sources_failed",
            sources=["github", "lark_task", "lark_msg"],
        )
        _log_end(started, 1)
        return 1

    report = generate(members, until.astimezone(_VIEW_TZ).date(), cfg.team_name)
    logger.info(
        "generate_done",
        members=len(members),
        markdown_chars=len(report.markdown),
        html_chars=len(report.html),
    )

    with Storage() as storage:
        storage.save_report(report)
    logger.info("report_saved", day=report.date.isoformat())

    exit_code = 0
    if dry_run:
        logger.info("push_skipped", reason="dry_run", channels=["email", "lark_bot"])
    else:
        # 两个推送渠道各自独立尝试、互不阻断（design.md §6.1 备用渠道语义）
        if not cfg.email_recipients:
            logger.info("push_email_skipped", reason="email_recipients 未配置")
        else:
            email_ok = email.send(report, cfg.email_recipients)
            logger.info("push_email", success=email_ok, recipients=cfg.email_recipients)
            if not email_ok:
                exit_code = 1

        if not os.environ.get("LARK_BOT_WEBHOOK_URL"):
            logger.info("push_lark_skipped", reason="webhook 未配置")
        else:
            chat_id = cfg.chats[0].chat_id if cfg.chats else "default"
            lark_ok = lark_bot.send(report, chat_id)
            logger.info("push_lark", success=lark_ok, chat_id=chat_id)
            if not lark_ok:
                exit_code = 1

    _log_end(started, exit_code)
    return exit_code


def _check_env_group(
    group: str, keys: tuple[str, ...], *, any_of: bool = False
) -> bool:
    values = [os.environ.get(key, "").strip() for key in keys]
    ok = any(values) if any_of else all(values)
    logger.info("check_env", group=group, ok=ok)
    return ok


def run_check() -> int:
    """健康检查：配置加载、凭据存在性、外部服务连通性；失败项记日志。"""
    failures = 0
    try:
        cfg = get_config()
        logger.info("check_config", ok=True, team=cfg.team_name)
    except AutoDailyError as exc:
        logger.error("check_config", ok=False, error=exc.message)
        return 1

    # GITHUB_TOKEN 可选（匿名限流低），仅提示不计数
    logger.info(
        "check_env",
        group="github_token",
        ok=bool(os.environ.get("GITHUB_TOKEN", "").strip()),
        optional=True,
    )
    if not _check_env_group("lark_app", ("LARK_APP_ID", "LARK_APP_SECRET")):
        failures += 1
    if not _check_env_group(
        "lark_user", ("LARK_USER_ID", "LARK_USER_KEY"), any_of=True
    ):
        failures += 1
    if not _check_env_group("smtp", _SMTP_ENV_KEYS):
        failures += 1
    if not _check_env_group("lark_bot_webhook", ("LARK_BOT_WEBHOOK_URL",)):
        failures += 1

    try:
        response = httpx.get(_GITHUB_CHECK_URL, timeout=10.0)
        github_ok = response.status_code == 200
        if not github_ok:
            logger.error(
                "check_connectivity", target="github", http_status=response.status_code
            )
    except httpx.HTTPError as exc:
        github_ok = False
        logger.error(
            "check_connectivity", target="github", error=f"{type(exc).__name__}: {exc}"
        )
    if not github_ok:
        failures += 1
    else:
        logger.info("check_connectivity", target="github", ok=True)

    try:
        lark_auth.get_lark_auth().request_headers()
        logger.info("check_connectivity", target="lark_project_token", ok=True)
    except AutoDailyError as exc:
        failures += 1
        logger.error(
            "check_connectivity",
            target="lark_project_token",
            ok=False,
            error=exc.message,
        )

    try:
        lark_auth.get_lark_tenant_auth().request_headers()
        logger.info("check_connectivity", target="lark_tenant_token", ok=True)
    except AutoDailyError as exc:
        failures += 1
        logger.error(
            "check_connectivity",
            target="lark_tenant_token",
            ok=False,
            error=exc.message,
        )

    logger.info("check_end", failures=failures)
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="智能日报生成器：采集 → 生成 → 推送")
    parser.add_argument(
        "--check", action="store_true", help="健康检查：验证配置、凭据与外部服务连通性"
    )
    parser.add_argument("--dry-run", action="store_true", help="执行采集与生成但不推送")
    args = parser.parse_args(argv)
    if args.check:
        return run_check()
    if args.dry_run:
        return run_pipeline(dry_run=True)

    # 默认流程先做工作日门禁（proposal 3.3）；--check / --dry-run 跳过便于随时验证
    today = datetime.now(UTC).astimezone(_VIEW_TZ).date()
    if not is_workday(today):
        logger.info("pipeline_skipped", reason="非工作日", day=today.isoformat())
        return 0
    return run_pipeline(dry_run=False)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AutoDailyError as exc:
        logger.error("pipeline_fatal", error=exc.message)
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001 — 顶层兜底：未预期异常也须记日志后失败退出
        logger.error("pipeline_fatal", error=f"{type(exc).__name__}: {exc}")
        sys.exit(1)
