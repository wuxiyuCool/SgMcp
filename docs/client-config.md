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

## 可用工具一览（经网关暴露）

**审批类（由网关自身提供）**

| 工具 | 说明 |
|------|------|
| `list_pending_approvals` | 列出待审批事项 |
| `approve_request(request_id)` | 批准，批准后执行下游调用 |
| `reject_request(request_id)` | 驳回，不执行 |
| `gateway_call(server, tool, args)` | **统一入口**，调用注册的下游工具 |

**下游（经 gateway_call 路由）**

- it_ops：`create_incident` / `list_incidents` / `update_incident_status` / `create_change`(需审批) / `get_change` / `register_asset` / `list_assets`
- common-tools：`now` / `timestamp` / `generate_id` / `slugify` / `echo`

## 示例调用序列（变更单审批）

1. AI 调用：`gateway_call("it_ops", "create_change", {"title": "升级财务库", "risk": "high"})`
2. 网关返回：`{approved: false, request_id: "apr_..."}`
3. 审批人：`list_pending_approvals` → `approve_request("apr_...")`
4. 网关执行 `it_ops.create_change`，返回变更单。
