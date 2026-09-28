"""【上层·审批】审批网关 MCP server（对外唯一 MCP 入口，内建聚合器）。

上层是全平台的统一入口，职责：
1. **统一入口** —— AI 客户端只需连接网关，其余 server 对客户端完全透明。
2. **聚合器** —— 启动时作为 MCP Client 连接各下游 server（中层 it_ops / go_datahub、
   下层 common 等，见 aggregator.py），拉取 tools/list 合并成路由表；
   对 AI 不逐个展示下游工具，而是每个 server 收敛为一个 route_* 聚合路由工具
   （method 枚举列出全部可调用方法）。下游清单由 MCP_DOWNSTREAMS 等环境变量
   配置，网关不硬编码任何业务工具。
3. **审批闸门（HITL）** —— 对策略标记「需审批」的写操作，先建立审批单，
   审批通过后才转发下游执行；未批准则不执行。审批决策受 `MCP_APPROVAL_TOKEN`
   保护（见 approvals.py 与 docs/architecture.md §3.1），否则「AI 自己批自己」。
4. **路由转发** —— AI 调工具时按「server + 工具名」把调用经 Streamable HTTP
   转发到对应下游 server，带超时、只读幂等重试与调用追踪号。
5. **审计** —— 每次工具调用与审批决策结构化落盘（data/audit/gateway.jsonl），
   入参脱敏、结果截断，可经 query_audit_log 回查。

运行：
- stdio：  gateway-server
- HTTP：   gateway-server --transport http --port 9000

启动顺序：先起各下游 server，再起网关（聚合器带重试窗口，容忍秒级启动竞态；
下游彻底缺席时网关照常启动，可调用 refresh_routes 运行时补拉工具表）。
"""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_gateway.aggregator import Aggregator
from mcp_gateway.approvals import gate
from mcp_gateway.routing import Router, ToolRoute
from mcp_shared.approval import ApprovalAction, ApprovalRequest
from mcp_shared.audit import audit_log, digest
from mcp_shared.config import load_platform_env
from mcp_shared.http_kit import actor_hint, approval_hint, session_hint
from mcp_shared.mcp_client import DownstreamError, timeout_for

# 统一敏感配置入口：先把 config/platform.env（不进 git）注入环境，OS 环境变量优先。
# 必须在模块导入期完成——下游注册表（MCP_DOWNSTREAMS 等）在导入期读取。
load_platform_env()

logger = logging.getLogger("mcp_gateway")

_INSTRUCTIONS = """本网关是企业三层 MCP 平台的唯一入口。使用顺序：
1) 想知道有什么能力 → list_tool_catalog（按下游分组的说明书，含参数与示例）；
2) 同一域内调用 → 该域的 route_* 工具（method=工具名，params=入参，支持对象/JSON/扁平串）；
   跨域或工具名不确定 → gateway_call(server, tool, arguments)；
3) 返回 approved=false 表示该操作需要人工审批、尚未执行，把 request_id 交给审批人，
   审批人用 list_pending_approvals + approve_request（需审批令牌）放行；不要自行批准；
4) 长耗时批量操作走异步：data_submit_collect_job → data_get_job_status（勿同步等待）；
5) 出错时读错误里的 kind/建议，必要时 refresh_routes 重新聚合，不要盲猜工具名重试；
6) 调外部业务平台（ITOM 等）时用 account_ref（先 itops_itom_accounts 取引用名），
   **不要向用户索要账号口令**——凭据只存在服务端配置里，对话中出现即视为泄露。"""

mcp = MCPServer("enterprise-gateway", instructions=_INSTRUCTIONS)

# 路由表初始为空，由聚合器在启动时（main / 手动 sync）填充
router = Router()
aggregator = Aggregator(router)
audit = audit_log("gateway")

