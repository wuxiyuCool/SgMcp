"""下游路由与执行调度。

网关把「server + tool」路由到对应下游 server。聚合器（aggregator.py）在启动时
拉取各下游 tools/list 并注册到这里；执行统一走官方 SDK 的 `Client` 以
Streamable HTTP 转发（exec_kind="http"）——网关与业务实现完全解耦，
不再依赖任何业务包的进程内直连。

MCP 直连调用的实现收敛在 `mcp_shared.mcp_client`（网关专用；业务层服务间内部
调用走下游的 /internal/* REST 面，不经网关也不走 MCP），本模块只做转发编排。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from mcp_shared.mcp_client import call_downstream_http

logger = logging.getLogger("mcp_gateway.routing")

__all__ = ["ToolRoute", "Router", "call_downstream_http"]


@dataclass
class ToolRoute:
    server: str
    tool: str
    exec_kind: str  # "local" | "http"
    requires_approval: bool
    summary: str
    endpoint: str | None = None  # exec_kind="http" 时的下游 MCP 地址
    headers: dict[str, str] | None = None  # 跨机调用下游时的附加 HTTP 头（如 Bearer 鉴权）
    input_schema: dict[str, Any] | None = None  # 下游 tools/list 拉取的入参 schema（工具发现用）


class Router:
    """维护已注册的下游路由，并把调用分发到对应执行器。"""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], ToolRoute] = {}

    @property
    def routes(self) -> list[ToolRoute]:
        return list(self._routes.values())

    def register(self, route: ToolRoute) -> None:
        self._routes[(route.server, route.tool)] = route

    def replace_server_routes(self, server: str, routes: list[ToolRoute]) -> None:
        """整体替换某 server 的路由（聚合器每轮 sync 后调用，下游删工具网关同步消失）。"""
        self._routes = {k: v for k, v in self._routes.items() if k[0] != server}
        for route in routes:
            self.register(route)

    def resolve(self, server: str, tool: str) -> ToolRoute | None:
        return self._routes.get((server, tool))

    def execute(self, server: str, tool: str, args: dict[str, Any]) -> Any:
        route = self.resolve(server, tool)
        if route is None:
            raise LookupError(f"未注册的路由: {server}.{tool}")
        if route.exec_kind == "local":
            raise RuntimeError("local 执行器已移除：网关统一走 MCP Streamable HTTP 转发")
        if route.exec_kind == "http":
            if not route.endpoint:
                raise RuntimeError(f"路由 {server}.{tool} 未配置 endpoint")
            logger.info("执行(HTTP) %s.%s -> %s", server, tool, route.endpoint)
            return call_downstream_http(route.endpoint, tool, args, headers=route.headers)
        raise RuntimeError(f"未知执行类型: {route.exec_kind}")
