"""跨层领域模型（design.md §3）。

collector / generator / notifier / main 都依赖本模块；本模块保持纯数据，
不包含任何外部服务调用或存储逻辑。
"""

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class CommitRecord:
    """代码提交记录（design.md §3.1）。"""

    author: str
    message: str
    timestamp: datetime
    repo: str
    additions: int
    deletions: int
    files_changed: int


@dataclass
class TaskRecord:
    """任务状态变更记录（design.md §3.1）。"""

    assignee: str
    title: str
    status_from: str
    status_to: str
    updated_at: datetime


@dataclass
class MessageRecord:
    """群消息记录（design.md §3.1）。"""

    sender: str
    content: str
    timestamp: datetime
    chat_name: str


@dataclass
class MemberReport:
    """成员日报聚合（design.md §3.2，生成层输入）。

    failed_sources 是 design.md §3.2 之外补充的字段：tasks.md T3~T5 约定
    采集失败时返回空列表，因此「失败」无法用空列表本身表达，只能经由本
    字段传递（标识取 collector 模块名：github / lark_task / lark_msg）。
    生成层据此区分两种展示：空列表表示采集成功但当日无记录（今日无记录），
    源出现在 failed_sources 中则标注「数据获取失败」（tasks.md Task 6）。
    """

    name: str
    github_username: str
    commits: list[CommitRecord] = field(default_factory=list)
    tasks: list[TaskRecord] = field(default_factory=list)
    messages: list[MessageRecord] = field(default_factory=list)
    failed_sources: list[str] = field(default_factory=list)


@dataclass
class DailyReport:
    """日报（design.md §3.2，生成层输出）。"""

    date: date
    team_name: str
    members: list[MemberReport]
    generated_at: datetime
    markdown: str = ""
    html: str = ""
