"""【上层·审批】审批网关 MCP server。

上层是全平台的统一入口，职责：
1. **统一入口** —— AI 客户端只需连接网关，其余 server 对客户端透明。
2. **审批闸门（HITL）** —— 对标记为「需审批」的写操作，先建立审批单，
   审批通过后才转发到下游 server 执行；未批准则不执行。
3. **路由分发** —— 按「业务系统 + 工具名」把调用路由到对应中层/下层 server。
4. **审计** —— 记录每次工具调用（可扩展为持久化事件溯源日志）。

运行：
- stdio：  gateway-server
- HTTP：   gateway-server --transport http --port 9000

试点默认使用 local 执行器直连 it_ops / common 业务逻辑；生产改用
MCP Streamable HTTP 真实转发（routing.ToolRoute 的 exec_kind="http"）。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_gateway.approvals import gate
from mcp_gateway.routing import Router, ToolRoute, install_local_executors
from mcp_shared.approval import ApprovalAction
from mcp_shared.config import load_platform_env

# 统一敏感配置入口：先把 config/platform.env（不进 git）注入环境，OS 环境变量优先。
# 必须在模块导入期完成——路由注册（_build_router）会读取其中的 URL/TOKEN。
load_platform_env()

logger = logging.getLogger("mcp_gateway")

mcp = MCPServer("enterprise-gateway")


# ---------------------------------------------------------------------------
# 下游注册表（试点：it_ops / common 用本地执行器；生产切换 HTTP 转发见 routing）
# ---------------------------------------------------------------------------
def _build_router() -> Router:
    r = Router()
    # it_ops 写操作（create_change 为高风险，需审批）
    for tool, approval, summary in (
        ("create_incident", False, "创建 IT 运维工单"),
        ("update_incident_status", False, "更新工单状态"),
        ("create_change", True, "创建变更单（高风险，需审批）"),
        ("register_asset", False, "登记 IT 资产"),
    ):
        r.register(ToolRoute(server="it_ops", tool=tool, exec_kind="local", requires_approval=approval, summary=summary))
    # it_ops 只读工具（直接路由）
    for t in ("list_incidents", "list_assets", "get_change"):
        r.register(ToolRoute(server="it_ops", tool=t, exec_kind="local", requires_approval=False, summary=f"it_ops.{t}（只读）"))
    # 下层通用工具（只读，直接放行）
    for t in ("now", "timestamp", "generate_id", "slugify", "echo"):
        r.register(ToolRoute(server="common-tools", tool=t, exec_kind="local", requires_approval=False, summary=f"common-tools.{t}（通用）"))
    # 中层 go_datahub（Go 重活 server，Streamable HTTP 转发，默认 :9300）
    # 跨机部署：MCP_GODATAHUB_URL 指远端地址，MCP_GODATAHUB_TOKEN 配 Bearer 鉴权
    godh = os.environ.get("MCP_GODATAHUB_URL", "http://127.0.0.1:9300/mcp")
    godh_token = os.environ.get("MCP_GODATAHUB_TOKEN")
    godh_headers = {"Authorization": f"Bearer {godh_token}"} if godh_token else None
    for tool, approval, summary in (
        ("submit_collect_job", True, "提交批量采集任务（大规模数据作业，需审批）"),
        ("cancel_job", False, "中止运行中的采集任务"),
    ):
        r.register(ToolRoute(server="go_datahub", tool=tool, exec_kind="http", requires_approval=approval, summary=summary, endpoint=godh, headers=godh_headers))
    for t in ("list_sources", "get_job_status", "list_jobs", "db_ping"):
        r.register(ToolRoute(server="go_datahub", tool=t, exec_kind="http", requires_approval=False, summary=f"go_datahub.{t}（只读）", endpoint=godh, headers=godh_headers))
    return r


router = install_local_executors(_build_router())


# ---------------------------------------------------------------------------
# 向 AI 客户端暴露的工具
# ---------------------------------------------------------------------------
@mcp.tool()
def list_pending_approvals() -> list[dict[str, Any]]:
    """列出所有待审批事项（审批人视角）。"""
    return [r.model_dump(mode="json") for r in gate.list(status="pending")]


@mcp.tool()
def approve_request(request_id: str, comment: str | None = None) -> dict[str, Any]:
    """批准一条待审批事项。批准后网关会把该调用执行到下游 server。"""
    try:
        req = gate.decide(request_id, ApprovalAction.APPROVE, by="approver", comment=comment)
    except (LookupError, ValueError) as e:
        # LookupError=审批单不存在；ValueError=该单已决策过（状态机保护）。
        # 一并转成 ToolError：这是「调用方用错了」而非服务端故障，
        # 应作为可读的协议错误返回，而不是让 SDK 包成 UnexpectedToolError。
        raise ToolError(str(e)) from e
    result = router.execute(req.source_server, req.tool_name, req.tool_args)
    return {"request_id": req.id, "status": req.status.value, "executed": True, "result": result}


@mcp.tool()
def reject_request(request_id: str, comment: str | None = None) -> dict[str, Any]:
    """驳回一条待审批事项，不执行任何下游调用。"""
    try:
        req = gate.decide(request_id, ApprovalAction.REJECT, by="approver", comment=comment)
    except (LookupError, ValueError) as e:
        raise ToolError(str(e)) from e
    return {"request_id": req.id, "status": req.status.value, "executed": False}


@mcp.tool()
def list_routes() -> list[dict[str, Any]]:
    """列出网关可路由的全部下游工具（server / tool / 描述 / 是否需审批）。

    调用 gateway_call 之前先用本工具确认真实存在的工具名与参数说明，
    不要凭猜测直接调用（如创建 IT 工单的真实工具是 it_ops.create_incident）。
    """
    return [
        {"server": r.server, "tool": r.tool, "summary": r.summary, "requires_approval": r.requires_approval}
        for r in router.routes
    ]


@mcp.tool()
def gateway_call(
    server: str,
    tool: str,
    arguments: dict[str, Any],
    requested_by: str = "agent",
) -> dict[str, Any]:
    """统一入口：AI 客户端通过本工具调用任意注册的下游 server 工具。

    - 不确定有哪些工具时，先调用 list_routes 查询（勿猜测工具名）。
    - 只读 / 无需审批的工具：立即路由执行。
    - 需审批的写工具：创建审批单挂起，返回审批单 ID，等待人工审批。
    """
    route = router.resolve(server, tool)
    if route is None:
        same_server = sorted(r.tool for r in router.routes if r.server == server)
        hint = f"该 server 可用工具: {same_server}" if same_server else "请先调用 list_routes 查看可用 server/tool"
        raise ToolError(f"未注册的路由: {server}.{tool}。{hint}")

    if not route.requires_approval:
        result = router.execute(server, tool, arguments)
        return {"approved": True, "executed": True, "result": result}

    req = gate.request(
        source_server=server,
        tool_name=tool,
        tool_args=arguments,
        requested_by=requested_by,
        title=route.summary,
        description=f"工具 {server}.{tool} 需要审批后方可执行",
    )
    return {
        "approved": False,
        "executed": False,
        "request_id": req.id,
        "message": f"已创建审批单 {req.id}，请使用 approve_request / reject_request 处理",
    }


# ---------------------------------------------------------------------------
def main() -> None:
    from mcp_shared import run_server

    logger.info("网关启动，注册路由 %d 条", len(router.routes))
    run_server(mcp, description="【上层·审批】审批网关 MCP server", default_port=9000)


if __name__ == "__main__":
    main()
