# 企业级三层 MCP 平台

基于 **Model Context Protocol (MCP)** 的企业内部 AI 工具平台。采用三层层级架构，各层分工明确，层间通过 **MCP Streamable HTTP** 通信。

## 架构总览

```
                        ┌─────────────────────────────┐
   AI 客户端 / Agent     │      【上层】 gateway        │
   (Claude / Copilot…  ) │   审批网关 · 统一入口 · 审计   │
                        │   认证 / 授权 / 审批流 / 路由   │
                        └──────────────┬──────────────┘
                                       │ MCP over HTTP
              ┌────────────────────────┴─────────────────────────┐
              │                       │                          │
   ┌──────────┴──────────┐ ┌──────────┴──────────┐   ┌──────────┴──────────┐
   │  【中层】 business    │ │   【中层】 business   │   │   【中层】 business   │
   │  it_ops  (试点)      │ │ purchasing (预留)   │   │  manufacturing(预留) │
   │  IT运维/工单/资产     │ │  采购系统             │   │  制造系统             │
   └──────────┬──────────┘ └──────────┬──────────┘   └──────────┬──────────┘
              │                       │                          │
              └──────────┐   ┌────────┴────────┐   ┌────────────┘
                         │   │                 │
               ┌─────────┴───┴─────────┐ ┌────┴──────────┐
               │  【下层】 common        │ │  其他通用能力   │
               │  通用工具 MCP          │ │  (文件/时间/通知)│
               └───────────────────────┘ └───────────────┘
```

## 三层职责

| 层 | 目录 | 职责 | 关键能力 |
|----|------|------|----------|
| **上层 审批** | `layers/gateway` | 统一入口、审批流、认证授权、审计 | HITL 人工审批、路由、RBAC、日志审计 |
| **中层 执行** | `layers/business/*` | 各业务系统按域拆分，独立 server | it_ops 试点；purchasing/manufacturing 预留 |
| **下层 通用** | `layers/common` | 与业务无关的通用工具 | 文件、时间、通知、ID 生成等 |

## 中间层分工策略

中层按业务域拆分为多个独立 MCP server，每个 server 负责一个系统：

| Server | 状态 | 分工 |
|--------|------|------|
| `it_ops` | ✅ 试点中 | IT 运维：服务台工单、变更、资产、监控 |
| `purchasing` | 🔒 预留 | 采购系统：采购申请、订单、供应商 |
| `manufacturing` | 🔒 预留 | 制造系统：工单、排产、物料 |

## 检索与复用说明

本平台**复用了业界成熟的架构模式**，而非重复造轮子：

- **上层网关模式** —— 借鉴 [Akka MCP Gateway](https://github.com/akka/mcp-gateway)（统一入口、认证授权、审计日志）、[Docker MCP Enterprise Gateway](https://www.docker.com/products/mcp-enterprise-gateway/)。网关集中处理认证/授权/审计，下游 server 不各自为政。
- **审批闸门（HITL）** —— 借鉴 [MCP Hangar Human-in-the-Loop Approval Gate](https://www.mcp-hangar.io/blog/2026-04-13-mcp-hitl-approval-gate)，关键写操作进入人工审批闸门。
- **MCP 实现** —— 使用 [官方 Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)（`mcp` / FastMCP），遵循 [MCP 官方最佳实践](https://mcp-best-practice.github.io/mcp-best-practice/)。
- **业务垂直 server** —— 参考 [erpnext-mcp-server](https://github.com/hatlabs/erpnext-mcp-server)、[maximo-mcp-server](https://www.npmjs.com/package/@soumyaprasadrana/maximo-mcp-server) 等按系统拆分的做法。

## 快速开始

```bash
# 1. 安装依赖（需 Python 3.11+ 与 uv，或用 pip）
uv sync --all-packages        # 或：pip install -e "layers/common" 等

# 2. 启动各层 server（三个终端）
uv run itops-server           # 中层 IT 运维
uv run common-server          # 下层通用工具
uv run gateway-server         # 上层审批网关

# 3. 配置 MCP 客户端连接 gateway（统一入口）
#    见 docs/client-config.md
```

## 目录结构

```
mcp/
├── pyproject.toml              # 根工程编排（可选）
├── README.md
├── docs/                       # 设计文档
│   ├── architecture.md
│   └── client-config.md
├── shared/                     # 跨层共享包（协议/常量/工具）
│   └── mcp_shared/
├── layers/
│   ├── common/                 # 【下层】通用工具 MCP
│   ├── business/               # 【中层】业务系统 MCP
│   │   ├── it_ops/             #   IT 运维（试点）
│   │   ├── purchasing/         #   采购（预留）
│   │   └── manufacturing/      #   制造（预留）
│   └── gateway/                # 【上层】审批网关 MCP
└── scripts/                    # 一键启动脚本
```

## License

企业内私有项目。
