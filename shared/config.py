"""config.yaml 读取、校验与全局访问（design.md §2.4 / §6.3）。

字段结构必须与 config.yaml.example 保持一致：新增配置项时先同步修改
example 与本文件，再改调用方，避免两者脱节。tests/test_config.py 中
有专门用例加载 example 本身做对齐校验。

进程为 cron 一次性运行：get_config() 在进程内首次调用时读取并缓存，
即满足 §6.3「修改 config.yaml 后下次执行自动生效」。
"""

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from shared.errors import ConfigError

DEFAULT_CONFIG_PATH = "config.yaml"

# 飞书项目工作项类型（OpenAPI 查询按类型进行；配置缺省时用平台内置三类型）
DEFAULT_WORK_ITEM_TYPES = ("task", "bug", "story")


@dataclass
class ChatConfig:
    """飞书群组：chat_id 用于 API 调用，name 用于日报中展示消息来源。"""

    chat_id: str
    name: str


@dataclass
class MemberConfig:
    """成员账号映射（design.md §3.3）。"""

    name: str
    github: str
    lark: str


@dataclass
class Config:
    """config.yaml 的类型化视图。"""

    team_name: str
    lark_project_id: str
    lark_work_item_types: list[str]
    repos: list[str]
    chats: list[ChatConfig]
    message_keywords: list[str]
    sensitive_keywords: list[str]
    members: list[MemberConfig]
    # 以下为可选字段：config 未配置时取空默认值
    email_recipients: list[str] = field(default_factory=list)
    holidays: list[date] = field(default_factory=list)
    extra_workdays: list[date] = field(default_factory=list)


def _non_empty_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("config", f"{label} 必须是非空字符串，当前为 {value!r}")
    return value


def _str_list(value: Any, label: str) -> list[str]:
    if not isinstance(value, list):
        raise ConfigError(
            "config", f"{label} 必须是字符串列表，当前为 {type(value).__name__}"
        )
    return [_non_empty_str(item, f"{label} 的元素") for item in value]


def _parse_date_list(value: Any, label: str) -> list[date]:
    """ISO 日期字符串列表 → date 列表；格式非法直接报错，不留哑数据。"""
    dates: list[date] = []
    for item in _str_list(value, label):
        try:
            dates.append(date.fromisoformat(item))
        except ValueError as exc:
            raise ConfigError(
                "config", f"{label} 含非法日期：{item}（应为 YYYY-MM-DD）"
            ) from exc
    return dates


def _parse_chats(value: Any) -> list[ChatConfig]:
    if not isinstance(value, list):
        raise ConfigError(
            "config", f"配置项 chats 必须是对象列表，当前为 {type(value).__name__}"
        )
    chats: list[ChatConfig] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(
                "config", f"chats[{index}] 必须是包含 chat_id 与 name 的对象"
            )
        chats.append(
            ChatConfig(
                chat_id=_non_empty_str(item.get("chat_id"), f"chats[{index}].chat_id"),
                name=_non_empty_str(item.get("name"), f"chats[{index}].name"),
            )
        )
    return chats


def _parse_members(value: Any) -> list[MemberConfig]:
    if not isinstance(value, list):
        raise ConfigError(
            "config", f"配置项 members 必须是对象列表，当前为 {type(value).__name__}"
        )
    if not value:
        # 空成员表无法生成任何成员段落，属于配置错误而非「无数据」
        raise ConfigError(
            "config", "配置项 members 至少需要一名成员（身份映射是日报的核心输入）"
        )
    members: list[MemberConfig] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ConfigError(
                "config", f"members[{index}] 必须是包含 name / github / lark 的对象"
            )
        members.append(
            MemberConfig(
                name=_non_empty_str(item.get("name"), f"members[{index}].name"),
                github=_non_empty_str(item.get("github"), f"members[{index}].github"),
                lark=_non_empty_str(item.get("lark"), f"members[{index}].lark"),
            )
        )
    return members


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """读取并校验配置；文件缺失、YAML 语法错误或字段不合法时抛出 ConfigError。"""
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(
            "config",
            f"配置文件不存在：{config_path}（请从 config.yaml.example 复制生成）",
        )
    try:
        raw: Any = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError("config", f"YAML 解析失败（{config_path}）：{exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError(
            "config", f"配置根节点必须是键值映射，当前为 {type(raw).__name__}"
        )

    required_keys = (
        "team_name",
        "repos",
        "chats",
        "lark_project_id",
        "message_keywords",
        "sensitive_keywords",
        "members",
    )
    missing = [key for key in required_keys if key not in raw]
    if missing:
        raise ConfigError("config", f"缺少必填配置项：{', '.join(missing)}")

    return Config(
        team_name=_non_empty_str(raw["team_name"], "配置项 team_name"),
        lark_project_id=_non_empty_str(
            raw["lark_project_id"], "配置项 lark_project_id"
        ),
        lark_work_item_types=_str_list(
            raw.get("lark_work_item_types") or list(DEFAULT_WORK_ITEM_TYPES),
            "配置项 lark_work_item_types",
        ),
        repos=_str_list(raw["repos"], "配置项 repos"),
        chats=_parse_chats(raw["chats"]),
        message_keywords=_str_list(raw["message_keywords"], "配置项 message_keywords"),
        sensitive_keywords=_str_list(
            raw["sensitive_keywords"], "配置项 sensitive_keywords"
        ),
        members=_parse_members(raw["members"]),
        email_recipients=_str_list(
            raw.get("email_recipients") or [], "配置项 email_recipients"
        ),
        holidays=_parse_date_list(raw.get("holidays") or [], "配置项 holidays"),
        extra_workdays=_parse_date_list(
            raw.get("extra_workdays") or [], "配置项 extra_workdays"
        ),
    )


_config: Config | None = None


def get_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    """全局配置访问：进程内首次调用读取文件并缓存。"""
    global _config
    if _config is None:
        _config = load_config(path)
    return _config


def reset_config() -> None:
    """清空进程内缓存；测试或需要强制重读时使用。"""
    global _config
    _config = None
