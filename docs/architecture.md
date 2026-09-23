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

### 1.1 三层工具粒度体系

AI 在网关工具表里看到按「粒度」分层的工具，从粗到细三级（工具名前缀 = 层级/领域语义）：

| 层 | 前缀 | 实现 | 定位 |
|----|------|------|------|
| 第 1 层 领域工具（粗粒度，AI 优先用） | 领域前缀：`itops.*`（IT 运维；制造域将来 `mfg.*`） | 中层各领域 Python MCP | 各领域自定义的常用通用操作 |
| 第 2 层 领域通用查询（中粒度） | `itops.query_dataset` | 中层 Python MCP | 通用工具查不到的本域不常用数据集；dataset_id 用 **Literal 枚举限定本域**（带中文说明），配套 `itops.list_data_tables` 发现数据表 |
| 第 3 层 全局数据平台（细粒度） | `data.*` | 重活层 Go MCP | **全量数据集**枚举（`data.query_dataset`，中文说明构建期动态生成）+ 大批量处理（`data.batch_process` 等） |

数据集域归属与中文说明由配置驱动（`DATASET_<名称>_DOMAIN` / `_DESC`，两侧同一约定）；
第 2 层越域时给可读错误引导去第 3 层 `data.query_dataset`。

## 2. 通信与调用链

```
AI 客户端 ──MCP──▶ [上层 gateway = MCP 聚合 + 鉴权 + 路由（唯一入口）]
                       │  启动时：Aggregator 作为 MCP Client 连各下游
                       │          拉 tools/list → 合并本地工具表（路由）
                       │  gateway_call(server, tool, args)
                       │     ├─ 只读：直接路由转发
                       │     └─ 需审批写操作：建审批单 → 人工 approve → 转发
                       ▼
        [中层业务 Python MCP Server]      [中层重活 Go MCP Server]
         - 暴露给 AI 的工具: business.*    - /mcp          -> 给网关聚合，暴露给 AI
         - 内部方法: 调 Go、调 DB、        - /internal/*   -> 给业务 Python 内部调
           调其他服务（见下）                   （双端点设计，同端口同 Bearer 鉴权）
                       │                        ▲
                       └── 内部 HTTP 直调 /internal/*（不经网关）─┘
```

- 网关内建**聚合器**（`mcp_gateway.aggregator`）：启动时对注册表里的每个下游
  server 建立 MCP Client 连接，拉取 `tools/list` 合并成本地工具表；AI 调工具时
  按路由经 Streamable HTTP 转发到对应下游。**网关不硬编码任何业务工具**——
  下游新增/删除工具，网关重新聚合即同步（运行时可用 `refresh_routes` 工具补拉）。
- **工具命名前缀 = 层级语义**：业务层 Python 统一 `business.*`
  （`@mcp.tool(name="business.xxx")` 别名，函数名不变）；重活层 Go 统一 `heavy.*`。
  AI 在网关工具表里一眼分清工具归属，也便于按前缀配置审批/授权策略。
- **服务间内部调用走 `/internal/*` REST 面（双端点设计）**：重活层 Go server
  同端口两类端点——`/mcp` 只说 MCP 协议（给网关聚合、暴露给 AI）；
  `/internal/v1/*`（HTTP + JSON）给业务 Python 服务间直调，不经网关、不暴露给 AI。
  内部端点（`healthz` / `dsn_refs` / `tables` / `batch/import` / `batch/query`）与
  同名 heavy.* 工具共用 `internal/dbhub` 核心实现：表名列名白名单 + 值参数绑定 +
  错误脱敏，参数错误 4xx、库侧失败 200 + ok=false。业务层用 httpx2 + Bearer
  直调（`MCP_GODATAHUB_TOKEN`，与 `GO_DATAHUB_TOKEN` 同值）。
  内置示例见 it_ops 的 `business.export_assets_to_warehouse` /
  `business.query_warehouse_assets`（完整三跳 `gateway → business → heavy`）。
- 下游注册表来自环境变量（可写入 `config/platform.env`）：`MCP_DOWNSTREAMS`
  （`name=url` 逗号分隔）；`MCP_GODATAHUB_URL/TOKEN` 为 go_datahub 的兼容入口。
  未显式配置时使用默认注册表（it_ops:9200 / common:9100 / go_datahub:9300）。
