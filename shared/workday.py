"""工作日判断（proposal 3.3：非工作日不触发日报生成）。

规则：周一至周五且不在节假日列表为工作日；调休补班的周末视为工作日。
节假日（holidays）与补班日（extra_workdays）由维护者按国务院每年安排
更新到 config.yaml，随 config 热更新机制每次执行重新读取。
"""

from datetime import date

from shared.config import Config, get_config


def is_workday(day: date, config: Config | None = None) -> bool:
    """判断给定日期是否为工作日；显式传入 config 便于测试与多配置场景。

    补班日优先于节假日：同日同时出现在两张表时按上班处理（维护失误时
    宁可多跑一次日报，也不静默缺报）。
    """
    cfg = config if config is not None else get_config()
    if day in cfg.extra_workdays:
        return True
    if day in cfg.holidays:
        return False
    return day.weekday() < 5
