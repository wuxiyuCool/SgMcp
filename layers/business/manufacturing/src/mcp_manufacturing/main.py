"""【中层·制造】制造系统 MCP server 入口（预留）。

尚未实现业务工具，仅给出可运行骨架；扩展方式参考 it_ops 目录。
"""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_shared import run_server

mcp = MCPServer("manufacturing")


@mcp.tool()
def hello() -> str:
    """制造系统预留占位：返回欢迎信息。"""
    return "制造系统 MCP 预留，暂未接入业务工具。参考 it_ops 目录扩展。"


def main() -> None:
    run_server(mcp, description="【中层·制造】制造系统 MCP server", default_port=9400)


if __name__ == "__main__":
    main()