- 某个下游未就绪时，聚合器在重试窗口内（`MCP_DISCOVERY_RETRY_SECONDS`，默认 6s，
  适配脚本同时拉起全部 server 的启动竞态）反复重试；窗口耗尽仍不可达则跳过告警，
  网关照常启动——下游上线后调 `refresh_routes` 即可补进工具表。
- 转发实现为 `mcp_shared.mcp_client.call_downstream_http`：官方 SDK v2 的
  `Client` 真实转发，已通过 `tests/ops` 端到端验证（真实起进程、真实 HTTP 转发、
  落库产出真实工单号）。结果解包规则与 `tests/ops/client.py` 一致
  （`{"result": [...]}` 信封剥离 + 文本 JSON 解析），转发返回值与下游工具真实
  返回形态保持一致。

**下游 Client 的两个要点：**

1. **必须显式 `mode="legacy"`**。SDK v2 的 `Client` 默认 `mode="auto"`，会先发 `server/discover` 探测、失败后回退 `initialize`，两请求背靠背发出；实测在 streamable-HTTP 传输上回退请求会把完整 URL 百分号编码进请求路径（`POST http%3A//127.0.0.1%3A9200/mcp` → 404 Not Found），导致连接失败。本平台各 server 均使用老握手协议（不存在 `2026-07-28` 现代协议），显式 `legacy` 既规避该竞态，也省掉一次无用探测。此要点仅涉及**网关聚合的 /mcp 面**；`/internal/*` 内部面是普通 HTTP+JSON，业务层直接用 httpx2 调用，无此问题。
2. **同步桥接**：SDK v2 的 sync 工具运行在 worker 线程（无事件循环），`call_downstream_http` 用 `asyncio.run` 起临时 loop 驱动异步 `Client`；不要在已有事件循环的线程中调用本函数。

## 3. 审批流（HITL）

1. AI 调用 `gateway_call(server='it_ops', tool='business.create_change', ...)`。
2. 网关查聚合工具表：`business.create_change` 命中审批策略 `requires_approval=True`
   （默认集合 `business.create_change` / `heavy.submit_collect_job`，可用
   `MCP_APPROVAL_TOOLS` 覆盖，见 `aggregator.requires_approval`）。
3. 网关创建 `ApprovalRequest`（状态 `pending`），返回审批单 ID；**不执行**。
4. 审批人调用 `list_pending_approvals` 查看，用 `approve_request` / `reject_request` 决定。
5. 批准 → 网关调用下游执行；驳回 → 不执行。

审批单模型见 `shared/src/mcp_shared/approval.py`。

## 4. 扩展一个新业务系统（如新增"财务"）

1. 复制 `layers/business/it_ops` 目录为 `layers/business/finance`。
2. 重命名包、写业务工具（`store.py` 换真数据源）。
3. 起新 server（如 `finance-server --transport http --port 9400`）。
4. 在网关侧环境变量 `MCP_DOWNSTREAMS` 追加 `finance=http://127.0.0.1:9400/mcp`
   （或运行期让 AI 调 `refresh_routes` 补拉）——**无需改网关代码**。
5. （可选）如该系统有高风险写工具，把工具名加进 `MCP_APPROVAL_TOOLS`。

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
| 协议级冒烟 | `tests/smoke_test.py` | 下层/中层用 SDK **内存 Client** 直连对象；网关段**进程内起真实 HTTP 下游**（uvicorn 线程），完整验证「聚合器 tools/list 合并 + HTTP 转发 + 审批流」，无需部署 |
| 真实链路 | `tests/ops/run_ops_test.py` | 经 **Streamable HTTP** 连真实进程：验证部署可用性、聚合一致性、审批流、并发稳定性；退出码 0/1 可直接进 CI |
| 一键自检 | `scripts/ops-check.ps1` | 起三层 server → 跑运维测试集 → 停 server，返回退出码 |

两层测试互补：冒烟测试跑得快、适合改代码后立刻验；运维测试集跑得真、适合发版前/巡检。`tests/ops/client.py` 是复用的 MCP 客户端小工具（`call` / `call_raw` / `list_tools`），已内含 `mode="legacy"` 与 `{"result": ...}` 信封解包。

## 8. 中层 Go 重活 server（go_datahub）

### 8.1 定位与轻重分工

