"""formatter.py / template.py 单元测试。"""

from datetime import UTC, date, datetime

import pytest

from generator import formatter, template
from shared.errors import GeneratorError
from shared.models import CommitRecord, MemberReport, MessageRecord, TaskRecord

DAY = date(2026, 11, 20)
# UTC 08:30 → UTC+8 16:30（同日）
T1 = datetime(2026, 11, 20, 8, 30, tzinfo=UTC)
T2 = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)


def _member(**overrides: object) -> MemberReport:
    params: dict = {"name": "张三", "github_username": "zhangsan"}
    params.update(overrides)
    return MemberReport(**params)


def test_markdown_contains_three_sections():
    member = _member(
        commits=[CommitRecord("zhangsan", "feat: 新增导出", T1, "org/repo", 10, 4, 2)],
        tasks=[TaskRecord("张三", "导出功能", "进行中", "已完成", T1)],
        messages=[MessageRecord("李四", "讨论了联调进度", T2, "项目群")],
    )
    report = formatter.generate([member], DAY, "示例团队")

    assert "# 示例团队 日报（2026-11-20）" in report.markdown
    assert "### 代码提交" in report.markdown
    assert "### 任务进展" in report.markdown
    assert "### 协作沟通" in report.markdown


def test_commit_line_details():
    member = _member(
        commits=[
            CommitRecord(
                "zhangsan", "feat: 新增导出\n\n详细描述", T1, "org/repo", 10, 4, 2
            )
        ]
    )
    report = formatter.generate([member], DAY, "示例团队")

    # 只取首行；时间转为 UTC+8；统计口径为增删行数
    assert "- `org/repo` 16:30（+10/-4）feat: 新增导出" in report.markdown


def test_task_transition_line():
    member = _member(
        tasks=[
            TaskRecord("张三", "导出功能", "进行中", "已完成", T1),
            TaskRecord("张三", "权限修复", "", "待处理", T2),
        ]
    )
    report = formatter.generate([member], DAY, "示例团队")

    assert "- 16:30 导出功能：进行中 → 已完成" in report.markdown
    # 新建流转没有原状态，展示为「新建」
    assert "- 17:00 权限修复：新建 → 待处理" in report.markdown


def test_message_truncated_and_chat_shown(monkeypatch):
    monkeypatch.setattr(formatter, "_TRUNCATE_LIMIT", 10)
    long_text = "这是一条很长很长的讨论内容需要被截断"
    member = _member(messages=[MessageRecord("李四", long_text, T1, "项目群")])
    report = formatter.generate([member], DAY, "示例团队")

    # 发信人、群名、时间与截断后的内容组成完整行；原文不得完整出现
    assert (
        "- 16:30 李四（项目群）：" + formatter._truncate(long_text) in report.markdown
    )
    assert "需要被截断" not in report.markdown


def test_no_record_member_is_compact():
    report = formatter.generate(
        [_member(name="李四", github_username="lisi")], DAY, "示例团队"
    )

    assert "## 李四" in report.markdown
    assert "今日无记录" in report.markdown
    # 完全无记录的成员不展开三个板块标题
    assert "### 代码提交" not in report.markdown


def test_failed_source_annotation():
    member = _member(failed_sources=["github"])
    report = formatter.generate([member], DAY, "示例团队")

    assert "GitHub 数据获取失败" in report.markdown
    assert "### 代码提交" in report.markdown
    # 其余两个空板块显示「今日无记录」，不被失败标注覆盖
    assert report.markdown.count("今日无记录") == 2


def test_failed_source_with_partial_records():
    member = _member(
        commits=[
            CommitRecord("zhangsan", "feat: 正常仓库的提交", T1, "org/good", 1, 1, 1)
        ],
        failed_sources=["github"],
    )
    report = formatter.generate([member], DAY, "示例团队")

    assert "feat: 正常仓库的提交" in report.markdown
    assert "GitHub 数据获取失败" in report.markdown


def test_html_structure_and_escaping():
    member = _member(
        commits=[CommitRecord("zhangsan", "feat: x", T1, "org/repo", 1, 1, 1)],
        messages=[
            MessageRecord("李四", "<script>alert(1)</script> 进度", T2, "项目群")
        ],
    )
    report = formatter.generate([member], DAY, "示例团队")

    assert report.html.startswith("<!DOCTYPE html>")
    assert "<h2>张三</h2>" in report.html
    assert "<h3>代码提交</h3>" in report.html
    assert "<h3>任务进展</h3>" in report.html
    assert "<h3>协作沟通</h3>" in report.html
    # 群聊原文进 HTML 前必须转义（design.md §6.2）
    assert "<script>alert(1)</script>" not in report.html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; 进度" in report.html


def test_generate_returns_structured_daily_report():
    member = _member(
        commits=[CommitRecord("zhangsan", "feat: x", T1, "org/repo", 1, 1, 1)]
    )
    report = formatter.generate([member], DAY, "示例团队")

    assert report.date == DAY
    assert report.team_name == "示例团队"
    assert report.members == [member]  # 结构化数据原样保留
    assert report.generated_at.tzinfo is not None
    assert report.markdown
    assert report.html


def test_empty_team():
    report = formatter.generate([], DAY, "示例团队")

    assert "# 示例团队 日报（2026-11-20）" in report.markdown
    assert report.html.startswith("<!DOCTYPE html>")


def test_render_error_wrapped_as_generator_error(monkeypatch):
    class _ExplodingTemplate:
        def render(self, **kwargs: object) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr(template, "get_markdown_template", lambda: _ExplodingTemplate())

    with pytest.raises(GeneratorError, match="日报渲染失败"):
        formatter.generate([], DAY, "示例团队")


def test_templates_renderable():
    """template.py 的两套模板必须可独立渲染（防模板常量语法损坏）。"""
    member = _member(
        commits=[CommitRecord("zhangsan", "feat: x", T1, "org/repo", 1, 1, 1)],
        failed_sources=["lark_msg"],
    )
    context = {
        "team_name": "示例团队",
        "date": DAY.isoformat(),
        "members": [formatter._build_member_view(member)],
        "generated_at": "18:00",
    }
    markdown = template.get_markdown_template().render(**context)
    html = template.get_html_template().render(**context)

    assert "飞书消息 数据获取失败" in markdown
    assert "飞书消息 数据获取失败" in html
