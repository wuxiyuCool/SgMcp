"""各层 server 共用的启动器。

五个 server（gateway / it_ops / purchasing / manufacturing / common）过去各自
复制一份 argparse main()，本模块把「stdio / HTTP 双模式 + 端口参数」收敛为一处：

- stdio（默认）：本地子进程接入，MCP 客户端经标准输入输出通信。
- --transport http：Streamable HTTP 独立部署，监听 http://127.0.0.1:<port>/mcp。

注意：SDK v2 中传输参数（host/port）必须传给 run()，而不是 MCPServer 构造器。

## HTTP 模式的企业化前置层（可选，全部由环境变量开关）

| 环境变量 | 作用 |
|----------|------|
| `<TOKEN_ENV>`（如 `MCP_SERVER_TOKEN`） | 非空则所有 /mcp 请求必须带 `Authorization: Bearer <token>` |
| `MCP_RATE_LIMIT_PER_SECOND` | 按客户端 IP 的滑动窗口限流上限（0/未设=不限流） |
| `MCP_TRUSTED_PROXY` | 逗号分隔的可信反代 IP/CIDR；仅当连接来自可信反代时才采信 `X-Forwarded-For` 做限流分桶 |

未启用鉴权时行为与旧版一致（本机开发零摩擦）；启用后 `/healthz` 始终免鉴权，
便于负载均衡与巡检探活。
"""

from __future__ import annotations

import argparse
import ipaddress
from typing import Any, Callable

from mcp_shared import http_kit
from mcp_shared.config import get as cfg

DEFAULT_TOKEN_ENV = "MCP_SERVER_TOKEN"


def _trusted_proxies(raw: str | None) -> list[ipaddress._BaseNetwork]:
    nets: list[ipaddress._BaseNetwork] = []
    for item in (raw or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            nets.append(ipaddress.ip_network(item if "/" in item else f"{item}/32", strict=False))
        except ValueError:
            continue
    return nets


def run_server(
    mcp: Any,
    *,
    description: str,
    default_port: int,
    token_env: str = DEFAULT_TOKEN_ENV,
    health_info: Callable[[], dict[str, Any]] | None = None,
) -> None:
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

    _install_audit(mcp)

    if args.transport != "http":
        mcp.run()
        return

    token = (cfg(token_env) or "").strip()
    limit = _positive_int(cfg("MCP_RATE_LIMIT_PER_SECOND"))

    import uvicorn

    # 始终加前置层：/healthz 探活对运维恒可用；鉴权与限流按配置生效
    app = http_kit.wrap_mcp_app(
        mcp.streamable_http_app(),
        server_name=getattr(mcp, "name", None) or description,
        token=token or None,
        rate_limit_per_second=limit,
        trusted_proxies=_trusted_proxies(cfg("MCP_TRUSTED_PROXY")),
        health_info=health_info,
    )
    if token:
        print(f"[{getattr(mcp, 'name', 'server')}] 已启用 Bearer 鉴权（{token_env}）；/healthz 免鉴权")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


def _positive_int(raw: str | None) -> int:
    try:
        return max(int(raw or 0), 0)
    except ValueError:
        return 0


def _install_audit(mcp: Any) -> None:
    """给 server 装上统一审计中间件（本层每次 tools/call 落一行 JSONL）。

    中间件挂在 SDK 的 `Server.middleware` 上，因此新写业务工具不必自己埋点；
    `MCP_AUDIT_PERSIST=0` 可关落盘（只走日志，测试用）。
    """
    from mcp_shared.audit import AuditMiddleware

    if any(isinstance(x, AuditMiddleware) for x in getattr(mcp, "middleware", [])):
        return
    slow_ms = _positive_int(cfg("MCP_AUDIT_SLOW_MS")) or 3000
    mcp.middleware.append(AuditMiddleware(getattr(mcp, "name", None) or "server", slow_ms=slow_ms))
