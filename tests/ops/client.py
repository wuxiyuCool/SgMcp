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


def _make_client(url: str, raise_exceptions: bool, headers: dict[str, str] | None):
    """构造 SDK Client；headers 非空时经自建 httpx2.AsyncClient 注入（远端 Go 带 token 直测用）。"""
    from mcp import Client

    if not headers:
        return Client(url, raise_exceptions=raise_exceptions, mode="legacy")
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    transport = streamable_http_client(url, http_client=httpx2.AsyncClient(headers=headers))
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
