# 三层架构设计说明

本文档描述企业三层 MCP 平台的架构决策与技术方案。

## 1. 分层原则

三层划分依据「关注点分离」与「控制面 / 数据面隔离」：

| 层 | 职责 | 关键关注点 |
|----|------|------------|
| **上层 审批（Gateway）** | 唯一入口、认证授权、HITL 审批、审计路由 | 安全、合规、可审计 |
| **中层 执行（Business）** | 按业务域拆分的系统 server，负责真实业务逻辑 | 业务正确性、领域模型 |
| **下层 通用（Common）** | 与业务无关的基础工具 | 复用性、稳定性 |

**为什么这样分层：**

- **上层审批**：企业中 AI 不能直接操作核心系统。所有调用先经网关，高风险写操作进入人工审批闸门（HITL），批准后才执行。这也借鉴了业界 MCP Gateway 模式（统一认证/审计/授权）。
- **中层拆 server**：采购、制造、IT 运维是不同系统，各自独立 server 便于独立部署、隔离故障、按域授权。避免"大而全的单一 server"。
- **下层通用**：时间、ID、文件、通知等能力下沉，供各中层 server 复用，避免重复实现。

## 2. 通信与调用链

```
AI 客户端 ──MCP──▶ [上层 gateway]
                       │  gateway_call(server, tool, args)
                       │     ├─ 只读：直接路由执行
                       │     └─ 需审批写操作：建审批单 → 人工 approve → 执行
                       ▼
              [中层/下层各 server]  （HTTP/本地执行器）
```

- 试点采用 **local 执行器**（网关进程内直连 it_ops 业务逻辑）跑通审批流。
- 生产采用 **MCP Streamable HTTP**：下游 server 独立部署 `--transport http`，网关用官方 SDK v2 的 `Client` 真实转发（`routing.call_downstream_http`），对应 `exec_kind="http"` 分支。该实现已通过 `tests/ops` 端到端验证（真实起进程、真实 HTTP 转发、落库产出真实工单号）。

**下游 Client 的两个要点：**

1. **必须显式 `mode="legacy"`**。SDK v2 的 `Client` 默认 `mode="auto"`，会先发 `server/discover` 探测、失败后回退 `initialize`，两请求背靠背发出；实测在 streamable-HTTP 传输上回退请求会把完整 URL 百分号编码进请求路径（`POST http%3A//127.0.0.1%3A9200/mcp` → 404 Not Found），导致连接失败。本平台各 server 均使用老握手协议（不存在 `2026-07-28` 现代协议），显式 `legacy` 既规避该竞态，也省掉一次无用探测。
2. **同步桥接**：SDK v2 的 sync 工具运行在 worker 线程（无事件循环），`call_downstream_http` 用 `asyncio.run` 起临时 loop 驱动异步 `Client`；不要在已有事件循环的线程中调用本函数。

## 3. 审批流（HITL）

1. AI 调用 `gateway_call(server='it_ops', tool='create_change', ...)`。
2. 网关查路由：`create_change` 标记 `requires_approval=True`。
3. 网关创建 `ApprovalRequest`（状态 `pending`），返回审批单 ID；**不执行**。
4. 审批人调用 `list_pending_approvals` 查看，用 `approve_request` / `reject_request` 决定。
5. 批准 → 网关调用下游执行；驳回 → 不执行。

审批单模型见 `shared/src/mcp_shared/approval.py`。

## 4. 扩展一个新业务系统（如新增"财务"）

1. 复制 `layers/business/it_ops` 目录为 `layers/business/finance`。
2. 重命名包、写业务工具（`store.py` 换真数据源）。
3. 在网关 `_build_router()` 注册工具路由，标注 `requires_approval`。
4.（生产）配置网关通过 HTTP 转发到该 server。

## 5. 安全与审计（后续推进）

- **认证鉴权**：网关接入企业 IdP（OAuth/OIDC），角色映射 RBAC。参考 [Akka MCP Gateway](https://github.com/akka/mcp-gateway)。
- **审计日志**：每次工具调用记录事件（用户、工具、参数、时间）。可扩为事件溯源。
- **超时/撤销**：审批单支持超时自动关闭。

## 6. 技术选型依据

- Python 3.10+，官方 [`mcp`](https://github.com/modelcontextprotocol/python-sdk) SDK **v2**（`>=2.2,<3`）。
- 用 SDK v2 高层 API **`MCPServer`**（`from mcp.server import MCPServer`）快速暴露 tool；注意旧 `mcp.server.fastmcp.FastMCP` 导入路径在 v2 中**已移除且无兼容层**，社区版 fastmcp v4 是另一个独立项目。
- **不引入 fastmcp v4**：其 `[server]` extra 会连带约 25 个依赖，而本项目只需「暴露工具 + 转发调用 + 双传输」，官方 SDK 已完整覆盖，额外依赖树与精简目标相悖（对比见 README「技术选型」）。
- Streamable HTTP 作为生产传输；传输参数（host/port）传给 `mcp.run()` 而非构造器，由 `shared/mcp_shared/server_kit.run_server` 统一收敛（避免五个 server 各抄一份 argparse）。
- 工具返回 `list[...]` 时 SDK 会包一层 `{"result": [...]}` 信封（标量/dict 则不包），客户端解包时需注意。
- pip + venv 管理依赖（每 server 独立 `pyproject.toml`，开发期共用根 `.venv`）。

## 7. 测试策略

| 层级 | 文件 | 特点 |
|------|------|------|
| 协议级冒烟 | `tests/smoke_test.py` | 用 SDK **内存 Client** 直连 server 对象：不起网络、不需要部署，验证工具 schema / 序列化 / 结果解包 |
| 真实链路 | `tests/ops/run_ops_test.py` | 经 **Streamable HTTP** 连真实进程：验证部署可用性、审批流、并发稳定性；退出码 0/1 可直接进 CI |
| 一键自检 | `scripts/ops-check.ps1` | 起三层 server → 跑运维测试集 → 停 server，返回退出码 |

两层测试互补：冒烟测试跑得快、适合改代码后立刻验；运维测试集跑得真、适合发版前/巡检。`tests/ops/client.py` 是复用的 MCP 客户端小工具（`call` / `call_raw` / `list_tools`），已内含 `mode="legacy"` 与 `{"result": ...}` 信封解包。
