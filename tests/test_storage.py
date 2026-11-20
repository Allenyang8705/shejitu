"""storage.py 单元测试。"""

from datetime import date, datetime

from shared.models import DailyReport
from shared.storage import Storage


def _report(
    report_date: date, generated_at: datetime, markdown: str = "# 日报"
) -> DailyReport:
    return DailyReport(
        date=report_date,
        team_name="测试团队",
        members=[],
        generated_at=generated_at,
        markdown=markdown,
        html="<h1>日报</h1>",
    )


def test_save_and_get_roundtrip(tmp_path):
    # db 路径中的父目录不存在时应自动创建（data/ 属运行产物）
    with Storage(tmp_path / "sub" / "autodaily.db") as storage:
        storage.save_report(
            _report(date(2026, 11, 20), datetime(2026, 11, 20, 18, 0, 0))
        )
        loaded = storage.get_report(date(2026, 11, 20))
    assert loaded is not None
    assert loaded.date == date(2026, 11, 20)
    assert loaded.team_name == "测试团队"
    assert loaded.markdown == "# 日报"
    assert loaded.html == "<h1>日报</h1>"
    assert loaded.generated_at == datetime(2026, 11, 20, 18, 0, 0)


def test_get_missing_date_returns_none(tmp_path):
    with Storage(tmp_path / "db.sqlite") as storage:
        assert storage.get_report(date(2026, 1, 1)) is None


def test_same_date_returns_latest(tmp_path):
    with Storage(tmp_path / "db.sqlite") as storage:
        storage.save_report(
            _report(
                date(2026, 11, 20), datetime(2026, 11, 20, 12, 0, 0), markdown="第一版"
            )
        )
        storage.save_report(
            _report(
                date(2026, 11, 20), datetime(2026, 11, 20, 18, 5, 0), markdown="第二版"
            )
        )
        loaded = storage.get_report(date(2026, 11, 20))
    assert loaded is not None
    assert loaded.markdown == "第二版"


def test_save_returns_increasing_id(tmp_path):
    with Storage(tmp_path / "db.sqlite") as storage:
        first = storage.save_report(
            _report(date(2026, 11, 19), datetime(2026, 11, 19, 18, 0, 0))
        )
        second = storage.save_report(
            _report(date(2026, 11, 20), datetime(2026, 11, 20, 18, 0, 0))
        )
    assert second > first
