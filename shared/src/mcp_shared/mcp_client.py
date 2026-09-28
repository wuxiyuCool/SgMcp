"""MCP 直连客户端（同步桥接 + 超时/重试 + 结果解包），当前仅供**上层网关转发**使用。

架构约定（双端点设计，见 docs/architecture.md §2）：
- 各 server 的 ``/mcp`` 面只说 MCP 协议：给网关聚合、暴露给 AI；
- 服务间内部调用（业务 Python → Go 重活等）走对方的 ``/internal/*`` REST 面
  （HTTP + JSON，不经网关），不占用 MCP 通道——两种通道各司其职，不混用。

`call_downstream_http` 为网关转发实现：官方 SDK 的 `Client` 经 Streamable HTTP 调
下游，含 ``{"result": [...]}`` 信封剥离与文本 JSON 解包。企业化要点：

1. **超时**：必须给整条调用（连接 + initialize + 工具执行）设上限，否则下游半死
   （TCP 连着但不响应）会永久占住 SDK worker 线程，把整个网关拖成不可用。
   默认 ``MCP_DOWNSTREAM_TIMEOUT_SECONDS``（120s），可按工具覆盖
   （``MCP_DOWNSTREAM_TIMEOUT_<工具名大写>``）。
2. **幂等重试**：只对**只读/幂等**工具在超时或连接失败后重试一次
   （``MCP_DOWNSTREAM_RETRIES``，默认 1，0=关闭）。写操作绝不自动重试——
   重试一张「创建工单」会产生两条工单，这是 AI 网关最常见的生产事故。
   幂等判定来自 tools/list 的 ``annotations.readOnlyHint``（网关聚合时记录）。
3. **可诊断错误**：下游失败统一转 ToolError，消息带「原因 + 下一步建议」，
   并透传 ``X-Sg-Call-Id``，让 AI 报错里的追踪号能与网关/下游日志对上。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("mcp_shared.mcp_client")

DEFAULT_TIMEOUT = 120.0


def bearer_headers(token: str | None) -> dict[str, str] | None:
    """Bearer 鉴权头；token 为空返回 None（本机免鉴权场景）。"""
    return {"Authorization": f"Bearer {token}"} if token else None


def timeout_for(tool: str, default: float = DEFAULT_TIMEOUT) -> float:
    """按工具取超时：``MCP_DOWNSTREAM_TIMEOUT_DATA_BATCH_IMPORT=300`` 优先于全局值。"""
    key = f"MCP_DOWNSTREAM_TIMEOUT_{tool.replace('.', '_').upper()}"
    for name in (key, "MCP_DOWNSTREAM_TIMEOUT_SECONDS"):
        raw = os.environ.get(name)
        if raw:
            try:
                val = float(raw)
                if val > 0:
                    return val
            except ValueError:
                logger.warning("%s=%r 不是数字，忽略", name, raw)
    return default


def retries_for(tool: str, read_only: bool) -> int:
    """可重试次数：仅只读工具重试；写工具永远 0（防重复落库）。"""
    if not read_only:
        return 0
    raw = os.environ.get("MCP_DOWNSTREAM_RETRIES", "1")
    try:
        return max(int(raw), 0)
    except ValueError:
        return 1


class DownstreamError(RuntimeError):
    """下游调用失败（带分类与建议），由网关转成可读 ToolError。"""

    def __init__(self, message: str, *, kind: str, hint: str, tool: str, attempts: int) -> None:
        self.kind = kind
        self.hint = hint
        self.tool = tool
        self.attempts = attempts
        super().__init__(f"{message}｜kind={kind}｜建议：{hint}（已尝试 {attempts} 次）")


def _flatten(exc: BaseException) -> list[BaseException]:
    """展开 ExceptionGroup / __cause__ 链：anyio 的 TaskGroup 会把真实错误
    （ConnectError 等）包一层，不展开就只能归类成"协议错误"，给运维的建议会是错的。"""
    out: list[BaseException] = []
    stack: list[BaseException] = [exc]
    while stack:
        cur = stack.pop()
        out.append(cur)
        stack.extend(getattr(cur, "exceptions", None) or ())  # BaseExceptionGroup 子异常
        if cur.__cause__ is not None and cur.__cause__ not in out:
            stack.append(cur.__cause__)
    return out


def classify_error(exc: BaseException, endpoint: str, tool: str = "",
                   status_sink: list[int] | None = None) -> tuple[str, str, bool]:
    """对外暴露的分类入口（网关聚合失败也用它，避免运维只看到 ExceptionGroup）。"""
    return _classify(exc, endpoint, tool, status_sink)


def _classify(exc: Exception, endpoint: str, tool: str,
              status_sink: list[int] | None = None) -> tuple[str, str, bool]:
    """异常（含被 TaskGroup 包裹的）→ (kind, 面向 AI 的建议, 是否可安全重试)。"""
    import httpx2

    cands = _flatten(exc)
    text = " ".join(f"{type(c).__name__}:{c}" for c in cands).lower()

    def any_is(*types) -> bool:
        return any(isinstance(c, types) for c in cands)

    if any_is(httpx2.ConnectError, httpx2.ConnectTimeout, ConnectionError, OSError) or "refused" in text:
        # 先判"连不上"：ConnectTimeout 里也有 timeout 字样，按超时归类会把建议指向错方向
        return ("unreachable",
                "下游服务不可达。请用 list_downstreams 查看聚合状态、必要时调 refresh_routes 重新聚合；"
                f"并确认目标服务已启动（当前地址 {endpoint.split('//')[-1].split('/')[0]}）",
                True)
    if any_is(asyncio.TimeoutError, httpx2.TimeoutException) or "timed out" in text or "timeout" in text:
        return ("timeout",
                f"下游 {tool} 响应超时。可稍后重试；若为大批量操作请改用异步任务"
                "（data_submit_collect_job → data_get_job_status）",
                True)
    if any_is(httpx2.HTTPStatusError):
        code = 0
        for c in cands:
            code = getattr(getattr(c, "response", None), "status_code", 0) or code
        if code in (401, 403):
            return ("unauthorized",
                    "下游鉴权失败（token 缺失或不匹配）。请核对 config/platform.env 的 *_TOKEN "
                    "与服务侧配置是否同值",
                    False)
        if code == 404:
            return ("not_found", "下游 MCP 端点路径不存在，请核对 MCP_DOWNSTREAMS 中的 URL（应以 /mcp 结尾）", False)
        if code and code >= 500:
            return ("downstream_5xx", "下游服务内部错误，请稍后重试或查看该 server 日志", True)
        return ("http_error", "下游拒绝了请求，请检查入参是否符合工具 schema", False)
    # SDK 常把传输层状态码裹成 RuntimeError/ClosedResource 文本，这里兜底识别
    if "401" in text or "403" in text or "unauthorized" in text:
        return ("unauthorized",
                "下游鉴权失败（HTTP 401/403）。请核对两侧令牌：网关的 "
                "MCP_DOWNSTREAM_TOKEN_<域名大写>（或 MCP_GODATAHUB_TOKEN）必须与服务侧 "
                "MCP_SERVER_TOKEN / GO_DATAHUB_TOKEN 同值",
                False)
    if "404" in text:
        return ("not_found", "下游 MCP 端点返回 404，请核对 MCP_DOWNSTREAMS 的 URL（应以 /mcp 结尾）", False)
    by_status = classify_http_status(status_sink or [])  # SDK 吞掉状态码时，用钩子记下的真实 HTTP 码兜底
    if by_status:
        return by_status
    return ("protocol_error",
            "下游返回了无法处理的响应。请用 list_routes 核对工具是否仍在册，或调 refresh_routes 重新聚合",
            False)


def new_http_client(headers: dict[str, str] | None, timeout: float,
                    status_sink: list[int] | None = None):
    """建带超时的 httpx2 客户端；status_sink 收集 HTTP 状态码。

    为什么要 sink：SDK 会把传输层错误统一裹成 ``MCPError -32603 Server returned an
    error response``，状态码丢失——401（令牌不对）与 500（下游炸了）看起来一模一样，
    运维只能瞎猜。这里在响应钩子里留下真实状态码供分类使用。
    """
    import httpx2

    async def _record(response):   # httpx 异步客户端的钩子必须是协程
        if status_sink is not None and response.status_code >= 400:
            status_sink.append(response.status_code)

    return httpx2.AsyncClient(
        headers=headers or {},
        timeout=httpx2.Timeout(timeout, connect=min(timeout, 10.0)),
        event_hooks={"response": [_record]},
        # trust_env=False：下游是内网服务地址，不吃 HTTP(S)_PROXY/NO_PROXY 环境变量。
        # 踩过的坑：本机配了代理时，「下游未启动」的连接拒绝会被代理转成读超时，
        # 错误分类从 unreachable 变 timeout，把排障方向带偏。
        trust_env=False,
    )


def classify_http_status(status_sink: list[int]) -> tuple[str, str, bool] | None:
    """按记录到的 HTTP 状态码给分类（401/403 优先，其次 404、5xx）。"""
    if not status_sink:
        return None
    code = status_sink[-1]
    if code in (401, 403):
        return ("unauthorized",
                "下游鉴权失败（HTTP %d）。请核对两侧令牌是否同值：网关的 "
                "MCP_DOWNSTREAM_TOKEN_<域名大写> / MCP_GODATAHUB_TOKEN 对应服务侧的 "
                "MCP_SERVER_TOKEN / GO_DATAHUB_TOKEN" % code,
                False)
    if code == 404:
        return ("not_found", "下游端点 404，请核对 MCP_DOWNSTREAMS 的 URL（应以 /mcp 结尾）", False)
    if code >= 500:
        return ("downstream_5xx", f"下游服务内部错误（HTTP {code}），请查看该 server 日志后重试", True)
    return ("http_error", f"下游返回 HTTP {code}，请检查入参是否符合工具 schema", False)


def _unwrap_result(result: Any) -> Any:
    """把 CallToolResult 解包成「工具真正 return 的值」。

    与 tests/ops/client.py 的 _unwrap 同一套规则：
    - SDK v2 对「返回 list」的工具会用 ``{"result": [...]}`` 包一层信封 → 剥掉；
    - 无结构化输出时取文本内容，单一文本尝试 JSON 解析（dict/list 工具的常见形态），
      解析失败则原样返回字符串（util_* 等纯文本工具）。
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
    *,
    read_only: bool = False,
    call_id: str | None = None,
    actor: str | None = None,
    timeout: float | None = None,
) -> Any:
    """通过 MCP Streamable HTTP 调用下游 server 工具（同步桥接，带超时/幂等重试）。

    headers 非空时（跨机部署的 Bearer 鉴权等）与超时设置一并交给自建
    httpx2.AsyncClient，再包成 streamable_http_client —— 该 context manager 即
    Client 认可的 Transport（duck-typed __aenter__ 返回读写流），直接传给 Client。

    SDK v2 的 sync 工具运行在 worker 线程（无事件循环），这里用 asyncio.run
    起临时 loop 驱动官方 Client；工具内不要在已有 loop 的线程调用本函数。

    mode 必须显式传 "legacy"，不能用默认的 "auto"：
    auto 会先探测 server/discover，对「老握手」server 探测失败后回退 initialize，
    而这两个请求是背靠背（pipelined）发出的。实测在 streamable-HTTP 传输上，
    回退的 initialize 会把完整 URL 百分号编码进请求路径
    （POST http%3A//127.0.0.1%3A9200/mcp → 404 Not Found），导致连接失败。
    本平台各 server 均使用老握手协议，因此显式 legacy 既规避竞态、也省掉探测请求。
    """
    from mcp import Client

    limit = timeout if timeout is not None else timeout_for(tool)
    hdrs = dict(headers or {})
    if call_id:
        hdrs["X-Sg-Call-Id"] = call_id

    status_sink: list[int] = []

    def _make_client() -> Client:
        from mcp.client.streamable_http import streamable_http_client

        # 超时同时给 httpx（连接/读写）与 asyncio（整体 wall-clock）兜底
        transport = streamable_http_client(
            endpoint, http_client=new_http_client(hdrs, limit, status_sink)
        )
        return Client(transport, raise_exceptions=False, mode="legacy")

    async def _call_once() -> Any:
        async with _make_client() as client:
            return await client.call_tool(tool, arguments)

    attempts = 1 + retries_for(tool, read_only)
    last: tuple[str, str, bool] | None = None
    for i in range(attempts):
        started = time.monotonic()
        try:
            result = asyncio.run(asyncio.wait_for(_call_once(), timeout=limit + 5))
        except Exception as e:  # noqa: BLE001 — 分类后统一转 DownstreamError
            last = _classify(e, endpoint, tool, status_sink)
            kind, hint, retryable = last
            logger.warning("下游调用失败 %s（%s/%s，第 %d 次）: %s", tool, kind, endpoint, i + 1, e)
            if not retryable or i + 1 >= attempts:
                raise DownstreamError(f"调用下游 {tool} 失败：{type(e).__name__}: {e}",
                                      kind=kind, hint=hint, tool=tool,
                                      attempts=i + 1) from e
            time.sleep(min(0.5 * (i + 1), 2.0))
            continue
        elapsed = time.monotonic() - started
        if elapsed > limit * 0.8:
            logger.warning("下游 %s 接近超时：%.1fs/%.1fs", tool, elapsed, limit)
        if getattr(result, "is_error", False):
            texts = [b.text for b in result.content if getattr(b, "type", "") == "text"]
            detail = "; ".join(texts) or str(result.content)
            # 注意：消息里不能带 endpoint URL——防泄露检查以 "://" 为连接串特征，
            # 且下游文本已含可读原因，URL 只服务于排障日志（上面已记）
            raise DownstreamError(f"下游 {tool} 执行失败: {detail}", kind="tool_error",
                                  hint="按上述下游错误修正入参后重试；不确定时先调 list_tool_catalog 看参数说明",
                                  tool=tool, attempts=i + 1)
        return _unwrap_result(result)

    raise DownstreamError(f"调用下游 {tool} 失败", kind=(last or ("unknown",))[0],
                          hint=(last or ("unknown", "", False))[1], tool=tool, attempts=attempts)