# 结果体积上限（AI 上下文与 token 成本的硬保护）：条目数 / 单值字符数
def _env_int(name: str, default: int) -> int:
    """配置写错时退回默认值，绝不让网关因为一个坏数字起不来。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        val = int(raw)
        return val if val > 0 else default
    except ValueError:
        logger.warning("%s=%r 不是正整数，回退默认 %d", name, raw, default)
        return default


MAX_RESULT_ITEMS = _env_int("MCP_MAX_RESULT_ITEMS", 200)
MAX_RESULT_CHARS = _env_int("MCP_MAX_RESULT_CHARS", 20000)


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


def _suggest_tools(name: str, server: str | None = None, n: int = 5) -> list[str]:
    """工具名写错时的相近候选（编辑距离 + 子串），让 AI 一次纠正而不是反复猜。"""
    import difflib

    pool = [r.tool for r in router.routes if not server or _norm(r.server) == _norm(server)]
    canonical = {p: p for p in pool}
    got = difflib.get_close_matches(name.replace(".", "_"), list(canonical), n=n, cutoff=0.55)
    low = name.lower()
    got += [p for p in pool if p not in got and (low in p or p in low or low.replace("_", ".") in p)]
    return got[:n]


# ---------------------------------------------------------------------------
# 审批令牌：把「AI 通道」与「审批人通道」用密钥区分开
# ---------------------------------------------------------------------------
def _approver_token() -> str:
    return (os.environ.get("MCP_APPROVAL_TOKEN") or "").strip()


def _require_approve_authority(request_id: str, approval_token: str | None) -> None:
    """配置了 MCP_APPROVAL_TOKEN 时，审批决策必须携带匹配令牌。

    没这道闸，模型可以先挂单再自己 approve_request——HITL 形同虚设。
    未配置令牌时放行（本机开发/试点），但启动日志与 describe_gateway 会明确标注
    「审批未鉴权」，生产必须配置。
    """
    expected = _approver_token()
    if not expected:
        return
    # 令牌可由审批人客户端的请求头 X-Sg-Approval-Token 带上（推荐：模型看不到也不必复述），
    # 也接受 approval_token 参数（运维脚本/手工调用用）
    supplied = (approval_token or approval_hint.get() or "").strip()
    if supplied != expected:
        audit.record("approval_denied", request_id=request_id, reason="令牌缺失或不匹配")
        raise ToolError(
            f"审批决策被拒：approve_request / reject_request 需要审批令牌（approval_token），"
            "它只交给人类审批人，不写入 AI 可见的配置。请把这一步交由持令牌的审批人完成；"
            "若你就是审批人，请在独立的运维客户端（配置 MCP_APPROVAL_TOKEN）中操作。"
        )


def _identity_extras() -> dict[str, Any]:
    """审计附加维度：会话与调用者标识（HTTP 前置层从请求头写入 contextvar）。"""
    out: dict[str, Any] = {}
    sid = session_hint.get()
    if sid:
        out["session"] = sid
    actor = actor_hint.get()
    if actor:
        out["principal"] = actor   # 反代/IdP 注入的真实身份，优先于 AI 自报的 requested_by
    return out


def _actor(requested_by: str) -> str:
    """审计里的"谁发起"：请求头身份优先，其次 AI 自报（自报不可信，仅作线索）。"""
    return actor_hint.get() or requested_by


# ---------------------------------------------------------------------------
# 结果规模保护
# ---------------------------------------------------------------------------
def _cap_result(result: Any) -> Any:
    """截断超大结果并在 dict 上标注 truncated（AI 上下文与 token 的硬保护）。

    一次工具调用把 10 万行数据灌进对话，会既撑爆上下文又产生高额 token 费用——
    正确姿势是异步 job 或分页，这里负责「兜底 + 引导」。
    """
    if isinstance(result, list):
        if len(result) > MAX_RESULT_ITEMS:
            logger.warning("结果 %d 条超上限 %d，已截断", len(result), MAX_RESULT_ITEMS)
            return {
                "result": result[:MAX_RESULT_ITEMS],
                "returned": MAX_RESULT_ITEMS,
                "total": len(result),
                "truncated": True,
                "note": f"仅返回前 {MAX_RESULT_ITEMS} 条；请用更精确的过滤条件、"
                        "调小 limit，或改用异步任务 data_submit_collect_job",
            }
        return result
    if isinstance(result, str) and len(result) > MAX_RESULT_CHARS:
        return result[:MAX_RESULT_CHARS] + f"…(截断，原长 {len(result)}，可用 MCP_MAX_RESULT_CHARS 调整)"
    if isinstance(result, dict):
        dumped = len(str(result))
        if dumped > MAX_RESULT_CHARS:
            trimmed = _trim_dict(result, MAX_RESULT_CHARS)
            trimmed["_truncated"] = True
            trimmed["_note"] = f"结果过大（约 {dumped} 字符），已裁剪列表字段；请缩小查询范围"
            return trimmed
    return result


def _trim_dict(data: dict[str, Any], budget: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    used = 0
    for k, v in data.items():
        if isinstance(v, list) and len(v) > 20:
            v = v[:20] + [f"…(共 {len(data[k])} 项)"]
        size = len(str(v))
        if used + size > budget and out:
            out[k] = f"…(因结果过大省略，{len(str(data[k]))} 字符)"
            used = budget
            continue
        out[k] = v
        used += size
    return out


# ---------------------------------------------------------------------------
# 执行 + 审计
# ---------------------------------------------------------------------------
def _new_call_id() -> str:
    return "cal_" + uuid.uuid4().hex[:12]


def _execute(route: ToolRoute, args: dict[str, Any], call_id: str, *, actor: str, via: str) -> dict[str, Any]:
    """转发到下游并记审计；下游失败转成带 kind/建议的可读工具错误。"""
    started = time.monotonic()
    try:
        result = router.execute(route.server, route.tool, args,
                               read_only=route.read_only, call_id=call_id)
    except DownstreamError as e:
        ms = round((time.monotonic() - started) * 1000, 1)
        audit.record("tool_error", call_id=call_id, actor=actor, via=via, server=route.server,
                     tool=route.tool, args=args, kind=e.kind, error=str(e), ms=ms,
                     attempts=e.attempts)
        raise ToolError(f"{e}｜call_id={call_id}") from e
    except Exception as e:  # noqa: BLE001 — 未分类异常也要落审计再抛出
        ms = round((time.monotonic() - started) * 1000, 1)
        audit.record("tool_error", call_id=call_id, actor=actor, via=via, server=route.server,
                     tool=route.tool, args=args, kind=type(e).__name__, error=str(e), ms=ms)
        raise ToolError(f"网关执行 {route.server}.{route.tool} 失败：{e}｜call_id={call_id}") from e
    ms = round((time.monotonic() - started) * 1000, 1)
    audit.record("tool_call", call_id=call_id, actor=actor, via=via, server=route.server,
                 tool=route.tool, args=args, ok=True, ms=ms,
                 approval=route.requires_approval, **_identity_extras())
    return {"approved": True, "executed": True, "result": _cap_result(result),
            "call_id": call_id, "elapsed_ms": ms}


def _dispatch(route: ToolRoute, args_obj: dict[str, Any], requested_by: str,
              call_id: str | None = None) -> dict[str, Any]:
    """已解析路由 + 规范化入参 → 立即执行或挂审批。gateway_call / route_* 工具共用。"""
    call_id = call_id or _new_call_id()
    if not route.requires_approval:
        return _execute(route, args_obj, call_id, actor=_actor(requested_by), via="dispatch")

    req = gate.request(
        source_server=route.server,   # 存规范名（宽容解析可能救回了脏 server/tool 写法）
        tool_name=route.tool,
        tool_args=args_obj,
        requested_by=_actor(requested_by),
        title=route.summary,
        description=f"工具 {route.server}.{route.tool} 需要审批后方可执行",
        call_id=call_id,
    )
    audit.record("approval_created", call_id=call_id, actor=req.requested_by, server=req.source_server,
                 tool=req.tool_name, args=args_obj, request_id=req.id,
                 expires_at=req.expires_at.isoformat() if req.expires_at else None)
    ttl_note = ""
    if req.status.value == "approved":
        ttl_note = "（该等值请求此前已批准，请查 list_approvals 确认是否已执行）"
    return {
        "approved": False,
        "executed": False,
        "request_id": req.id,
        "call_id": call_id,
        "status": req.status.value,
        "expires_at": req.expires_at.isoformat() if req.expires_at else None,
        "message": (f"已创建审批单 {req.id}，目标操作【尚未执行】。请通知审批人用 "
                    "list_pending_approvals + approve_request（需审批令牌）放行；"
                    "AI 不得自行批准。" + ttl_note),
    }


@mcp.tool()
def list_pending_approvals(limit: int = 50) -> list[dict[str, Any]]:
    """列出所有待审批事项（审批人视角）。

    返回每条审批单：id（"apr_" 前缀，传给 approve_request/reject_request）、
    title、source_server、tool_name、tool_args（即将执行的原始入参，敏感字段以 *** 遮蔽）、
    requested_by、created_at、expires_at（超时自动关闭）。
    参数 limit=最多返回条数（默认 50）。
    """
    items = gate.list(status="pending")[: max(int(limit), 1)]
    return [_approval_view(r) for r in items]


@mcp.tool()
def list_approvals(status: Literal["pending", "approved", "rejected", "timeout", "cancelled", "exec_failed", "all"] = "all",
                   limit: int = 20) -> list[dict[str, Any]]:
    """审批台账：按状态查看审批单（含已决/执行失败），用于事后核查与重试。

    参数：status=状态过滤（默认 all）；limit=最多条数（默认 20，按创建时间倒序）。
    每条含 attempts（执行尝试次数）与 exec_error（最近一次执行失败原因）。
    """
    items = gate.list(None if status == "all" else status)
    items = sorted(items, key=lambda r: r.created_at, reverse=True)[: max(int(limit), 1)]
    return [_approval_view(r) for r in items]


def _approval_view(req: ApprovalRequest) -> dict[str, Any]:
    from mcp_shared.audit import redact

    return {
        "id": req.id,
        "title": req.title,
        "source_server": req.source_server,
        "tool_name": req.tool_name,
        "tool_args": redact(req.tool_args, key="tool_args"),
        "args_digest": digest(req.tool_args),
        "requested_by": req.requested_by,
        "status": req.status.value,
        "created_at": req.created_at.isoformat(),
        "expires_at": req.expires_at.isoformat() if req.expires_at else None,
        "decided_by": req.decided_by,
        "decided_at": req.decided_at.isoformat() if req.decided_at else None,
        "comment": req.comment,
        "attempts": req.attempts,
        "exec_error": req.exec_error,
        "call_id": req.call_id,
    }


@mcp.tool()
def approve_request(request_id: str, comment: str | None = None,
                    approval_token: str | None = None) -> dict[str, Any]:
    """批准一条待审批事项并立即执行到下游（需审批令牌）。

    参数：
    - request_id: 来自 gateway_call 返回值或 list_pending_approvals，形如 "apr_6a19736d88d2"
    - comment: 可选审批意见
    - approval_token: 审批令牌——服务端配了 MCP_APPROVAL_TOKEN 时必填且必须匹配。
      该令牌只交给人类审批人（独立运维客户端），不要填入 AI 侧配置，也不要在对话中索取；
      没有它就无法绕过 HITL 闸门。

    返回：{"request_id","status":"approved","executed":true,"result":<下游执行结果>}
    下游执行失败时 status 变为 "exec_failed" 并带 exec_error，可修正入参后
    用 retry_execution 重放，或重新批准。
    """
    return _decide(request_id, ApprovalAction.APPROVE, comment, approval_token)


@mcp.tool()
def reject_request(request_id: str, comment: str | None = None,
                   approval_token: str | None = None) -> dict[str, Any]:
    """驳回一条待审批事项，不执行任何下游调用（同样需要审批令牌）。"""
    result = _decide(request_id, ApprovalAction.REJECT, comment, approval_token, execute=False)
    return result


def _decide(request_id: str, action: ApprovalAction, comment: str | None,
            approval_token: str | None, *, execute: bool = True) -> dict[str, Any]:
    _require_approve_authority(request_id, approval_token)
    try:
        req = gate.decide(request_id, action, by="approver", comment=comment)
    except (LookupError, ValueError) as e:
        # LookupError=审批单不存在；ValueError=该单已决策过（状态机保护）。
        raise ToolError(str(e)) from e
    audit.record("approval_decided", request_id=req.id, actor="approver",
                 server=req.source_server, tool=req.tool_name, status=req.status.value,
                 comment=comment, args=req.tool_args, call_id=req.call_id)
    if not execute:
        return {"request_id": req.id, "status": req.status.value, "executed": False}
    return _run_approved(req)


def _run_approved(req: ApprovalRequest) -> dict[str, Any]:
    """审批单 → 下游执行；失败回写 exec_failed（可重放），不会出现「批了但没做」。"""
    call_id = req.call_id or _new_call_id()
    try:
        result = router.execute(req.source_server, req.tool_name, req.tool_args,
                               read_only=False, call_id=call_id)
    except Exception as e:  # noqa: BLE001 — 执行失败要记录到单据上再报给调用方
        gate.mark_execution(req.id, ok=False, error=str(e))
        audit.record("approval_exec_failed", request_id=req.id, server=req.source_server,
                     tool=req.tool_name, args=req.tool_args, error=str(e), call_id=call_id,
                     attempts=req.attempts)
        raise ToolError(
            f"审批单 {req.id} 已批准但下游执行失败：{e}。"
            f"单据状态已置 exec_failed（第 {req.attempts} 次尝试），"
            "修正原因后可用 retry_execution 重放，无需重新审批。"
        ) from e
    req = gate.mark_execution(req.id, ok=True)
    audit.record("approval_executed", request_id=req.id, server=req.source_server,
                 tool=req.tool_name, args=req.tool_args, call_id=call_id, attempts=req.attempts)
    return {"request_id": req.id, "status": req.status.value, "executed": True,
            "result": _cap_result(result), "call_id": call_id, "attempts": req.attempts}


@mcp.tool()
def retry_execution(request_id: str, approval_token: str | None = None) -> dict[str, Any]:
    """重放一条「已批准但执行失败」（status=exec_failed）的审批单。

    用途：下游临时故障（库不可达、超时）后，不必让审批人重新决策一遍，
    直接按原入参重放。仅 exec_failed / approved 未成功的单可重放。
    """
    _require_approve_authority(request_id, approval_token)
    req = gate.get(request_id)
    if req is None:
        raise ToolError(f"审批单不存在: {request_id}")
    if req.status.value not in ("exec_failed", "approved"):
        raise ToolError(f"审批单 {request_id} 状态为 {req.status.value}，无需/不可重放执行")
    audit.record("approval_retry", request_id=req.id, server=req.source_server,
                 tool=req.tool_name, actor="approver", call_id=req.call_id)
    return _run_approved(req)


@mcp.tool()
def list_routes(server: str | None = None, tool: str | None = None,
                keyword: str | None = None, full_schema: bool = False) -> list[dict[str, Any]]:
    """列出网关聚合到的全部下游工具明细（server / 工具名 / 描述 / 入参 / 是否需审批）。

    数据来自启动时（及最近一次 refresh_routes）从各下游 server 拉取的 tools/list。
    日常调用请优先用各域的 route_* 聚合路由工具（method 枚举即本清单的工具名）；
    本工具用于查看入参细节与排查 gateway_call 的未注册路由报错，
    不要凭猜测直接调用（如创建 IT 工单的真实工具是 itops_create_incident）。

    参数（结果体积直接决定 AI 上下文成本，请尽量带过滤条件）：
    - server: 只看某个下游（归一化匹配，"go-datahub" 等价 "go_datahub"）
    - tool:   只看某个工具（支持漏前缀与下划线/点号别名）
    - keyword: 工具名或描述子串过滤（不区分大小写）
    - full_schema: true 才返回完整入参 JSON Schema（默认只给参数名/类型/必填摘要，省 token）

    每条含 tool_alias 字段（点号换成下划线的别名）——若你的平台对参数值中的 "."
    序列化有问题，请一律改用 tool_alias。
    """
    routes = router.routes
    if server:
        routes = [r for r in routes if _norm(r.server) == _norm(server)]
    if tool:
        resolved = _resolve_route(server or "", tool)
        if resolved is not None:
            routes = [resolved]
    if keyword:
        kw = keyword.lower()
        routes = [r for r in routes if kw in r.tool.lower() or kw in (r.summary or "").lower()]
    out = []
    for r in routes:
        item: dict[str, Any] = {
            "server": r.server,
            "tool": r.tool,
            "tool_alias": r.tool.replace(".", "_"),
            "summary": r.summary,
            "requires_approval": r.requires_approval,
            "read_only": r.read_only,
        }
        if full_schema:
            item["input_schema"] = r.input_schema
        else:
            item["params"] = _schema_params(r)
        out.append(item)
    return out


_TYPE_PLACEHOLDER: dict[str, Any] = {"integer": 1, "number": 1.5, "boolean": True, "string": "示例值"}


def _iter_props(schema: dict[str, Any]):
    """展开入参属性：含 oneOf/anyOf（Optional 注解）与嵌套 required。"""
    schemas = [schema]
    for key in ("oneOf", "anyOf"):
        for sub in (schema.get(key) or []):
            if isinstance(sub, dict):
                schemas.append(sub)
    props: dict[str, Any] = {}
    required: set[str] = set()
    for sch in schemas:
        if not isinstance(sch, dict):
            continue
        for k, p in (sch.get("properties") or {}).items():
            props.setdefault(k, p)
        required |= set(sch.get("required") or [])
    return props, required


def _schema_params(r: ToolRoute) -> dict[str, Any]:
    """入参 schema 摘要：每个参数的类型/必填/一行描述/枚举候选。"""
    props, required = _iter_props(r.input_schema or {})
    out: dict[str, Any] = {}
    for k, p in props.items():
        if not isinstance(p, dict):
            continue
        ptype = p.get("type")
        if isinstance(ptype, list):
            ptype = "/".join(ptype)
        enum = p.get("enum")
        if not ptype and enum:
            ptype = "/".join(str(e) for e in enum) or "any"
        desc = ((p.get("description") if isinstance(p, dict) else "") or "").strip().splitlines()
        info: dict[str, Any] = {
            "type": ptype or "any",
            "required": k in required,
            "desc": desc[0][:120] if desc else "",
        }
        if enum and len(enum) <= 12:
            info["enum"] = list(enum)
        out[k] = info
    return out


@mcp.tool()
def list_tool_catalog(server: str | None = None, keyword: str | None = None,
                      mode: Literal["compact", "full"] = "compact") -> list[dict[str, Any]]:
    """全平台工具目录（调用视角说明书）：每个工具在哪个下游 MCP server、如何从上层网关传递调用下来。

    调用链：AI 客户端 → 本网关（顶层统一入口）选 route_* 域路由工具或 gateway_call
    → 网关按路由表经 Streamable HTTP 转发 → 工具所在下游 MCP server（见各组 endpoint）
    → 下游执行工具并原路返回。

    参数（目录是 token 大户，默认精简，请按需过滤）：
    - server:   只看某个下游域
    - keyword:  工具名/描述子串过滤（不区分大小写）
    - mode:     compact（默认）只给一行摘要；full 才给逐参数明细与调用示例

    返回按 MCP server 分组，每组含 mcp_server / endpoint / route_tool / call_chain /
    tool_count / 概览 tools（full 模式下含 params、required、requires_approval、example）。

    想知道"某个工具是谁提供的、参数含义、该走哪个入口"时调本工具，比 list_routes 更可读。
    """
    by_server: dict[str, list[ToolRoute]] = {}
    for r in router.routes:
        if server and _norm(r.server) != _norm(server):
            continue
        if keyword:
            kw = keyword.lower()
            if kw not in r.tool.lower() and kw not in (r.summary or "").lower():
                continue
        by_server.setdefault(r.server, []).append(r)

    catalog: list[dict[str, Any]] = []
    for srv, routes in sorted(by_server.items()):
        endpoint = next((r.endpoint for r in routes if r.endpoint), None)
        route_tool = _route_tool_name(srv)
        routes = sorted(routes, key=lambda x: x.tool)
        group: dict[str, Any] = {
            "mcp_server": srv,
            "endpoint": endpoint,
            "route_tool": route_tool,
            "call_chain": (
                f"AI→网关(:9000)→{route_tool}(method=工具名, params=入参)"
                f"→Streamable HTTP 转发→{srv}({endpoint})→执行并原路返回"
            ),
            "tool_count": len(routes),
            "approval_required_tools": [r.tool for r in routes if r.requires_approval],
        }
        if mode == "full":
            group["tools"] = [_catalog_tool(r) for r in routes]
        else:
            group["tools"] = [
                {"tool": r.tool, "in_mcp": srv, "summary": r.summary,
                 "requires_approval": r.requires_approval, "read_only": r.read_only}
                for r in routes
            ]
        catalog.append(group)
    if not catalog:
        raise ToolError(
            f"目录中没有匹配的工具（server={server!r} keyword={keyword!r}）。"
            f"可用域: {sorted({r.server for r in router.routes})}；"
            "下游刚更新过请先调 refresh_routes。"
        )
    return catalog


def _catalog_tool(r: ToolRoute) -> dict[str, Any]:
    params = _schema_params(r)
    required_names = [k for k, v in params.items() if v["required"]]
    simple = {"integer", "number", "boolean", "string"}
    if params and all(v["type"] in simple for v in params.values()):
        example_params = {k: _TYPE_PLACEHOLDER[v["type"]] for k, v in params.items()}
    elif params:
        # 含对象/数组等复杂参数：kv 摆不平，给最小示例并注明完整形态走 gateway_call
        example_params = {k: "<见params>" for k in (required_names or params)}
    else:
        example_params = {}
    return {
        "tool": r.tool,
        "in_mcp": r.server,
        "summary": r.summary,
        "params": params,
        "required": required_names,
        "requires_approval": r.requires_approval,
        "read_only": r.read_only,
        "example": {"method": r.tool, "params": example_params},
    }


@mcp.tool()
def list_downstreams() -> list[dict[str, Any]]:
    """列出网关配置的下游 server 清单与**聚合健康度**（诊断路由缺失时先用本工具）。

    每条含 name / url / tools_aggregated / status（ok | failed:原因 | never_synced）/
    auth（配了 Bearer 时为 bearer）。
    """
    return aggregator.downstream_status()


@mcp.tool()
def refresh_routes() -> dict[str, Any]:
    """重新连接各下游 server 拉取 tools/list，刷新网关路由表并重建 route_* 聚合路由工具。

    适用场景：某个下游在网关启动后才上线、下游新增/删除了工具。
    返回本轮聚合结果（各 server 成功拉到的工具数 / 失败原因）。
    """
    started = time.monotonic()
    report = aggregator.sync()
    audit.record("routes_refreshed", ok=report.ok, failed=report.failed,
                 ms=round((time.monotonic() - started) * 1000, 1))
    return {"ok": report.ok, "failed": report.failed,
            "route_tools": sorted(_ROUTE_TOOLS), "total_routes": len(router.routes)}


@mcp.tool()
def describe_gateway() -> dict[str, Any]:
    """网关自身状态一览（排障与部署自检）：版本、路由数、下游健康度、生效的安全开关。

    重点看 `security`：审批令牌是否已配（未配=AI 可自批，生产必须配）、网关 Bearer
    是否启用、结果上限与下游超时。
    """
    return {
        "ok": True,
        "routes": len(router.routes),
        "downstreams": aggregator.downstream_status(),
        "last_sync": {"ok": aggregator.last_report.ok, "failed": aggregator.last_report.failed,
                      "at": aggregator.last_sync_at},
        "pending_approvals": len(gate.list(status="pending")),
        "approval_store": str(gate.store_path),
        "audit_log": str(audit.path),
        "security": {
            "approval_token_required": bool(_approver_token()),
            "gateway_bearer_enabled": bool((os.environ.get("MCP_GATEWAY_TOKEN") or "").strip()),
            "downstream_timeout_seconds": timeout_for("any"),
            "max_result_items": MAX_RESULT_ITEMS,
            "max_result_chars": MAX_RESULT_CHARS,
            "rate_limit_per_second": int(os.environ.get("MCP_RATE_LIMIT_PER_SECOND") or 0),
        },
        "route_tools": sorted(_ROUTE_TOOLS),
    }


@mcp.tool()
def query_audit_log(limit: int = 25, event: str | None = None, server: str | None = None,
                    tool: str | None = None, call_id: str | None = None) -> list[dict[str, Any]]:
    """查最近的调用审计（谁在何时调了什么、参数摘要、结果、耗时、错误）。

    参数：limit=条数（默认 25，上限 500，倒序最新在前）；event=tool_call / tool_error /
    approval_created / approval_decided / approval_executed / approval_exec_failed …；
    server / tool / call_id 等值过滤。
    入参里的敏感字段（token/password/dsn/authorization 等）在落盘时已遮蔽为 ***。
    """
    filters: dict[str, Any] = {}
    for key, val in (("event", event), ("server", server), ("tool", tool), ("call_id", call_id)):
        if val:
            filters[key] = val
    return audit.query(limit=min(int(limit), 500), **filters)


@mcp.tool()
def gateway_call(
    server: str,
    tool: str,
    arguments: dict[str, Any] | str | None = None,
    requested_by: str = "agent",
    idempotency_key: str | None = None,
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
    - idempotency_key: 可选。**写操作可能超时/断连时务必带**（如用 generate_id 生成的号）：
      同一个键的重复请求不会二次执行——AI 平台的重试因此不会重复建单/重复导入。
      窗口由 MCP_IDEMPOTENCY_TTL_SECONDS 控制（默认 600，0=关闭）。

    调用示例——创建高优工单：
      {"server": "it_ops", "tool": "create_incident",
       "arguments": {"title": "打印机故障", "priority": "high"}}

    示例——查当前时间（扁平串形态）：
      {"server": "common-tools", "tool": "now", "arguments": "timezone_name=Asia/Shanghai"}

    示例——Go 数据源清单（无入参）：
      {"server": "go_datahub", "tool": "data_list_dsn_refs", "arguments": {}}

    返回结构：
    - 免审批:   {"approved": true, "executed": true, "result": <下游返回值>, "call_id": "cal_xxx",
                "elapsed_ms": 12.3}
    - 需审批:   {"approved": false, "executed": false, "request_id": "apr_xxx", "message": ...}
      ← 此时目标操作【尚未执行】！需审批人（持 MCP_APPROVAL_TOKEN）调
        list_pending_approvals + approve_request 放行后才会真正执行。
      只读工具的超时/不可达会由网关自动重试一次；写工具绝不自动重试。

    推荐流程：不确定时先 list_tool_catalog(mode="full", keyword=...) 查工具与入参，
    再发起本调用；报错时读 message 里的 kind 与建议，别原样重试。
    """
    args_obj = _coerce_args(arguments)
    route = _resolve_route(server, tool)
    if route is None:
        raise ToolError(_route_miss_message(server, tool))
    return _dispatch_idempotent(route, args_obj, requested_by, idempotency_key, via="gateway_call")


