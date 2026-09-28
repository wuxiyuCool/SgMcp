# SgMcp 开发部署手册

> 企业级三层 MCP 平台：Python 干轻活（网关/审批/轻业务），Go 干重活（大批量采集/多数据库交互）。
> 本文档覆盖：代码分层、MCP 工具编写位置、本地调试、离线打包、Linux 部署、Go 独立机部署、常见问题。

---

## 1. 代码层级总览

```
SgMcp/
├── config/                          # 【Python 侧敏感配置入口】*.env 不进 git，只留 .example
│   └── platform.env.example         #   网关→Go 的 URL / Bearer 令牌
├── shared/                          # 跨层共享包（改这里 → 所有 Python server 生效）
│   └── src/mcp_shared/
│       ├── approval.py              # 审批单模型
│       ├── server_kit.py            # 统一启动器（--transport/--host/--port）
│       └── config.py                # platform.env 加载器（OS 环境变量 > 文件）
├── layers/
│   ├── gateway/                     # 【上层·审批网关】唯一对外入口 :9000
│   │   └── src/mcp_gateway/
│   │       ├── main.py              # ★ 网关工具 + 聚合路由工具 route_*（动态重建）
│   │       ├── routing.py           #   下游转发（HTTP/local）、ToolRoute 定义
│   │       └── approvals.py         #   HITL 审批闸门
│   ├── common/                      # 【下层·通用工具】:9100
│   │   └── src/mcp_common_server/
│   │       └── tools.py             # ★ 通用工具写这里（now/echo/hash_text/json_tool/datetime_convert…）
│   ├── business/
│   │   ├── it_ops/                  # 【中层·IT运维 Python】:9200
│   │   │   └── src/mcp_itops/
│   │   │       ├── main.py          # ★ 工单/变更/资产工具写这里
│   │   │       └── store.py         #   数据层（试点为内存 store，接真实库改这里）
│   │   ├── purchasing/              # 【中层·采购 预留】结构同 it_ops
│   │   ├── manufacturing/           # 【中层·制造 预留】
│   │   └── go_datahub/              # 【中层·数据重活 Go】:9300（独立机部署）
│   │       ├── cmd/datahub-server/
│   │       │   └── main.go          # ★ Go MCP 工具定义（AddTool）写这里
│   │       ├── internal/
│   │       │   ├── collector/       # ★ 采集器框架：Source 接口 + 内置采集器
│   │       │   │   └── collector.go #   新数据源 = 实现 Collect() + Default() 注册一行
│   │       │   ├── filetools/       # ★ 文件重工具：流式哈希/统计/CSV↔JSONL 转换
│   │       │   │   └── filetools.go #   data_hash_file / data_file_stats / data_convert_file
│   │       │   ├── dbhub/           #   多库驱动统一层（oracle/mysql/pg/mssql）+ 错误脱敏
│   │       │   ├── jobs/            #   异步 job 注册表（submit→轮询模式）
│   │       │   └── config/          #   Go 侧配置加载（datahub.env）
│   │       └── config/
│   │           └── datahub.env.example  # 监听/令牌/DSN_<名称> 注册表
├── tests/
│   ├── smoke_test.py                # 协议级冒烟（内存 Client，不起网络）
│   └── ops/run_ops_test.py          # 真实 HTTP 链路 52 项断言（含企业化守护套件，可进 CI）
└── scripts/
    ├── setup.ps1 / run-demo.ps1 / ops-check.ps1      # Windows 开发
    └── serversctl.sh                                 # Linux 启停（start|stop|restart|status）
```

★ = 日常写业务代码的位置。

### 1.1 当前对外方法清单（按域）

AI 客户端只需记忆 3 个 route_* 工具（见 §2.5）；下列方法是各域 `method` 枚举取值。

**common-tools（下层·轻活，15）**：`now` `timestamp` `echo` `slugify` `generate_id`
`hash_text` `base64_codec` `uuid_generate` `random_string` `json_tool`
`datetime_convert`（时区转换+偏移）`url_parse` `regex_find` `text_stats` `server_self_check`（下层自检）

**it_ops（中层·业务，13）**：`itops_create_incident` `itops_list_incidents` `itops_update_incident_status`
`itops_create_change`【需审批】 `itops_get_change` `itops_register_asset` `itops_list_assets`
`itops_export_assets_to_warehouse` `itops_query_warehouse_assets` `itops_batch_process`
`itops_list_datasets` `itops_query_dataset` `itops_list_data_tables`

