"""审批闸门管理：网关层的 HITL（Human-in-the-Loop）审批核心。

当 AI 客户端请求某个"需要审批"的写操作时，网关不直接放行，
而是创建一条 ApprovalRequest 挂起。审批人通过网关的
approve_request / reject_request 工具作决定，通过后才转发到下游 server 执行。

企业化要点（对照业界 MCP 网关实践）：

1. **持久化**：审批单以 JSONL 事件流落盘（`data/audit/approvals.jsonl`，目录可用
   `MCP_DATA_DIR` / `MCP_APPROVAL_STORE` 覆盖）。网关重启后待办不丢——否则
   AI 拿到的 request_id 全部失效，审批链断裂。
2. **超时关闭**：`MCP_APPROVAL_TTL_MINUTES`（默认 240，0=不过期）；过期单自动置
   `timeout`，不再可决策，防止陈旧入参（数据已变）被放行。
3. **重复挂起去重**：`MCP_APPROVAL_DEDUPE_SECONDS`（默认 300）内，同一
   server+tool+入参指纹 的 pending 单只建一次，返回同一 request_id——AI 平台
   常见"超时后自动重试"，否则会产生 N 张等值审批单让人重复决策。
4. **执行结果回写**：批准后下游执行失败置 `exec_failed`（可重新批准重放），
   不会出现"单已批但动作没做"的黑洞。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from mcp_shared.approval import ApprovalAction, ApprovalRequest, ApprovalStatus
from mcp_shared.audit import digest
from mcp_shared.config import audit_dir

logger = logging.getLogger("mcp_gateway.approvals")


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(int(float(raw)), 0)
    except ValueError:
        logger.warning("%s=%r 非法，回退默认 %d", name, raw, default)
        return default


class ApprovalGate:
    """线程安全的审批闸门：内存索引 + JSONL 事件流持久化。"""

    def __init__(self, store: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._requests: dict[str, ApprovalRequest] = {}
        self._store = store
        self._pending_keys: dict[str, str] = {}  # 去重指纹 -> request_id

    # ---- 存储 ----
    @property
    def store_path(self) -> Path:
        if self._store is not None:
            return self._store
        raw = os.environ.get("MCP_APPROVAL_STORE")
        return Path(raw) if raw else audit_dir() / "approvals.jsonl"

    @property
    def persist(self) -> bool:
        return os.environ.get("MCP_APPROVAL_PERSIST", "1") != "0"

    def _append(self, op: str, payload: dict[str, Any]) -> None:
        if not self.persist:
            return
        line = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "op": op, **payload}
        try:
            p = self.store_path
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        except Exception as e:  # noqa: BLE001 — 落盘失败不能影响审批主流程
            logger.warning("审批单落盘失败: %s", e)

    def load(self) -> int:
        """从事件流重放审批单状态（网关启动时调用）。返回恢复条数。"""
        p = self.store_path
        if not p.is_file():
            return 0
        n = 0
        with self._lock:
            for raw in p.read_text(encoding="utf-8").splitlines():
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                op = ev.get("op")
                if op == "request":
                    try:
                        req = ApprovalRequest.model_validate(ev["request"])
                    except Exception:  # noqa: BLE001 — 兼容旧版本字段
                        continue
                    self._requests[req.id] = req
                    if req.status == ApprovalStatus.PENDING:
                        self._pending_keys[_fingerprint(req)] = req.id
                    n += 1
                elif op in ("decide", "exec"):
                    req = self._requests.get(ev.get("id") or "")
                    if req is None:
                        continue
                    for field in ("decided_at", "decided_by", "comment",
                                  "executed_at", "exec_error", "attempts"):
                        if ev.get(field) is not None:
                            setattr(req, field, ev[field])
                    if ev.get("status"):
                        # 事件里的 status 是字符串；必须回写为枚举，否则后续 .value 读取会炸
                        req.status = ApprovalStatus(ev["status"])
                    if req.status != ApprovalStatus.PENDING:
                        self._pending_keys.pop(_fingerprint(req), None)
            expired = self._sweep_expired_locked(mark=False)
            for req in expired:
                req.status = ApprovalStatus.TIMEOUT
                self._pending_keys.pop(_fingerprint(req), None)
                self._append("decide", {"id": req.id, "status": ApprovalStatus.TIMEOUT.value,
                                        "comment": "启动时检测到已超时"})
        logger.info("审批单恢复 %d 条（其中超时关闭 %d 条）", n, len(expired))
        return n

    # ---- 写 ----
    def request(
        self,
        source_server: str,
        tool_name: str,
        tool_args: dict,
        requested_by: str,
        title: str,
        description: Optional[str] = None,
        call_id: Optional[str] = None,
        ttl_minutes: int | None = None,
    ) -> ApprovalRequest:
        """挂起一条审批；命中去重窗口时返回既有单（幂等）。"""
        ttl = _int_env("MCP_APPROVAL_TTL_MINUTES", 240) if ttl_minutes is None else ttl_minutes
        req = ApprovalRequest(
            source_server=source_server,
            tool_name=tool_name,
            tool_args=tool_args,
            requested_by=requested_by,
            title=title,
            description=description,
            call_id=call_id,
            expires_at=(datetime.now(timezone.utc) + timedelta(minutes=ttl)) if ttl else None,
        )
        fp = _fingerprint(req)
        with self._lock:
            self._sweep_expired_locked(mark=True)
            reuse = self._pending_keys.get(fp)
            if reuse and reuse in self._requests:
                existing = self._requests[reuse]
                window = _int_env("MCP_APPROVAL_DEDUPE_SECONDS", 300)
                age = (datetime.now(timezone.utc) - existing.created_at).total_seconds()
                if existing.status == ApprovalStatus.PENDING and window and age <= window:
                    logger.info("审批去重命中：%s.%s 复用 %s（%.0fs 内的等值请求）",
                                source_server, tool_name, reuse, age)
                    return existing
            self._requests[req.id] = req
            self._pending_keys[fp] = req.id
            self._append("request", {"request": req.model_dump(mode="json"), "args_digest": digest(tool_args)})
            self._enforce_pending_cap_locked()
            return req

    def decide(self, request_id: str, action: ApprovalAction, by: str, comment: str | None = None) -> ApprovalRequest:
        with self._lock:
            self._sweep_expired_locked(mark=True)
            req = self._requests.get(request_id)
            if req is None:
                raise LookupError(f"审批单不存在: {request_id}（可能已被清理，请用 list_pending_approvals 查当前待办）")
            req.decide(action, by=by, comment=comment)
            self._pending_keys.pop(_fingerprint(req), None)
            self._append("decide", {"id": req.id, "status": req.status.value,
                                    "decided_by": by, "comment": comment})
            return req

    def mark_execution(self, request_id: str, *, ok: bool, error: str | None = None) -> ApprovalRequest:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise LookupError(f"审批单不存在: {request_id}")
            req.mark_execution(ok=ok, error=error)
            self._append("exec", {"id": req.id, "status": req.status.value, "ok": ok,
                                  "error": req.exec_error, "attempts": req.attempts})
            return req

    # ---- 读 ----
    def get(self, request_id: str) -> ApprovalRequest | None:
        with self._lock:
            return self._requests.get(request_id)

    def list(self, status: str | None = None) -> list[ApprovalRequest]:
        with self._lock:
            self._sweep_expired_locked(mark=True)
            items = list(self._requests.values())
            if status:
                items = [r for r in items if r.status.value == status]
            return [r.model_copy() for r in sorted(items, key=lambda r: r.created_at)]

    # ---- 内部 ----
    def _sweep_expired_locked(self, *, mark: bool) -> list[ApprovalRequest]:
        out = [r for r in self._requests.values() if r.expired]
        if mark:
            for req in out:
                req.status = ApprovalStatus.TIMEOUT
                self._pending_keys.pop(_fingerprint(req), None)
                self._append("decide", {"id": req.id, "status": ApprovalStatus.TIMEOUT.value,
                                        "comment": "超时自动关闭"})
        return out

    def _enforce_pending_cap_locked(self) -> None:
        cap = _int_env("MCP_APPROVAL_MAX_PENDING", 200)
        if cap <= 0:
            return
        pending = sorted((r for r in self._requests.values()
                          if r.status == ApprovalStatus.PENDING), key=lambda r: r.created_at)
        for req in pending[: max(0, len(pending) - cap)]:
            req.status = ApprovalStatus.CANCELLED
            req.comment = "超出待办上限自动撤销"
            self._pending_keys.pop(_fingerprint(req), None)
            self._append("decide", {"id": req.id, "status": req.status.value, "comment": req.comment})


def _fingerprint(req: ApprovalRequest) -> str:
    """等值请求指纹（去重用）：server + tool + 入参摘要 + 创建分钟桶。"""
    bucket = req.created_at.strftime("%Y%m%d%H%M")
    return f"{req.source_server}|{req.tool_name}|{digest(req.tool_args)}|{bucket}"


gate = ApprovalGate()
