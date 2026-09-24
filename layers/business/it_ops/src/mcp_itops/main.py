"""【中层·IT运维】IT 运维系统 MCP server（试点，业务层 Python）。

架构约定（见 docs/architecture.md「三层工具粒度体系」）：
- 第 1 层·领域工具（粗粒度，AI 优先用）：本域常用操作，统一 ``itops.`` 领域前缀
  （制造域将来为 ``mfg.*`` 等），经上层网关聚合，AI 只连网关。
- 第 2 层·领域通用查询（中粒度）：``itops.query_dataset``——dataset_id 用 Literal
  枚举限定**本域数据集**（带中文说明）；领域通用工具查不到的不常用数据集由此查询，
  配套 ``itops.list_data_tables`` 发现数据表。
- 第 3 层·全局数据平台（细粒度，Go MCP）：``data.query_dataset``（全量数据集枚举）、
  ``data.list_datasets``、``data.batch_process`` 批量处理——越域数据引导去第 3 层。
- 重活层 Go server 是**双端点**设计：``/mcp`` 给网关聚合暴露给 AI；
  ``/internal/v1/*``（HTTP + JSON）给业务 Python 服务间内部直调（不经网关）。

运行：
- stdio：  itops-server
- HTTP：   itops-server --transport http --port 9200
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Literal

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_itops.store import store
from mcp_shared import run_server
from mcp_shared.config import get as cfg
from mcp_shared.dsrouting import list_datasets, route_db, route_table, table_of
from mcp_shared.mcp_client import bearer_headers

mcp = MCPServer("it-ops")


# ---------------------------------------------------------------------------
# 内部方法：直调 Go 重活层的 /internal/* REST 端点（不经网关，不暴露给 AI）
# ---------------------------------------------------------------------------
def _heavy_base() -> str:
    """重活层内部 API 基址：从 MCP_GODATAHUB_URL（…/mcp）推导出服务根地址。"""
    url = (cfg("MCP_GODATAHUB_URL", "http://127.0.0.1:9300/mcp") or "").rstrip("/")
    return url[: -len("/mcp")] if url.endswith("/mcp") else url


def _call_heavy_internal(path: str, json_body: dict[str, Any] | None = None,
                         params: dict[str, Any] | None = None) -> dict[str, Any]:
    """业务层 → Go 重活的内部 HTTP 直调（POST/GET + Bearer，统一检查 ok 字段）。"""
    headers = bearer_headers(cfg("MCP_GODATAHUB_TOKEN")) or {}
    url = _heavy_base() + path
    try:
        if json_body is None:
            resp = httpx2.get(url, params=params, headers=headers, timeout=30)
        else:
            # 批量导入上限 5 万行，给足超时
            resp = httpx2.post(url, json=json_body, headers=headers, timeout=120)
    except httpx2.HTTPError as e:
        raise ToolError(
            f"内部调用重活层失败（{url}）：{type(e).__name__}: {e}。"
            "请确认 go_datahub 已部署，或稍后重试。"
        ) from e
    if resp.status_code >= 400:
        raise ToolError(f"内部调用重活层 {path} 失败 [{resp.status_code}]: {resp.text}")
    data = resp.json()
    if not data.get("ok", False) and "ok" in data:
        # 库侧执行失败：重活层返回 200 + ok=false，转可读工具错误
        raise ToolError(f"重活层 {path} 执行失败: {data.get('error', '未知错误')}")
    return data


# ---------------------------------------------------------------------------
# IT 运维业务工具（对 AI 暴露，itops.* 前缀）
# 事件单支持三种落地通道：local=本地 SQLite 持久化（零外部依赖）；sql=经业务名路由写入
# 数据库（内部直调重活层 batch/import|query）；api=调外部 ITSM 系统 REST API。
# ---------------------------------------------------------------------------
@mcp.tool(name="itops.create_incident")
def create_incident(title: str, priority: str = "medium", reporter: str = "agent",
                    channel: str = "local", tenant_id: str = "default") -> dict[str, Any]:
    """创建一条 IT 运维工单，返回含 id 的工单对象。

    参数：
    - title: 工单标题（必填），如 "3楼打印机卡纸"
    - priority: 枚举 low/medium/high/critical，默认 medium（其他值报错）
    - reporter: 报障人标识，默认 agent
    - channel: 落地通道枚举 local/sql/api，默认 local
      local=本地 SQLite 持久化（默认，零外部依赖，数据跨服务重启保留，ID 可长期引用；库路径可用 MCP_ITOPS_DB 覆盖）；
      sql=按数据集路由写库（需 DATASET_INCIDENTS_* 配置，工单落 incidents 表，持久化）；
      api=调外部 ITSM 系统 REST（需 MCP_ITSM_API_URL 配置）
    - tenant_id: 租户标识，默认 default（仅 sql 通道参与路由）

    返回示例：{"id": "INC-0205B08E", "title": ..., "status": "new", "priority": ...}
    后续用返回的 id 调 itops.update_incident_status 流转状态。
    """
    if priority not in {"low", "medium", "high", "critical"}:
        raise ValueError(f"非法优先级: {priority}")
    if channel == "local":
        return store.create_incident(title=title, priority=priority, reporter=reporter)
    rec = store.create_incident(title=title, priority=priority, reporter=reporter)
    if channel == "sql":
        db_type, dsn_ref = route_db("incidents", tenant_id)
        result = _call_heavy_internal("/internal/v1/batch/import", json_body={
            "db_type": db_type, "dsn_ref": dsn_ref, "table": table_of("incidents"),
            "rows": [rec],
        })
        return {**rec, "channel": "sql", "imported": result.get("imported", 1)}
    if channel == "api":
        return {**_call_external_itsm("POST", "/incidents", json_body=rec), "channel": "api"}
    raise ValueError(f"channel 只支持 local/sql/api: {channel!r}")


@mcp.tool(name="itops.list_incidents")
def list_incidents(status: str | None = None, channel: str = "local",
                   tenant_id: str = "default", limit: int = 100) -> list[dict[str, Any]]:
    """查询工单列表，可按状态过滤（new/open/in_progress/resolved/closed）。

    channel: local=本地 SQLite（默认，持久化）；sql=按数据集路由查库（incidents 表，
    等值过滤 + 行数上限）；api=调外部 ITSM 系统 REST API。
    注意：返回注解必须是精确的 list[dict]——Any 注解会让 SDK 把单元素列表
    塌缩成单个对象，破坏调用方的解包约定。
    """
    if channel == "local":
        return store.list_incidents(status=status)
    if channel == "sql":
        db_type, dsn_ref = route_db("incidents", tenant_id)
        filters = {"status": status} if status else {}
        result = _call_heavy_internal("/internal/v1/batch/query", json_body={
            "db_type": db_type, "dsn_ref": dsn_ref, "table": table_of("incidents"),
            "filters": filters, "limit": limit,
        })
        rows = result.get("rows", [])
        return rows if isinstance(rows, list) else [rows]
    if channel == "api":
        params = {"status": status} if status else {"limit": limit}
        data = _call_external_itsm("GET", "/incidents", params=params)
        # 外部 API 返回形态不可控，归一化成列表
        if isinstance(data, dict):
            for key in ("incidents", "data", "items", "result"):
                if isinstance(data.get(key), list):
                    return data[key]
            return [data]
        return data if isinstance(data, list) else [data]
    raise ValueError(f"channel 只支持 local/sql/api: {channel!r}")


def _call_external_itsm(method: str, path: str, json_body: dict[str, Any] | None = None,
                        params: dict[str, Any] | None = None) -> dict[str, Any]:
    """调外部 ITSM 业务系统的 REST API（api 通道）。

    外部系统地址/令牌：MCP_ITSM_API_URL（如 http://itsm.internal:8080）+
    MCP_ITSM_API_TOKEN。未配置 → 可读错误引导配置。
    """
    base = (cfg("MCP_ITSM_API_URL") or "").rstrip("/")
    if not base:
        raise ToolError(
            "api 通道未配置外部 ITSM 系统：请设置 MCP_ITSM_API_URL"
            "（与 MCP_ITSM_API_TOKEN，可选）后重试；当前可改用 channel=local/sql。"
        )
    headers: dict[str, str] = {}
    token = cfg("MCP_ITSM_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = base + path
    try:
        if method == "GET":
            resp = httpx2.get(url, params=params, headers=headers, timeout=30)
        else:
            resp = httpx2.post(url, json=json_body, headers=headers, timeout=30)
    except httpx2.HTTPError as e:
        raise ToolError(f"调用外部 ITSM API 失败（{url}）：{type(e).__name__}: {e}") from e
    if resp.status_code >= 400:
        raise ToolError(f"外部 ITSM API {path} 返回 [{resp.status_code}]: {resp.text}")
    try:
        return resp.json()
    except ValueError as e:
        raise ToolError(f"外部 ITSM API 返回非 JSON 响应: {resp.text[:200]}") from e


@mcp.tool(name="itops.update_incident_status")
def update_incident_status(incident_id: str, status: str) -> dict[str, Any]:
    """流转工单状态，返回更新后的完整工单对象。

    参数：
    - incident_id: 必须是 create_incident 返回的真实 id（形如 "INC-0205B08E"）。
      勿凭空编造；不确定时先用 itops.list_incidents 查现有工单（数据持久化，历史 ID 依然有效）
    - status: 枚举 new/open/in_progress/resolved/closed

    工单不存在时返回可读错误"工单不存在: xxx"。
    """
    rec = store.update_incident_status(incident_id, status)
    if rec is None:
        raise LookupError(f"工单不存在: {incident_id}")
    return rec


@mcp.tool(name="itops.create_change")
def create_change(title: str, change_type: str = "standard", implementer: str = "ops", risk: str = "low") -> dict[str, Any]:
    """创建变更单，返回含 id（形如 "CHG-4B36C912"）的对象。

    参数：title=变更描述；change_type=standard/emergency（标准/紧急）；
    implementer=实施人；risk=low/medium/high。

    ⚠️ 高风险写操作：经网关 gateway_call 调用时会被挂起为审批单（返回
    request_id="apr_xxx" 且尚未执行），需审批人 approve_request 后才真正创建。
    """
    rec = store.create_change(title=title, change_type=change_type, implementer=implementer, risk=risk)
    rec["approval_required"] = True  # 交由上层审批的标记
    return rec


@mcp.tool(name="itops.get_change")
def get_change(change_id: str) -> dict[str, Any]:
    """查询变更单详情。"""
    rec = store.get_change(change_id)
    if rec is None:
        raise LookupError(f"变更单不存在: {change_id}")
    return rec


@mcp.tool(name="itops.register_asset")
def register_asset(name: str, asset_type: str, owner: str) -> dict[str, Any]:
    """登记一台 IT 资产（录入 CMDB），返回含 id（形如 "AST-5C332490"）的对象。

    参数：name=资产名（如 "srv-ops-01"）；
    asset_type=枚举 laptop/server/network/software；owner=负责人。
    """
    return store.create_asset(name=name, asset_type=asset_type, owner=owner)


@mcp.tool(name="itops.list_assets")
def list_assets(asset_type: str | None = None) -> list[dict[str, Any]]:
    """查询资产清单（对象数组）。asset_type 可选过滤：laptop/server/network/software，
    留空或传 null 返回全部。"""
    return store.list_assets(asset_type=asset_type)


# ---------------------------------------------------------------------------
# 内部调用示例：业务层经内部 REST 直调 Go 重活的库表能力
# ---------------------------------------------------------------------------
@mcp.tool(name="itops.export_assets_to_warehouse")
def export_assets_to_warehouse(dsn_ref: str, table: str = "cmdb_assets", asset_type: str | None = None) -> dict[str, Any]:
    """把 CMDB 资产批量归档到数据仓库（大批量写库属重活，内部直调 go_datahub
    的 /internal/v1/batch/import，不经网关、不暴露给 AI）。

    dsn_ref 为重活层 config/datahub.env 里登记的 DSN 引用名（如 warehouse_pg），
    可用 heavy.list_dsn_refs 查看；table 需已存在（可用 heavy.list_tables 确认）。
    """
    rows = store.list_assets(asset_type=asset_type)
    if not rows:
        return {"ok": True, "exported": 0, "message": "无资产可归档"}
    result = _call_heavy_internal("/internal/v1/batch/import", json_body={
        "db_type": os.environ.get("MCP_WAREHOUSE_DB_TYPE", "pg"),
        "dsn_ref": dsn_ref,
        "table": table,
        "rows": rows,
    })
    return {"ok": True, "exported": result.get("imported", len(rows))}


@mcp.tool(name="itops.query_warehouse_assets")
def query_warehouse_assets(dsn_ref: str, table: str = "cmdb_assets", asset_type: str | None = None, limit: int = 100) -> dict[str, Any]:
    """查询已归档到数据仓库的资产（内部直调 go_datahub 的
    /internal/v1/batch/query，等值过滤 + 行数上限，防全表拖库）。
    """
    filters: dict[str, Any] = {}
    if asset_type:
        filters["asset_type"] = asset_type
    result = _call_heavy_internal("/internal/v1/batch/query", json_body={
        "db_type": os.environ.get("MCP_WAREHOUSE_DB_TYPE", "pg"),
        "dsn_ref": dsn_ref,
        "table": table,
        "filters": filters,
        "limit": limit,
    })
    return {"ok": True, "count": result.get("count", 0), "rows": result.get("rows", [])}


# ---------------------------------------------------------------------------
# 批处理示例（Python 侧实现，与 Go 侧 heavy.batch_process 同一套业务名路由约定）：
# 业务 Python 负责轻编排（路由解析 + 任务登记），重活下放重活层内部 REST 执行
# ---------------------------------------------------------------------------
@mcp.tool(name="itops.batch_process")
def batch_process(dataset_id: str, period: str, tenant_id: str, mode: str = "simulate") -> dict[str, Any]:
    """按业务数据集名批量处理数据（Python 侧路由实现）。

    与 heavy.batch_process 共用同一套 env 路由约定（DATASET_*）：本工具在业务层
    完成业务名 → 租户数据源 / 按月分表的路由解析；mode=simulate 本地演练完成，
    mode=real 经内部 REST 直调重活层 /internal/v1/batch/process 真实执行
    （重活下放，不经网关、不暴露给 AI）。
    """
    db_type, dsn_ref = route_db(dataset_id, tenant_id)   # 按租户路由
    table = route_table(dataset_id, period)              # 按月分表
    task_id = f"task-{uuid.uuid4().hex[:12]}"
    routed = {"db": dsn_ref, "db_type": db_type, "table": table, "mode": mode}

    if mode == "simulate":
        return {
            "task_id": task_id, "status": "completed", "dataset_id": dataset_id,
            "period": period, "tenant_id": tenant_id, "routed": routed,
            "processed": 1000, "detail": "simulate 模式：模拟处理 1000 行（不触库）",
        }
    if mode == "real":
        result = _call_heavy_internal("/internal/v1/batch/process", json_body={
            "db_type": db_type, "dsn_ref": dsn_ref, "table": table, "mode": "real",
        })
        return {
            "task_id": task_id, "status": "completed", "dataset_id": dataset_id,
            "period": period, "tenant_id": tenant_id, "routed": routed,
            "processed": result.get("processed", 0), "detail": result.get("detail", ""),
        }
    raise ValueError(f"mode 只支持 simulate/real: {mode!r}")


@mcp.tool(name="itops.list_datasets")
def business_list_datasets() -> dict[str, Any]:
    """列出已配置的业务数据集及其路由规则（租户 → dsn_ref、按月分表）。

    调 batch_process 前先用本工具确认 dataset_id / tenant_id 的合法取值。只读。
    """
    return {"ok": True, "datasets": list_datasets()}


# ---------------------------------------------------------------------------
# 第 2 层·领域通用查询（中粒度）：本域数据集枚举限定 + 配套表发现
# ---------------------------------------------------------------------------
# it_ops 域的数据集枚举（新增本域数据集时在此登记 + 配 DATASET_<NAME>_* 路由）。
# 中文说明写在 Literal 描述里；更细的发现用 itops.list_data_tables。
ITOPS_DOMAIN = "itops"


@mcp.tool(name="itops.query_dataset")
def query_dataset(
    dataset_id: Literal["incidents", "assets"],
    tenant_id: str = "default",
    period: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """按本域（IT 运维）数据集做通用查询（第 2 层·领域通用查询）。

    dataset_id 枚举（中文说明）：incidents=IT 运维事件单（工单流水，固定表）；
    assets=CMDB 资产台账（固定表）。本枚举之外的领域数据集（如订单）不在本工具
    范围——请改用第 3 层全局数据平台 data.query_dataset（全量数据集枚举）。
    period 仅周期数据集需要（按月分表 YYYY-MM）；本域数据集均为固定表，留空即可。
    """
    _guard_domain(dataset_id)
    db_type, dsn_ref = route_db(dataset_id, tenant_id)
    table = route_table(dataset_id, period) if period else table_of(dataset_id)
    result = _call_heavy_internal("/internal/v1/batch/query", json_body={
        "db_type": db_type, "dsn_ref": dsn_ref, "table": table,
        "filters": filters or {}, "limit": limit,
    })
    return {
        "ok": True, "dataset_id": dataset_id, "routed": {"db": dsn_ref, "table": table},
        "count": result.get("count", 0), "rows": result.get("rows", []),
    }


@mcp.tool(name="itops.list_data_tables")
def list_data_tables(dataset_id: Literal["incidents", "assets"], tenant_id: str = "default") -> dict[str, Any]:
    """列出一个本域数据集路由库中的数据表（查询前的发现入口：先看表存在、再用
    itops.query_dataset 查询；越域数据集用 data.list_datasets / data.query_dataset）。
    """
    _guard_domain(dataset_id)
    db_type, dsn_ref = route_db(dataset_id, tenant_id)
    result = _call_heavy_internal("/internal/v1/tables", params={
        "db_type": db_type, "dsn_ref": dsn_ref,
    })
    return {"ok": True, "dataset_id": dataset_id, "db": dsn_ref, "tables": result.get("tables", [])}


def _guard_domain(dataset_id: str) -> None:
    """域校验：数据集不属于本域时给可读引导（去第 3 层全局数据平台查）。"""
    domain = os.environ.get(f"DATASET_{dataset_id.upper()}_DOMAIN", "")
    if domain and domain.strip().lower() != ITOPS_DOMAIN:
        raise ToolError(
            f"数据集 {dataset_id!r} 属于 {domain} 域，不在 it_ops 领域枚举内；"
            "请改用第 3 层全局数据平台 data.query_dataset（全量数据集枚举）。"
        )


def main() -> None:
    run_server(mcp, description="【中层·IT运维】IT 运维 MCP server", default_port=9200)


if __name__ == "__main__":
    main()
