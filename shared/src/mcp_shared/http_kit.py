"""各层 MCP server 的 HTTP 前置层：Bearer 鉴权、/healthz 探活、按客户端限流。

设计成**纯 ASGI 包装**（不依赖 Starlette 内部结构），因此 SDK 升级或换传输层都不
受影响；stdio 模式不需要这些能力（本地子进程天然只有父进程能连）。

- **Bearer 鉴权**：环境变量非空才启用（网关 `MCP_GATEWAY_TOKEN`、业务层默认
  `MCP_SERVER_TOKEN`），与 Go 侧 `GO_DATAHUB_TOKEN` 同一套约定：常数时间比较、
  失败 401。默认不启用 → 本机开发零摩擦；跨机/上生产必须配。
- **/healthz**：GET 免鉴权，返回 `{ok, server, uptime_s, requests, auth, ...}`，
  供 systemd 探针 / 负载均衡 / `ops-check` 使用。
- **限流**：按客户端标识滑动窗口（`MCP_RATE_LIMIT_PER_SECOND`），超限 429 +
  `Retry-After`。`X-Forwarded-For` 仅当连接来自 `MCP_TRUSTED_PROXY` 列出的反代
  时才可信，否则一律用真实连接 IP——避免伪造头绕过限流。
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import time
from collections import deque
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, MutableMapping

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

# 当前请求的会话/调用标识，供内层工具写审计时带上（stdio 下为 None）
session_hint: ContextVar[str | None] = ContextVar("sg_session_hint", default=None)
call_id_hint: ContextVar[str | None] = ContextVar("sg_call_id_hint", default=None)
# 审批令牌走请求头：配在「审批人专用客户端」里，模型看不到也无需复述（AI 侧配置不含此头）
approval_hint: ContextVar[str | None] = ContextVar("sg_approval_hint", default=None)
actor_hint: ContextVar[str | None] = ContextVar("sg_actor_hint", default=None)

_TRUSTED_DEFAULT: tuple[ipaddress._BaseNetwork, ...] = ()


def _headers(scope: Scope) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}


def _peer_ip(scope: Scope) -> str:
    client = scope.get("client")
    return str(client[0]) if client and client[0] else "unknown"


def client_identity(scope: Scope, trusted_proxies: tuple | list = _TRUSTED_DEFAULT) -> str:
    """限流分桶标识：可信反代之后取 X-Forwarded-For 首项，否则取真实连接 IP。"""
    peer = _peer_ip(scope)
    if trusted_proxies:
        try:
            addr = ipaddress.ip_address(peer)
        except ValueError:
            addr = None
        if addr is not None and any(addr in net for net in trusted_proxies):
            fwd = _headers(scope).get("x-forwarded-for")
            if fwd:
                return fwd.split(",")[0].strip()
    return peer


class RateLimiter:
    """进程内滑动窗口限流（单实例够用；多副本部署请在网关/负载均衡层限流）。"""

    def __init__(self, limit_per_second: int) -> None:
        self.limit = int(limit_per_second)
        self.window = 1.0
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """返回 (是否放行, 建议等待秒数)。limit<=0 表示不限流。"""
        if self.limit <= 0:
            return True, 0.0
        now = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        while bucket and now - bucket[0] > self.window:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry = self.window - (now - bucket[0])
            return False, max(retry, 0.1)
        bucket.append(now)
        if len(self._hits) > 4096:  # 防 key 无限增长
            self.purge(now)
        return True, 0.0

    def purge(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for key in [k for k, b in self._hits.items() if not b or now - b[-1] > self.window * 4]:
            self._hits.pop(key, None)


class HttpFrontend:
    """把鉴权 / 探活 / 限流挂在 MCP ASGI app 前面。"""

    def __init__(
        self,
        app: ASGIApp,
        *,
        server_name: str,
        token: str | None = None,
        rate_limit_per_second: int = 0,
        trusted_proxies: tuple | list = _TRUSTED_DEFAULT,
        health_info: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self.app = app
        self.server_name = server_name
        self.token = (token or "").strip() or None
        self.trusted_proxies = trusted_proxies
        self.info = health_info
        self._started = time.time()
        self._limiter = RateLimiter(rate_limit_per_second) if rate_limit_per_second > 0 else None
        self._requests = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        self._requests += 1
        path = scope.get("path", "")
        if path in ("/healthz", "/health") and scope.get("method") == "GET":
            await self._healthz(send)
            return

        if self.token and not self._authorized(scope):
            await _respond(send, 401, {"ok": False, "error": "unauthorized"},
                           extra_headers=[(b"www-authenticate", b"Bearer")])
            return

        if self._limiter is not None:
            ok, retry = self._limiter.allow(client_identity(scope, self.trusted_proxies))
            if not ok:
                await _respond(send, 429, {"ok": False, "error": "rate_limited",
                                           "retry_after_s": round(retry, 2)},
                               extra_headers=[(b"retry-after", str(int(retry) + 1).encode())])
                return

        # 会话/调用标识暴露给内层工具（审计字段）；同任务链内 contextvar 可传播
        hdrs = _headers(scope)
        s_tok = session_hint.set(hdrs.get("mcp-session-id"))
        c_tok = call_id_hint.set(hdrs.get("x-sg-call-id"))
        a_tok = approval_hint.set(hdrs.get("x-sg-approval-token"))
        actor_tok = actor_hint.set(hdrs.get("x-sg-actor"))
        try:
            # 响应头带上会话/调用号，客户端排障时可直接引用（审计↔报错对得上）
            async def _send(message: Message) -> None:
                if message.get("type") == "http.response.start":
                    extra = []
                    if hdrs.get("mcp-session-id"):
                        extra.append((b"x-sg-session", hdrs["mcp-session-id"].encode("latin-1")))
                    if hdrs.get("x-sg-call-id"):
                        extra.append((b"x-sg-call-id", hdrs["x-sg-call-id"].encode("latin-1")))
                    if extra:
                        message = {**message, "headers": list(message.get("headers", [])) + extra}
                await send(message)

            await self.app(scope, receive, _send)
        finally:
            approval_hint.reset(a_tok)
            actor_hint.reset(actor_tok)
            session_hint.reset(s_tok)
            call_id_hint.reset(c_tok)

    def _authorized(self, scope: Scope) -> bool:
        got = _headers(scope).get("authorization", "")
        return hmac.compare_digest(got, f"Bearer {self.token}")

    async def _healthz(self, send: Send) -> None:
        body: dict[str, Any] = {
            "ok": True,
            "server": self.server_name,
            "uptime_s": round(time.time() - self._started, 1),
            "requests": self._requests,
            "auth": bool(self.token),
        }
        if self.info is not None:
            try:
                body.update(self.info())
            except Exception:  # noqa: BLE001 — 探针不得因附加信息失败
                pass
        await _respond(send, 200, body)


async def _respond(send: Send, status: int, body: dict[str, Any],
                   extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(payload)).encode()),
        *(extra_headers or []),
    ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})


def wrap_mcp_app(app: ASGIApp, **kwargs: Any) -> HttpFrontend:
    """包装 SDK 的 streamable_http_app()，返回可直接交给 uvicorn 的 ASGI callable。"""
    return HttpFrontend(app, **kwargs)