**go_datahub（中层·重活，17）**：
- 采集 job：`data_list_sources` `data_submit_collect_job`【需审批】 `data_get_job_status` `data_list_jobs` `data_cancel_job`
- 数据库：`data_list_dsn_refs` `data_db_ping` `data_list_tables` `data_db_query_preview` `data_batch_query` `data_batch_import`
- 业务路由：`data_list_datasets` `data_query_dataset` `data_batch_process`
- 文件：`data_hash_file`（流式哈希）`data_file_stats`（大小/行数/编码/预览）`data_convert_file`（CSV↔JSONL 互转）

## 2. 编写 MCP 工具

### 2.1 Python 轻活（官方 SDK v2 `MCPServer`）

在对应 server 的入口文件里加函数即可（`layers/common/.../tools.py`、`layers/business/it_ops/.../main.py` 等）：

```python
@mcp.tool()
def my_tool(param: str, limit: int = 10) -> dict:
    """一句话描述（AI 客户端可见，写清楚参数含义和取值范围）。"""
    return {"ok": True, "param": param}
```

要点：
- 类型注解即输入 schema；返回 `list` 会被 SDK 包成 `{"result": [...]}` 信封，返回 `dict` 不包
- 抛异常 → 协议层 `is_error` 工具错误（网关会转成可读报错）

### 2.2 Go 重活（官方 go-sdk `mcp.AddTool`）

在 `layers/business/go_datahub/cmd/datahub-server/main.go` 的 `buildServer()` 里加：

```go
mcp.AddTool(server, &mcp.Tool{
    Name:        "my_heavy_tool",
    Description: "描述（含参数说明）",
    Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
}, func(ctx context.Context, req *mcp.CallToolRequest, in MyIn) (*mcp.CallToolResult, MyOut, error) {
    return nil, MyOut{...}, nil   // 返回 error 即工具级错误；Out 自动成为结构化输出
})
```

大批量/长耗时任务**一律走 job 异步模式**（勿做同步大工具）：
`submit_collect_job`（立即返回 job_id）→ 后台 goroutine 流式采集 → `get_job_status` 轮询。
新数据源实现 `collector.Source` 接口（`Collect(ctx, params, emit)`）并在 `Default()` 注册。

### 2.3 数据库访问（Go 侧）

- 连接串**只写**在 `config/datahub.env`：`DSN_ORDER_PG=postgres://...`
- 工具参数用 `dsn_ref: "order_pg"` 引用（`list_dsn_refs` 可查已登记名称）
- 驱动报错自动脱敏（`dbhub.Scrub`），密码不进 MCP 消息/审批单/日志

### 2.4 新工具接入网关（零注册，自动聚合）

网关内置**聚合器**：启动时作为 MCP Client 连接各下游 server 拉取 `tools/list`，
自动合并进路由表——**新增工具不需要在网关注册任何东西**，重启下游 + 网关调
`refresh_routes`（或重启网关）即生效。只需遵守命名约定：

- 工具名**不含点号**，用层级前缀：`itops_*`（IT 运维域）/ `data_*`（Go 数据域）/ 无前缀（common）
- 审批策略在环境变量 `MCP_APPROVAL_TOOLS`（逗号分隔工具名）配置；未设置用默认集合
  （`itops_create_change` / `data_submit_collect_job`）

### 2.5 AI 客户端暴露模型（route_* 聚合路由）

网关**不把下游工具逐个展示**给 AI。AI 连接 `http://网关:9000/mcp` 后看到的工具：