| | Python 各层 | Go go_datahub |
|---|---|---|
| 职责 | 协议编排、审批网关、轻业务 CRUD | 大批量数据采集、多数据库高并发交互 |
| 数据源 | 内存 store（试点） | Oracle / MySQL / PG / MSSQL + HTTP + 文件 |
| 位置 | `layers/gateway|common|business/*` | `layers/business/go_datahub`（默认 :9300） |

### 8.2 选型：官方 `modelcontextprotocol/go-sdk`

与全平台「用官方 SDK 而非社区框架」（见 README 技术选型）的哲学一致：
go-sdk 已由 MCP 官方仓库维护（v1.8+，stable，Go 1.24+），协议对齐最快、依赖精简，
提供 typed `mcp.AddTool`（入出参结构体自动推导 JSON Schema 并校验）与
`NewStreamableHTTPHandler`。对比 mark3labs/mcp-go：其差异点（SSE 兼容、更多 hook）
本平台用不上——网关转发只走 Streamable HTTP。吞吐来自 Go 本身
（goroutine + `database/sql` 连接池），与 SDK 无关。

### 8.3 异步 job 模式（关键设计）

大批量采集**不能**在一次 MCP 工具调用里同步完成（网关 HTTP 转发会超时、审批链路会被长阻塞）：

1. `heavy.submit_collect_job(source, params)` 校验采集器后立即返回 `job_id`（`pending`），**不阻塞**；
2. 采集在后台 goroutine 流式执行，逐行累计 `rows` 进度，支持 `heavy.cancel_job` 中止（context 传播）；
3. 客户端轮询 `heavy.get_job_status(job_id)` 取 `running/completed/failed/cancelled`。

网关侧 `heavy.submit_collect_job` 标记 `requires_approval=True`：审批人放行后 job 才真正创建。

### 8.4 采集器框架

`internal/collector` 定义 `Source` 接口（`Collect(ctx, params, emit)`）与注册表，
内置 `synthetic`（压测演示）/ `csv` / `http_json` / `db_query`（四库流式 SELECT）四种。
新数据源 = 实现接口 + `Default()` 注册一行。
`internal/dbhub` 统一多库驱动（纯 Go：go-ora / go-sql-driver / pgx stdlib / go-mssqldb，
免 Oracle Instant Client 等 CGO 依赖）与 DSN 规范；`tableops.go` 提供贴近业务的
库表操作（`heavy.list_tables` / `heavy.batch_import` / `heavy.batch_query`）：
表名列名白名单校验 + 值参数绑定（防注入）、事务批量写入（单次上限 5 万行）、
等值过滤 + 行数上限（防拖库）、四库方言（占位符 pg=$n、oracle=:n、其余=?；
分页 LIMIT / FETCH FIRST / TOP）集中处理，错误经 `Scrub` 脱敏。

### 8.5 与 Python 侧的兼容要点

- Go server 应答老握手协议，网关 `Client(mode="legacy")` 直连无障碍（已由 ops 测试集验证）；
- 工具输出走 typed output → `structured_content`，网关 `call_downstream_http` 优先读结构化字段，
  不依赖 `{"result": ...}` 信封；
- 错误按 Go handler 返回 error → SDK 打包为 `is_error` 工具错误，与 it_ops 行为一致；
- 网关地址可用 `MCP_GODATAHUB_URL` 环境变量覆盖（默认 `http://127.0.0.1:9300/mcp`）；
- 测试：`go test ./cmd/datahub-server/`（内存传输协议级）；`scripts/ops-check.ps1` 会自动构建/起停
  Go server 并跑 34 项断言，无 Go 环境时自动 `--skip-go` 降级。

### 8.5.1 业务名路由与批处理（dataset routing）

AI 与业务层只认业务数据集名，物理库表由路由决定（两侧同一套 env 约定）：

```
db    = routeDB("orders", "t1")          # 按租户路由：DATASET_ORDERS_DSN_T1 → dsn_ref
table = routeTable("orders", "2024-05")  # 按月分表：orders_202405
```

- **Go 侧** `internal/dsrouting`：`heavy.batch_process`（异步 job，立即返回 task_id，
  `heavy.get_job_status` 轮询；任务消息记录路由明细）+ `heavy.list_datasets`（数据集发现）。
  批处理核心 `internal/batchproc`：simulate（演练不触库）/ real（连路由库统计目标表）。
