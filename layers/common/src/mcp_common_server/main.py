"""【下层】通用工具 MCP server 入口。

提供与业务无关的通用工具（实现见 tools.py），是三层中最底层的基础服务。
运行：
- stdio：  common-server
- HTTP：   common-server --transport http --port 9100
"""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_common_server import tools
from mcp_shared import run_server

mcp = MCPServer("common-tools")


@mcp.tool()
def now(timezone_name: str = "UTC") -> str:
    """返回当前时间。timezone_name 为 IANA 时区名（如 UTC、Asia/Shanghai）。"""
    return tools.now(timezone_name)


@mcp.tool()
def generate_id(prefix: str = "id") -> str:
    """生成全局唯一 ID，例如 generate_id(prefix='ticket') -> ticket_a1b2..."""
    return tools.generate_id(prefix)


@mcp.tool()
def slugify(text: str) -> str:
    """将文本转为小写连字符 slug（用于命名/文件名）。"""
    return tools.slugify(text)


@mcp.tool()
def echo(message: str, uppercase: bool = False) -> str:
    """原样返回消息（用于连通性测试 / 冒烟测试）。"""
    return tools.echo(message, uppercase)


@mcp.tool()
def timestamp() -> int:
    """返回当前 Unix 时间戳（秒）。"""
    return tools.timestamp()


def main() -> None:
    run_server(mcp, description="【下层】通用工具 MCP server", default_port=9100)


if __name__ == "__main__":
    main()