| 工具 | 作用 |
| --- | --- |
| `route_it_ops` / `route_common_tools` / `route_go_datahub` | **日常调用主入口**：一个下游域一个路由工具。`method` 参数是枚举（自动列出该域全部可调用方法），`params` 传该方法入参（对象 / JSON 字符串 / 扁平 `k=v;k2=v2` 串均可）。工具描述逐方法标注 `参数(*必填)` 与 `【需审批】` |
| `gateway_call` | 跨域通用入口（server+tool+arguments；arguments 同支持三形态，原 `gateway_call_kv` 已并入） |
| `list_routes` | 全量路由明细（server/tool/入参 schema/审批标记） |
| `list_tool_catalog` | 工具目录说明书：按 MCP server 分组，逐工具标注所在下游（in_mcp/endpoint）、参数类型与必填、`route_*` 可直接套用的调用示例，及「AI→网关→route_*→HTTP 转发→下游」的完整传递链 |
| `refresh_routes` / `list_downstreams` | 运行时补拉工具表并**重建 route_* 枚举** / 诊断下游清单 |
| `list_pending_approvals` / `list_approvals` / `approve_request` / `reject_request` / `retry_execution` | HITL 审批：待办、台账、决策（需审批令牌）、执行失败重放 |
| `describe_gateway` | 部署自检：路由数、下游聚合健康度、**生效的安全开关**（令牌/Bearer/超时/上限） |
| `query_audit_log` | 调用审计回查（按 event/server/tool/call_id 过滤，敏感入参已脱敏） |

写操作可调 `idempotency_key`（`gateway_call` 与 `route_*` 都支持）：AI 平台超时重发时，
同一 key 不会二次执行——这是对"模型重试 = 重复建单"这类事故的兜底。

route_* 工具由 `aggregator.on_sync → rebuild_route_tools()` 在每轮聚合后整体重建，
枚举始终与下游最新工具表一致。新增下游 server 时同样自动生成对应 route 工具，
无需改网关代码。

## 3. 本地开发调试（Windows）

```powershell
powershell -File scripts/setup.ps1        # 建 .venv + editable 安装（或 uv sync --all-packages，
                                          # 内网需 UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/）
powershell -File scripts/run-demo.ps1     # 一键起 common:9100 / itops:9200 / gateway:9000 (+本地 Go)
powershell -File scripts/ops-check.ps1    # 一键自检：起服务→52 项断言（含 guard 守护套件）→停服务
python tests/smoke_test.py                # 改代码后的秒级协议冒烟
cd layers/business/go_datahub; go test ./...   # Go 侧内存链路测试
```

## 4. 打包流程（Windows 开发机 → Linux 离线包）

产物：自包含 `sgmcp-deploy-<日期>.tar.gz`（源码 + Linux/Windows Go 二进制 + cp3xx 离线 wheels + 安装/启停脚本）。

```bash
# ① Go 交叉编译（纯 Go 驱动，CGO 关，无 glibc 依赖）
cd layers/business/go_datahub
GOOS=linux   GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o /tmp/godeploy/datahub-server ./cmd/datahub-server
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -o /tmp/godeploy/datahub-server.exe ./cmd/datahub-server

# ② 源码快照（git archive 自动排除未跟踪垃圾；.gitattributes 保证 .sh 导出为 LF）
mkdir -p $ST/app && git archive HEAD | tar -x -C $ST/app
# 放入 Go 二进制到 app/layers/business/go_datahub/bin-linux/

# ③ 离线 wheels（关键：按目标机 Python 版本解析，marker 按 Linux 求值）
uv pip compile req.in --python-platform x86_64-unknown-linux-gnu --python-version 310 \
    --find-links $ST/wheels -o $ST/requirements-linux.txt     # req.in 内容：mcp-gateway-server
python -m pip wheel --no-deps -w $ST/wheels ./shared ./layers/common ./layers/business/it_ops \
    ./layers/business/purchasing ./layers/business/manufacturing ./layers/gateway
python -m pip download -r $ST/requirements-linux.txt -d $ST/wheels -f $ST/wheels --no-deps \
    --only-binary=:all: --platform manylinux2014_x86_64 --python-version 310 --implementation cp

# ④ 打包
tar czf sgmcp-deploy-$(date +%Y%m%d).tar.gz -C $ST . 
```

⚠️ **改过任何 Python 源码后，必须重建 ③ 里的本地 6 个 wheel 再打包**——离线安装装的是 wheel 拷贝，源码新≠包里的 wheel 新（本项目踩过：`--host` 修复进了源码但 wheel 是旧的）。

## 5. 部署流程

### 5.0 systemd 守护（生产推荐）

三个 Python server + Go server 均提供 unit 模板（`deploy/systemd/`），一键安装：

