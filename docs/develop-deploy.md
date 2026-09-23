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
│   │       ├── main.py              # ★ 网关工具 + 路由注册表 _build_router()
│   │       ├── routing.py           #   下游转发（HTTP/local）、ToolRoute 定义
│   │       └── approvals.py         #   HITL 审批闸门
│   ├── common/                      # 【下层·通用工具】:9100
│   │   └── src/mcp_common_server/
│   │       └── tools.py             # ★ 通用工具写这里（now/echo/generate_id…）
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
│   │       │   ├── dbhub/           #   多库驱动统一层（oracle/mysql/pg/mssql）+ 错误脱敏
│   │       │   ├── jobs/            #   异步 job 注册表（submit→轮询模式）
│   │       │   └── config/          #   Go 侧配置加载（datahub.env）
│   │       └── config/
│   │           └── datahub.env.example  # 监听/令牌/DSN_<名称> 注册表
├── tests/
│   ├── smoke_test.py                # 协议级冒烟（内存 Client，不起网络）
│   └── ops/run_ops_test.py          # 真实 HTTP 链路 26 项断言（可进 CI）
└── scripts/
    ├── setup.ps1 / run-demo.ps1 / ops-check.ps1      # Windows 开发
    └── serversctl.sh                                 # Linux 启停（start|stop|restart|status）
```

★ = 日常写业务代码的位置。

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

### 2.4 新工具接入网关（必做）

上层网关按「server+tool」白名单路由，在 `layers/gateway/src/mcp_gateway/main.py` 的 `_build_router()` 注册：

```python
r.register(ToolRoute(server="it_ops", tool="my_tool", exec_kind="local",
                     requires_approval=False, summary="我的工具"))
# 写操作/高风险 → requires_approval=True（进 HITL 审批闸门）
# 跨机 Go 路由 → exec_kind="http", endpoint=<MCP_GODATAHUB_URL>, headers=<token 头>
```

## 3. 本地开发调试（Windows）

```powershell
powershell -File scripts/setup.ps1        # 建 .venv + editable 安装（或 uv sync --all-packages，
                                          # 内网需 UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/）
powershell -File scripts/run-demo.ps1     # 一键起 common:9100 / itops:9200 / gateway:9000 (+本地 Go)
powershell -File scripts/ops-check.ps1    # 一键自检：起服务→26 项断言→停服务（退出码可进 CI）
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

# Go 数据机：参照 mcp-gateway.service 手写同款（datahub-server.service 模板在 deploy/systemd/），
# 或继续 nohup；unit 里只需指路径，监听/token/DSN 全由 config/datahub.env 控制。
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
NO_PROXY='*' .venv/bin/python tests/ops/run_ops_test.py --skip-go   # 20 项断言 OPS_TEST_OK

# 防火墙（对外提供时）
firewall-cmd --add-port=9000/tcp --permanent && firewall-cmd --reload
```

升级发版 = 传新包 → 覆盖解压 → `HOST=0.0.0.0 bash scripts/serversctl.sh restart`
（离线 wheel 安装方式需追加：`.venv/bin/pip install --no-index --find-links ../wheels --force-reinstall mcp-gateway-server mcp-shared ...`）

### 5.2 Go 数据机（独立服务器）

只需两样文件：`bin-linux/datahub-server`（或 .exe）+ `config/datahub.env`：

```bash
# 目录布局：任意目录下保持  datahub-server 与 config/datahub.env 同层或 config 在上级
./datahub-server                 # 自动读 config/datahub.env（exe 位置向上找 6 级）
```

`datahub.env` 关键项：
```ini
GO_DATAHUB_ADDR=0.0.0.0
GO_DATAHUB_PORT=9300
GO_DATAHUB_TOKEN=<与网关机 MCP_GODATAHUB_TOKEN 一致>
DSN_ORDER_PG=postgres://user:pass@10.x.x.x:5432/orders?sslmode=disable
DSN_WMS_MSSQL=sqlserver://user:pass@10.x.x.x:1433?database=wms
```
防火墙只放行网关机 IP 访问 9300。

### 5.3 AI 客户端接入

| 配置项 | 值 |
|--------|-----|
| 传输类型 | HTTP Streamable |
| 服务 URL | `http://<网关机IP>:9000/mcp` ← **`/mcp` 路径必须带** |
| 认证 | 无（Bearer 仅用于网关→Go 内部链路） |

### 5.4 配置优先级（两端一致）

**命令行参数 > OS 环境变量 > .env 配置文件**。打包后动态改配置：直接编辑对应 .env 重启进程，无需重新编译/打包。

## 6. 常见问题速查

| 症状 | 原因/解法 |
|------|-----------|
| 客户端连不上，curl `/mcp` 返回 400 Missing session ID | 服务正常！检查 URL 是否带 `/mcp` |
| curl 超时 / refused | 未用 `HOST=0.0.0.0` 启动，或防火墙未放行 |
| `unrecognized arguments: --host` | venv 里是旧 server_kit：覆盖源码（editable）或 force-reinstall mcp-shared |
| 改了源码但服务器行为没变（离线安装） | wheel 是拷贝安装，需 `--force-reinstall` 重装对应包 |
| Linux 报 `bad interpreter: /usr/bin/env bash^M` | .sh 被转成 CRLF：仓库已用 .gitattributes 强制 LF，重新导出即可 |
| 网关调用报 `Error executing tool gateway_call` | 看 message：未注册路由→错误里已列出该 server 可用工具；AI 端应先调 **`list_routes`**（网关工具发现入口，返回全部 server/tool/审批标记） |
| AI 客户端首两次 gateway_call 失败后成功 | 模型在猜工具名/参数形状；接入后先让它 `list_routes` 一次即可避免 |
| Windows 终端中文乱码 | GBK 控制台显示问题，设 `PYTHONIOENCODING=utf-8`，不影响数据 |
| Go 采集任务一直 pending/running | `get_job_status` 轮询；失败看 `error` 字段与 Go 进程日志 |

## 7. 安全边界

- 真实 `.env`（token/DSN）永不进 git（`.gitignore` + 只提交 `.example`）
- 密码零传输：数据库连接串只存 Go 机本地，工具传 `dsn_ref` 名称
- 高风险写操作（含 `submit_collect_job`）必须经网关 HITL 审批后执行
- 对外只开 9000；9100/9200/9300 内网互通；跨公网必须前置 TLS 反代
