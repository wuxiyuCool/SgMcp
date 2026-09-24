"""【上层·审批】审批网关 MCP server（对外唯一 MCP 入口，内建聚合器）。

上层是全平台的统一入口，职责：
1. **统一入口** —— AI 客户端只需连接网关，其余 server 对客户端完全透明。
2. **聚合器** —— 启动时作为 MCP Client 连接各下游 server（中层 it_ops / go_datahub、
   下层 common 等，见 aggregator.py），拉取 tools/list 合并成本地工具表；
   下游清单由 MCP_DOWNSTREAMS 等环境变量配置，网关不硬编码任何业务工具。
3. **审批闸门（HITL）** —— 对策略标记「需审批」的写操作，先建立审批单，
   审批通过后才转发下游执行；未批准则不执行。
4. **路由转发** —— AI 调工具时按「server + 工具名」把调用经 Streamable HTTP
   转发到对应下游 server。
5. **审计** —— 记录每次工具调用（可扩展为持久化事件溯源日志）。

运行：
- stdio：  gateway-server
- HTTP：   gateway-server --transport http --port 9000

启动顺序：先起各下游 server，再起网关（聚合器带重试窗口，容忍秒级启动竞态；
下游彻底缺席时网关照常启动，可调用 refresh_routes 运行时补拉工具表）。
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_gateway.aggregator import Aggregator
from mcp_gateway.approvals import gate
from mcp_gateway.routing import Router
from mcp_shared.approval import ApprovalAction
from mcp_shared.config import load_platform_env

# 统一敏感配置入口：先把 config/platform.env（不进 git）注入环境，OS 环境变量优先。
# 必须在模块导入期完成——下游注册表（MCP_DOWNSTREAMS 等）在导入期读取。
load_platform_env()

logger = logging.getLogger("mcp_gateway")

mcp = MCPServer("enterprise-gateway")

# 路由表初始为空，由聚合器在启动时（main / 手动 sync）填充
router = Router()
aggregator = Aggregator(router)


# ---------------------------------------------------------------------------
# 向 AI 客户端暴露的工具
# ---------------------------------------------------------------------------
@mcp.tool()
def list_pending_approvals() -> list[dict[str, Any]]:
    """列出所有待审批事项（审批人视角）。

    返回每条审批单：id（"apr_" 前缀，传给 approve_request/reject_request）、
    title、source_server、tool_name、tool_args（即将执行的原始入参）、requested_by。
    """
    return [r.model_dump(mode="json") for r in gate.list(status="pending")]


@mcp.tool()
def approve_request(request_id: str, comment: str | None = None) -> dict[str, Any]:
    """批准一条待审批事项并立即执行到下游。

    参数：
    - request_id: 来自 gateway_call 返回值或 list_pending_approvals，形如 "apr_6a19736d88d2"
    - comment: 可选审批意见

    返回：{"request_id", "status": "approved", "executed": true, "result": <下游执行结果>}
    注意：每个 request_id 只能决策一次，重复决策会报错。
    """
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
    """列出网关聚合到的全部下游工具（server / 工具名 / 描述 / 入参 schema / 是否需审批）。

    数据来自启动时（及最近一次 refresh_routes）从各下游 server 拉取的 tools/list。
    调用 gateway_call 之前先用本工具确认真实存在的工具名与参数说明，
    不要凭猜测直接调用（如创建 IT 工单的真实工具是 it_ops.create_incident）。
    """
    return [
        {
            "server": r.server,
            "tool": r.tool,
            "summary": r.summary,
            "input_schema": r.input_schema,
            "requires_approval": r.requires_approval,
        }
        for r in router.routes
    ]


@mcp.tool()
def list_downstreams() -> list[dict[str, Any]]:
    """列出网关配置的下游 server 清单（聚合目标）。诊断路由缺失时先用本工具。"""
    return aggregator.downstream_status()


@mcp.tool()
def refresh_routes() -> dict[str, Any]:
    """重新连接各下游 server 拉取 tools/list，刷新网关本地工具表。

    适用场景：某个下游在网关启动后才上线、下游新增/删除了工具。
    返回本轮聚合结果（各 server 成功拉到的工具数 / 失败原因）。
    """
    report = aggregator.sync()
    return {"ok": report.ok, "failed": report.failed}


@mcp.tool()
def gateway_call(
    server: str,
    tool: str,
    arguments: dict[str, Any],
    requested_by: str = "agent",
) -> dict[str, Any]:
    """统一入口：经网关调用任意已聚合的下游工具。

    参数要求（三个必填项缺一不可，格式错误会被直接拒绝）：
    - server: 下游服务名，取值必须是 list_routes 结果中的 server 字段
    - tool:   工具名，必须与 list_routes 结果中的 tool 字段一字不差
      （例：建 IT 工单是 server="it_ops", tool="create_incident"；
        不存在 create_ticket 这种名字，勿凭猜测调用）
    - arguments: JSON 对象（不是字符串！）。目标工具无需入参时必须传 {}
    - requested_by: 可选，发起人标识，用于审计

    调用示例——创建高优工单：
      {"server": "it_ops", "tool": "create_incident",
       "arguments": {"title": "打印机故障", "priority": "high"}}

    调用示例——Go 数据源清单（无入参）：
      {"server": "go_datahub", "tool": "data.list_dsn_refs", "arguments": {}}

    返回结构：
    - 免审批:   {"approved": true, "executed": true, "result": <下游返回值>}
    - 需审批:   {"approved": false, "executed": false, "request_id": "apr_xxx",
                "message": ...}  ← 此时目标操作【尚未执行】！
      需请审批人调 list_pending_approvals 查看、approve_request(request_id) 放行后才会真正执行

    推荐流程：不确定时先 list_routes 查工具与入参 schema，再发起本调用。
    """
    route = router.resolve(server, tool)
    if route is None:
        same_server = sorted(r.tool for r in router.routes if r.server == server)
        if same_server:
            hint = f"该 server 可用工具: {same_server}"
        else:
            hint = (
                "请先调用 list_routes 查看可用 server/tool；"
                "若下游刚上线，先调用 refresh_routes 重新拉取工具表"
            )
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

    report = aggregator.sync()
    logger.info("网关启动，聚合下游 %d 个，路由 %d 条（%s）",
                len(aggregator.specs), len(router.routes), report)
    run_server(mcp, description="【上层·审批】审批网关 MCP server", default_port=9000)


if __name__ == "__main__":
    main()
