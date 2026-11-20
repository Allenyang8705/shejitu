"""飞书任务采集模块（design.md §2.1 / §4.1）。

获取指定飞书项目在时间窗口内的任务状态变更：先按 updated_at 窗口查询
各类型工作项，再逐项取流转记录，映射为 TaskRecord（status_from → status_to）。

失败语义与 github.py 一致（design.md §3.1 / §4.1）：失败作为数据返回。
项目内按工作项类型隔离：单类型失败记 ERROR 日志并置 failed_targets，
不丢其他类型已取得的数据；CollectorError 仅用于契约违规。

端点与响应结构核对点（官方文档站本环境不可达，检索实现，联调只改常量）：
- 工作项查询 POST /open_api/{project_key}/work_item/query
  （body: work_item_type_key / page_num / page_size / field_conditions.updated_at.between）
- 流转记录 GET /open_api/{project_key}/work_item/{type}/{id}/logs
  （响应 data.logs[].from_state / to_state / created_at）
- 若 query 实际返回纯 ID 列表，需在 _query_work_items 补工作项详情接口
"""

from datetime import UTC, datetime
from typing import Any

from collector import lark_auth
from collector.lark_auth import LarkAuth
from shared import logger
from shared.config import get_config
from shared.errors import CollectorError
from shared.models import CollectResult, TaskRecord

_PAGE_SIZE = 50  # 查询接口单页上限（文档检索值）
_WORK_ITEM_QUERY_PATH = "/open_api/{project_key}/work_item/query"
_WORK_ITEM_LOGS_PATH = (
    "/open_api/{project_key}/work_item/{work_item_type_key}/{work_item_id}/logs"
)


def _get_auth() -> LarkAuth:
    """单例入口独立成函数，便于测试替换鉴权实现。"""
    return lark_auth.get_lark_auth()


def _ms_to_datetime(value: Any) -> datetime:
    """飞书毫秒时间戳 → aware UTC datetime（时间统一约定）。"""
    return datetime.fromtimestamp(int(float(value)) / 1000, tz=UTC)


def _state_name(value: Any) -> str | None:
    """状态字段兼容字符串与 {name}/{value} 对象两种响应形态。"""
    if value is None:
        return None
    if isinstance(value, dict):
        inner = value.get("name") or value.get("value")
        return str(inner) if inner is not None else None
    return str(value)


def _first_field(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("value")
        if isinstance(value, str) and value.strip():
            return value
    return None


def _query_work_items(
    auth: LarkAuth, project_id: str, type_key: str, since: datetime, until: datetime
) -> list[dict[str, Any]]:
    """按 updated_at 窗口分页查询某类型工作项。"""
    path = _WORK_ITEM_QUERY_PATH.format(project_key=project_id)
    items: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = auth.api_request(
            "POST",
            path,
            json_body={
                "work_item_type_key": type_key,
                "page_num": page,
                "page_size": _PAGE_SIZE,
                "field_conditions": [
                    {
                        "field_key": "updated_at",
                        "field_type": "time",
                        "operator": "between",
                        "value": [
                            str(int(since.timestamp() * 1000)),
                            str(int(until.timestamp() * 1000)),
                        ],
                    }
                ],
            },
        )
        data = payload.get("data")
        data = data if isinstance(data, dict) else {}
        batch = data.get("data") or data.get("work_items") or []
        if not isinstance(batch, list):
            raise CollectorError("lark_task", f"工作项查询响应结构异常：{path}")
        for entry in batch:
            if isinstance(entry, dict):
                items.append(entry)
            else:
                # query 若返回纯 ID 列表则缺少 title/assignee，无法成记录；
                # 显式记日志便于发现该形态并在联调时补详情接口
                logger.info(
                    "lark_task_item_not_dict",
                    project_id=project_id,
                    work_item_type=type_key,
                    entry_type=type(entry).__name__,
                )
        if len(batch) < _PAGE_SIZE and not data.get("has_more"):
            break
        page += 1
    return items


def _transitions_to_records(
    auth: LarkAuth,
    project_id: str,
    type_key: str,
    item: dict[str, Any],
    since: datetime,
    until: datetime,
) -> list[TaskRecord]:
    """取单个工作项的流转记录并映射为窗口内的 TaskRecord 列表。"""
    work_item_id = str(
        item.get("id") or item.get("key") or item.get("work_item_id") or ""
    )
    if not work_item_id:
        logger.info(
            "lark_task_item_missing_id", project_id=project_id, work_item_type=type_key
        )
        return []
    path = _WORK_ITEM_LOGS_PATH.format(
        project_key=project_id, work_item_type_key=type_key, work_item_id=work_item_id
    )
    payload = auth.api_request("GET", path)
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    logs = data.get("logs") or data.get("transitions") or []
    if not isinstance(logs, list):
        raise CollectorError("lark_task", f"流转记录响应结构异常：{path}")

    assignee = _first_field(item, ("owner", "assignee", "handler")) or "unknown"
    title = _first_field(item, ("title", "name", "subject")) or ""
    records: list[TaskRecord] = []
    for entry in logs:
        if not isinstance(entry, dict):
            continue
        status_from = _state_name(entry.get("from_state") or entry.get("from"))
        status_to = _state_name(entry.get("to_state") or entry.get("to"))
        raw_time = (
            entry.get("created_at") or entry.get("updated_at") or entry.get("time")
        )
        if status_to is None or raw_time is None:
            continue
        timestamp = _ms_to_datetime(raw_time)
        if not since <= timestamp <= until:
            # 查询按工作项维度过滤，流转记录需按变更时刻二次过滤
            continue
        records.append(
            TaskRecord(
                assignee=assignee,
                title=title,
                status_from=status_from or "",
                status_to=status_to,
                updated_at=timestamp,
            )
        )
    return records


def _collect_project(
    auth: LarkAuth, project_id: str, since: datetime, until: datetime
) -> tuple[list[TaskRecord], bool]:
    """聚合全部工作项类型的记录；返回 (records, 是否存在类型级失败)。"""
    records: list[TaskRecord] = []
    any_failed = False
    for type_key in get_config().lark_work_item_types:
        try:
            for item in _query_work_items(auth, project_id, type_key, since, until):
                records.extend(
                    _transitions_to_records(
                        auth, project_id, type_key, item, since, until
                    )
                )
        except Exception as exc:  # noqa: BLE001 — 类型间隔离，单类型失败不丢其他类型数据
            any_failed = True
            logger.error(
                "lark_task_type_failed",
                project_id=project_id,
                work_item_type=type_key,
                error=f"{type(exc).__name__}: {exc}",
            )
    return records, any_failed


def collect(
    project_id: str, since: datetime, until: datetime
) -> CollectResult[TaskRecord]:
    """采集指定飞书项目在窗口内的任务状态变更（design.md §4.1 契约）。"""
    if since.tzinfo is None or until.tzinfo is None:
        raise CollectorError(
            "lark_task", "since / until 必须是带时区的 datetime（统一 aware UTC 约定）"
        )
    try:
        records, any_failed = _collect_project(_get_auth(), project_id, since, until)
    except Exception as exc:  # noqa: BLE001 — 失败作为数据返回（design.md §4.1），含凭据缺失
        logger.error(
            "lark_task_failed",
            project_id=project_id,
            error=f"{type(exc).__name__}: {exc}",
        )
        return CollectResult(records=[], failed_targets=[project_id])
    return CollectResult(
        records=records, failed_targets=[project_id] if any_failed else []
    )
