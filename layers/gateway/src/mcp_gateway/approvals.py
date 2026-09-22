"""审批闸门管理：网关层的 HITL（Human-in-the-Loop）审批核心。

当 AI 客户端请求某个\"需要审批\"的写操作时，网关不直接放行，
而是创建一条 ApprovalRequest 挂起。审批人通过网关的
approve_request / reject_request 工具作决定，通过后才转发到下游 server 执行。
"""

from __future__ import annotations

import threading
from typing import Optional

from mcp_shared.approval import ApprovalAction, ApprovalRequest


class ApprovalGate:
    """线程安全的审批闸门，持有多条待办/已决审批。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requests: dict[str, ApprovalRequest] = {}

    def request(
        self,
        source_server: str,
        tool_name: str,
        tool_args: dict,
        requested_by: str,
        title: str,
        description: Optional[str] = None,
    ) -> ApprovalRequest:
        with self._lock:
            req = ApprovalRequest(
                source_server=source_server,
                tool_name=tool_name,
                tool_args=tool_args,
                requested_by=requested_by,
                title=title,
                description=description,
            )
            self._requests[req.id] = req
            return req

    def decide(self, request_id: str, action: ApprovalAction, by: str, comment: str | None = None) -> ApprovalRequest:
        with self._lock:
            req = self._requests.get(request_id)
            if req is None:
                raise LookupError(f"审批单不存在: {request_id}")
            req.decide(action, by=by, comment=comment)
            return req

    def list(self, status: str | None = None) -> list[ApprovalRequest]:
        with self._lock:
            items = list(self._requests.values())
            if status:
                items = [r for r in items if r.status.value == status]
            return [r.model_copy() for r in items]


gate = ApprovalGate()
