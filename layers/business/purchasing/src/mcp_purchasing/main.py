"""【中层·采购】采购系统 MCP server 入口（预留）。

尚未接入业务工具：测试用占位工具已按《MCP 服务开发规范》移除——
没有业务价值的工具不该进聚合目录（AI 会看到并可能误调）。
扩展方式参考 it_ops 目录（命名 {domain}_{action}_{resource}、写工具挂审批、每域 ≤ 8 个）。
"""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_shared import run_server

mcp = MCPServer("purchasing")


def main() -> None:
    run_server(mcp, description="【中层·采购】采购系统 MCP server", default_port=9300)


if __name__ == "__main__":
    main()
