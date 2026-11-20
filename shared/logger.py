"""JSON lines 日志（design.md §6.3：只使用 INFO 与 ERROR 两级）。

每行一个 JSON 对象：{"ts", "level", "event", ...附加字段}。附加字段承载
结构化信息（数据源采集条数、推送结果等），满足 tasks.md Task 9 对执行
日志内容的要求。默认输出到 stdout，cron 环境下由调度器统一收集。
"""

import json
import sys
from datetime import datetime
from typing import IO, Any

_output: IO[str] = sys.stdout


def set_output(stream: IO[str]) -> None:
    """重定向日志输出流（测试或写入文件时使用）。"""
    global _output
    _output = stream


def _emit(level: str, event: str, fields: dict[str, Any]) -> None:
    line = json.dumps(
        {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "level": level,
            "event": event,
            **fields,
        },
        ensure_ascii=False,
    )
    # 逐行 flush，保证进程异常退出时已产生的日志不丢失
    print(line, file=_output, flush=True)


def info(event: str, **fields: Any) -> None:
    """记录 INFO 级事件。"""
    _emit("INFO", event, fields)


def error(event: str, **fields: Any) -> None:
    """记录 ERROR 级事件。"""
    _emit("ERROR", event, fields)
