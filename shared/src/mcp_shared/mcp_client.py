"""MCP 直连客户端（同步桥接 + 结果解包），当前仅供**上层网关转发**使用。

架构约定（双端点设计，见 docs/architecture.md §2）：
- 各 server 的 ``/mcp`` 面只说 MCP 协议：给网关聚合、暴露给 AI；
- 服务间内部调用（业务 Python → Go 重活等）走对方的 ``/internal/*`` REST 面
  （HTTP + JSON，不经网关），不占用 MCP 通道——两种通道各司其职，不混用。

`call_downstream_http` 最初为网关转发实现：官方 SDK 的 `Client` 经
Streamable HTTP 调下游，含 ``{"result": [...]}`` 信封剥离与文本 JSON 解包。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


def bearer_headers(token: str | None) -> dict[str, str] | None:
    """Bearer 鉴权头；token 为空返回 None（本机免鉴权场景）。"""
    return {"Authorization": f"Bearer {token}"} if token else None


def _unwrap_result(result: Any) -> Any:
    """把 CallToolResult 解包成「工具真正 return 的值」。

    与 tests/ops/client.py 的 _unwrap 同一套规则：
    - SDK v2 对「返回 list」的工具会用 ``{"result": [...]}`` 包一层信封 → 剥掉；
    - 无结构化输出时取文本内容，单一文本尝试 JSON 解析（dict/list 工具的常见形态），
      解析失败则原样返回字符串（echo/slugify 等纯文本工具）。
    """
    if result.structured_content is not None:
        data = result.structured_content
    else:
        texts = [b.text for b in result.content if getattr(b, "type", "") == "text"]
        if len(texts) == 1:
            try:
                return json.loads(texts[0])
            except (ValueError, TypeError):
                return texts[0]
        return texts
    if isinstance(data, dict) and set(data) == {"result"}:
        return data["result"]
    return data


def call_downstream_http(
    endpoint: str,
    tool: str,
    arguments: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> Any:
    """通过 MCP Streamable HTTP 调用下游 server 工具（同步桥接）。

    headers 非空时（跨机部署的 Bearer 鉴权等），自建带默认头的 httpx2.AsyncClient
    交给 streamable_http_client —— 该 context manager 即 Client 认可的
    Transport（duck-typed __aenter__ 返回读写流），直接传给 Client。

    SDK v2 的 sync 工具运行在 worker 线程（无事件循环），这里用 asyncio.run
    起临时 loop 驱动官方 Client；工具内不要在已有 loop 的线程调用本函数。

    mode 必须显式传 "legacy"，不能用默认的 "auto"：
    auto 会先探测 server/discover，对「老握手」server 探测失败后回退 initialize，
    而这两个请求是背靠背（pipelined）发出的。实测在 streamable-HTTP 传输上，
    回退的 initialize 会把完整 URL 百分号编码进请求路径
    （POST http%3A//127.0.0.1%3A9200/mcp → 404 Not Found），导致连接失败。
    见 SDK mcp/client/_probe.py:negotiate_auto 的注释（该竞态 SDK 自身有记录）。
    本平台各 server 均使用老握手协议，并不存在 2026-07-28 现代协议，
    因此显式 legacy 既规避竞态、也省掉一次无意义的探测请求。
    """
    from mcp import Client

    def _make_client(raise_exceptions: bool) -> Client:
        if not headers:
            return Client(endpoint, raise_exceptions=raise_exceptions, mode="legacy")
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        transport = streamable_http_client(endpoint, http_client=httpx2.AsyncClient(headers=headers))
        return Client(transport, raise_exceptions=raise_exceptions, mode="legacy")

    async def _call() -> Any:
        # raise_exceptions=False：拿到原始 CallToolResult，把下游错误转成
        # 带完整细节的 ToolError（普通 RuntimeError 会被 SDK 脱敏成通用消息，
        # 只有 ToolError 透传细节给 AI 客户端）
        from mcp.server.mcpserver.exceptions import ToolError

        async with _make_client(False) as client:
            result = await client.call_tool(tool, arguments)
        if result.is_error:
            texts = [b.text for b in result.content if getattr(b, "type", "") == "text"]
            # 注意：消息里不能带 endpoint URL——防泄露检查以 "://" 为连接串特征，
            # 且下游文本已含可读原因，URL 只服务于排障日志（logger 已记）
            raise ToolError(f"下游 {tool} 执行失败: {'; '.join(texts) or result.content}")
        return _unwrap_result(result)

    return asyncio.run(_call())