def _route_miss_message(server: str, tool: str) -> str:
    same_server = sorted(r.tool for r in router.routes if _norm(r.server) == _norm(server))
    hints = []
    near = _suggest_tools(tool, server if same_server else None)
    if near:
        hints.append(f"是否想调用: {near}")
    if same_server:
        hints.append(f"{server} 域共 {len(same_server)} 个方法（用 route_{_norm(server)} 的 method 枚举）")
    else:
        hints.append(f"已聚合的域: {sorted({r.server for r in router.routes})}")
    hints.append("确认名称请调 list_routes 或 list_tool_catalog；下游刚上线请调 refresh_routes")
    return f"未注册的路由: {server}.{tool}。" + "；".join(hints)


# ---------------------------------------------------------------------------
# 幂等：AI 平台超时重试不会二次执行
# ---------------------------------------------------------------------------
_IDEMPOTENT: dict[str, tuple[float, dict[str, Any]]] = {}


def _idem_ttl() -> int:
    try:
        return max(int(os.environ.get("MCP_IDEMPOTENCY_TTL_SECONDS", "600")), 0)
    except ValueError:
        return 600


def _idem_purge(now_ts: float) -> None:
    for key in [k for k, (ts, _) in _IDEMPOTENT.items() if now_ts - ts > _idem_ttl()]:
        _IDEMPOTENT.pop(key, None)


