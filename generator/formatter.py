"""日报生成（design.md §2.2 / §4.2）。

将各成员的采集数据整理为视图数据，经 Markdown / HTML 两套模板渲染出
同源一致的日报。展示规则（models.py failed_sources 语义的兑现）：
- 含失败源的成员，对应板块标注「数据获取失败」，优先于「今日无记录」；
- 完全无记录且无失败的成员，整块显示「今日无记录」。
"""

from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any

from generator import template
from shared.errors import GeneratorError
from shared.models import DailyReport, MemberReport

_VIEW_TZ = timezone(timedelta(hours=8))  # 受众为中国团队，固定 UTC+8 显示
_TRUNCATE_LIMIT = 100  # 消息与提交首行截断长度，防止长文本撑爆日报

# failed_sources 标识（collector 模块名）→ 日报展示名
_SOURCE_LABELS = {"github": "GitHub", "lark_msg": "飞书消息"}


def _display_time(value: datetime) -> str:
    """渲染用时间：转 UTC+8 后取时分。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(_VIEW_TZ).strftime("%H:%M")


def _truncate(text: str) -> str:
    text = text.strip()
    if len(text) <= _TRUNCATE_LIMIT:
        return text
    return text[:_TRUNCATE_LIMIT] + "…"


def _failure_note(source: str) -> str:
    label = _SOURCE_LABELS.get(source, source)
    return f"{label} 数据获取失败"


def _build_member_view(member: MemberReport) -> dict[str, Any]:
    """MemberReport → 模板视图数据（时间、截断、状态行等展示形态在此定型）。"""
    commits = [
        {
            "repo": commit.repo,
            "time": _display_time(commit.timestamp),
            "additions": commit.additions,
            "deletions": commit.deletions,
            "message": _truncate(commit.message.split("\n", 1)[0]),
        }
        for commit in member.commits
    ]
    messages = [
        {
            "time": _display_time(message.timestamp),
            "sender": message.sender,
            "chat": message.chat_name,
            "content": _truncate(message.content),
        }
        for message in member.messages
    ]
    return {
        "name": member.name,
        "commits": commits,
        "messages": messages,
        "commits_failed": "github" in member.failed_sources,
        "commits_failed_label": _failure_note("github"),
        "messages_failed": "lark_msg" in member.failed_sources,
        "messages_failed_label": _failure_note("lark_msg"),
        "has_any": bool(commits or messages or member.failed_sources),
    }


def generate(members: list[MemberReport], date: date, team_name: str) -> DailyReport:
    """生成日报（design.md §4.2 契约）。"""
    context = {
        "team_name": team_name,
        "date": date.isoformat(),
        "members": [_build_member_view(member) for member in members],
        "generated_at": _display_time(datetime.now(UTC)),
    }
    try:
        markdown = template.get_markdown_template().render(**context)
        html = template.get_html_template().render(**context)
    except Exception as exc:
        raise GeneratorError("generator", f"日报渲染失败：{exc}") from exc
    return DailyReport(
        date=date,
        team_name=team_name,
        members=members,
        generated_at=datetime.now(UTC),
        markdown=markdown.strip(),
        html=html,
    )
