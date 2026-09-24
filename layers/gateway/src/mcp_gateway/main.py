"""【上层·审批】审批网关 MCP server（对外唯一 MCP 入口，内建聚合器）。

上层是全平台的统一入口，职责：
1. **统一入口** —— AI 客户端只需连接网关，其余 server 对客户端完全透明。
2. **聚合器** —— 启动时作为 MCP Client 连接各下游 server（中层 it_ops / go_datahub、
   下层 common 等，见 aggregator.py），拉取 tools/list 合并成路由表；
   对 AI 不逐个展示下游工具，而是每个 server 收敛为一个 route_* 聚合路由工具
   （method 枚举列出全部可调用方法）。下游清单由 MCP_DOWNSTREAMS 等环境变量
   配置，网关不硬编码任何业务工具。
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
from typing import Any, Literal

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
    """入参宽容化：统一把各种平台传输形态转成 dict。

    接受：dict / None / JSON 字符串（"{...}"）/ 扁平 kv 字符串（"k=v;k2=v2"，
    供序列化嵌套 JSON 困难的平台直接使用——原 gateway_call_kv 的能力已并入）。
    """
    if arguments is None or arguments == "":
        return {}
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        import json
        if arguments.lstrip().startswith("{"):
            try:
                parsed = json.loads(arguments)
            except ValueError as e:
                raise ToolError(f"arguments 不是合法 JSON：{e}；原始值: {arguments!r}") from e
            if not isinstance(parsed, dict):
                raise ToolError(f"arguments 解析后应为对象，实为 {type(parsed).__name__}")
            return parsed
        if "=" in arguments:
            return _parse_kv_params(arguments)
        raise ToolError(
            f"arguments 是无法理解的字符串: {arguments!r}"
            "（对象请传 JSON 字符串 {{\"k\": ...}} 或扁平串 k=v;k2=v2）"
        )
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
    """列出网关聚合到的全部下游工具明细（server / 工具名 / 描述 / 入参 schema / 是否需审批）。

    数据来自启动时（及最近一次 refresh_routes）从各下游 server 拉取的 tools/list。
    日常调用请优先用各域的 route_* 聚合路由工具（method 枚举即本清单的工具名）；
    本工具用于查看入参 schema 细节与排查 gateway_call 的未注册路由报错，
    不要凭猜测直接调用（如创建 IT 工单的真实工具是 itops_create_incident）。

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
    """重新连接各下游 server 拉取 tools/list，刷新网关路由表并重建 route_* 聚合路由工具。

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
    """统一入口（跨域通用）：经网关调用任意已聚合的下游工具。

    同一 server 下有多个方法要调用时，**优先用该域的 route_* 路由工具**
    （method 枚举直接列出全部方法，无需猜 server/tool 字符串），本工具作为
    跨域或动态场景的通用入口保留。

    参数要求：
    - server: 下游服务名，取值必须是 list_routes 结果中的 server 字段
    - tool:   工具名，必须与 list_routes 结果中的 tool 字段一字不差
      （例："itops_create_incident"；不存在 create_ticket 这种名字，勿凭猜测调用；
        工具名一律下划线前缀、不含点号）
    - arguments: 目标工具的入参，三种形态均可（自动识别）：
        ① 对象 {"title": "打印机故障", "priority": "high"}
        ② JSON 字符串 "{\"title\": ...}"（部分平台会把对象转成字符串，同样可用）
        ③ 扁平串 "title=打印机故障;priority=high"（序列化嵌套 JSON 困难的平台直接用；
           值自动识别 int/float/bool；入参本身是对象/数组的字段——如
           data_submit_collect_job 的 params、data_batch_import 的 rows——请改用形态①）
      无入参的工具传 {} 或省略
    - requested_by: 可选，发起人标识，用于审计

    调用示例——创建高优工单：
      {"server": "it_ops", "tool": "create_incident",
       "arguments": {"title": "打印机故障", "priority": "high"}}

    示例——查当前时间（扁平串形态）：
      {"server": "common-tools", "tool": "now", "arguments": "timezone_name=Asia/Shanghai"}

    示例——Go 数据源清单（无入参）：
      {"server": "go_datahub", "tool": "data_list_dsn_refs", "arguments": {}}

    返回结构：
    - 免审批:   {"approved": true, "executed": true, "result": <下游返回值>}
    - 需审批:   {"approved": false, "executed": false, "request_id": "apr_xxx",
                "message": ...}  ← 此时目标操作【尚未执行】！
      需请审批人调 list_pending_approvals 查看、approve_request(request_id) 放行后才会真正执行

    推荐流程：不确定时先 list_routes 查工具与入参 schema，再发起本调用。
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
    """已解析路由 + 规范化入参 → 立即执行或挂审批。gateway_call / route_* 工具共用。"""
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