```bash
# 网关机（root）：
bash deploy/systemd/install-units.sh /opt/mcp/sgmcp-deploy/app        # 开机自启 + 崩溃自动拉起
systemctl status mcp-gateway mcp-itops mcp-common
journalctl -u mcp-gateway -f              # 日志（替代 logs/*.log）
# 改端口/监听：编辑 /etc/systemd/system/mcp-*.service 里 Environment= 后 systemctl daemon-reload && restart

# Go 数据机：bash deploy/systemd/install-datahub-unit.sh /opt/datahub（详见 §5.2）
```

安装脚本会自动停掉 `serversctl.sh` 拉起的旧进程避免端口冲突。`serversctl.sh` 保留用于无 root 场景与临时调试。

### 5.1 Linux 网关机（Python 三层）

```bash
# 上传解压（目标机 Python 3.10 用 cp310 包；3.12 用 cp312 包）
scp sgmcp-deploy-*.tar.gz user@10.45.34.223:/opt/mcp/ && ssh user@10.45.34.223
cd /opt/mcp && tar xzf sgmcp-deploy-*.tar.gz && cd sgmcp-deploy/app

# 装依赖（二选一）
bash ../install-offline.sh     # 无外网：pip --no-index 从 ../wheels 装
bash ../install-online.sh      # 有外网：uv sync --all-packages（editable，日后覆盖源码即生效）

# 配敏感文件（不进包，现场创建）
cp config/platform.env.example config/platform.env && vi config/platform.env
#   MCP_GODATAHUB_URL=http://<Go机IP>:9300/mcp
#   MCP_GODATAHUB_TOKEN=<与 Go 机 GO_DATAHUB_TOKEN 一致>

# 启停管理
HOST=0.0.0.0 bash scripts/serversctl.sh restart    # ★ HOST 不设默认只监听 127.0.0.1
bash scripts/serversctl.sh status                  # 三个 [UP] 即正常
ss -tlnp | grep 9000                               # 应见 *:9000
NO_PROXY='*' .venv/bin/python tests/ops/run_ops_test.py --skip-go   # 无 Go 层降级跑，退出码 0 打 OPS_TEST_OK

# 防火墙（对外提供时）
firewall-cmd --add-port=9000/tcp --permanent && firewall-cmd --reload
```

升级发版 = 传新包 → 覆盖解压 → `HOST=0.0.0.0 bash scripts/serversctl.sh restart`
（离线 wheel 安装方式需追加：`.venv/bin/pip install --no-index --find-links ../wheels --force-reinstall mcp-gateway-server mcp-shared ...`）

### 5.2 Go 数据机（独立服务器）

推荐 systemd 守护，一键脚本（离线包内 `deploy/systemd/install-datahub-unit.sh`）：

```bash
# 1) 摆放两样东西：
mkdir -p /opt/datahub/config
cp <包>/sgmcp-deploy/app/layers/business/go_datahub/bin-linux/datahub-server /opt/datahub/
chmod +x /opt/datahub/datahub-server
cp <包>/sgmcp-deploy/app/layers/business/go_datahub/config/datahub.env.example /opt/datahub/config/datahub.env

# 2) 填配置（datahub.env 关键项）：
#    GO_DATAHUB_ADDR=0.0.0.0
#    GO_DATAHUB_TOKEN=<与网关机 MCP_GODATAHUB_TOKEN 一致>
#    DSN_ORCL_ERP=oracle://...   DSN_ORCL_WMS=oracle://...   （同类型可配任意多个源，类型自动推断）
#    DSN_MSSQL_FIN=sqlserver://...?database=finance

# 3) 安装守护（root）：
bash <包>/sgmcp-deploy/app/deploy/systemd/install-datahub-unit.sh /opt/datahub

# 4) 验证：
systemctl status datahub-server
journalctl -u datahub-server -f
ss -tlnp | grep 9300
firewall-cmd --add-port=9300/tcp --permanent && firewall-cmd --reload   # 建议只放行网关机 IP
```

unit 只注入路径/用户；监听、token、DSN 全由 `config/datahub.env` 控制，改完 `systemctl restart datahub-server` 即生效。
临时试跑：`cd /opt/datahub && ./datahub-server`（前台，自动向上找 config）。

### 5.3 AI 客户端接入

| 配置项 | 值 |
|--------|-----|
| 传输类型 | HTTP Streamable |
| 服务 URL | `http://<网关机IP>:9000/mcp` ← **`/mcp` 路径必须带** |
| 认证 | 无（Bearer 仅用于网关→Go 内部链路） |

