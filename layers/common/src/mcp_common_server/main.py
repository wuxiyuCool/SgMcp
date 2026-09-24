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
    """返回当前时间（ISO 8601 字符串，如 "2026-09-24T09:30:00+08:00"）。

    参数 timezone_name：IANA 时区名字符串，常用值：
    "Asia/Shanghai"（北京时间）、"UTC"（默认）、"America/New_York"。
    无效时区名会报错，请严格使用上述格式（勿传 "北京"/"GMT+8" 之类）。
    """
    return tools.now(timezone_name)


@mcp.tool()
def generate_id(prefix: str = "id") -> str:
    """生成全局唯一 ID，返回字符串，形如 "ticket_a1b2c3d4e5f6"。

    参数 prefix：ID 前缀（小写英文为佳），如 "order" → "order_a1b2c3d4e5f6"。
    每次调用结果必然不同，可用于幂等键/追踪号。
    """
    return tools.generate_id(prefix)


@mcp.tool()
def slugify(text: str) -> str:
    """把任意文本转为小写连字符 slug（用于命名/文件名/URL）。

    例："Deploy API v2!" → "deploy-api-v2"。仅保留字母数字，其余字符折叠为单个 "-"。
    注意：中文会被整体去除（结果可能为空串），中文命名请自行给拼音。
    """
    return tools.slugify(text)


@mcp.tool()
def echo(message: str, uppercase: bool = False) -> str:
    """原样返回消息。用于连通性/冒烟测试。

    参数：message=要回显的文本（必填）；uppercase=true 时返回其大写形式。
    例：{"message": "abc", "uppercase": true} → "ABC"。
    """
    return tools.echo(message, uppercase)


@mcp.tool()
def timestamp() -> int:
    """返回当前 Unix 时间戳（秒，整数）。无参数。"""
    return tools.timestamp()


def main() -> None:
    run_server(mcp, description="【下层】通用工具 MCP server", default_port=9100)


if __name__ == "__main__":
    main()
