"""【中层·IT运维】IT 运维系统 MCP server（试点，业务层 Python）。

架构约定（见 docs/architecture.md「三层工具粒度体系」）：
- 第 1 层·领域工具（粗粒度，AI 优先用）：本域常用操作，统一 ``itops_`` 领域前缀
  （制造域将来为 ``mfg.*`` 等），经上层网关聚合，AI 只连网关。
- 第 2 层·领域通用查询（中粒度）：``itops_query_dataset``——dataset_id 用 Literal
  枚举限定**本域数据集**（带中文说明）；领域通用工具查不到的不常用数据集由此查询，
  配套 ``itops_list_data_tables`` 发现数据表。
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
from typing import Annotated, Any, Literal

import httpx2
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from mcp_itops import itom
from mcp_itops.store import LocalStoreUnavailable, local_store, new_incident
from mcp_shared import run_server
from mcp_shared.config import get as cfg
from mcp_shared.dsrouting import domain_datasets, list_datasets, route_db, route_table, table_of
from mcp_shared.http_kit import call_id_hint
from mcp_shared.limits import check_list_size, check_text
from mcp_shared.mcp_client import bearer_headers

# 内部 REST 调用超时（秒）：GET 快、POST 批量导入慢；不配超时会让 worker 线程悬死
_GET_TIMEOUT = float(os.environ.get("MCP_ITOPS_GET_TIMEOUT", "30"))
_POST_TIMEOUT = float(os.environ.get("MCP_ITOPS_POST_TIMEOUT", "120"))
_MAX_LIMIT = 1000


def _clamp_limit(limit: int) -> int:
    """行数上限：AI 传 100000 也只会拿到 1000，避免把整库灌进对话上下文。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 100
    return max(1, min(n, _MAX_LIMIT))


def _local():
    """取 local 通道存储；解释器没带 sqlite3 时转成可读工具错误（服务本身照常启动）。"""
    try:
        return local_store()
    except LocalStoreUnavailable as e:
        raise ToolError(str(e)) from e


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
    """业务层 → Go 重活的内部 HTTP 直调（POST/GET + Bearer，统一检查 ok 字段）。

    带上 X-Sg-Call-Id：网关转发时会写这个头，重活层日志据此可以把
    「AI 的一次报错」跨三层对上（网关审计 → 业务日志 → Go 日志）。
    """
    headers = bearer_headers(cfg("MCP_GODATAHUB_TOKEN")) or {}
    if call_id_hint.get():
        headers["X-Sg-Call-Id"] = call_id_hint.get()
    url = _heavy_base() + path
    try:
        if json_body is None:
            resp = httpx2.get(url, params=params, headers=headers, timeout=_GET_TIMEOUT)
        else:
            # 批量导入上限 5 万行，给足超时
            resp = httpx2.post(url, json=json_body, headers=headers, timeout=_POST_TIMEOUT)
    except httpx2.HTTPError as e:
        raise ToolError(
            f"内部调用重活层失败（{url}）：{type(e).__name__}: {e}。"
            "请确认 go_datahub 已部署（data_list_dsn_refs 可探活），或稍后重试。"
        ) from e
    if resp.status_code >= 400:
        raise ToolError(f"内部调用重活层 {path} 失败 [{resp.status_code}]: {resp.text}")
    data = resp.json()
    if not data.get("ok", False) and "ok" in data:
        # 库侧执行失败：重活层返回 200 + ok=false，转可读工具错误
        raise ToolError(f"重活层 {path} 执行失败: {data.get('error', '未知错误')}")
    return data


