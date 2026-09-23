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
| **上层 审批** | `layers/gateway` | 统一入口、**聚合器**、审批流、认证授权、审计 | 启动时聚合下游 tools/list、HITL 人工审批、路由转发、RBAC、日志审计 |
| **中层 执行** | `layers/business/*` | 各业务系统按域拆分，独立 server | it_ops 试点；purchasing/manufacturing 预留 |
| **下层 通用** | `layers/common` | 与业务无关的通用工具 | 文件、时间、通知、ID 生成等 |

## 网关聚合器

网关对外是唯一 MCP 入口，对内是各下游 server 的 MCP 客户端：

```
AI ──MCP──▶ 网关（唯一入口）
              ├─ 启动时：作为 MCP Client 连各下游 server
              │           拉 tools/list → 合并成本地工具表（路由）
              └─ AI 调工具：gateway_call → 按路由经 Streamable HTTP 转发
```

- **零硬编码**：网关不写死任何业务工具，下游清单由 `MCP_DOWNSTREAMS`
  （`name=url` 逗号分隔）等环境变量配置，新增业务 server 无需改网关代码。
- **启动竞态容忍**：下游未就绪时每秒重试（窗口 `MCP_DISCOVERY_RETRY_SECONDS`，
  默认 6s）；彻底缺席则跳过告警照常启动，上线后调 `refresh_routes` 工具补拉。
- **审批策略外置**：需 HITL 审批的工具名由 `MCP_APPROVAL_TOOLS` 配置
  （默认 `business.create_change,heavy.submit_collect_job`）。
- 详见 `docs/architecture.md` §2 与 `mcp_gateway/aggregator.py`。

## 工具命名与服务间调用约定

- **命名前缀 = 层级语义**：业务层 Python server 对 AI 暴露的工具统一
  `business.*` 前缀（如 `business.create_incident`）；重活层 Go server 统一
  `heavy.*` 前缀（如 `heavy.batch_import`）。AI 在网关工具表里一眼分清工具归属。
- **服务间内部调用走 `/internal/*` REST 面（双端点设计）**：重活层 Go server
  同端口暴露两类端点——`/mcp` 给网关聚合、暴露给 AI（只说 MCP 协议）；
  `/internal/v1/*` 给业务 Python 服务间直调（HTTP + JSON，不经网关、不暴露给 AI）。
  业务层用 httpx2 + Bearer 直调（复用 `MCP_GODATAHUB_TOKEN`，与 GO_DATAHUB_TOKEN 同值）。
  内部端点清单：`GET /internal/healthz`（存活探针）、`GET /internal/v1/dsn_refs`、
  `GET /internal/v1/tables`、`POST /internal/v1/batch/import`、`POST /internal/v1/batch/query`——
  与同名 heavy.* 工具共用 dbhub 核心实现，行为一致（参数错误 4xx；库侧失败 200 + ok=false）。
  内置示例（it_ops，完整三跳链路 `gateway → business → heavy`）：
  - `business.export_assets_to_warehouse`：CMDB 资产批量归档入库，
    内部直调 `/internal/v1/batch/import`（事务写入、dsn_ref 引用服务端 DSN）。
  - `business.query_warehouse_assets`：查询归档结果，内部直调 `/internal/v1/batch/query`。
- **重活层库表操作**（均列名白名单 + 值参数绑定 + 连接串脱敏，支持 pg/mysql/mssql/oracle）：
  `heavy.list_tables`（列用户表）、`heavy.batch_import`（事务批量写入，单次上限 5 万行）、
  `heavy.batch_query`（等值过滤 + 行数上限防拖库）。目标库经 `dsn_ref` 引用
  `config/datahub.env` 的 DSN 注册表，密码不经 AI/网关/日志流转。

## 三层工具粒度体系

AI 在网关工具表里看到的是按「粒度分层」组织的工具，从粗到细三级：

| 层 | 前缀 | 实现 | 定位 | 代表工具 |
|----|------|------|------|----------|
| **第 1 层 领域工具**（粗粒度，AI 优先用） | 领域前缀：`itops.*`（IT 运维；制造域将来 `mfg.*`） | 中层各领域 Python MCP | 各领域自定义的常用通用操作 | `itops.create_incident`、`itops.batch_process` |
| **第 2 层 领域通用查询**（中粒度） | 领域前缀 + query_dataset | 中层 Python MCP | 通用工具查不到的**本域**不常用数据集，dataset_id 用 **Literal 枚举限定本域**（带中文说明） | `itops.query_dataset`、`itops.list_data_tables`（发现数据表） |
| **第 3 层 全局数据平台**（细粒度） | `data.*` | 重活层 Go MCP | **全量数据集**枚举 + 大批量处理 | `data.query_dataset`、`data.list_datasets`、`data.batch_process` |