def _dispatch_idempotent(route: ToolRoute, args_obj: dict[str, Any], requested_by: str,
                         idempotency_key: str | None, *, via: str) -> dict[str, Any]:
    ttl = _idem_ttl()
    if not ttl or not idempotency_key:
        return _dispatch(route, args_obj, requested_by)
    now_ts = time.time()
    _idem_purge(now_ts)
    ckey = f"{route.server}|{route.tool}|{idempotency_key.strip()}"
    hit = _IDEMPOTENT.get(ckey)
    if hit and now_ts - hit[0] <= ttl:
        audit.record("idempotent_hit", server=route.server, tool=route.tool,
                     key=idempotency_key, actor=requested_by, via=via)
        cached = dict(hit[1])
        cached["idempotent_replay"] = True
        cached["idempotency_key"] = idempotency_key
        return cached
    out = _dispatch(route, args_obj, requested_by)
    # 只缓存已执行/已挂单的结果；审批单复用同一 request_id 即天然幂等
    if out.get("executed") or out.get("request_id"):
        _IDEMPOTENT[ckey] = (time.time(), out)
    return out


# （原 gateway_call_kv 独立工具已并入 gateway_call 的 arguments 扁平串形态，
#  避免同一能力出现两个重复入口）


def _parse_kv_params(params: str | dict | None) -> dict[str, Any]:
    """解析扁平串 "k=v;k2=v2"：值类型推断，且支持引号包裹保住原样。

    不加引号规则会静默改坏数据：`version=1.10` → 浮点 1.1、`id=007` → 7 这类
    「看起来像数字的字符串」是版本号/编号/手机号的常见形态。因此：
    - 值被 `"..."` 或 `'...'` 包裹 → 一律按字符串（并剥引号，可含空格/分号转义）；
    - 裸 `true/false/null` → 对应标量；纯数字（无-leading-zero 语义歧义时）→ int/float；
    - 其他 → 字符串原样。
    兼容平台实际发来 JSON 对象/字符串的情况（转交 _coerce_args）。
    """
    if params is None or params == "":
        return {}
    if isinstance(params, dict):
        return params
    if not isinstance(params, str):
        raise ToolError(f"params 类型不支持: {type(params).__name__}")
    if params.lstrip().startswith("{"):
        return _coerce_args(params)
    out: dict[str, Any] = {}
    for pair in _split_kv_pairs(params):
        k, sep, v = pair.partition("=")
        if not sep:
            raise ToolError(f"params 片段缺少 '='：{pair!r}（格式应为 k=v;k2=v2）")
        out[k.strip()] = _coerce_kv_value(v.strip())
    return out