# ---------------------------------------------------------------------------
# IT 运维业务工具（对 AI 暴露，itops_* 前缀）
# 事件单支持三种落地通道：local=本地 SQLite 持久化（需解释器带 stdlib sqlite3）；sql=经业务名路由写入
# 数据库（内部直调重活层 batch/import|query）；api=调外部 ITSM 系统 REST API。
# ---------------------------------------------------------------------------
@mcp.tool(name="itops_create_incident")
def create_incident(title: str, priority: str = "medium", reporter: str = "agent",
                    channel: Literal["local", "sql", "api"] = "local",
                    tenant_id: str = "default") -> dict[str, Any]:
    """创建一条 IT 运维工单，返回含 id 的工单对象。

    参数：
    - title: 工单标题（必填），如 "3楼打印机卡纸"
    - priority: 枚举 low/medium/high/critical，默认 medium（其他值报错）
    - reporter: 报障人标识，默认 agent
    - channel: 落地通道枚举 local/sql/api，默认 local
      local=本地 SQLite 持久化（默认，仅需 stdlib sqlite3，数据跨服务重启保留，ID 可长期引用；库路径可用 MCP_ITOPS_DB 覆盖）；
      sql=按数据集路由写库（需 DATASET_INCIDENTS_* 配置，工单落 incidents 表，持久化）；
      api=调外部 ITSM 系统 REST（需 MCP_ITSM_API_URL 配置）
    - tenant_id: 租户标识，默认 default（仅 sql 通道参与路由）

    返回示例：{"id": "INC-0205B08E", "title": ..., "status": "new", "priority": ...}
    后续用返回的 id 调 itops_update_incident_status 流转状态。
    """
    if priority not in {"low", "medium", "high", "critical"}:
        raise ValueError(f"非法优先级: {priority}（可选 low/medium/high/critical）")
    check_text(title, limit=500, what="title")
    check_text(reporter, limit=100, what="reporter")
    if channel == "local":
        return _local().create_incident(title=title, priority=priority, reporter=reporter)
    rec = new_incident(title=title, priority=priority, reporter=reporter)
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


@mcp.tool(name="itops_list_incidents")
def list_incidents(status: str | None = None, channel: Literal["local", "sql", "api"] = "local",
                   tenant_id: str = "default", limit: int = 100) -> list[dict[str, Any]]:
    """查询工单列表，可按状态过滤（new/open/in_progress/resolved/closed）。

    channel: local=本地 SQLite（默认，持久化）；sql=按数据集路由查库（incidents 表，
    等值过滤 + 行数上限）；api=调外部 ITSM 系统 REST API。
    注意：返回注解必须是精确的 list[dict]——Any 注解会让 SDK 把单元素列表
    塌缩成单个对象，破坏调用方的解包约定。
    """
    if channel == "local":
        return _local().list_incidents(status=status)
    if channel == "sql":
        db_type, dsn_ref = route_db("incidents", tenant_id)
        limit = _clamp_limit(limit)
        filters = {"status": status} if status else {}
        result = _call_heavy_internal("/internal/v1/batch/query", json_body={
            "db_type": db_type, "dsn_ref": dsn_ref, "table": table_of("incidents"),
            "filters": filters, "limit": limit,
        })
        rows = result.get("rows", [])
        return rows if isinstance(rows, list) else [rows]
    if channel == "api":
        limit = _clamp_limit(limit)
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


@mcp.tool(name="itops_update_incident_status")
def update_incident_status(incident_id: str,
                           status: Literal["new", "open", "in_progress", "resolved", "closed"]) -> dict[str, Any]:
    """流转工单状态，返回更新后的完整工单对象。

    参数：
    - incident_id: 必须是 create_incident 返回的真实 id（形如 "INC-0205B08E"）。
      勿凭空编造；不确定时先用 itops_list_incidents 查现有工单（数据持久化，历史 ID 依然有效）
    - status: 枚举 new/open/in_progress/resolved/closed

    工单不存在时返回可读错误"工单不存在: xxx"。
    """
    rec = _local().update_incident_status(incident_id, status)
    if rec is None:
        raise LookupError(f"工单不存在: {incident_id}")
    return rec


@mcp.tool(name="itops_create_change")
def create_change(title: str, change_type: Literal["standard", "emergency"] = "standard",
                  implementer: str = "ops",
                  risk: Literal["low", "medium", "high"] = "low") -> dict[str, Any]:
    """创建变更单，返回含 id（形如 "CHG-4B36C912"）的对象。

    参数：title=变更描述；change_type=standard/emergency（标准/紧急）；
    implementer=实施人；risk=low/medium/high。

    ⚠️ 高风险写操作：经网关 gateway_call 调用时会被挂起为审批单（返回
    request_id="apr_xxx" 且尚未执行），需审批人 approve_request 后才真正创建。
    """
    check_text(title, limit=500, what="title")
    rec = _local().create_change(title=title, change_type=change_type, implementer=implementer, risk=risk)
    rec["approval_required"] = True  # 交由上层审批的标记
    return rec


@mcp.tool(name="itops_get_change")
def get_change(change_id: str) -> dict[str, Any]:
    """查询变更单详情。"""
    rec = _local().get_change(change_id)
    if rec is None:
        raise LookupError(f"变更单不存在: {change_id}")
    return rec


