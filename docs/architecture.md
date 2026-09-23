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

1. `submit_collect_job(source, params)` 校验采集器后立即返回 `job_id`（`pending`），**不阻塞**；
2. 采集在后台 goroutine 流式执行，逐行累计 `rows` 进度，支持 `cancel_job` 中止（context 传播）；
3. 客户端轮询 `get_job_status(job_id)` 取 `running/completed/failed/cancelled`。

网关侧 `submit_collect_job` 标记 `requires_approval=True`：审批人放行后 job 才真正创建。

### 8.4 采集器框架

`internal/collector` 定义 `Source` 接口（`Collect(ctx, params, emit)`）与注册表，
内置 `synthetic`（压测演示）/ `csv` / `http_json` / `db_query`（四库流式 SELECT）四种。
新数据源 = 实现接口 + `Default()` 注册一行。
`internal/dbhub` 统一多库驱动（纯 Go：go-ora / go-sql-driver / pgx stdlib / go-mssqldb，
免 Oracle Instant Client 等 CGO 依赖）与 DSN 规范。

### 8.5 与 Python 侧的兼容要点

- Go server 应答老握手协议，网关 `Client(mode="legacy")` 直连无障碍（已由 ops 测试集验证）；
- 工具输出走 typed output → `structured_content`，网关 `call_downstream_http` 优先读结构化字段，
  不依赖 `{"result": ...}` 信封；
- 错误按 Go handler 返回 error → SDK 打包为 `is_error` 工具错误，与 it_ops 行为一致；
- 网关地址可用 `MCP_GODATAHUB_URL` 环境变量覆盖（默认 `http://127.0.0.1:9300/mcp`）；
- 测试：`go test ./cmd/datahub-server/`（内存传输协议级）；`scripts/ops-check.ps1` 会自动构建/起停
  Go server 并跑 25 项断言，无 Go 环境时自动 `--skip-go` 降级。

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
  MCP 工具参数用 `dsn_ref:"order_pg"` 引用；`list_dsn_refs` 只回名称不回值；
  驱动报错经 `dbhub.Scrub` 把 DSN/密码替换为 `***` 才返回。
  由此密码不进入 AI 客户端上下文、网关审批单（tool_args）、审计日志与错误回显。
  （兼容：仍可直接传裸 `dsn`，仅建议本机调试用。）
- 回归覆盖：`go test ./internal/config/`（解析/优先级/引号剥离）、`tests/smoke_test.py`
  的 config 段、`tests/ops` 「未知 dsn_ref 不泄露凭证」断言、ops-check 26 项全链路。
