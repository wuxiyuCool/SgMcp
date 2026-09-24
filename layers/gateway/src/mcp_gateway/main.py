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
from mcp_gateway.routing import Router, ToolRoute
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
def _coerce_args(arguments: dict[str, Any] | str | None) -> dict[str, Any]:
    """入参宽容化：部分 AI 平台会把 arguments 序列化成 JSON 字符串传输，统一转回 dict。"""
    if arguments is None or arguments == "":
        return {}
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        import json
        try:
            parsed = json.loads(arguments)
        except ValueError as e:
            raise ToolError(f"arguments 不是合法 JSON：{e}；原始值: {arguments!r}") from e
        if not isinstance(parsed, dict):
            raise ToolError(f"arguments 解析后应为对象，实为 {type(parsed).__name__}")
        return parsed
    raise ToolError(f"arguments 类型不支持: {type(arguments).__name__}")


def _norm(s: str) -> str:
    return s.strip().lower().replace("-", "_")


def _resolve_route(server: str, tool: str) -> ToolRoute | None:
    """路由宽容解析：精确匹配优先，其次归一化大小写/连字符、补/剥工具层级前缀。

    AI 平台常见错法（均应被救回而非报错重试）：
    - server 写成 "go-datahub"/"GO_DATAHUB" → 归一化匹配
    - tool 漏层级前缀："create_incident" → itops.create_incident；"list_dsn_refs" → data.list_dsn_refs
    - tool 用下划线别名 "data_list_dsn_refs"（部分平台序列化参数值里的 "." 会损坏）→ 等价命中
    - tool 写成完整 "server.tool" 或带别的 server 前缀 → 按工具名全表定位
    """
    route = router.resolve(server, tool)
    if route is not None:
        return route
    routes = router.routes
    sn, tn = _norm(server), _norm(tool)
    tn_und = tn.replace(".", "_")

    def full_forms(r: ToolRoute) -> set[str]:
        """全名可接受形态：原名、归一化、点号↔下划线互换。"""
        n = _norm(r.tool)
        return {r.tool, n, n.replace(".", "_"), n.replace("_", ".")}

    def tail_forms(r: ToolRoute) -> set[str]:
        """剥层级前缀后的尾段形态（仅剥已知层级前缀 itops_/data_ 或点号前缀，
        防止 generate_id 这类普通名被误剥成 id 造成误匹配）。"""
        out = {r.tool}
        for sep in (".",):
            if sep in r.tool:
                out.add(r.tool.rsplit(sep, 1)[-1])
        for known in ("itops_", "data_"):
            if r.tool.startswith(known):
                out.add(r.tool[len(known):])
        return {_norm(t) for t in out}

    # 1) server 归一化 + tool 全名（含点号/下划线互换）
    for r in routes:
        if _norm(r.server) == sn and (tool in full_forms(r) or tn in full_forms(r) or tn_und in full_forms(r)):
            return r
    # 2) server 归一化 + tool 漏层级前缀（create_incident → itops_create_incident）
    for r in routes:
        if _norm(r.server) == sn and (tn in tail_forms(r) or tn_und in tail_forms(r)):
            return r
    # 3) server 不可靠但 tool 全局唯一：按全名/尾段形态全表找（多命中不同工具则放弃，避免歧义）
    tail_matches = [r for r in routes
                    if tool in full_forms(r) or tn in full_forms(r) or tn_und in full_forms(r)
                    or tn in tail_forms(r) or tn_und in tail_forms(r)]
    uniq = {r.tool for r in tail_matches}
    if len(uniq) == 1:
        return tail_matches[0]
    return None


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

    每条含 tool_alias 字段（点号换成下划线的别名，如 data_list_dsn_refs）——
    若你的平台对参数值中的 "." 序列化有问题，请一律改用 tool_alias。
    """
    return [
        {
            "server": r.server,
            "tool": r.tool,
            "tool_alias": r.tool.replace(".", "_"),
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
    arguments: Any = None,
    requested_by: str = "agent",
) -> dict[str, Any]:
    """统一入口：经网关调用任意已聚合的下游工具。

    参数要求：
    - server: 下游服务名，取值必须是 list_routes 结果中的 server 字段
    - tool:   工具名，必须与 list_routes 结果中的 tool 字段一字不差
      （例：建 IT 工单是 server="it_ops", tool="itops_create_incident"；
        不存在 create_ticket 这种名字，勿凭猜测调用；工具名一律下划线前缀、不含点号）
    - arguments: 目标工具的入参对象，如 {"title": "打印机故障", "priority": "high"}；
      无入参的工具传 {} 或省略。也兼容 JSON 字符串形式（"{...}"），平台传输把对象
      转成字符串也不会失败
    - requested_by: 可选，发起人标识，用于审计

    调用示例——创建高优工单：
      {"server": "it_ops", "tool": "create_incident",
       "arguments": {"title": "打印机故障", "priority": "high"}}

    示例——查当前时间：
      {"server": "common-tools", "tool": "now", "arguments": {"timezone_name": "Asia/Shanghai"}}

    示例——Go 数据源清单（无入参）：
      {"server": "go_datahub", "tool": "data_list_dsn_refs", "arguments": {}}

    返回结构：
    - 免审批:   {"approved": true, "executed": true, "result": <下游返回值>}
    - 需审批:   {"approved": false, "executed": false, "request_id": "apr_xxx",
                "message": ...}  ← 此时目标操作【尚未执行】！
      需请审批人调 list_pending_approvals 查看、approve_request(request_id) 放行后才会真正执行

    推荐流程：不确定时先 list_routes 查工具与入参 schema，再发起本调用。
    tool 也可传 list_routes 返回的 tool_alias（下划线别名，如 data_list_dsn_refs），
    规避部分平台对参数值中 "." 的序列化缺陷。
    若你的平台序列化嵌套 JSON 对象困难，改用 gateway_call_kv（参数扁平字符串，无嵌套）。
    """
    args_obj = _coerce_args(arguments)
    route = _resolve_route(server, tool)
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
    return _dispatch(route, args_obj, requested_by)


def _dispatch(route: ToolRoute, args_obj: dict[str, Any], requested_by: str) -> dict[str, Any]:
    """已解析路由 + 规范化入参 → 立即执行或挂审批。gateway_call / gateway_call_kv 共用。"""
    if not route.requires_approval:
        result = router.execute(route.server, route.tool, args_obj)
        return {"approved": True, "executed": True, "result": result}

    req = gate.request(
        source_server=route.server,   # 存规范名（宽容解析可能救回了脏 server/tool 写法）
        tool_name=route.tool,
        tool_args=args_obj,
        requested_by=requested_by,
        title=route.summary,
        description=f"工具 {route.server}.{route.tool} 需要审批后方可执行",
    )
    return {
        "approved": False,
        "executed": False,
        "request_id": req.id,
        "message": f"已创建审批单 {req.id}，请使用 approve_request / reject_request 处理",
    }


@mcp.tool()
def gateway_call_kv(
    server: str,
    tool: str,
    params: str = "",
    requested_by: str = "agent",
) -> dict[str, Any]:
    """统一入口（扁平参数版）：与 gateway_call 等价，但入参用 "k=v;k2=v2" 字符串，
    顶层不出现嵌套 JSON 对象——供序列化嵌套 JSON 困难的 AI 平台使用，优先推荐本平台调用。

    参数：
    - server / tool: 同 gateway_call（脏名自动归一）
    - params: 分号分隔的 key=value，值自动识别 int/float/bool，其余按字符串。例：
        params="title=打印机故障;priority=high"
        params="dsn_ref=orcl_erp;sql=SELECT 1 FROM dual"
        params="timezone_name=Asia/Shanghai"
      无入参的工具留空 ""。
    - 限制：入参本身是对象/数组的工具（data_submit_collect_job 的 params、
      data_batch_import 的 rows、data_query_dataset 的 filters）请改用 gateway_call。

    返回结构与 gateway_call 相同（需审批时返回 request_id 且操作尚未执行）。
    """
    args_obj = _parse_kv_params(params)
    route = _resolve_route(server, tool)
    if route is None:
        same_server = sorted(r.tool for r in router.routes if r.server == server)
        hint = f"该 server 可用工具: {same_server}" if same_server else "请先调用 list_routes 查询"
        raise ToolError(f"未注册的路由: {server}.{tool}。{hint}")
    return _dispatch(route, args_obj, requested_by)


def _parse_kv_params(params: str | dict | None) -> dict[str, Any]:
    """解析 "k=v;k2=v2"；值类型推断；兼容平台实际发来 JSON 对象/字符串的情况。"""
    if params is None or params == "":
        return {}
    if isinstance(params, dict):
        return params
    if not isinstance(params, str):
        raise ToolError(f"params 类型不支持: {type(params).__name__}")
    if params.lstrip().startswith("{"):
        return _coerce_args(params)
    out: dict[str, Any] = {}
    for pair in params.replace("；", ";").split(";"):
        pair = pair.strip()
        if not pair:
            continue
        k, sep, v = pair.partition("=")
        if not sep:
            raise ToolError(f"params 片段缺少 '='：{pair!r}（格式应为 k=v;k2=v2）")
        v = v.strip()
        if v.lower() in ("true", "false"):
            out[k.strip()] = v.lower() == "true"
        elif v.lower() in ("none", "null"):
            out[k.strip()] = None
        else:
            try:
                out[k.strip()] = int(v)
            except ValueError:
                try:
                    out[k.strip()] = float(v)
                except ValueError:
                    out[k.strip()] = v
    return out


# ---------------------------------------------------------------------------
def main() -> None:
    from mcp_shared import run_server

    report = aggregator.sync()
    logger.info("网关启动，聚合下游 %d 个，路由 %d 条（%s）",
                len(aggregator.specs), len(router.routes), report)
    run_server(mcp, description="【上层·审批】审批网关 MCP server", default_port=9000)


if __name__ == "__main__":
    main()
