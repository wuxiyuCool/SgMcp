"""各层 server 共用的启动器。

五个 server（gateway / it_ops / purchasing / manufacturing / common）过去各自
复制一份 argparse main()，本模块把「stdio / HTTP 双模式 + 端口参数」收敛为一处：

- stdio（默认）：本地子进程接入，MCP 客户端经标准输入输出通信。
- --transport http：Streamable HTTP 独立部署，监听 http://127.0.0.1:<port>/mcp。

注意：SDK v2 中传输参数（host/port）必须传给 run()，而不是 MCPServer 构造器。
"""

from __future__ import annotations

import argparse
from typing import Any


def run_server(mcp: Any, *, description: str, default_port: int) -> None:
    """解析 --transport / --port 命令行参数并启动 server（阻塞直至退出）。"""
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio=本地子进程（默认）；http=Streamable HTTP 独立部署",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=default_port,
        help="HTTP 监听端口（--transport http 时生效）",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="HTTP 监听地址（--transport http 时生效）；服务器对外部署用 0.0.0.0",
    )
    args = parser.parse_args()

    if args.transport == "http":
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()