- 数据集的域归属与中文说明由配置驱动：`DATASET_<名称>_DOMAIN`（如 itops/order）、
  `DATASET_<名称>_DESC`（如「IT 运维事件单（工单流水）」）；第 3 层的全量枚举描述在
  Go server 构建期动态生成（配置变化重启即生效），AI 看名知义。
- 分层引导：第 2 层越域（如 it_ops 域想查订单）→ 可读错误引导改用第 3 层
  `data.query_dataset`；第 3 层枚举覆盖全量数据集，不常用数据集先
  `data.list_datasets` / `itops.list_data_tables` 发现再查。

## 业务名路由与批处理（dataset routing）

AI 与业务层只认**业务数据集名**（不是表名），物理库表由路由规则决定：

```
用户："帮我批量处理上个月的订单数据"
→ AI 调 heavy.batch_process(dataset_id="orders", period="2024-05", tenant_id="t1")
→ 网关转发到 Go MCP → 内部：
     db    = routeDB("orders", "t1")          # 按租户路由（DATASET_ORDERS_DSN_T1）
     table = routeTable("orders", "2024-05")  # 按月分表（orders_202405）
     异步执行 → 立即返回 task_id → heavy.get_job_status 轮询
→ AI 回复用户"任务已提交"
```

- 路由配置（env，Go/Python 两侧同一套约定，见 `datahub.env.example`）：
  `DATASET_<名称>_DBTYPE`、`DATASET_<名称>_DSN_<租户大写>`（租户专属）、
  `DATASET_<名称>_DSN_DEFAULT`（兜底）；值为 `dsn_ref`，不存连接串。
- **两侧都实现了同款**：Go `heavy.batch_process`（异步 job，`heavy.list_datasets`
  发现数据集）；Python `business.batch_process`（业务层路由解析，real 模式经内部
  REST 下放重活层执行）+ `business.list_datasets`。两侧路由清单一致（有测试断言）。
- mode=simulate 演练（不触库，任意环境可跑通全流程）；mode=real 真实执行。

## 业务操作多通道（SQL / API 双实现）

同一业务操作支持多种落地通道，以事件单为例：

| 通道 | business.create_incident / list_incidents 的实现 | 依赖 |
|------|--------------------------------------------------|------|
| `local`（默认） | 内置内存库，零依赖演示 | 无 |
| `sql` | 业务名路由（`incidents` 数据集）→ 内部直调重活层批量写/查库 | `DATASET_INCIDENTS_*` + 真实库 |
| `api` | 调外部 ITSM 系统 REST API | `MCP_ITSM_API_URL`（+ 可选 `MCP_ITSM_API_TOKEN`） |

外部 API 未配置时返回可读错误引导切换通道；外部系统按需对接（POST `/incidents`、
GET `/incidents?status=`，返回形态归一化为列表）。

## 中间层分工策略

中层按业务域拆分为多个独立 MCP server，每个 server 负责一个系统：

| Server | 状态 | 分工 |
|--------|------|------|
| `it_ops` | ✅ 试点中 | IT 运维：服务台工单、变更、资产、监控（Python 轻活） |
| `go_datahub` | ✅ 试点中 | 数据重活：大批量采集 / 多数据库交互（Go，官方 go-sdk） |
| `purchasing` | 🔒 预留 | 采购系统：采购申请、订单、供应商 |
| `manufacturing` | 🔒 预留 | 制造系统：工单、排产、物料 |

**轻重分工**：Python 各层负责协议编排与轻业务；重活（并发采集、Oracle/MySQL/PG/MSSQL 批量读写）
由 Go 的 `go_datahub` 承担，长任务走 `heavy.submit_collect_job → heavy.get_job_status` 异步 job 模式，
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
| `tests/smoke_test.py` | **协议级冒烟**：下层/中层用 SDK 内存 Client 直连 server 对象；网关段进程内起真实 HTTP 下游，验证聚合器 + 审批流 | `python tests/smoke_test.py` |
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

运维测试集当前 **34 项断言**，覆盖：三层可达性与工具清单、**网关聚合一致性**（list_routes == 下游直连 tools/list 并集）、通用工具功能（含时区）、it_ops 工单/资产读写、go_datahub 异步 job 全流程、**业务名路由批处理全流程**（heavy/business 两侧 routeDB 按租户 + routeTable 按月分表 + task_id 轮询，两侧数据集清单一致）、库表操作（list_tables/batch_import 失败路径与表名白名单）、db_ping 与 dsn_ref 防泄露、**business→heavy 内部 REST 直调链路**、**事件单 api 通道未配置报可读错误**、网关只读放行与 HTTP 转发、HITL 审批全流程（挂起→批准执行→驳回不执行→已决不可重复决策→审批放行 Go 采集→未注册路由报错）、并发压测。未部署 Go server 时可用 `--skip-go` 降级。

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