### 5.4 配置优先级（两端一致）

**命令行参数 > OS 环境变量 > .env 配置文件**。打包后动态改配置：直接编辑对应 .env 重启进程，无需重新编译/打包。

## 6. 常见问题速查

> **AI 平台「测试连接」报 `Initialization failed … status 500`（或 421/403）**：
> 九成是 Host/Origin 校验没过，不是令牌问题。看网关日志
> （`journalctl -u mcp-gateway | grep -i "Invalid Host\|Invalid Origin"`）：
> - 有 `Invalid Host header: <你填的地址>` → 网关以 `--host 127.0.0.1` 启动却被外部访问。
>   改 `MCP_HOST=0.0.0.0` 对外服务（推荐），或配 `MCP_ALLOWED_HOSTS=<该地址>:*`。
> - 有 `Invalid Origin header` → 同理补 `MCP_ALLOWED_ORIGINS=<scheme>://<地址>:<端口>`。
> - 都没有、日志里是 401 → 才是 `MCP_GATEWAY_TOKEN` 不匹配。
> **注意：实测该平台「测试连接」会把 401 与 421 都显示成 `status 500: Internal Server Error`**，
> 所以「加了指令头反而连不上」通常是头的**值**不对（令牌不一致），不是格式问题——
> `Authorization` 现在三种写法都认：`Bearer xxx` / `bearer xxx` / 裸 `xxx`。
> 自查命令（绕开 MCP 协议直接看状态码）：
> ```bash
> curl -s -o /dev/null -w "HTTP %{http_code}" -X POST http://<网关>:9000/mcp \
>   -H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
>   -H 'Host: <网关IP>:9000' -H 'Origin: http://<网关IP>:9000' \
>   -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"1"}}}'
> ```
> 期望 200；421/403 即上述 Host/Origin 问题，401 是令牌问题。

| 症状 | 原因/解法 |
|------|-----------|
| 客户端连不上，curl `/mcp` 返回 400 Missing session ID | 服务正常！检查 URL 是否带 `/mcp` |
| curl 超时 / refused | 未用 `HOST=0.0.0.0` 启动，或防火墙未放行 |
| 加了 `Authorization` 反而连不上（平台报 500） | 令牌值不一致（401）。用 `curl -w "%{http_code}"` 看真实码；注意别同时配「认证方式=API Key」和自定义 `Authorization`，两个头会互相覆盖 |
| `unrecognized arguments: --host` | venv 里是旧 server_kit：覆盖源码（editable）或 force-reinstall mcp-shared |
| 改了源码但服务器行为没变（离线安装） | wheel 是拷贝安装，需 `--force-reinstall` 重装对应包 |
| Linux 报 `bad interpreter: /usr/bin/env bash^M` | .sh 被转成 CRLF：仓库已用 .gitattributes 强制 LF，重新导出即可 |
| 网关调用报 `Error executing tool gateway_call` | 看 message：未注册路由→错误里已列出该 server 可用工具；AI 端应改用对应域的 **`route_*`** 工具（method 枚举不会猜错名），或先调 **`list_routes`** 确认 server/tool |
| AI 客户端首两次 gateway_call 失败后成功 | 模型在猜工具名/参数形状；接入后先让它 `list_routes` 一次即可避免 |
| Windows 终端中文乱码 | GBK 控制台显示问题，设 `PYTHONIOENCODING=utf-8`，不影响数据 |
| Go 采集任务一直 pending/running | `get_job_status` 轮询；失败看 `error` 字段与 Go 进程日志 |

## 7. 安全边界

- 真实 `.env`（token/DSN）永不进 git（`.gitignore` + 只提交 `.example`）
- 密码零传输：数据库连接串只存 Go 机本地，工具传 `dsn_ref` 名称
- 高风险写操作（含 `submit_collect_job`）必须经网关 HITL 审批后执行
- 对外只开 9000；9100/9200/9300 内网互通；跨公网必须前置 TLS 反代


---

## 8. 企业化开关与上线清单

### 8.1 安全/可靠性开关（全部在 `config/platform.env`，不配即开发默认）

