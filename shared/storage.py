"""SQLite 日报历史存取（design.md §2.4 / ADR-002）。

本期不做历史检索与统计分析（proposal §2.2），因此只保存渲染结果与元
数据：成员明细已完整包含在 markdown / html 内，不做关系化拆表。
get_report() 返回的 DailyReport 成员列表为空，仅用于回看历史内容。

写入为追加式：同一天多次执行各自保留一条记录，查询取 generated_at
最新的一条，避免覆盖式更新丢失执行痕迹。
"""

import sqlite3
from datetime import date, datetime
from pathlib import Path
from types import TracebackType
from typing import Self

from shared.models import DailyReport

DEFAULT_DB_PATH = Path("data") / "autodaily.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date TEXT NOT NULL,
    team_name TEXT NOT NULL,
    markdown TEXT NOT NULL,
    html TEXT NOT NULL,
    generated_at TEXT NOT NULL
)
"""


class Storage:
    """日报历史表的最小封装：建库、写入、按日期查询。"""

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH) -> None:
        path = Path(db_path)
        # data/ 为运行产物目录（gitignore），不存在时按需创建，免去部署方手工准备
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(_SCHEMA)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def save_report(self, report: DailyReport) -> int:
        """追加一条日报记录，返回自增 id。"""
        cursor = self._conn.execute(
            "INSERT INTO reports (report_date, team_name, markdown, html, generated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (
                report.date.isoformat(),
                report.team_name,
                report.markdown,
                report.html,
                report.generated_at.isoformat(),
            ),
        )
        self._conn.commit()
        # INSERT 成功后 lastrowid 必然存在，断言仅用于类型收窄
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    def get_report(self, query_date: date) -> DailyReport | None:
        """查询指定日期最新一条日报；成员明细不还原（见模块 docstring）。"""
        row = self._conn.execute(
            "SELECT report_date, team_name, markdown, html, generated_at"
            " FROM reports WHERE report_date = ?"
            " ORDER BY generated_at DESC, id DESC LIMIT 1",
            (query_date.isoformat(),),
        ).fetchone()
        if row is None:
            return None
        report_date_str, team_name, markdown, html, generated_at_str = row
        return DailyReport(
            date=date.fromisoformat(report_date_str),
            team_name=team_name,
            members=[],
            generated_at=datetime.fromisoformat(generated_at_str),
            markdown=markdown,
            html=html,
        )