def _split_kv_pairs(params: str) -> list[str]:
    """按 ';' 切分，但引号内的 ';' 不算分隔符。"""
    pairs, buf, quote = [], [], ""
    for ch in params.replace("；", ";"):
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            continue
        if ch == ";":
            pairs.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    pairs.append("".join(buf))
    return [p.strip() for p in pairs if p.strip()]


_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")


def _coerce_kv_value(v: str) -> Any:
    """扁平串取值的类型推断（宁可不转，也不把好数据改坏）。

    - 引号包裹 → 字符串（剥引号）
    - true/false/null → 标量
    - `-?\\d+` → int，但带前导零（"007"）保留字符串——那是编号/工号语义
    - `-?\\d+\\.\\d+` → float，但小数位以 0 结尾（"1.10"、"2.50"）保留字符串——版本号语义
    - 其他 → 原样字符串
    """
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    low = v.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("none", "null"):
        return None
    if _INT_RE.match(v):
        stripped = v.lstrip("-")
        if len(stripped) > 1 and stripped[0] == "0":
            return v
        return int(v)
    if _FLOAT_RE.match(v):
        frac = v.split(".", 1)[1]
        if frac.endswith("0"):
            return v
        return float(v)
    return v


# ---------------------------------------------------------------------------
# 聚合路由工具（route_*）：每个下游 server 收敛为一个入口，
# method 参数用枚举列出该域全部可调用方法，描述中标注各方法入参与审批要求。
# 每轮 aggregator.sync()（启动 / refresh_routes）后整体重建，枚举跟随最新路由表。
# ---------------------------------------------------------------------------

