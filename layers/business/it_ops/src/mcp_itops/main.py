"""【中层·IT运维】IT 运维系统 MCP server（试点）。

中层负责"执行"：接收上层网关转发的工具调用，调用真实业务逻辑。
包含三类 IT 运维工具：工单（创建/查询/流转）、变更（高风险写，示例中标注需审批）、
资产（CMDB 登记/查询）。
运行：
- stdio：  itops-server
- HTTP：   itops-server --transport http --port 9200
"""

from __future__ import annotations

from typing import Any

from mcp.server import MCPServer

from mcp_itops.store import store
from mcp_shared import run_server

mcp = MCPServer("it-ops")


@mcp.tool()
def create_incident(title: str, priority: str = "medium", reporter: str = "agent") -> dict[str, Any]:
    """创建一条 IT 运维工单。priority: low/medium/high/critical。"""
    if priority not in {"low", "medium", "high", "critical"}:
        raise ValueError(f"非法优先级: {priority}")
    return store.create_incident(title=title, priority=priority, reporter=reporter)


@mcp.tool()
def list_incidents(status: str | None = None) -> list[dict[str, Any]]:
    """查询工单列表，可按状态过滤（new/open/in_progress/resolved/closed）。"""
    return store.list_incidents(status=status)


@mcp.tool()
def update_incident_status(incident_id: str, status: str) -> dict[str, Any]:
    """更新工单状态。示例写操作。"""
    rec = store.update_incident_status(incident_id, status)
    if rec is None:
        raise LookupError(f"工单不存在: {incident_id}")
    return rec


@mcp.tool()
def create_change(title: str, change_type: str = "standard", implementer: str = "ops", risk: str = "low") -> dict[str, Any]:
    """创建变更单。**注意**：本工具属于高风险写操作，生产环境中应由上层网关审批后放行。"""
    rec = store.create_change(title=title, change_type=change_type, implementer=implementer, risk=risk)
    rec["approval_required"] = True  # 交由上层审批的标记
    return rec


@mcp.tool()
def get_change(change_id: str) -> dict[str, Any]:
    """查询变更单详情。"""
    rec = store.get_change(change_id)
    if rec is None:
        raise LookupError(f"变更单不存在: {change_id}")
    return rec


@mcp.tool()
def register_asset(name: str, asset_type: str, owner: str) -> dict[str, Any]:
    """登记一台 IT 资产（录入 CMDB）。asset_type: laptop/server/network/software。"""
    return store.create_asset(name=name, asset_type=asset_type, owner=owner)


@mcp.tool()
def list_assets(asset_type: str | None = None) -> list[dict[str, Any]]:
    """查询资产清单，可按类型过滤。"""
    return store.list_assets(asset_type=asset_type)


def main() -> None:
    run_server(mcp, description="【中层·IT运维】IT 运维 MCP server", default_port=9200)


if __name__ == "__main__":
    main()
