"""【中层·采购】采购系统 MCP server（预留）。

本目录是采购业务线的占位骨架，作为 it_ops 试点验证三层架构后，按相同模式扩展：
- 采购申请 / 采购订单 / 供应商 / 合同 等工具
- 高风险写操作（下单、付款）在网关层注册 requires_approval=True

参考 it_ops 目录的 store.py + main.py 结构。
"""

__version__ = "0.1.0"