- **Python 侧** `mcp_shared/dsrouting.py`：`business.batch_process`（业务层路由解析，
  real 模式经内部 REST `/internal/v1/batch/process` 下放重活执行）+ `business.list_datasets`。
  两侧数据集清单一致性有测试断言（`gw_list_datasets`）。
- **内部面** 对应端点：`POST /internal/v1/batch/process`、`GET /internal/v1/datasets`。

### 8.6 跨机部署（Go server 独立主机）

Go 服务与 Python 网关分机部署时的配置（调用方法：**Bearer token over Streamable HTTP**，
内网轻量鉴权，DSN 凭证不过公网）：

**Go 主机（远端）**：
```powershell
# 监听所有网卡 + 启用鉴权（token 也可用环境变量 GO_DATAHUB_TOKEN 提供）
datahub-server.exe -addr 0.0.0.0 -port 9300 -token <强随机串>
# 防火墙只放行网关主机 IP 访问 9300：
# New-NetFirewallRule -DisplayName mcp-go-datahub -Direction Inbound -LocalPort 9300 -Protocol TCP -RemoteAddress <网关IP>
```

**网关机（Python 侧环境变量，须在启动 gateway 进程前设置）**：
```powershell
$env:MCP_GODATAHUB_URL   = "http://<go主机IP>:9300/mcp"
$env:MCP_GODATAHUB_TOKEN = "<与 Go 侧一致的 token>"   # 不设则不带 Authorization 头（本机免鉴权场景）
```
路由注册时读取这两个变量，`ToolRoute.headers` 携带 `Authorization: Bearer`，
`call_downstream_http` 经自建 `httpx2.AsyncClient` 注入（SDK 的 `streamable_http_client`
支持传入预配置 http_client，该 context manager 本身即 `Client` 认可的 Transport）。

**脚本行为**：`run-demo.ps1` 检测到 `MCP_GODATAHUB_URL` 指向非本机时不再本地起停 Go；
`ops-check.ps1 -GodhUrl http://<ip>:9300/mcp` 同理，并把该地址传给测试集 `--godh-url` 直测远端。

**注意**：
- token 错误/缺失时 Go 返回 HTTP 401，网关侧表现为 `RuntimeError: 下游 ... 执行失败`；
- go-sdk 自带 DNS-rebinding 防护：仅拦截「本机回环来源 + 非本机 Host」的组合，跨机正常互访不受影响；
- token 属敏感信息：放部署环境变量/密钥管理，勿写入代码库；跨公网请前置 TLS 反代而非裸 9300。

### 8.7 敏感配置统一入口（不进 git，打包后可改）

两端配置文件**分离**（适配分机部署，各自机器各管各的）：

| 文件 | 读取方 | 键 |
|------|--------|-----|
| `config/platform.env` | Python 网关 / ops 测试集（`mcp_shared.config`） | `MCP_GODATAHUB_URL`、`MCP_GODATAHUB_TOKEN` |
| `layers/business/go_datahub/config/datahub.env` | Go server（`internal/config`） | `GO_DATAHUB_ADDR/PORT/TOKEN`、`DSN_<名称>` |

统一规则：
- **不进版本库**：`.gitignore` 排除 `*.env` 与 `**/config/*.env`；仓库只留 `*.example` 模板，
  部署时 `copy platform.env.example platform.env` 填值。
- **优先级**：命令行参数 > OS 环境变量 > 配置文件——临时排障用环境变量覆盖，不改文件。
- **打包后可改**：文件在产物之外（Go 从 exe 位置逐级向上找 6 级；也可用
  `GO_DATAHUB_CONFIG` / `MCP_CONFIG_FILE` 指定绝对路径），改完重启进程即生效，无需重编译。
- **DSN 引用机制（密码零传输）**：数据库连接串只存 Go 服务器的 `DSN_<名称>`，
  MCP 工具参数用 `dsn_ref:"order_pg"` 引用；`heavy.list_dsn_refs` 只回名称不回值；
  驱动报错经 `dbhub.Scrub` 把 DSN/密码替换为 `***` 才返回。
  由此密码不进入 AI 客户端上下文、网关审批单（tool_args）、审计日志与错误回显。
  （兼容：仍可直接传裸 `dsn`，仅建议本机调试用。）
- 回归覆盖：`go test ./internal/config/`（解析/优先级/引号剥离）、`tests/smoke_test.py`
  的 config 段、`tests/ops` 「未知 dsn_ref 不泄露凭证」断言、ops-check 26 项全链路。
