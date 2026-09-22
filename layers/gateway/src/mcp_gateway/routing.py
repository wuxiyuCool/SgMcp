"""下游路由与执行调度。

网关把「server + tool」路由到对应下游 server，并通过执行器真正调用：
- exec_kind="local"：本进程内直接调用业务逻辑（试点用，见 local_executors）。
- exec_kind="http"：通过官方 SDK 的 Client 以 Streamable HTTP 转发到独立部署的
  下游 server（生产用）。路由需配置 endpoint，如 http://127.0.0.1:9200/mcp。

试点说明：local 执行器延迟导入 it_ops / common 的业务模块，因此网关包声明了
对 mcp-itops-server / mcp-common-server 的依赖；生产切到 HTTP 转发后可移除。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger("mcp_gateway.routing")


@dataclass
class ToolRoute:
    server: str
    tool: str
    exec_kind: str  # "local" | "http"
    requires_approval: bool
    summary: str
    endpoint: str | None = None  # exec_kind="http" 时的下游 MCP 地址


class Router:
    """维护已注册的下游路由，并把调用分发到对应执行器。"""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], ToolRoute] = {}
        self._executors: dict[str, Callable[..., Any]] = {}

    @property
    def routes(self) -> list[ToolRoute]:
        return list(self._routes.values())

    def register(self, route: ToolRoute) -> None:
        self._routes[(route.server, route.tool)] = route

    def register_executor(self, server: str, fn: Callable[..., Any]) -> None:
        """注册某 server 的本地执行器（exec_kind="local" 时用）。"""
        self._executors[server] = fn

    def resolve(self, server: str, tool: str) -> ToolRoute | None:
        return self._routes.get((server, tool))

    def execute(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        route = self.resolve(server, tool)
        if route is None:
            raise LookupError(f"未注册的路由: {server}.{tool}")
        if route.exec_kind == "local":
            fn = self._executors.get(server)
            if fn is None:
                raise RuntimeError(f"server {server} 未注册本地执行器")
            logger.info("执行(本地) %s.%s args=%s", server, tool, args)
            return fn(tool, **args)
        if route.exec_kind == "http":
            if not route.endpoint:
                raise RuntimeError(f"路由 {server}.{tool} 未配置 endpoint")
            logger.info("执行(HTTP) %s.%s -> %s", server, tool, route.endpoint)
            return call_downstream_http(route.endpoint, tool, args)
        raise RuntimeError(f"未知执行类型: {route.exec_kind}")


def call_downstream_http(endpoint: str, tool: str, arguments: dict[str, Any]) -> Any:
    """通过 MCP Streamable HTTP 调用下游 server 工具（同步桥接）。

    SDK v2 的 sync 工具运行在 worker 线程（无事件循环），这里用 asyncio.run
    起临时 loop 驱动官方 Client；工具内不要在已有 loop 的线程调用本函数。

    mode 必须显式传 "legacy"，不能用默认的 "auto"：
    auto 会先探测 server/discover，对「老握手」server 探测失败后回退 initialize，
    而这两个请求是背靠背（pipelined）发出的。实测在 streamable-HTTP 传输上，
    回退的 initialize 会把完整 URL 百分号编码进请求路径
    （POST http%3A//127.0.0.1%3A9200/mcp → 404 Not Found），导致连接失败。
    见 SDK mcp/client/_probe.py:negotiate_auto 的注释（该竞态 SDK 自身有记录）。
    本平台三层全部使用老握手协议，并不存在 2026-07-28 现代协议，
    因此显式 legacy 既规避竞态、也省掉一次无意义的探测请求。
    """
    from mcp import Client

    async def _call() -> Any:
        async with Client(endpoint, raise_exceptions=True, mode="legacy") as client:
            result = await client.call_tool(tool, arguments)
        if result.is_error:
            raise RuntimeError(f"下游 {endpoint}.{tool} 执行失败: {result.content}")
        if result.structured_content is not None:
            return result.structured_content
        return [b.text for b in result.content if getattr(b, "type", "") == "text"]

    return asyncio.run(_call())


# ---------------------------------------------------------------------------
# 本地执行器：直接调用各层业务逻辑（试点用，模拟「真实业务系统被网关调用」）
# ---------------------------------------------------------------------------
def _itops_local_executor(tool: str, **kwargs: Any) -> Any:
    # 延迟导入，保持网关与业务实现解耦。
    # 注意走 main.py 的工具函数层（而非 store）——默认值与参数校验都在这一层，
    # 与 exec_kind="http" 时下游 server 的行为保持一致。
    from mcp_itops import main as itops_server

    fn = getattr(itops_server, tool, None)
    if fn is None:
        raise LookupError(f"it_ops 无此工具: {tool}")
    return fn(**kwargs)


def _common_local_executor(tool: str, **kwargs: Any) -> Any:
    from mcp_common_server import tools  # 复用下层通用工具逻辑

    fn = {
        "now": tools.now,
        "timestamp": tools.timestamp,
        "generate_id": tools.generate_id,
        "slugify": tools.slugify,
        "echo": tools.echo,
    }.get(tool)
    if fn is None:
        raise LookupError(f"common-tools 无此工具: {tool}")
    return fn(**kwargs)


def install_local_executors(router: Router) -> Router:
    router.register_executor("it_ops", _itops_local_executor)
    router.register_executor("common-tools", _common_local_executor)
    return router