| 键 | 默认 | 作用 | 不配的代价 |
|----|------|------|------------|
| `MCP_GATEWAY_TOKEN` | 空 | 网关端口 Bearer 鉴权 | 任何能连到 :9000 的人=可以调用全部下游工具 |
| `MCP_SERVER_TOKEN` | 空 | 业务/下层端口 Bearer 鉴权（各 server 同名） | 绕过网关直连下游即可跳过审批 |
| `MCP_DOWNSTREAM_TOKEN_<域名大写>` | 空 | 网关→下游的 Bearer 头 | 下游启用鉴权时聚合 401 |
| `MCP_APPROVAL_TOKEN` | 空 | **审批令牌**：approve/reject/retry 必须携带 | AI 可自问自答放行高风险写操作 |
| `MCP_RATE_LIMIT_PER_SECOND` | 0（不限） | 按客户端滑动窗口限流，超限 429 | 平台重试风暴直接打穿下游 |
| `MCP_TRUSTED_PROXY` | 空 | 可信反代 IP/CIDR，仅此来源采信 X-Forwarded-For | 伪造头即可绕过限流分桶 |
| `MCP_ALLOWED_HOSTS` / `MCP_ALLOWED_ORIGINS` | 空（=沿用 SDK 默认） | 本机绑定时追加放行域名/IP（支持 `host:*`） | 用域名或内网 IP 访问时 initialize 被 421/403 拒（AI 平台显示成 500） |
| `MCP_DOWNSTREAM_TIMEOUT_SECONDS` | 60 | 下游调用整体超时（可按工具覆盖） | 下游半死→worker 线程挂死→网关整体不可用 |
| `MCP_DOWNSTREAM_RETRIES` | 1 | **只读**工具的失败重试次数（写操作恒不重试） | 手动重试写操作=重复落库 |
| `MCP_IDEMPOTENCY_TTL_SECONDS` | 600 | `idempotency_key` 缓存窗口（0=关闭） | AI 重发即二次执行 |
| `MCP_MAX_RESULT_ITEMS` / `MCP_MAX_RESULT_CHARS` | 200 / 20000 | 返回给 AI 的结果规模上限（超限截断并引导） | 一次查询吃掉整个上下文窗口 |
| `MCP_APPROVAL_TTL_MINUTES` | 240 | 审批单超时自动关闭 | 陈旧参数快照被放行 |
| `MCP_APPROVAL_DEDUPE_SECONDS` | 300 | 等值请求合并到同一张审批单 | 一次重试堆 N 张单让人重复决策 |
| `MCP_AUDIT_DIR` | `<root>/data/audit` | 审计与审批台账目录 | — |
| `MCP_AUDIT_MAX_BYTES` / `MCP_AUDIT_KEEP` | 20MB / 3 | 审计文件按大小轮转与保留份数（防写满磁盘） | 长期运行撑爆数据盘 |
| `MCP_AUDIT_SLOW_MS` | 3000 | 慢调用告警阈值 | — |
| `MCP_FILE_ROOTS` | 空=拒绝 | Python 侧文件类入参的路径白名单 | 任意文件读取 |
| `GO_DATAHUB_FILE_ROOTS` | 临时/工作/exe 目录 | Go 文件工具路径围栏 | AI 可读本机任意文件、覆盖写 |
| `GO_DATAHUB_POOL_MAX` / `GO_DATAHUB_POOL_IDLE_SECONDS` | 8 / 600 | 数据库连接池复用与空闲回收 | 每次调用重连，跨机房明显变慢 |
| `GO_DATAHUB_JOB_HISTORY` | 500 | 异步任务历史保留条数 | 长跑进程任务表只增不减 |
| `GO_DATAHUB_IMPORT_GROUP` | 500 | 批量导入单语句合并行数 | 5 万行=5 万次往返 |

### 8.1.1 三层怎么读到这些开关

- **systemd（Linux）**：`mcp-gateway` / `mcp-itops` / `mcp-common` 三个 unit 都带
  `EnvironmentFile=-@APP_DIR@/config/platform.env`，改完文件 `systemctl daemon-reload && systemctl restart mcp-*` 即生效。
  三个 Python 层默认共用同一个 `MCP_SERVER_TOKEN`；要让某层用不同令牌，在该 unit 里再加一行
  `Environment=MCP_SERVER_TOKEN=xxx` 覆盖，并同步网关侧的 `MCP_DOWNSTREAM_TOKEN_<域名大写>`。