@mcp.tool(name="itops_register_asset")
def register_asset(name: str, asset_type: Literal["laptop", "server", "network", "software"],
                   owner: str) -> dict[str, Any]:
    """登记一台 IT 资产（录入 CMDB），返回含 id（形如 "AST-5C332490"）的对象。

    参数：name=资产名（如 "srv-ops-01"）；
    asset_type=枚举 laptop/server/network/software；owner=负责人。
    """
    return _local().create_asset(name=name, asset_type=asset_type, owner=owner)


@mcp.tool(name="itops_list_assets")
def list_assets(asset_type: Literal["laptop", "server", "network", "software"] | None = None) -> list[dict[str, Any]]:
    """查询资产清单（对象数组）。asset_type 可选过滤：laptop/server/network/software，
    留空或传 null 返回全部。"""
    return _local().list_assets(asset_type=asset_type)


# ---------------------------------------------------------------------------
# ITOM 平台（itom.shougang.com.cn）真实对接：预设账户，AI 只流转 account_ref
# 口令永远在服务端 config/platform.env，不进对话、不进审计、不回传调用方
# ---------------------------------------------------------------------------
def _itom(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except itom.ItomError as e:
        raise ToolError(str(e)) from e


@mcp.tool(name="itops_itom_accounts")
def itom_accounts() -> list[dict[str, Any]]:
    """列出服务端已配置的 ITOM 账户（只有引用名、备注、打码工号和登录状态）。

    口令不在返回里，也不要向用户索要口令——用户只需要从这份清单里挑一个
    account_ref。返回示例：
    [{"account_ref": "wangxu", "note": "运维中心", "user_id": "w***u",
      "credential": "password", "logged_in": false}]
    """
    return _itom(itom.accounts_summary)


@mcp.tool(name="itops_itom_login")
def itom_login(account_ref: str) -> dict[str, Any]:
    """用服务端预存的凭据登录 ITOM，返回会话状态（token 本身不外泄）。

    account_ref 必须来自 itops_itom_accounts；登录态在服务端缓存，过期自动重登，
    所以后续调用不必每次都先登录。
    """
    return _itom(itom.login_status, account_ref)


@mcp.tool(name="itops_itom_logout")
def itom_logout(account_ref: str | None = None) -> dict[str, Any]:
    """清掉服务端的 ITOM 会话（换账号、凭据改了、或排查平台侧 session 时用）。

    account_ref 留空=清掉全部会话。只影响本进程缓存，不注销平台侧登录。
    """
    return {"ok": True, "cleared": _itom(itom.reset, account_ref) if account_ref else itom.reset()}


@mcp.tool(name="itops_itom_get")
def itom_get(account_ref: str, path: str, params: dict[str, Any] | None = None,
             method: Literal["GET", "POST"] = "GET",
             body: dict[str, Any] | None = None,
             fields: list[str] | None = None, max_rows: int = 0) -> dict[str, Any]:
    """带 ITOM 登录态发**只读**请求（path 只能是站内相对路径）。

    参数：
    - account_ref: itops_itom_accounts 里的引用名
    - path: 形如 /event-manage/findByPage（自动拼到 MCP_ITOM_URL 后面；禁止 //、..、查询串）
    - params: 查询参数；登录态（uid/token/cookie）由服务端自动注入，不要自己传
    - method: GET 或 POST。平台的分页查询用 POST 承载只读语义，所以这里允许 POST，
      但路径末段必须是只读形态（find/get/list/query/search/view 等），否则拒绝
    - body: POST 的业务字段，只写内层即可（服务端自动补 reqHeader/reqBody 信封），如
      {"createdDtGt": "2026-08-28 14:09:54", "createdDtLt": "2026-09-29 14:09:54", "pageSize": "20"}
    - fields: 字段白名单。平台的列表一行有 90+ 列（含报障人手机号），不投影就是几百 KB
      灌进对话；查已知列表请优先用对应的业务工具（如 itops_itom_incidents 已带默认投影）
    - max_rows: 返回行数上限（0=不限），超出会在 truncated 里提示怎么翻页

    返回 {"ok": true, "status": 200, "retCode": "0000000", "data": ...}；
    列表类响应会归一成 {"rows", "returned", "total", "rows_key"}。
    写操作请用 itops_itom_call（需人工审批），不要用本工具绕过。
    """
    return _itom(itom.request_json, account_ref, method, path, params=params, body=body,
                 read_channel=True, fields=fields, max_rows=max_rows)


@mcp.tool(name="itops_itom_call")
def itom_call(account_ref: str, path: str, method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST",
              body: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """向 ITOM 平台发起写操作（建单/流转/更新），经网关调用会被挂起为审批单。

    参数同 itops_itom_get：body 只写业务字段（服务端补 reqHeader/reqBody 信封），
    登录态不用自己传；只读查询（findByPage 这类）请用 itops_itom_get，不必走审批。
    ⚠️ 该工具会真实改动生产 ITOM 数据，默认在 MCP_APPROVAL_TOOLS 名单内：
    经网关调用时返回 request_id="apr_xxx" 且尚未执行，需审批人 approve_request 才落地。
    """
    return _itom(itom.request_json, account_ref, method, path, params=params, body=body)


@mcp.tool(name="itops_itom_incidents")
def itom_incidents(account_ref: str, days: int = 7, system_type: str | None = None,
                   event_no: str | None = None, page_num: int = 1,
                   page_size: int = 20) -> dict[str, Any]:
    """查 ITOM 事件单列表（真实生产数据），已按业务列投影，不含手机号等个人信息。

    参数：
    - account_ref: itops_itom_accounts 里的引用名
    - days: 回溯天数（平台按创建时间过滤，默认 7 天；不给窗口平台不返回数据）
    - system_type: 系统类型代码（如 "SG062"=采购管理系统，实测生效）
    - event_no: 事件单号精确查（如 "SG260927001"）
    - page_num / page_size: 分页，page_size 上限 100

    返回 data={"rows":[{eventNo,eventState,eventStateName,eventLevel,eventLevelName,
    eventNature,eventNatureName,problemDesc,systemTypeName,systemSubclassName,dealStaffName,
    createdByName,createdDt,responseTime,solveTime,...}],"returned":20,"total":120,
    "rows_key":"resultData"}——码值都带中文含义，直接引用 xxxName 字段即可，不要自己解读数字码。
    注意：平台的 eventState 过滤入参无效（传任何值都是 0 行），problemDesc 只做整句精确匹配，
    所以**状态统计请对返回的 rows 自行分组**，别指望本工具按状态过滤。
    """
    return _itom(itom.incidents, account_ref, days=days, system_type=system_type,
                 event_no=event_no, page_num=page_num, page_size=page_size)


@mcp.tool(name="itops_itom_incident_form")
def itom_incident_form() -> list[dict[str, Any]]:
    """「新增事件单」表单说明书：每个必填字段的含义、取值来源与引导顺序。

    引导式建单的正确流程：先调本工具拿到字段清单，再按 dept → group → system →
    subclass → menu 的**父子顺序**用 itops_itom_options 逐级列候选给用户选，
    报修人/处理人用姓名（或工号）由服务端查档案回填电话，最后调 itops_itom_create_incident。
    """
    return list(itom.INCIDENT_FORM)


@mcp.tool(name="itops_itom_options")
def itom_options(account_ref: str, kind: str, parent: str | None = None,
                 keyword: str | None = None, limit: int = 50,
                 role: str | None = None) -> list[dict[str, Any]]:
    """列出 ITOM 某个下拉的候选项（精简投影，不含身份证等敏感列）。

    参数：
    - kind: dept 部门分类 / group 小组 / system 系统分类 / subclass 系统子类 / menu 报修菜单 /
      custom 报修人档案 / staff 处理人 / level|nature|role|state|category|solveMode 码值字典
    - parent: 上一级选中的代码。group 传部门 orgCode，system 传 groupCode，
      subclass/menu 传上级 sysSid，staff 传**一级系统**的 sysSid（传子类 sid 查不到人）
    - keyword: 名称过滤（custom 是平台精确匹配；同名多条会都返回）
    - role: 仅 kind=staff 必填（1 一线/2 二线/3 系统负责人），平台按系统+角色两键取人

    把返回的 name 列给用户选，选中项的 code/sid 再传给 itops_itom_create_incident。
    """
    return _itom(itom.options, account_ref, kind, parent=parent, keyword=keyword,
                 limit=_clamp_limit(limit), role=role)


@mcp.tool(name="itops_itom_create_incident")
def itom_create_incident(account_ref: str, dept: str, group: str, system: str, subclass: str,
                         menu: str, reporter: str, level: str, nature: str, role: str,
                         handler: str, description: str, reporter_phone: str | None = None,
                         reporter_hint: str | None = None, handler_hint: str | None = None,
                         repair_dept_code: str | None = None,
                         confirm: bool = False) -> dict[str, Any]:
    """新增 ITOM 事件单。**默认只返回预览不提交**，用户确认后再带 confirm=true 调用。

    各参数传**用户说的名称**（也可传代码），服务端逐级解析成 code/sid 后拼请求体：
    dept 部门分类、group 小组、system 系统分类、subclass 系统子类、menu 报修菜单、
    reporter 报修人姓名（自动回填电话/报修部门/作业区/岗位）、level 事件等级（可传"低"）、
    nature 事件性质（可传"故障"）、role 人员角色、handler 处理人姓名或工号
    （自动回填处理人电话）、description 问题描述。
    平台有重名数据：解析到多条会**直接报错并列出候选**让你回问用户，
    不要改成猜值或取第一条；消歧可用 reporter_hint/handler_hint 传部门等区分信息。

    返回 {ok, preview:true, resolved, payload} 表示等待确认；confirm=true 才真实建单
    （经网关调用还会先走人工审批）。缺必填（如报修人无档案电话）会返回引导性错误。
    """
    prepared = _itom(itom.prepare_incident, account_ref, dept=dept, group=group, system=system,
                     subclass=subclass, menu=menu, reporter=reporter, level=level, nature=nature,
                     role=role, handler=handler, description=description,
                     reporter_phone=reporter_phone, reporter_hint=reporter_hint,
                     handler_hint=handler_hint, repair_dept_code=repair_dept_code)
    if not confirm:
        return {"ok": True, "preview": True, "submitted": False,
                "account_ref": account_ref, "resolved": prepared["resolved"],
                "payload": prepared["payload"], "warnings": prepared["warnings"],
                "next": "把 payload 的关键字段与 warnings 念给用户确认，"
                        "无 warnings 且用户同意后再带 confirm=true 重新调用才会建单"}
    if prepared["warnings"]:
        raise ToolError("建单前仍有未确认项，请先解决再带 confirm=true 调用："
                        + "；".join(prepared["warnings"]))
    result = _itom(itom.submit_incident, account_ref, prepared["payload"])
    return {**result, "preview": False, "submitted": True}


# ---------------------------------------------------------------------------
# 内部调用示例：业务层经内部 REST 直调 Go 重活的库表能力
# ---------------------------------------------------------------------------
@mcp.tool(name="itops_export_assets_to_warehouse")
def export_assets_to_warehouse(dsn_ref: str, table: str = "cmdb_assets", asset_type: str | None = None) -> dict[str, Any]:
    """把 CMDB 资产批量归档到数据仓库（大批量写库属重活，内部直调 go_datahub
    的 /internal/v1/batch/import，不经网关、不暴露给 AI）。

    dsn_ref 为重活层 config/datahub.env 里登记的 DSN 引用名（如 warehouse_pg），
    可用 data_list_dsn_refs 查看；table 需已存在（可用 data_list_tables 确认）。
    """
    rows = _local().list_assets(asset_type=asset_type)
    check_list_size(rows, limit=50000, what="待归档资产行")
    if not rows:
        return {"ok": True, "exported": 0, "message": "无资产可归档"}
    result = _call_heavy_internal("/internal/v1/batch/import", json_body={
        "db_type": os.environ.get("MCP_WAREHOUSE_DB_TYPE", "pg"),
        "dsn_ref": dsn_ref,
        "table": table,
        "rows": rows,
    })
    return {"ok": True, "exported": result.get("imported", len(rows))}


@mcp.tool(name="itops_query_warehouse_assets")
def query_warehouse_assets(dsn_ref: str, table: str = "cmdb_assets", asset_type: str | None = None, limit: int = 100) -> dict[str, Any]:
    """查询已归档到数据仓库的资产（内部直调 go_datahub 的
    /internal/v1/batch/query，等值过滤 + 行数上限，防全表拖库）。
    """
    limit = _clamp_limit(limit)
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
# 批处理示例（Python 侧实现，与 Go 侧 data_batch_process 同一套业务名路由约定）：
# 业务 Python 负责轻编排（路由解析 + 任务登记），重活下放重活层内部 REST 执行
# ---------------------------------------------------------------------------
@mcp.tool(name="itops_batch_process")
def batch_process(dataset_id: str, period: str, tenant_id: str = "default",
                  mode: Literal["simulate", "real"] = "simulate") -> dict[str, Any]:
    """按业务数据集名批量处理数据（Python 侧路由实现）。

    与 data_batch_process 共用同一套 env 路由约定（DATASET_*）：本工具在业务层
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


@mcp.tool(name="itops_list_datasets")
def business_list_datasets() -> dict[str, Any]:
    """列出已配置的业务数据集及其路由规则（租户 → dsn_ref、按月分表）。

    调 batch_process 前先用本工具确认 dataset_id / tenant_id 的合法取值。只读。
    """
    return {"ok": True, "datasets": list_datasets()}


# ---------------------------------------------------------------------------
# 第 2 层·领域通用查询（中粒度）：本域数据集枚举由配置驱动 + 配套表发现
# ---------------------------------------------------------------------------
# it_ops 域的数据集枚举：优先取 DATASET_<名称>_DOMAIN=itops 的配置项（改配置即扩枚举，
# 无需改代码）；未配置任何本域数据集时回退到内置示例（incidents / assets）。
ITOPS_DOMAIN = "itops"


def _domain_datasets() -> tuple[str, ...]:
    names = tuple(domain_datasets(ITOPS_DOMAIN))
    return names or ("incidents", "assets")


_ITOPS_DATASET_ENUM = _domain_datasets()
DatasetId = Literal[_ITOPS_DATASET_ENUM]  # type: ignore[valid-type]


def _dataset_enum_doc() -> str:
    """把数据集的中文说明/路由拼成参数描述（配置驱动，AI 看名知义）。"""
    lines = []
    for info in list_datasets():
        if info["dataset"] not in _ITOPS_DATASET_ENUM:
            continue
        desc = info["desc"] or "（未配说明，见 DATASET_*_DESC）"
        pairs = ", ".join(f"租户 {t}→{r}" for t, r in zip(info["tenants"], info["dsn_tenant_refs"]))
        routes = pairs or f"default→{info['dsn_default'] or '（未配置）'}"
        lines.append(f"{info['dataset']}={desc}；库 {info['db_type']}，路由 {routes}")
    return "；".join(lines) or "本域数据集未配置（DATASET_*_DOMAIN=itops），调用会报未配置路由"


# 必填参数带 Annotated 元数据才会进生成的 JSON Schema（可选参数的元数据会被 SDK 丢弃）
DatasetIdDoc = Annotated[DatasetId, Field(description="本域数据集（说明由 DATASET_* 配置生成）："
                                                      + _dataset_enum_doc())]


@mcp.tool(name="itops_query_dataset")
def query_dataset(
    dataset_id: DatasetIdDoc,
    tenant_id: str = "default",
    period: str | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """按本域（IT 运维）数据集做通用查询（第 2 层·领域通用查询）。

    dataset_id 的本域枚举与中文说明见该参数描述（由 DATASET_* 配置动态生成）。
    枚举之外的数据集（如订单）不在本工具范围——请改用第 3 层全局数据平台
    data.query_dataset（全量数据集枚举）。
    period 仅周期数据集需要（按月分表 YYYY-MM）；本域数据集多为固定表，留空即可。
    tenant_id 传 default 表示按兜底路由查（default 是保留字，不作为租户名）。
    """
    _guard_domain(dataset_id)
    db_type, dsn_ref = route_db(dataset_id, tenant_id)
    table = route_table(dataset_id, period) if period else table_of(dataset_id)
    limit = _clamp_limit(limit)
    result = _call_heavy_internal("/internal/v1/batch/query", json_body={
        "db_type": db_type, "dsn_ref": dsn_ref, "table": table,
        "filters": filters or {}, "limit": limit,
    })
    return {
        "ok": True, "dataset_id": dataset_id, "routed": {"db": dsn_ref, "table": table},
        "count": result.get("count", 0), "rows": result.get("rows", []),
        "limit": limit, "truncated": result.get("count", 0) >= limit,
    }


@mcp.tool(name="itops_list_data_tables")
def list_data_tables(dataset_id: DatasetIdDoc, tenant_id: str = "default") -> dict[str, Any]:
    """列出一个本域数据集路由库中的数据表（查询前的发现入口：先看表存在、再用
    itops_query_dataset 查询；越域数据集用 data.list_datasets / data.query_dataset）。

    tenant_id 传 default 表示按兜底路由解析。
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