# （原 gateway_call_kv 独立工具已并入 gateway_call 的 arguments 扁平串形态，
#  避免同一能力出现两个重复入口）


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
# 聚合路由工具（route_*）：每个下游 server 收敛为一个入口，
# method 参数用枚举列出该域全部可调用方法，描述中标注各方法入参与审批要求。
# 每轮 aggregator.sync()（启动 / refresh_routes）后整体重建，枚举跟随最新路由表。
# ---------------------------------------------------------------------------

_ROUTE_TOOLS: dict[str, str] = {}  # 已注册的 route 工具名 -> server 名


def _route_tool_name(server: str) -> str:
    return "route_" + _norm(server)


def _method_signature(r: ToolRoute) -> str:
    """由入参 schema 生成方法签名摘要，如 itops_create_incident(title*, priority, reporter)。"""
    schema = r.input_schema or {}
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    args = ", ".join(f"{k}*" if k in required else k for k in props)
    return f"{r.tool}({args})" if args else f"{r.tool}()"


def _make_route_fn(server: str, methods: list[str]):
    literal = Literal[tuple(methods)]  # type: ignore[valid-type]

    def route_fn(method, params: Any = None, requested_by: str = "agent") -> dict[str, Any]:
        route = router.resolve(server, method) or _resolve_route(server, method)
        if route is None:
            raise ToolError(
                f"{server} 域没有方法 {method!r}；当前可调用: {methods}。"
                "若下游刚更新，请先调 refresh_routes 重建本路由工具"
            )
        return _dispatch(route, _coerce_args(params), requested_by)

    route_fn.__name__ = _route_tool_name(server)
    route_fn.__annotations__ = {
        "method": literal,
        "params": Any,
        "requested_by": str,
        "return": dict[str, Any],
    }
    return route_fn


def rebuild_route_tools() -> list[str]:
    """按当前路由表重建全部 route_* 工具（先摘旧的再注册新的，幂等）。"""
    for name in list(_ROUTE_TOOLS):
        try:
            mcp.remove_tool(name)
        except Exception:  # noqa: BLE001 — 名字已被外部摘除时容忍，保持映射自洽
            pass
        _ROUTE_TOOLS.pop(name, None)

    by_server: dict[str, list[ToolRoute]] = {}
    for r in router.routes:
        by_server.setdefault(r.server, []).append(r)

    for server, routes in sorted(by_server.items()):
        routes.sort(key=lambda r: r.tool)
        methods = [r.tool for r in routes]
        lines = [
            f"- {_method_signature(r)}{'【需审批】' if r.requires_approval else ''}：{r.summary}"
            for r in routes
        ]
        description = (
            f"调用下游服务 {server} 的聚合路由入口：method 从枚举中选目标方法，"
            "params 传该方法入参——对象 {\"k\": v}、JSON 字符串、"
            "或扁平串 \"k=v;k2=v2\"（值自动识别 int/bool）均可；无参方法留空。\n"
            f"可调用方法（* 为必填参数，【需审批】的方法调用后只产生审批单、须 approve_request 才执行）：\n"
            + "\n".join(lines)
        )
        name = _route_tool_name(server)
        mcp.add_tool(_make_route_fn(server, methods), name=name, description=description)
        _ROUTE_TOOLS[name] = server
    logger.info("重建聚合路由工具: %s", sorted(_ROUTE_TOOLS))
    return sorted(_ROUTE_TOOLS)


aggregator.on_sync = rebuild_route_tools


# ---------------------------------------------------------------------------
def main() -> None:
    from mcp_shared import run_server

    report = aggregator.sync()
    logger.info("网关启动，聚合下游 %d 个，路由 %d 条（%s）",
                len(aggregator.specs), len(router.routes), report)
    run_server(mcp, description="【上层·审批】审批网关 MCP server", default_port=9000)


if __name__ == "__main__":
    main()
