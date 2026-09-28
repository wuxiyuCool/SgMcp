"""审批流模型：上层网关的核心数据结构。

所有需要人工审批（HITL）的写操作，均由上层网关建立一条 ApprovalRequest，
中层/下层 server 不直接放行，而是等待网关审批通过后回调执行。
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel, Field


class ApprovalStatus(str, enum.Enum):
    PENDING = "pending"  # 待审批
    APPROVED = "approved"  # 已通过
    REJECTED = "rejected"  # 已驳回
    TIMEOUT = "timeout"  # 超时未处理
    CANCELLED = "cancelled"  # 已撤销
    EXEC_FAILED = "exec_failed"  # 已批准但下游执行失败（可重新批准重试）


class ApprovalAction(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ApprovalRequest(BaseModel):
    """一条待审批事项。"""

    id: str = Field(default_factory=lambda: f"apr_{uuid.uuid4().hex[:12]}")
    # 发起方信息
    source_server: str  # 哪个 server 发起的（如 it_ops）
    tool_name: str  # 要执行的工具名
    tool_args: dict[str, Any]  # 工具入参（快照，供审批人查看）
    # 审批上下文
    title: str  # 展示标题
    description: Optional[str] = None
    requested_by: str  # 发起人（用户标识）
    call_id: Optional[str] = None  # 贯穿网关→下游的调用追踪号
    approver_roles: list[str] = Field(default_factory=lambda: ["approver"])
    # 状态机
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    expires_at: Optional[datetime] = None  # 超时自动关闭（None=不过期）
    decided_at: Optional[datetime] = None
    decided_by: Optional[str] = None
    comment: Optional[str] = None
    attempts: int = 0  # 执行尝试次数（批准后）
    executed_at: Optional[datetime] = None
    exec_error: Optional[str] = None

    @property
    def expired(self) -> bool:
        return (
            self.status == ApprovalStatus.PENDING
            and self.expires_at is not None
            and datetime.now(timezone.utc) >= self.expires_at
        )

    def decide(self, action: ApprovalAction, by: str, comment: str | None = None) -> None:
        """审批人以批准/驳回作决定（执行失败的单允许重新批准重试）。"""
        decidable = {ApprovalStatus.PENDING, ApprovalStatus.EXEC_FAILED}
        if self.status not in decidable:
            raise ValueError(
                f"审批 {self.id} 已处于 {self.status.value}，无法再次决定"
                + ("（可用 retry_execution 重放执行）" if self.status == ApprovalStatus.APPROVED else "")
            )
        self.status = (
            ApprovalStatus.APPROVED if action == ApprovalAction.APPROVE
            else ApprovalStatus.REJECTED
        )
        self.decided_at = datetime.now(timezone.utc)
        self.decided_by = by
        self.comment = comment
        self.expires_at = None  # 已决策，不再受超时影响

    def mark_execution(self, *, ok: bool, error: str | None = None) -> None:
        """记录一次下游执行结果：失败置 EXEC_FAILED（可重试），成功置 APPROVED+完成。"""
        self.attempts += 1
        self.executed_at = datetime.now(timezone.utc)
        self.exec_error = None if ok else (error or "执行失败")[:500]
        if not ok:
            self.status = ApprovalStatus.EXEC_FAILED
