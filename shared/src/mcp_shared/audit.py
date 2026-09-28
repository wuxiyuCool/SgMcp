"""结构化调用审计（企业合规最小集）：JSONL 落盘 + 尾部查询 + 入参脱敏。

为什么需要：AI 经网关调用下游工具时，参数里可能带凭证、结果可能带业务数据。
企业排障与事后追责要求「谁、在什么时候、通过哪个工具、用什么参数、结果如何」
可离线核查，且**明文凭证不得落盘**（dsn/token/password 等键值只留 `***`，
长文本截断并标注原始长度）。

- 存储：`MCP_AUDIT_DIR`（默认 `<项目根>/data/audit/`）下的 `<component>.jsonl`，
  一行一个事件；目录不可写时降级为仅日志告警，绝不影响工具调用本身。
- 事件字段约定：`ts` / `component` / `event`，其余按事件类型附加（call_id、actor、
  server、tool、args、ok、error、ms 等）。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp_shared.config import audit_dir

logger = logging.getLogger("mcp_shared.audit")

# 键名包含这些片段即视为敏感值，整体遮蔽（密码零传输约定在审计侧的落地）
# phone/mobile/tel/idno 是业务平台（ITOM 建单）会带的个人信息：报障人与处理人手机号、
# 身份证号——它们会作为正常入参出现，同样不能进审计原文
SENSITIVE_KEYS = frozenset({
    "token", "password", "passwd", "pwd", "secret", "dsn", "authorization",
    "api_key", "apikey", "access_key", "credential", "cookie", "private_key",
    "phone", "mobile", "tel", "idno", "idcard", "id_no",
})

MAX_STR = 400  # 审计里长文本截断（完整值属调用方，日志不做大 payload 存档）


def is_sensitive_key(key: str) -> bool:
    k = key.strip().lower().replace("-", "_")
    return any(s in k for s in SENSITIVE_KEYS)


def redact(value: Any, *, key: str | None = None) -> Any:
    """递归脱敏：敏感键 → `***`，长字符串截断并标注长度。"""
    if key and is_sensitive_key(key):
        return "***"
    if isinstance(value, dict):
        return {str(k): redact(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value[:50]] + (
            [f"…(共 {len(value)} 项)"] if len(value) > 50 else []
        )
    if isinstance(value, str):
        return value if len(value) <= MAX_STR else f"{value[:MAX_STR]}…(len={len(value)})"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact(str(value), key=key)


def digest(value: Any) -> str:
    """入参指纹（审计比对用，不含原文）：短哈希 + 规模概览。"""
    import hashlib

    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return f"sha1:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:12]}/len={len(text)}"


def tail_jsonl(path: Path, limit: int = 50) -> list[dict[str, Any]]:
    """读 JSONL 尾部 N 行（倒序：最新在前）。文件缺失/坏行容忍。"""
    if not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("读取 %s 失败: %s", path, e)
        return []
    out: list[dict[str, Any]] = []
    for raw in reversed(lines[-max(limit, 1) * 4:]):  # 多余读取量供过滤使用
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


class AuditLog:
    """一个组件（server/网关）的审计写入器；线程安全，写失败只告警。"""

    def __init__(self, component: str, *, persist: bool | None = None) -> None:
        self.component = component
        self._persist = persist
        self._lock = threading.Lock()

    @property
    def persist(self) -> bool:
        """落盘开关：`MCP_AUDIT_PERSIST=0` 关闭（只走日志，测试/隐私敏感环境用）。"""
        if self._persist is not None:
            return self._persist
        return os.environ.get("MCP_AUDIT_PERSIST", "1") != "0"

    @property
    def path(self) -> Path:
        return audit_dir() / f"{self.component}.jsonl"

    def _rotate_if_needed(self, path: Path) -> None:
        """按大小轮转（默认 20MB × 3 份），防长期运行把磁盘写满。

        审计是"永远在写"的追加型文件，没有轮转迟早把部署机填满；轮转只改名，
        不删内容（保留 MCP_AUDIT_KEEP 份历史，超出的最旧一份丢弃）。
        """
        try:
            keep = max(int(os.environ.get("MCP_AUDIT_KEEP", "3") or 3), 1)
            limit = max(int(os.environ.get("MCP_AUDIT_MAX_BYTES", str(20 << 20)) or (20 << 20)), 4096)
        except ValueError:
            keep, limit = 3, 20 << 20
        try:
            if not path.is_file() or path.stat().st_size < limit:
                return
            oldest = path.with_suffix(path.suffix + f".{keep}")
            if oldest.exists():
                oldest.unlink()
            for i in range(keep - 1, 0, -1):
                src = path.with_suffix(path.suffix + f".{i}")
                if src.exists():
                    src.rename(path.with_suffix(path.suffix + f".{i + 1}"))
            path.rename(path.with_suffix(path.suffix + ".1"))
            logger.info("审计文件已达 %s 字节，轮转为 %s.1", limit, path.name)
        except OSError as e:
            logger.warning("审计轮转失败（继续追加写入）: %s", e)

    def record(self, event: str, **fields: Any) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "component": self.component,
            "event": event,
        }
        for k, v in fields.items():
            entry[k] = redact(v, key=k) if k in {"args", "tool_args", "arguments", "result"} else v
        if self.persist:
            path = self.path
            try:
                line = json.dumps(entry, ensure_ascii=False, default=str)
                with self._lock:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    self._rotate_if_needed(path)
                    with path.open("a", encoding="utf-8") as f:
                        f.write(line + "\n")
            except Exception as e:  # noqa: BLE001 — 审计不可用不能阻断业务调用
                logger.warning("审计落盘失败（%s）: %s", path, e)
        logger.debug("audit %s", entry)
        return entry

    def query(self, limit: int = 50, **filters: Any) -> list[dict[str, Any]]:
        """按字段等值过滤最近事件（如 server='it_ops'、event='tool_call'）。"""
        items = tail_jsonl(self.path, limit=max(limit * 4, 200))
        out = []
        for it in items:
            if all(str(it.get(k, "")) == str(v) for k, v in filters.items()):
                out.append(it)
            if len(out) >= limit:
                break
        return out


_INSTANCES: dict[str, AuditLog] = {}
_INSTANCE_LOCK = threading.Lock()


def audit_log(component: str) -> AuditLog:
    """取（并缓存）某组件的审计写入器。"""
    with _INSTANCE_LOCK:
        inst = _INSTANCES.get(component)
        if inst is None:
            inst = _INSTANCES[component] = AuditLog(component)
        return inst


class AuditMiddleware:
    """SDK server 中间件：统一审计本 server 收到的每一次 tools/call。

    挂在 `MCPServer.middleware` 上（`run_server` 为所有层自动装好），因此：
    - 网关与业务层用同一份事件字段，跨层能按 `call_id` 串起完整链路；
    - 新增业务工具不需要自己写埋点，也不会漏写。

    记录：工具名、发起人（`_meta.progressToken` 之外的 `x-sg-actor` 头 / 入参
    requested_by）、入参（脱敏）、耗时、成功/失败（含下游 is_error 的失败结果）、
    慢调用阈值告警。
    """

    def __init__(self, component: str, *, slow_ms: float = 3000.0) -> None:
        self.audit = audit_log(component)
        self.component = component
        self.slow_ms = slow_ms

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        import time

        if getattr(ctx, "method", "") != "tools/call":
            return await call_next(ctx)

        params = getattr(ctx, "params", None) or {}
        if not isinstance(params, dict):
            params = {}
        tool = str(params.get("name") or "")
        args = params.get("arguments") or {}
        meta = params.get("_meta") or {}
        headers = _request_headers(ctx)
        call_id = headers.get("x-sg-call-id") or (meta.get("x-sg-call-id") if isinstance(meta, dict) else None)
        actor = headers.get("x-sg-actor")
        started = time.monotonic()
        try:
            result = await call_next(ctx)
        except Exception as e:  # noqa: BLE001 — 观测型中间件：记录后原样抛出
            self.audit.record(
                "tool_error", component=self.component, server=self.component, tool=tool,
                args=args, error=f"{type(e).__name__}: {e}", call_id=call_id, actor=actor,
                ms=round((time.monotonic() - started) * 1000, 1),
            )
            raise
        ms = round((time.monotonic() - started) * 1000, 1)
        failed = bool(getattr(result, "is_error", False))
        self.audit.record(
            "tool_error" if failed else "tool_call",
            component=self.component, server=self.component, tool=tool, args=args,
            ok=not failed, ms=ms, call_id=call_id, actor=actor,
            **({"error": _error_text(result)} if failed else {}),
        )
        if ms > self.slow_ms:
            logger.warning("慢调用 %s.%s：%.0fms（阈值 %.0fms）",
                           self.component, tool, ms, self.slow_ms)
        return result


def _request_headers(ctx: Any) -> dict[str, str]:
    """HTTP 传输时取请求头（stdio 下没有 request，返回空）。"""
    request = getattr(ctx, "request", None)
    headers = getattr(request, "headers", None)
    if not headers:
        return {}
    try:
        return {k.lower(): v for k, v in headers.items()}
    except Exception:  # noqa: BLE001 — 头形态不可控时不影响调用
        return {}


def _error_text(result: Any) -> str:
    texts = [b.text for b in (getattr(result, "content", None) or []) if getattr(b, "type", "") == "text"]
    return ("; ".join(texts) or "工具返回错误")[:500]
