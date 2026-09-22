"""企业三层 MCP 平台共享包。

提供跨层复用的审批流模型与 server 启动器，避免各层重复实现。
"""

from mcp_shared.approval import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalStatus,
)
from mcp_shared.server_kit import run_server

__all__ = [
    "ApprovalAction",
    "ApprovalRequest",
    "ApprovalStatus",
    "run_server",
]
