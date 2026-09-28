"""网关聚合器：启动时作为 MCP Client 连各下游 server，拉 tools/list 合并成本地工具表。

对 AI 客户端而言网关是唯一 MCP 入口；对下游而言网关是普通 MCP 客户端。
聚合流程（对应架构：AI → 网关 → 聚合器 → 各中层 MCP）：

1. 启动时（`Aggregator.sync`）逐个连接下游，`tools/list` 拉取工具清单；
2. 合并进 `Router` 的路由表（exec_kind="http"，转发到对应下游）；
3. 某个下游未就绪时在重试窗口内等待（适配「脚本同时拉起全部 server」的启动竞态），
   窗口耗尽仍不可达则跳过并告警——网关照常启动，可用 `refresh_routes` 运行时补拉。

下游注册表配置（env，均可写入 config/platform.env）：
- ``MCP_DOWNSTREAMS``：逗号分隔的 ``name=url``，如
  ``it_ops=http://127.0.0.1:9200/mcp,common-tools=http://127.0.0.1:9100/mcp``。
  未设置时用默认注册表（it_ops + common-tools 本机约定端口 + go_datahub）。
- ``MCP_GODATAHUB_URL`` / ``MCP_GODATAHUB_TOKEN``：go_datahub 兼容入口，
  URL 覆盖默认地址，TOKEN 生成 Bearer 头（跨机部署用）。
- ``MCP_APPROVAL_TOOLS``：逗号分隔、需要 HITL 审批的**工具名**（跨 server 生效）；
  未设置用默认集合（create_change / submit_collect_job）。
- ``MCP_DISCOVERY_RETRY_SECONDS``：启动发现的重试窗口秒数（默认 6，0=只试一次）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable

from mcp_gateway.routing import Router, ToolRoute

logger = logging.getLogger("mcp_gateway.aggregator")

# 单个下游 tools/list 发现的超时（秒）：下游半死时不能拖住网关启动
DISCOVERY_TIMEOUT = float(os.environ.get("MCP_DISCOVERY_TIMEOUT_SECONDS", "10"))

# 默认下游注册表：中层 it_ops + 下层 common（Go 重活 go_datahub 由兼容入口单独追加）
DEFAULT_DOWNSTREAMS = (
    "it_ops=http://127.0.0.1:9200/mcp,common-tools=http://127.0.0.1:9100/mcp"
)

# 默认需审批工具（工具名带层级前缀：itops_=领域工具 / data_=全局数据平台；
# 匹配时点号会先归一为下划线，兼容旧写法；可用 MCP_APPROVAL_TOOLS 覆盖）
DEFAULT_APPROVAL_TOOLS = frozenset({
    "itops_create_change", "data_submit_collect_job",
    # 写真实生产 ITOM 数据的透传口（只读的 itops_itom_get 不在名单内）
    "itops_itom_call", "itops_itom_create_incident",
})


@dataclass
class DownstreamSpec:
    """一个下游 MCP server 的连接描述。"""

    name: str
    url: str
    headers: dict[str, str] | None = None  # 跨机部署的 Bearer 鉴权头等


@dataclass
class SyncReport:
    """一次聚合的结果：成功 server 的工具数 + 失败 server 的原因。"""

    ok: dict[str, int] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:  # 便于启动日志直接打印
        ok = ", ".join(f"{k}={v} 工具" for k, v in sorted(self.ok.items())) or "无"
        failed = ", ".join(f"{k}({v})" for k, v in sorted(self.failed.items())) or "无"
        return f"成功: {ok}; 失败: {failed}"


def requires_approval(tool: str, server: str | None = None) -> bool:
    """审批策略：MCP_APPROVAL_TOOLS 显式指定则完全以其为准，否则用默认集合。

    条目支持三种写法（逗号分隔，比对前把点号归一为下划线）：
    - `itops_create_change`　完整工具名
    - `data_batch_*`　　　　 前缀通配（整族写操作一次纳管）
    - `go_datahub:data_submit_collect_job`　带 server 限定（同名工具跨域差异化策略）

    比对前把点号归一为下划线（部分平台序列化参数值中的 "." 会损坏，
    全平台工具名已改用下划线前缀；此处兼容历史点号写法）。
    """
    import fnmatch

    canon = tool.replace(".", "_")
    raw = os.environ.get("MCP_APPROVAL_TOOLS")
    if raw is None:
        return canon in DEFAULT_APPROVAL_TOOLS
    for entry in (t.strip().replace(".", "_") for t in raw.split(",") if t.strip()):
        scope, _, pattern = entry.rpartition(":")
        if not pattern:  # 无 ":" 限定 → 整条即模式
            scope, pattern = "", entry
        if scope and server and scope != _norm_server(server):
            continue
        if "*" in pattern or "?" in pattern:
            if fnmatch.fnmatch(canon, pattern):
                return True
        elif canon == pattern:
            return True
    return False


def _norm_server(server: str) -> str:
    return server.strip().lower().replace("-", "_")


def _headers_for(server: str, token: str | None = None) -> dict[str, str] | None:
    """下游鉴权头：`MCP_DOWNSTREAM_TOKEN_<SERVER 大写>` 优先，其次显式 token。

    网关→下游的 Bearer 与 Go 侧 GO_DATAHUB_TOKEN 同一约定：下游配了 token，
    网关不配就会以 401 聚合失败（list_downstreams 里可见原因）。
    """
    raw = os.environ.get(f"MCP_DOWNSTREAM_TOKEN_{server.strip().upper().replace('-', '_')}") or token
    return {"Authorization": f"Bearer {raw}"} if raw else None


def downstream_specs_from_env() -> list[DownstreamSpec]:
    """解析下游注册表。显式设置 MCP_DOWNSTREAMS 时完全以其为准（可随意增删 server）。"""
    specs: list[DownstreamSpec] = []
    raw = os.environ.get("MCP_DOWNSTREAMS", "").strip()
    source = raw if raw else DEFAULT_DOWNSTREAMS
    for pair in source.split(","):
        pair = pair.strip()
        if not pair:
            continue
        name, sep, url = pair.partition("=")
        if not sep or not name.strip() or not url.strip():
            raise ValueError(f"MCP_DOWNSTREAMS 条目格式应为 name=url，实为: {pair!r}")
        specs.append(DownstreamSpec(name=name.strip(), url=url.strip(),
                                    headers=_headers_for(name.strip())))

    # go_datahub 兼容入口：未显式配置时按 MCP_GODATAHUB_URL/TOKEN 追加（保持既有部署习惯）
    godh_url = os.environ.get("MCP_GODATAHUB_URL", "http://127.0.0.1:9300/mcp")
    godh_token = os.environ.get("MCP_GODATAHUB_TOKEN")
    existing = next((s for s in specs if s.name == "go_datahub"), None)
    if existing is None:
        if not raw:  # 未显式指定 MCP_DOWNSTREAMS → 追加默认 go_datahub
            specs.append(DownstreamSpec("go_datahub", godh_url,
                                        _headers_for("go_datahub", godh_token)))
    elif existing.headers is None and godh_token:
        existing.headers = {"Authorization": f"Bearer {godh_token}"}
    return specs


class Aggregator:
    """维护「连接下游 → 拉取 tools/list → 合并进路由表」的聚合器。

    `on_sync` 回调在每轮 sync 结束后调用（无论成败），供网关重建对 AI 暴露的
    聚合路由工具（route_*）——工具枚举与描述必须跟随最新路由表。
    """

    def __init__(self, router: Router, specs: list[DownstreamSpec] | None = None) -> None:
        self.router = router
        self.specs = specs if specs is not None else downstream_specs_from_env()
        self.on_sync: Callable[[], None] | None = None
        self.last_report = SyncReport()
        self.last_sync_at: float | None = None

    # ---- 发现 ----
    async def _list_downstream(self, spec: DownstreamSpec,
                               status_sink: list[int] | None = None) -> list:
        """连一个下游拉 tools/list（返回 mcp.types.Tool 列表）。失败抛异常由调用方记录。"""
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client
        from mcp_shared.mcp_client import new_http_client

        # mode 必须显式 legacy：默认 auto 的探测/回退竞态会把 URL 编码进请求路径（见 routing.py 注释）
        # 发现同样要设超时——下游半死时不能让网关启动/refresh_routes 卡死
        transport = streamable_http_client(
            spec.url, http_client=new_http_client(spec.headers, DISCOVERY_TIMEOUT, status_sink)
        )
        client = Client(transport, raise_exceptions=True, mode="legacy")

        async with client:
            result = await client.list_tools()
        return list(result.tools)

    @staticmethod
    def header_config_error(spec: "DownstreamSpec") -> str | None:
        """请求头值必须是 ASCII：混进中文（通常是行内注释被当成值）时提前报错。

        不拦的话异常是 `UnicodeEncodeError: 'ascii' codec can't encode ...`，
        分类只会给出"下游返回了无法处理的响应"，把人往完全错误的方向带。
        """
        for key, val in (spec.headers or {}).items():
            try:
                val.encode("ascii")
            except UnicodeEncodeError:
                bad = "".join(ch for ch in val if ord(ch) > 127)[:8]
                return (f"config_error: 下游 {spec.name} 的请求头 {key} 含非 ASCII 字符（{bad!r}…）——"
                        f"多半是把中文注释写进了 config/platform.env 的值里；"
                        f"请把注释单独放一行后重启网关")
        return None

    def describe_failure(self, exc: BaseException, spec: "DownstreamSpec",
                         status_sink: list[int] | None = None) -> str:
        """聚合失败转成人能照着做的一句话（含 kind 与真实 HTTP 状态），而不是裸 ExceptionGroup。"""
        from mcp_shared.mcp_client import classify_error

        kind, hint, _ = classify_error(exc, spec.url, "tools/list", status_sink)
        return f"{kind}: {type(exc).__name__}（{spec.url}）——{hint}"

    def sync(self, retry_seconds: float | None = None) -> SyncReport:
        """对所有下游做一轮聚合（带重试窗口），把工具表合并进路由表。

        每个成功聚合的 server，其旧路由被**整体替换**（下游删了工具，网关同步消失）；
        聚合失败的 server 保留上一轮的路由不动（运行时 refresh 可恢复）。
        """
        if retry_seconds is None:
            retry_seconds = float(os.environ.get("MCP_DISCOVERY_RETRY_SECONDS", "6"))
        deadline = time.monotonic() + max(retry_seconds, 0.0)

        report = SyncReport()
        pending = list(self.specs)
        while True:
            still_pending: list[DownstreamSpec] = []
            for spec in pending:
                bad_header = self.header_config_error(spec)
                if bad_header:
                    logger.error("%s", bad_header)
                    still_pending.append(spec)
                    report.failed[spec.name] = bad_header
                    continue
                status_sink: list[int] = []
                try:
                    tools = asyncio.run(self._list_downstream(spec, status_sink))
                except Exception as e:  # noqa: BLE001 — 单个下游失败不能拖垮整体聚合
                    logger.warning("下游 %s (%s) 聚合失败: %s", spec.name, spec.url, e)
                    still_pending.append(spec)
                    report.failed[spec.name] = self.describe_failure(e, spec, status_sink)
                    continue
                report.failed.pop(spec.name, None)
                report.ok[spec.name] = len(tools)
                self._apply(spec, tools)
                logger.info("聚合 %s: %d 个工具 (%s)", spec.name, len(tools), spec.url)
            pending = still_pending
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(1.0)  # 窗口内每秒重试未就绪的下游

        if pending:
            logger.warning(
                "重试窗口耗尽，未聚合的下游: %s（网关继续启动，可稍后调用 refresh_routes 补拉）",
                ", ".join(s.name for s in pending),
            )
        self.last_report = report
        self.last_sync_at = time.time()
        if self.on_sync is not None:
            try:
                self.on_sync()
            except Exception:  # noqa: BLE001 — 回调失败不影响聚合结果
                logger.exception("on_sync 回调失败（route_* 工具可能未刷新）")
        return report

    # ---- 合并 ----
    def _apply(self, spec: DownstreamSpec, tools: list) -> None:
        routes = []
        for t in tools:
            desc = (t.description or "").strip()
            summary = desc.splitlines()[0] if desc else f"{spec.name}.{t.name}"
            ann = getattr(t, "annotations", None)
            routes.append(
                ToolRoute(
                    server=spec.name,
                    tool=t.name,
                    exec_kind="http",
                    requires_approval=requires_approval(t.name, spec.name),
                    summary=summary,
                    endpoint=spec.url,
                    headers=spec.headers,
                    input_schema=t.input_schema,
                    read_only=bool(getattr(ann, "readOnlyHint", False)),
                    open_world=getattr(ann, "openWorldHint", None),
                )
            )
        self.router.replace_server_routes(spec.name, routes)

    # ---- 供 list_routes / 诊断用 ----
    def downstream_status(self) -> list[dict[str, str]]:
        """各下游的聚合健康度（供网关 list_downstreams 诊断）。"""
        out = []
        for s in self.specs:
            agg = self.last_report.ok.get(s.name)
            item = {"name": s.name, "url": s.url,
                    "tools_aggregated": agg if agg is not None else 0,
                    "status": "ok" if agg is not None else (
                        f"failed: {self.last_report.failed.get(s.name)}" if s.name in self.last_report.failed
                        else "never_synced")}
            if s.headers:
                item["auth"] = "bearer"
            out.append(item)
        return out
