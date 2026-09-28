# MCP 客户端接入配置

AI 客户端（Claude Desktop、IDE 插件等）只需连接**上层网关**，即可访问全部已注册工具。

## 方式一：stdio（网关以标准输入输出运行）

以 Claude Desktop 为例，在客户端配置中加入网关 server：

```json
{
  "mcpServers": {
    "enterprise-gateway": {
      "command": "gateway-server",
      "args": []
    }
  }
}
```

需要确保 `gateway-server` 在 PATH 中（`pip install -e` 后即有）。

## 方式二：Streamable HTTP（生产推荐）

三个 server 各自 HTTP 独立部署，客户端只连网关：

```json
{
  "mcpServers": {
    "enterprise-gateway": {
      "url": "http://127.0.0.1:9000/mcp"
    }
  }
}
```

网关 HTTP 地址：`http://127.0.0.1:9000/mcp`。

网关侧配置了 `MCP_GATEWAY_TOKEN` 时，客户端必须带令牌（否则 401）。`Authorization` 头的
`Bearer ` 前缀可写可不写（`Bearer xxx` / `bearer xxx` / 直接 `xxx` 都认，大小写不敏感），
因为不少企业 AI 平台的「自定义请求头」只能填裸值：

```json
{
  "mcpServers": {
    "enterprise-gateway": {
      "url": "http://127.0.0.1:9000/mcp",
      "headers": { "Authorization": "Bearer <MCP_GATEWAY_TOKEN>" }
    }
  }
}
```
（写成不带前缀的裸令牌值同样能连。若平台把 401 显示成 `status 500`，先按
`develop-deploy.md` §6 用 curl 看真实状态码，别怀疑令牌本身。）

## 方式三：审批人专用客户端（与 AI 客户端分开）

审批决策与 AI 调用**必须走两套配置**：`MCP_APPROVAL_TOKEN` 只放在审批人这边。
AI 侧若拿得到令牌，就能自己 `approve_request` 放行高风险写操作，HITL 等于没有。

审批人用一个独立 MCP 客户端（同一个网关地址），**令牌写在请求头里**，模型既看不到、
也不需要把令牌当参数复述（AI 侧的配置里没有这个头，因此它无法自行放行）：

```json
{
  "mcpServers": {
    "enterprise-gateway-approver": {
      "url": "http://127.0.0.1:9000/mcp",
      "headers": {
        "Authorization": "Bearer <MCP_GATEWAY_TOKEN>",
        "X-Sg-Approval-Token": "<MCP_APPROVAL_TOKEN>",
        "X-Sg-Actor": "张三<zhangsan@corp>"
      }
    }
  }
}
```

审批机日常只需要四个工具：`list_pending_approvals` → `approve_request(request_id)` /
`reject_request(request_id)` / `retry_execution(request_id)`。带 `X-Sg-Approval-Token` 头时
`approval_token` 参数可省略；不带该头时仍可显式传参（运维脚本用）。`X-Sg-Actor` 会作为
**真实发起人**写进审计与审批台账（优先于 AI 自报的 `requested_by`）。

AI 侧配置保持只有 `Authorization`，看不到审批令牌：

```json
{
  "mcpServers": {
    "enterprise-gateway": {
      "url": "http://127.0.0.1:9000/mcp",
      "headers": { "Authorization": "Bearer <MCP_GATEWAY_TOKEN>" }
    }
  }
}
```
（写成不带前缀的裸令牌值同样能连。若平台把 401 显示成 `status 500`，先按
`develop-deploy.md` §6 用 curl 看真实状态码，别怀疑令牌本身。）

> 令牌生成（PowerShell）：
> `-join ((48..57)+(97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_})`
> 令牌属敏感信息：只进环境变量/密钥管理，不进 git，不进对话。

## 可用工具一览（经网关暴露）

**审批类（由网关自身提供）**

| 工具 | 说明 |
|------|------|
| `route_<域>`（如 `route_it_ops` / `route_common_tools` / `route_go_datahub`） | **域聚合路由入口**：`method` 枚举列出该域全部方法，`params` 支持对象 / JSON 串 / 扁平串 `k=v;k2=v2` |
| `gateway_call(server, tool, arguments, idempotency_key?)` | 跨域通用入口；带幂等键时重试不会二次执行 |
| `list_tool_catalog(server?, keyword?, mode?)` | 全平台工具说明书（默认精简，`mode="full"` 给逐参数明细与示例） |
| `list_routes(server?, tool?, keyword?, full_schema?)` | 在册路由明细（默认只给参数摘要，省上下文） |
| `list_downstreams` / `refresh_routes` / `describe_gateway` | 下游聚合健康度、运行时补拉工具表、部署自检（含安全开关） |
| `list_pending_approvals` / `list_approvals` | 待办与审批台账（含 exec_failed、attempts） |
| `approve_request(request_id, approval_token)` / `reject_request(...)` | 审批决策（网关配了 `MCP_APPROVAL_TOKEN` 时令牌必填） |
| `retry_execution(request_id, approval_token)` | 重放「已批准但下游执行失败」的单 |
| `query_audit_log(limit, event?, server?, tool?, call_id?)` | 调用审计回查（敏感入参已脱敏） |

**下游工具**：随部署而变，**不要背名字**——用 `list_tool_catalog`（或
`route_*` 的 method 枚举）现场确认。三层粒度约定：`itops_*` 领域工具、
`itops_query_dataset` 领域通用查询、`data_*` 全局数据平台（Go 重活）。

## 示例调用序列（变更单审批）

1. AI 调用：`route_it_ops(method="itops_create_change", params={"title": "升级财务库", "risk": "high"})`
2. 网关返回：`{approved: false, executed: false, request_id: "apr_...", expires_at: "...", call_id: "cal_..."}`
   ——目标操作**尚未执行**，AI 应把 request_id 交给审批人后停手。
3. 审批人（独立客户端）：`list_pending_approvals` → `approve_request("apr_...", approval_token="<令牌>")`
4. 网关转发执行 `it_ops_create_change`，返回变更单；若下游失败，单据转 `exec_failed`，
   修正原因后 `retry_execution` 重放即可。

## 接入自查（AI 客户端连不上时）

1. `curl http://<网关>:9000/healthz` —— 服务是否活着（免鉴权）。
2. 带 Bearer 调 `describe_gateway` —— 看 `security` 与 `downstreams` 是否全部 ok。
3. 下游缺席时调 `refresh_routes` 补拉，再 `list_downstreams` 确认工具数。
4. 报错里读 `kind` 与「建议」：`unauthorized`=令牌不对，`unreachable`=下游没起，
   `timeout`=改用异步任务，别原样重试。
