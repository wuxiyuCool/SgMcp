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
| `it_ops` | ✅ 试点中 | IT 运维：服务台工单、变更、资产、监控（Python 轻活） |
| `go_datahub` | ✅ 试点中 | 数据重活：大批量采集 / 多数据库交互（Go，官方 go-sdk） |
| `purchasing` | 🔒 预留 | 采购系统：采购申请、订单、供应商 |
| `manufacturing` | 🔒 预留 | 制造系统：工单、排产、物料 |

**轻重分工**：Python 各层负责协议编排与轻业务；重活（并发采集、Oracle/MySQL/PG/MSSQL 批量读写）
由 Go 的 `go_datahub` 承担，长任务走 `submit_collect_job → get_job_status` 异步 job 模式，
经网关 HITL 审批后放行。选型与实现细节见 `docs/architecture.md` §8。

## 检索与复用说明

本平台**复用了业界成熟的架构模式**，而非重复造轮子：

- **上层网关模式** —— 借鉴 [Akka MCP Gateway](https://github.com/akka/mcp-gateway)（统一入口、认证授权、审计日志）、[Docker MCP Enterprise Gateway](https://www.docker.com/products/mcp-enterprise-gateway/)。网关集中处理认证/授权/审计，下游 server 不各自为政。
- **审批闸门（HITL）** —— 借鉴 [MCP Hangar Human-in-the-Loop Approval Gate](https://www.mcp-hangar.io/blog/2026-04-13-mcp-hitl-approval-gate)，关键写操作进入人工审批闸门。
- **MCP 实现** —— 使用 [官方 Python MCP SDK v2](https://github.com/modelcontextprotocol/python-sdk)（`mcp>=2.2`）的高层 API `MCPServer` + `Client`，遵循 [MCP 官方最佳实践](https://mcp-best-practice.github.io/mcp-best-practice/)。
- **业务垂直 server** —— 参考 [erpnext-mcp-server](https://github.com/hatlabs/erpnext-mcp-server)、[maximo-mcp-server](https://www.npmjs.com/package/@soumyaprasadrana/maximo-mcp-server) 等按系统拆分的做法。

### 技术选型：为什么用官方 SDK v2，而不是 fastmcp v4

生态在 v2 时代发生了合并：官方 SDK 的旧 `FastMCP` 类已更名为 `MCPServer`（`mcp.server.fastmcp` 旧导入路径**已移除、无兼容层**），而社区版 fastmcp（jlowin/PrefectHQ）已独立为 v4。对本项目而言官方 SDK 是唯一合理选择：

| 维度 | 官方 SDK v2 `MCPServer` | fastmcp v4 |
|------|------------------------|------------|
| 依赖体积 | `mcp` 一个包，依赖少 | `fastmcp` 含 `[server]` extra，连带约 25 个依赖 |
| 本项目需要 | 工具暴露 + 客户端 + Streamable HTTP | 大量用不上的高级特性（中间件/代理/持久化等） |
| 维护归属 | MCP 官方仓库，协议对齐最快 | 社区项目，独立演进节奏 |

本项目只需要「暴露工具 + 转发调用 + 双传输」，官方 SDK 已完整覆盖，引入 fastmcp v4 会带来一份实际用不上的依赖树，与「精简」目标相悖。

> **重要实现细节**：本项目 `routing.call_downstream_http` 中下游 `Client` **必须显式传 `mode="legacy"`**。
> SDK v2 的 `Client` 默认 `mode="auto"`，会先探测 `server/discover`、失败后回退 `initialize`（两请求背靠背发出）；实测在该传输上回退请求会把完整 URL 百分号编码进请求路径（`POST http%3A//127.0.0.1%3A9200/mcp` → 404），导致调用失败。本平台各 server 均为老握手协议，显式 `legacy` 既规避该竞态，也省掉一次无意义的探测请求。

## 快速开始

```bash
# 1. 安装依赖（需 Python 3.10+）
#    一键：scripts/setup.ps1 会在项目根建单一 .venv 并 editable 安装全部 6 个包
powershell -ExecutionPolicy Bypass -File scripts/setup.ps1
#    或手动：
uv sync --all-packages        # 或：pip install -e shared -e layers/common -e layers/gateway ...

# 2. 启动各层 server（三个终端）
uv run itops-server           # 中层 IT 运维（默认 9200）
uv run common-server          # 下层通用工具（默认 9100）
uv run gateway-server         # 上层审批网关（默认 9000）
#    中层 Go 重活（可选，需 Go 1.24+；一键脚本会自动构建起停）：
cd layers/business/go_datahub && go build -o bin/datahub-server.exe ./cmd/datahub-server && bin/datahub-server.exe   # 默认 9300
#    Go 与网关分机部署：Go 侧 -addr 0.0.0.0 -token <串>，网关机设 MCP_GODATAHUB_URL / MCP_GODATAHUB_TOKEN
#    （详见 docs/architecture.md §8.6 跨机部署）
#    一键起全部层（HTTP）：scripts/run-demo.ps1

# 3. 配置 MCP 客户端连接 gateway（统一入口）
#    见 docs/client-config.md
```

## 测试与运维自检

| 测试 | 说明 | 运行 |
|------|------|------|
| `tests/smoke_test.py` | **协议级冒烟**：用 SDK 内存 Client 直连各层 server 对象，不起网络、不依赖部署 | `python tests/smoke_test.py` |
| `tests/ops/run_ops_test.py` | **真实链路运维测试集**：经 Streamable HTTP 连真实进程，覆盖连通性/功能/审批流/压测，退出码 0/1 可用于 CI | 见下 |
| `scripts/ops-check.ps1` | **一键自检**：起三层 server → 跑运维测试集 → 停 server | 见下 |

```powershell
# 一键自检（推荐，自动起停 server，退出码 0=全通过）
powershell -ExecutionPolicy Bypass -File scripts/ops-check.ps1
powershell -ExecutionPolicy Bypass -File scripts/ops-check.ps1 -Load 50   # 附带 50 次并发压测

# 手动：先起服务（scripts/run-demo.ps1），再跑测试集
python tests/ops/run_ops_test.py                  # check + smoke + approve 全部
python tests/ops/run_ops_test.py --suite check    # 只查连通性
python tests/ops/run_ops_test.py --load 100       # 附带 100 次并发压测
python tests/ops/run_ops_test.py --url http://127.0.0.1:9000/mcp   # 自定义网关地址
```

运维测试集当前 **26 项断言**，覆盖：三层可达性与工具清单、通用工具功能（含时区）、it_ops 工单/资产读写、go_datahub 异步 job 全流程、db_ping 与 dsn_ref 防泄露、网关只读放行与 HTTP 转发、HITL 审批全流程（挂起→批准执行→驳回不执行→已决不可重复决策→审批放行 Go 采集→未注册路由报错）、并发压测。未部署 Go server 时可用 `--skip-go` 降级。

## 敏感配置

密码/令牌/DSN 一律写在 **不进 git** 的 env 文件里（模板复制即用，打包后可直接改、重启生效）：

| 文件 | 归属 | 内容 |
|------|------|------|
| `config/platform.env` | Python 网关 | 网关→Go 的 URL 与 Bearer 令牌 |
| `layers/business/go_datahub/config/datahub.env` | Go server | 监听地址/端口/令牌 + `DSN_<名称>` 连接串注册表 |

优先级：命令行 > OS 环境变量 > 配置文件。数据库工具参数用 `dsn_ref` 引用服务端 DSN，
密码不经过 AI 客户端 / 网关审批单 / 日志。详见 `docs/architecture.md` §8.7。

## 目录结构

```
mcp/
├── pyproject.toml              # 根工程编排（uv workspace，package=false）
├── README.md
├── config/                     # 敏感配置入口（*.env 不进 git，只留 .example 模板）
├── docs/                       # 设计文档
│   ├── architecture.md
│   └── client-config.md
├── shared/                     # 跨层共享包（审批模型 / 启动器）
│   └── mcp_shared/
├── layers/
│   ├── common/                 # 【下层】通用工具 MCP
│   ├── business/               # 【中层】业务系统 MCP
│   │   ├── it_ops/             #   IT 运维（试点，Python 轻活）
│   │   ├── go_datahub/         #   数据重活（试点，Go 官方 go-sdk）
│   │   ├── purchasing/         #   采购（预留）
│   │   └── manufacturing/      #   制造（预留）
│   └── gateway/                # 【上层】审批网关 MCP
├── tests/
│   ├── smoke_test.py           # 协议级冒烟测试（内存 Client，无需部署）
│   └── ops/                    # 运维工具测试集（真实 HTTP 链路）
│       ├── client.py           #   MCP 客户端小工具
│       └── run_ops_test.py     #   连通性/功能/审批流/压测
└── scripts/                    # 一键脚本
    ├── setup.ps1               #   建 venv + editable 安装
    ├── run-demo.ps1            #   起三层 server
    └── ops-check.ps1           #   一键运维自检（起→测→停）
```

## License

企业内私有项目。