- **serversctl.sh / 手工起**：`load_platform_env()` 会从 cwd 逐级向上找 `config/platform.env`，
  所以在部署目录里直接 `bash scripts/serversctl.sh restart` 也会读到；OS 环境变量优先于文件。
- **Go 层**：只读自己的 `layers/business/go_datahub/config/datahub.env`（或 `GO_DATAHUB_CONFIG` 指定），
  与 Python 侧文件分离，适配分机部署。

### 8.2 上线前自检（三步）

```bash
# 1) 端口活着且真能应答（/healthz 免鉴权）
bash scripts/serversctl.sh health
curl -s http://127.0.0.1:9300/internal/healthz      # Go 层（版本/连接池/任务概览）

# 2) 网关侧配置生效情况——重点看 security
#    （AI 客户端或运维脚本调 describe_gateway）
curl -s -X POST http://127.0.0.1:9000/healthz        # 应为 {"ok":true,...}

# 3) 全链路回归（起服务 + 53 项断言，含企业化守护套件）
powershell -ExecutionPolicy Bypass -File scripts/ops-check.ps1              # Windows 默认模式
powershell -ExecutionPolicy Bypass -File scripts/ops-check.ps1 -Secure      # 全链路鉴权模式（下条）
bash scripts/serversctl.sh restart && .venv/bin/python tests/ops/run_ops_test.py   # Linux
```

`describe_gateway.security` 里 `approval_token_required` 与 `gateway_bearer_enabled`
只要有一个是 `false`，就是**未达生产标准**（ops-check 会打印提示）。

### 8.2.1 `-Secure` 全链路鉴权自检（推荐的验收方式）

```powershell
powershell -ExecutionPolicy Bypass -File scripts/ops-check.ps1 -Secure
```

脚本临时生成随机令牌并写入本次进程环境（不落盘、不进 git），四层全部以 Bearer 启动，
然后跑完整 53 项断言。它额外证明了默认模式证明不了的东西：

- 不带 `Authorization` 连不上网关（401），`/healthz` 仍免鉴权可探活；
- 网关用 `MCP_DOWNSTREAM_TOKEN_IT_OPS` / `_COMMON_TOOLS` / `MCP_GODATAHUB_TOKEN`
  能正常聚合启用了鉴权的下游（配错会直接在 `list_downstreams` 里显示 `unauthorized`）；
- **审批闸门**：无令牌 `approve_request` 被拒，审批人带 `X-Sg-Approval-Token` 请求头即放行
  （断言名「审批令牌闸门（AI 不可自批）」，默认模式下显示"跳过"）；
- Go 层带 token 时 `/mcp`、`/internal/*`、`/internal/healthz` 均可用。

### 8.3 审计与台账在哪

| 文件 | 内容 |
|------|------|
| `data/audit/enterprise-gateway.jsonl` | 网关路由级事件：`tool_call` / `tool_error` / `approval_*` / `idempotent_hit` / `routes_refreshed` |
| `data/audit/<server>.jsonl` | 各层 server 自己收到的每次 `tools/call`（由 `run_server` 自动挂载的中间件写入） |
| `data/audit/approvals.jsonl` | 审批台账事件流（创建/决策/执行），网关启动时重放恢复待办 |

入参里的 `token`/`password`/`dsn`/`authorization` 等敏感键在落盘前统一替换为 `***`，
长文本截断并标注原长；回查用网关工具 `query_audit_log`（按 `call_id` 可串起跨层链路）。

### 8.4 排障：报错怎么读

网关转发的失败消息形如：

```
调用下游 data_batch_import 失败：…｜kind=unreachable｜建议：下游服务不可达。请用
list_downstreams 查看聚合状态…（已尝试 1 次）｜call_id=cal_5f3a…
```

- `kind` 决定动作：`unreachable`→下游没起；`unauthorized`→两侧 token 不一致；
  `timeout`→改异步任务或按工具调大超时；`tool_error`→按下游原文改入参。
- **写调用 `已尝试 1 次` 是固定的**：网关不对写操作自动重试，重复执行只会发生在你自己重发时——
  带上 `idempotency_key` 就安全。
- 拿 `call_id` 去 `query_audit_log` 与 `logs/*.log`、Go 侧日志里查同一笔调用的全链细节。
