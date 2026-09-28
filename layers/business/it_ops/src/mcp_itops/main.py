"""【中层·IT运维】ITOM 平台对接 MCP server（业务层 Python）。

本域只保留**真实调用 ITOM 平台 API**（itom.shougang.com.cn）的工具：凭据只存在
服务端 config/platform.env，AI 侧全程只流转 account_ref。试点期的本地 SQLite
工单/变更/资产台账、数据仓库归档示例与批处理演示工具已按工具数量治理规范下线；
库表类数据查询统一走第 3 层全局数据平台（Go go_datahub 的 ``data_*`` 工具）。

工具规范（见 docs/develop-deploy.md「工具清单」）：
- 命名 ``{domain}_{action}_{resource}``，动词取白名单（get/query/list/create/
  submit/delete），全局唯一、永久稳定、不含 UUID/时间戳等动态元素（WeKnora 兼容）；
- description 写明「Use when / Do NOT use when」，错误带 ``[ERROR_CODE]`` 前缀
  与下一步建议（hint）；
- 读/写分离：写工具 annotations.readOnlyHint=false，写透传与建单在网关默认
  审批名单（MCP_APPROVAL_TOOLS）内。

运行：
- stdio：  itops-server
- HTTP：   itops-server --transport http --port 9200
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations

from mcp_itops import itom
from mcp_shared import run_server

mcp = MCPServer("it-ops")

_MAX_LIMIT = 200  # options 候选数上限：防整表下拉灌进对话上下文

# ---------------------------------------------------------------------------
# 工具注解（文档规范：读/写必须分离，写工具显式 readOnlyHint=false）
# ---------------------------------------------------------------------------
_RO = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True,
    open_world_hint=True,
)
_WRITE = ToolAnnotations(  # 通用写透传：可能改动生产数据，标记为潜在破坏性
    read_only_hint=False, destructive_hint=True, idempotent_hint=False,
    open_world_hint=True,
)
_CREATE = ToolAnnotations(  # 新建单据：只做增量写入，非破坏性
    read_only_hint=False, destructive_hint=False, idempotent_hint=False,
    open_world_hint=True,
)
_SESSION = ToolAnnotations(  # 会话管理：只清进程内缓存，不触外部平台
    read_only_hint=False, destructive_hint=False, idempotent_hint=True,
    open_world_hint=False,
)


def _clamp_limit(limit: int) -> int:
    """候选行数上限：AI 传 100000 也只会拿到 200。"""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return 50
    return max(1, min(n, _MAX_LIMIT))


def _itom(fn, *args, **kwargs):
    """itom.ItomError → ToolError：错误消息带 [ERROR_CODE] 前缀与可执行建议。"""
    try:
        return fn(*args, **kwargs)
    except itom.ItomError as e:
        raise ToolError(str(e)) from e


# ---------------------------------------------------------------------------
# 账户与会话（发现入口：先列账户拿 account_ref，再调业务工具）
# ---------------------------------------------------------------------------
@mcp.tool(name="itops_list_itom_accounts", title="列出 ITOM 账户",
          annotations=_RO)
def list_itom_accounts() -> list[dict[str, Any]]:
    """列出服务端已配置的 ITOM 账户（引用名、备注、打码工号、登录状态）。

    Use when：调任何 itops_*_itom_* 工具前不知道 account_ref 该填什么，先来
    这里挑一个引用名（把 account_ref 列给用户选，或按备注挑最合适的）。
    Do NOT use when：想排查配置/会话问题——改用 itops_get_itom_status。
    口令永不出现在返回里，也不要向用户索要口令：凭据只存在服务端配置。
    返回示例：[{"account_ref": "wangxu", "note": "运维中心", "user_id": "w***u",
    "credential": "password", "logged_in": false}]
    """
    return _itom(itom.accounts_summary)


@mcp.tool(name="itops_get_itom_status", title="ITOM 对接自检",
          annotations=_RO)
def get_itom_status() -> dict[str, Any]:
    """ITOM 对接自检：配置文件实际路径、账户清单、uid/token 注入形态、会话状态。

    Use when：调 ITOM 工具报「登录信息发生变化」(retCode=0200000)、配置改了
    不生效、或想确认账户是否处于登录态时，先调本工具——它会直接告诉你配置
    加载自哪里、缺哪个开关（hint 字段）。会话由服务端自动管理（TTL 内复用、
    失效自动重登），日常调用不需要手动登录。
    Do NOT use when：只想知道有哪些账户可用——itops_list_itom_accounts 更直接。
    只回「有没有/什么形态」，绝不回任何凭据值。
    """
    return _itom(itom.selfcheck)


@mcp.tool(name="itops_delete_itom_sessions", title="清除 ITOM 会话",
          annotations=_SESSION)
def delete_itom_sessions(account_ref: str | None = None) -> dict[str, Any]:
    """清除服务端缓存的 ITOM 登录会话（换账号、口令修改后、或排查平台侧 session）。

    Use when：配置里的口令改了、平台提示会话冲突、或想强制下一次调用重新登录。
    Do NOT use when：只是常规调用——会话过期会自动重登，无需手动清理。
    参数 account_ref 留空=清掉全部会话；只影响本进程缓存，不注销平台侧登录。
    """
    cleared = _itom(itom.reset, account_ref) if account_ref else itom.reset()
    return {"ok": True, "cleared": cleared}


# ---------------------------------------------------------------------------
# 平台透传（通用入口：只读走 query，写操作走 submit 并挂审批）
# ---------------------------------------------------------------------------
@mcp.tool(name="itops_query_itom", title="ITOM 只读查询透传",
          annotations=_RO)
def query_itom(account_ref: str, path: str, params: dict[str, Any] | None = None,
               method: Literal["GET", "POST"] = "GET",
               body: dict[str, Any] | None = None,
               fields: list[str] | None = None, max_rows: int = 0) -> dict[str, Any]:
    """带 ITOM 登录态发只读请求（path 只能是站内相对路径）。

    Use when：已知平台的某个查询接口路径（如 /event-manage/findByPage），
    且没有对应的专用业务工具时，用它直接查。查事件单列表请优先用
    itops_query_itom_incidents（已带字段投影与码值翻译）。
    Do NOT use when：要写数据（建单/流转/更新）——必须走 itops_submit_itom
    （经网关需人工审批），本工具拒绝一切写形态路径。
    参数：
    - account_ref: 取自 itops_list_itom_accounts
    - path: 站内相对路径（自动拼到 MCP_ITOM_URL 后；禁止 //、..、查询串）
    - params: 查询参数；登录态（uid/token/cookie）由服务端自动注入，不要自己传
    - method: GET 或 POST。平台的分页查询用 POST 承载只读语义，所以允许 POST，
      但路径末段必须是只读形态（find/get/list/query/search/view 等），否则拒绝
    - body: POST 的业务字段，只写内层（服务端自动补 reqHeader/reqBody 信封）
    - fields: 字段白名单。平台列表一行 90+ 列（含报障人手机号），不投影就是
      几百 KB 灌进对话
    - max_rows: 返回行数上限（0=不限），超出会在 truncated 里提示怎么翻页
    返回 {"ok": true, "status": 200, "retCode": "0000000", "data": ...}；
    列表类响应会归一成 {"rows", "returned", "total", "rows_key"}。
    """
    return _itom(itom.request_json, account_ref, method, path, params=params,
                 body=body, read_channel=True, fields=fields, max_rows=max_rows)


@mcp.tool(name="itops_submit_itom", title="ITOM 写操作透传（需审批）",
          annotations=_WRITE)
def submit_itom(account_ref: str, path: str,
                method: Literal["POST", "PUT", "PATCH", "DELETE"] = "POST",
                body: dict[str, Any] | None = None,
                params: dict[str, Any] | None = None) -> dict[str, Any]:
    """向 ITOM 平台发起写操作（建单/流转/更新），经网关调用会被挂起为审批单。

    Use when：需要调用平台的写接口（如 /event-manage/updateEvent 流转工单），
    且该操作没有专用业务工具时。新建事件单请优先用 itops_create_itom_incident
    （带引导式字段解析与确认预览）。
    Do NOT use when：只读查询（findByPage 这类）——用 itops_query_itom，
    不必走审批。
    参数同 itops_query_itom：body 只写业务字段（服务端补 reqHeader/reqBody
    信封），登录态不用自己传。
    ⚠️ 该工具会真实改动生产 ITOM 数据，在 MCP_APPROVAL_TOOLS 默认名单内：
    经网关调用时返回 request_id="apr_xxx" 且尚未执行，需审批人 approve_request
    才落地。
    """
    return _itom(itom.request_json, account_ref, method, path, params=params, body=body)


# ---------------------------------------------------------------------------
# 业务工具（高频场景的专用封装：投影、码值翻译、引导式建单）
# ---------------------------------------------------------------------------
@mcp.tool(name="itops_query_itom_incidents", title="查询 ITOM 事件单",
          annotations=_RO)
def query_itom_incidents(account_ref: str, days: int = 7, system_type: str | None = None,
                         event_no: str | None = None, page_num: int = 1,
                         page_size: int = 20) -> dict[str, Any]:
    """查 ITOM 事件单列表（真实生产数据），已按业务列投影，不含手机号等个人信息。

    Use when：用户问「最近的 IT 事件单/工单」「某系统这周报了多少障」
    「事件单 SG260927001 的详情」这类查询。码值都带中文含义（xxxName 字段），
    直接引用中文含义，不要自己解读数字码。
    Do NOT use when：查本域之外的数据（订单/库存等）——用 data_query_dataset。
    参数：
    - account_ref: 取自 itops_list_itom_accounts
    - days: 回溯天数（平台按创建时间过滤，默认 7 天；不给时间窗平台不返回数据）
    - system_type: 系统类型代码（如 "SG062"=采购管理系统）
    - event_no: 事件单号精确查（如 "SG260927001"）
    - page_num / page_size: 分页，page_size 上限 100
    返回 data={"rows":[{eventNo,eventState,eventStateName,eventLevel,eventLevelName,
    problemDesc,systemTypeName,dealStaffName,createdDt,responseTime,solveTime,...}],
    "returned":20,"total":120}。
    注意：平台的 eventState 过滤入参无效（传任何值都是 0 行），problemDesc 只做
    整句精确匹配——状态统计请对返回的 rows 自行分组，别指望按状态过滤。
    """
    return _itom(itom.incidents, account_ref, days=days, system_type=system_type,
                 event_no=event_no, page_num=page_num, page_size=page_size)


@mcp.tool(name="itops_list_itom_options", title="列出 ITOM 下拉候选",
          annotations=_RO)
def list_itom_options(account_ref: str, kind: str, parent: str | None = None,
                      keyword: str | None = None, limit: int = 50,
                      role: str | None = None) -> list[dict[str, Any]]:
    """列出 ITOM 下拉框的候选项（精简投影，不含身份证等敏感列）。

    Use when：引导式建单流程中，按 dept → group → system → subclass → menu 的
    父子顺序逐级列候选给用户选；或查报修人/处理人档案、码值字典。
    kind 取值：
    - form：返回「新增事件单」表单说明书（每个必填字段的含义与引导顺序）
    - dept 部门分类 / group 小组 / system 系统分类 / subclass 系统子类 /
      menu 报修菜单 / custom 报修人档案 / staff 处理人
    - level|nature|role|state|category|solveMode 码值字典
    参数：
    - parent: 上一级选中的代码。group 传部门 orgCode，system 传 groupCode，
      subclass/menu 传上级 sysSid，staff 传一级系统的 sysSid（传子类 sid 查不到人）
    - keyword: 名称过滤（custom 是平台精确匹配；同名多条会都返回）
    - role: 仅 kind=staff 必填（1 一线/2 二线/3 系统负责人），平台按系统+角色取人
    把返回的 name 列给用户选，选中项的 code/sid 再传给 itops_create_itom_incident。
    """
    return _itom(itom.options, account_ref, kind, parent=parent, keyword=keyword,
                 limit=_clamp_limit(limit), role=role)


@mcp.tool(name="itops_create_itom_incident", title="新增 ITOM 事件单（需审批）",
          annotations=_CREATE)
def create_itom_incident(account_ref: str, dept: str, group: str, system: str, subclass: str,
                         menu: str, reporter: str, level: str, nature: str, role: str,
                         handler: str, description: str, reporter_phone: str | None = None,
                         reporter_hint: str | None = None, handler_hint: str | None = None,
                         repair_dept_code: str | None = None,
                         confirm: bool = False) -> dict[str, Any]:
    """新增 ITOM 事件单。默认只返回预览不提交，用户确认后再带 confirm=true 调用。

    Use when：用户明确要报修/建事件单时。正确流程：先 itops_list_itom_options
    (kind="form") 拿字段说明书，再按 dept → group → system → subclass → menu 的
    父子顺序逐级列候选给用户选，最后调本工具。
    各参数传用户说的名称（也可传代码），服务端逐级解析成 code/sid 后拼请求体：
    dept 部门分类、group 小组、system 系统分类、subclass 系统子类、menu 报修菜单、
    reporter 报修人姓名（自动回填电话/报修部门/作业区/岗位）、level 事件等级
    （可传"低"）、nature 事件性质（可传"故障"）、role 人员角色、handler 处理人
    姓名或工号（自动回填处理人电话）、description 问题描述。
    Do NOT use when：用户只是询问/查询事件单——用 itops_query_itom_incidents。
    平台有重名数据：解析到多条会直接报错并列出候选，把候选念给用户确认后用
    reporter_hint/handler_hint 传部门等区分信息重试，不要猜值或取第一条。
    返回 {ok, preview:true, resolved, payload} 表示等待确认；confirm=true 才真实
    建单（经网关调用还会先走人工审批）。缺必填（如报修人无档案电话）会返回
    引导性错误。
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


def main() -> None:
    run_server(mcp, description="【中层·IT运维】ITOM 平台对接 MCP server", default_port=9200)


if __name__ == "__main__":
    main()
