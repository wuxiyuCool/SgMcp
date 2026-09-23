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

from mcp_gateway.routing import Router, ToolRoute

logger = logging.getLogger("mcp_gateway.aggregator")

# 默认下游注册表：中层 it_ops + 下层 common（Go 重活 go_datahub 由兼容入口单独追加）
DEFAULT_DOWNSTREAMS = (
    "it_ops=http://127.0.0.1:9200/mcp,common-tools=http://127.0.0.1:9100/mcp"
)

# 默认需审批工具（工具名带层级前缀：itops.*=领域工具 / data.*=全局数据平台；
# 可用 MCP_APPROVAL_TOOLS 覆盖）
DEFAULT_APPROVAL_TOOLS = frozenset({"itops.create_change", "data.submit_collect_job"})


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


def requires_approval(tool: str) -> bool:
    """审批策略：MCP_APPROVAL_TOOLS 显式指定则完全以其为准，否则用默认集合。"""
    raw = os.environ.get("MCP_APPROVAL_TOOLS")
    if raw is not None:
        return tool in {t.strip() for t in raw.split(",") if t.strip()}
    return tool in DEFAULT_APPROVAL_TOOLS


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
        specs.append(DownstreamSpec(name=name.strip(), url=url.strip()))

    # go_datahub 兼容入口：未显式配置时按 MCP_GODATAHUB_URL/TOKEN 追加（保持既有部署习惯）
    godh_url = os.environ.get("MCP_GODATAHUB_URL", "http://127.0.0.1:9300/mcp")
    godh_token = os.environ.get("MCP_GODATAHUB_TOKEN")
    godh_headers = {"Authorization": f"Bearer {godh_token}"} if godh_token else None
    existing = next((s for s in specs if s.name == "go_datahub"), None)
    if existing is None:
        if not raw:  # 未显式指定 MCP_DOWNSTREAMS → 追加默认 go_datahub
            specs.append(DownstreamSpec("go_datahub", godh_url, godh_headers))
    elif existing.headers is None:
        existing.headers = godh_headers
    return specs


class Aggregator:
    """维护「连接下游 → 拉取 tools/list → 合并进路由表」的聚合器。"""

    def __init__(self, router: Router, specs: list[DownstreamSpec] | None = None) -> None:
        self.router = router
        self.specs = specs if specs is not None else downstream_specs_from_env()

    # ---- 发现 ----
    async def _list_downstream(self, spec: DownstreamSpec) -> list:
        """连一个下游拉 tools/list（返回 mcp.types.Tool 列表）。失败抛异常由调用方记录。"""
        from mcp import Client

        if not spec.headers:
            client = Client(spec.url, raise_exceptions=True, mode="legacy")
        else:
            import httpx2
            from mcp.client.streamable_http import streamable_http_client

            # mode 必须显式 legacy：默认 auto 的探测/回退竞态会把 URL 编码进请求路径（见 routing.py 注释）
            transport = streamable_http_client(
                spec.url, http_client=httpx2.AsyncClient(headers=spec.headers)
            )
            client = Client(transport, raise_exceptions=True, mode="legacy")

        async with client:
            result = await client.list_tools()
        return list(result.tools)

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
                try:
                    tools = asyncio.run(self._list_downstream(spec))
                except Exception as e:  # noqa: BLE001 — 单个下游失败不能拖垮整体聚合
                    logger.warning("下游 %s (%s) 暂不可达: %s", spec.name, spec.url, e)
                    still_pending.append(spec)
                    report.failed[spec.name] = f"{type(e).__name__}: {e}"
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
        return report

    # ---- 合并 ----
    def _apply(self, spec: DownstreamSpec, tools: list) -> None:
        routes = []
        for t in tools:
            desc = (t.description or "").strip()
            summary = desc.splitlines()[0] if desc else f"{spec.name}.{t.name}"
            routes.append(
                ToolRoute(
                    server=spec.name,
                    tool=t.name,
                    exec_kind="http",
                    requires_approval=requires_approval(t.name),
                    summary=summary,
                    endpoint=spec.url,
                    headers=spec.headers,
                    input_schema=t.input_schema,
                )
            )
        self.router.replace_server_routes(spec.name, routes)

    # ---- 供 list_routes / 诊断用 ----
    def downstream_status(self) -> list[dict[str, str]]:
        return [{"name": s.name, "url": s.url} for s in self.specs]
