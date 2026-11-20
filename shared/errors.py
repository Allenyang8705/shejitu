"""自定义异常类型（design.md §6.1 错误处理策略的载体）。

统一继承 AutoDailyError，main.py 捕获基类即可覆盖所有已知业务失败；
未知异常保持原样抛出，避免掩盖编程错误。source 标识出错环节
（数据源用 collector 模块名），供日志与日报降级标注使用。
"""


class AutoDailyError(Exception):
    """项目异常基类：携带出错环节 source 与可读信息。"""

    def __init__(self, source: str, message: str) -> None:
        self.source = source
        self.message = message
        super().__init__(f"[{source}] {message}")


class ConfigError(AutoDailyError):
    """config.yaml 缺失、YAML 语法错误或字段校验不通过。"""


class CollectorError(AutoDailyError):
    """数据采集失败（source 取 github / lark_task / lark_msg）。"""


class GeneratorError(AutoDailyError):
    """日报生成失败。"""


class NotifierError(AutoDailyError):
    """推送失败（source 取 email / lark_bot）。"""
