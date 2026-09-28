"""面向「真实部署」的 MCP 客户端小工具。

与 tests/smoke_test.py 不同：冒烟测试用 SDK 的内存 Client 直连 server 对象（不起网络、
不测传输层）；本模块通过 **Streamable HTTP** 连真实进程，用于验证实际部署是否可用。

关键点：**必须显式传 mode="legacy"**。
SDK v2 的 Client 默认 mode="auto"，会先探测 `server/discover`、失败后回退
`initialize`（两个请求背靠背发出）。实测在该传输上回退请求会把完整 URL 百分号
编码进请求路径（POST http%3A//127.0.0.1%3A9200/mcp → 404），导致调用失败。
本平台各 server 均使用老握手协议，显式 legacy 既规避该竞态、也省一次无用探测。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

DEFAULT_URLS = {
    "gateway": "http://127.0.0.1:9000/mcp",
    "it_ops": "http://127.0.0.1:9200/mcp",
    "common-tools": "http://127.0.0.1:9100/mcp",
    "go_datahub": "http://127.0.0.1:9300/mcp",
}


def _unwrap(result: Any) -> Any:
    """把 CallToolResult 解包成 Python 数据（结构化优先，退回文本）。

    注意 SDK v2 对「返回 list」的工具会用 `{"result": [...]}` 包一层信封
    （标量/dict 返回值则是原始对象）。这里把信封剥掉，让调用方拿到的就是
    工具真正 return 的值——否则 list 工具会返回 dict，容易误用。
    """
    if result.is_error:
        raise RuntimeError(f"工具返回错误: {getattr(result, 'content', result)}")

    if result.structured_content is not None:
        data = result.structured_content
    else:
        texts = [b.text for b in result.content if getattr(b, "type", "") == "text"]
        if len(texts) == 1:
            try:
                data = json.loads(texts[0])
            except (ValueError, TypeError):
                return texts[0]
        else:
            return texts

    # 剥掉 list 返回值的 {"result": [...]} 信封
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def auto_headers(url: str) -> dict[str, str] | None:
    """未显式传 headers 时按目标地址自动选令牌（ops-check -Secure 全链路鉴权模式用）。

    网关地址取 ``MCP_GATEWAY_TOKEN``，其余（下游/Go）取 ``MCP_SERVER_TOKEN``；
    两个变量都没配就是 None，行为与不带鉴权的部署完全一致。
    """
    import os

    gw_url = (os.environ.get("MCP_OPS_GATEWAY_URL") or "").rstrip("/")
    token = None
    if gw_url and url.rstrip("/") == gw_url:
        token = os.environ.get("MCP_GATEWAY_TOKEN")
    if not token:
        token = os.environ.get("MCP_SERVER_TOKEN") or os.environ.get("MCP_GODATAHUB_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else None


def _make_client(url: str, raise_exceptions: bool, headers: dict[str, str] | None):
    """构造 SDK Client；始终经自建 httpx2.AsyncClient 走 streamable_http_client。

    为什么不让 SDK 自己建客户端：它默认 trust_env=True，会吃进 HTTP(S)_PROXY
    环境变量——本机配了代理时，连本机/内网 server 的请求被代理劫持，
    「服务未启动」会表现成 502/读超时，排障方向全偏。
    """
    from mcp import Client
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    headers = headers or auto_headers(url)
    http = httpx2.AsyncClient(
        headers=headers or {},
        timeout=httpx2.Timeout(30.0, connect=10.0),
        trust_env=False,
    )
    transport = streamable_http_client(url, http_client=http)
    return Client(transport, raise_exceptions=raise_exceptions, mode="legacy")


async def _call_async(url: str, tool: str, arguments: dict[str, Any] | None = None,
                      headers: dict[str, str] | None = None) -> Any:
    async with _make_client(url, True, headers) as client:
        result = await client.call_tool(tool, arguments or {})
    return _unwrap(result)


async def _call_raw_async(url: str, tool: str, arguments: dict[str, Any] | None = None,
                          headers: dict[str, str] | None = None) -> Any:
    """同 _call_async，但错误以 is_error 结果原样返回（不抛异常）。

    与网关网关路径 (routing.call_downstream_http) 的行为一致，用于测试
    「未注册路由 / 下游报错」这类需要检查 is_error 的用例。
    """
    async with _make_client(url, False, headers) as client:
        return await client.call_tool(tool, arguments or {})


async def _list_async(url: str, headers: dict[str, str] | None = None) -> list[str]:
    async with _make_client(url, True, headers) as client:
        tools = await client.list_tools()
    return [t.name for t in tools.tools]


# ---- 同步封装：给「非 async」脚本 / 运维修剪人员用 ----
def call(url: str, tool: str, arguments: dict[str, Any] | None = None,
         headers: dict[str, str] | None = None) -> Any:
    """同步调用一个 MCP 工具并返回解包后的数据。"""
    return asyncio.run(_call_async(url, tool, arguments, headers))


def call_raw(url: str, tool: str, arguments: dict[str, Any] | None = None,
             headers: dict[str, str] | None = None) -> Any:
    """同步调用，返回原始 CallToolResult（含 is_error），不抛异常。"""
    return asyncio.run(_call_raw_async(url, tool, arguments, headers))


def list_tools(url: str, headers: dict[str, str] | None = None) -> list[str]:
    """列出一个 server 暴露的工具名。"""
    return asyncio.run(_list_async(url, headers))



def healthz(mcp_url: str, path: str = "/healthz",
            headers: dict[str, str] | None = None) -> dict[str, Any]:
    """读 server 的 HTTP 探活端点（Python 层 /healthz，Go 层 /internal/healthz）。

    与 MCP 协议无关的裸 HTTP GET——负载均衡与巡检脚本用的就是它。
    """
    import httpx2

    base = mcp_url[: mcp_url.rfind("/")] if "/mcp" in mcp_url else mcp_url.rstrip("/")
    # trust_env=False：探活目标是本机/内网服务，不走系统代理（理由同 _make_client）
    with httpx2.Client(headers=headers or {}, timeout=10, trust_env=False) as client:
        resp = client.get(base + path)
    resp.raise_for_status()
    return resp.json()