_ROUTE_TOOLS: dict[str, str] = {}  # 已注册的 route 工具名 -> server 名
_MAX_ROUTE_METHODS = 40            # 枚举超过此数时截断，强制走 gateway_call


def _route_tool_name(server: str) -> str:
    return "route_" + _norm(server)


def _method_signature(r: ToolRoute) -> str:
    """由入参 schema 生成方法签名摘要，如 itops_create_incident(title*, priority, reporter)。"""
    props, required = _iter_props(r.input_schema or {})
    args = ", ".join(f"{k}*" if k in required else k for k in props)
    return f"{r.tool}({args})" if args else f"{r.tool}()"


def _make_route_fn(server: str, methods: list[str], exact: bool):
    literal = Literal[tuple(methods)]  # type: ignore[valid-type]

    def route_fn(method, params: "dict[str, Any] | str | None" = None,
                 requested_by: str = "agent",
                 idempotency_key: str | None = None) -> dict[str, Any]:
        route = router.resolve(server, method) or _resolve_route(server, method)
        if route is None:
            near = _suggest_tools(str(method), server)
            raise ToolError(
                f"{server} 域没有方法 {method!r}。"
                + (f"相近可用方法: {near}。" if near else f"当前可调用: {methods}。")
                + "若下游刚更新，请先调 refresh_routes 重建本路由工具"
            )
        args_obj = _coerce_args(params)
        if exact:  # 工具名必须与下游一字不差（避免宽容匹配把 typo 静默送到别的工具）
            if _norm(route.tool) != _norm(str(method)) and str(method).replace(".", "_") != route.tool:
                raise ToolError(
                    f"method {method!r} 与网关在册工具 {route.tool!r} 不一致，"
                    "请严格使用枚举中的名字（大小写/前缀/分隔符都算差异）"
                )
        return _dispatch_idempotent(route, args_obj, requested_by, idempotency_key, via="route")

    route_fn.__name__ = _route_tool_name(server)
    route_fn.__annotations__ = {
        "method": literal,
        "params": dict[str, Any] | str | None,   # 三种入参形态在 schema 里可见（对象/JSON串/留空）
        "requested_by": str,
        "idempotency_key": str | None,
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
        exact = len(methods) <= _MAX_ROUTE_METHODS
        shown = methods[:_MAX_ROUTE_METHODS]
        lines = [
            f"- {_method_signature(r)}{'【需审批】' if r.requires_approval else ''}"
            f"{'【只读】' if r.read_only else ''}：{r.summary}"
            for r in routes[:_MAX_ROUTE_METHODS]
        ]
        if not exact:
            lines.append(f"- …共 {len(methods)} 个方法，枚举已截断；"
                         "其余方法请用 gateway_call(server, tool) 或 list_routes(server=...) 查询")
        description = (
            f"调用下游服务 {server} 的聚合路由入口：method 从枚举中选目标方法，"
            "params 传该方法入参——对象 {\"k\": v}、JSON 字符串、"
            "或扁平串 \"k=v;k2=v2\"（值自动识别 int/bool）均可；无参方法留空。\n"
            "写操作可能超时/需重试时请带 idempotency_key（同一键不会二次执行）。\n"
            f"可调用方法（* 为必填参数，【需审批】的方法调用后只产生审批单、须审批人持令牌 "
            f"approve_request 才执行，AI 不得自批；【只读】方法失败会被网关自动重试一次）：\n"
            + "\n".join(lines)
        )
        name = _route_tool_name(server)
        mcp.add_tool(_make_route_fn(server, shown, exact), name=name, description=description)
        _ROUTE_TOOLS[name] = server
    logger.info("重建聚合路由工具: %s", sorted(_ROUTE_TOOLS))
    return sorted(_ROUTE_TOOLS)


aggregator.on_sync = rebuild_route_tools


def health_info() -> dict[str, Any]:
    """/healthz 探针附加信息（由 server_kit 的 HTTP 前置层使用）。"""
    return {
        "routes": len(router.routes),
        "downstreams_ok": sorted(aggregator.last_report.ok),
        "downstreams_failed": sorted(aggregator.last_report.failed),
        "pending_approvals": len(gate.list(status="pending")),
    }


# ---------------------------------------------------------------------------
def main() -> None:
    from mcp_shared import run_server

    if os.environ.get("MCP_APPROVAL_PERSIST", "1") != "0":
        restored = gate.load()
        if restored:
            logger.info("审批台账恢复 %d 条", restored)
    if not _approver_token():
        logger.warning(
            "未配置 MCP_APPROVAL_TOKEN：approve_request / reject_request 对 AI 客户端开放，"
            "HITL 闸门可被模型自行放行。生产部署必须配置审批令牌。"
        )
    report = aggregator.sync()
    logger.info("网关启动，聚合下游 %d 个，路由 %d 条（%s）",
                len(aggregator.specs), len(router.routes), report)
    run_server(mcp, description="【上层·审批】审批网关 MCP server", default_port=9000,
               token_env="MCP_GATEWAY_TOKEN", health_info=health_info)


if __name__ == "__main__":
    main()
