"""自定义异常与错误处理策略（design.md §2.4 / §6.1）。

统一继承 AutoDailyError，main.py 捕获基类即可覆盖所有已知业务失败；
未知异常保持原样抛出，避免掩盖编程错误。source 标识出错环节
（数据源用 collector 模块名），供日志与日报降级标注使用。
call_with_retry 提供跨采集 / 推送模块复用的固定间隔重试。
"""

import time
from collections.abc import Callable
from typing import TypeVar


class AutoDailyError(Exception):
    """项目异常基类：携带出错环节 source 与可读信息。"""

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")


class ConfigError(AutoDailyError):
    """config.yaml 缺失、YAML 语法错误或字段校验不通过。"""


class CollectorError(AutoDailyError):
    """数据采集失败（source 取 github / lark_msg）。"""


class GeneratorError(AutoDailyError):
    """日报生成失败。"""


class NotifierError(AutoDailyError):
    """推送失败（source 取 email / lark_bot）。"""


_T = TypeVar("_T")


def call_with_retry(
    operation: Callable[[], _T],
    *,
    retries: int = 3,
    interval: float = 5.0,
    retry_on: tuple[type[BaseException], ...],
) -> _T:
    """固定间隔重试（design.md §6.1：API 超时重试 3 次、间隔 5s）。

    operation 抛出 retry_on 中的异常时最多再重试 retries 次（不含首次），
    间隔 interval 秒；重试耗尽后原样抛出最后一次异常，降级行为由调用方
    决定。retry_on 未命中的异常立即透传，不重试。
    """
    attempts_left = retries
    while True:
        try:
            return operation()
        except retry_on:
            if attempts_left <= 0:
                raise
            attempts_left -= 1
            time.sleep(interval)
