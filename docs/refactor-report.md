# SgMcp 工具重构对照报告

> 依据《MCP 服务开发规范（AI 可执行版）》执行：删除用不上的测试/演示工具，
> it_ops 收敛为 ITOM 平台 API 对接域，并同步修复验证中发现的代码问题。
> 验证基线：`tests/smoke_test.py` 37 项 PASS；`tests/ops/run_ops_test.py` 真实部署链路 29/29 PASS。

## 1. it_ops 工具对照（23 → 8）

### 1.1 保留并规范命名（旧 → 新）

| 旧工具名 | 新工具名 | 说明 |
|---|---|---|
| `itops_itom_selfcheck` | `itops_get_itom_status` | 连通性/账户/会话自检，排障入口 |
| `itops_itom_incidents` | `itops_query_itom_incidents` | ITOM 工单查询 |
| — | `itops_list_itom_accounts` | ITOM 账户清单（工号打码展示） |
| — | `itops_query_itom` | ITOM API **只读**透传（试接口用） |
| — | `itops_submit_itom` | ITOM API **写**透传（需审批，挂单不触平台） |
| — | `itops_list_itom_options` | ITOM 元数据枚举（`kind="form"` 返回建单表单字段） |
| — | `itops_create_itom_incident` | 引导式建 ITOM 工单（需审批） |
| — | `itops_delete_itom_sessions` | 强制登出 ITOM 会话（写测试调试用，非审批工具） |

### 1.2 删除的测试/演示工具（14 个）

| 旧工具名 | 删除原因 |
|---|---|
| `itops_create_incident` | 本地台账演示（写 JSON 文件），非真实平台对接 |
| `itops_list_incidents` | 同上 |
| `itops_update_incident_status` | 同上 |
| `itops_create_change` | 变更单本地台账演示，无真实系统 |
| `itops_get_change` | 同上 |
| `itops_register_asset` | 本地 CMDB 演示 |
| `itops_list_assets` | 同上 |
| `itops_export_assets_to_warehouse` | 数仓导入演示（内部直调 `/internal/v1/batch/import`） |
| `itops_query_warehouse_assets` | 数仓查询演示（`/internal/v1/batch/query`） |
| `itops_batch_process` | 业务层批处理下放演示；重活统一由 Go `data_batch_process` 承担 |
| `itops_list_datasets` | 数据集发现演示；由 go_datahub `data_list_datasets` 提供 |
| `itops_query_dataset` | 与 go_datahub `data_query_dataset` 重复 |
| `itops_list_data_tables` | 与 go_datahub `data_list_tables` 重复 |
| `itops_itom_login` | 登录动作由会话层自动完成，不应暴露给 AI |

### 1.3 删除后的工具分布（it_ops = 纯 ITOM 对接域）

- **发现**：`itops_list_itom_accounts` / `itops_list_itom_options`
- **自检**：`itops_get_itom_status`
- **查询（只读）**：`itops_query_itom` / `itops_query_itom_incidents`
- **写入（需审批）**：`itops_submit_itom` / `itops_create_itom_incident`
- **会话**：`itops_delete_itom_sessions`

## 2. common 层工具对照（13 → 8，二阶段收敛）

初版仅清退 `echo`/`timestamp`（15→13）。二阶段按规范 §二/§八 完成整域治理：
无前缀旧名违反 `{domain}_{action}_{resource}`，且 13 > 8，全部合并重命名：

| 旧工具名 | 新工具名 | 说明 |
|---|---|---|
| `now` + `datetime_convert` | `util_get_time` | 当前时间/时区换算/偏移合一（value 空=当前） |
| `generate_id` + `uuid_generate` + `random_string` | `util_create_id` | kind=prefixed/uuid/random 一个入口 |
| `hash_text` + `base64_codec` + `slugify` | `util_get_encoded` | algo=md5~sha512/base64/slug |
| `json_tool` | `util_get_json` | validate/pretty/minify/keys |
| `url_parse` | `util_get_url` | 拆解 URL（不联网） |
| `regex_find` | `util_search_text` | 正则批量提取 |
| `text_stats` | `util_calc_stats` | 字数统计 |
| `server_self_check` | `util_get_status` | 下层自检 |
| `echo` / `timestamp` | （删除） | 演示占位，无业务价值 |

配套修复：
- 全部 util_* 工具补 `ToolAnnotations`（纯计算只读；util_create_id 非幂等）；
  description 按「Use when / Do NOT use when」重写。
- **错误消息可达性**：SDK 会把非 ToolError 异常裹成 UnexpectedToolError 并**丢掉原始消息**
  （旧 ValueError 的护栏文案 AI 从来看不到）。修复：`tools.fail()` 与
  `mcp_shared.limits.LimitError`（ValueError+ToolError 双身份，旧 `except ValueError`
  兼容不破）改抛 ToolError，错误带 `[COMMON_INVALID_INPUT]` 等码 + 建议。
- 网关自身 12 个静态工具与动态 route_* 补 annotations（读写分离，规范 §八）。
- 默认下游超时 60s → **120s**（规范 §六.5）。

## 3. 审批名单（HITL）变化

| | 工具 |
|---|---|
| 旧默认集合 | `itops_create_change` / `data_submit_collect_job` / `itops_itom_call` |
| **新默认集合** | `itops_submit_itom` / `itops_create_itom_incident` / `data_submit_collect_job` |

- 定义于 `layers/gateway/src/mcp_gateway/aggregator.py` `DEFAULT_APPROVAL_TOOLS`。
- `itops_query_itom`（只读）与 `itops_delete_itom_sessions`（仅登出会话）不在名单。
- `MCP_APPROVAL_TOOLS` 可覆盖，支持 `data_batch_*` 前缀通配与 `server:tool` 限定。

## 4. 随重构修复的代码问题

| 问题 | 修复 |
|---|---|
| aggregator 对 common 无前缀工具的只读注解 bug | `aggregator.py` 注解逻辑修正（echo/timestamp 清退相关回归） |
| 下游 Client `trust_env=True` 吃进系统代理 | `mcp_shared/mcp_client.py` 与 `tests/ops/client.py`（3 处）显式 `trust_env=False`——内网/本机地址经代理会「连接拒绝→读超时」变形，错误分类被带偏 |
| smoke_test 幂等测试用审批工具导致 KeyError | 改用非审批写工具 `itops_delete_itom_sessions` |
| smoke_test 环境变量泄漏（`MCP_ITOM_WANGXU_TOKEN`） | finally 恢复 `itom_env_saved` + 清会话缓存，修复 token_source 断言连锁失败 |

## 5. 文档同步

| 文档 | 内容 |
|---|---|
| `docs/develop-deploy.md` | §1.1 工具数（common 13 / it_ops 8）、§2.4 审批名单、§2.6 调用顺序与排障工具名、§2.6.1 引导式建单流程、疑难表、目录树、测试项数（52→29） |
| `docs/client-config.md` | 下游工具示例改 `itops_submit_itom` 审批流，去 `itops_query_dataset` |
| `docs/architecture.md` | §2 三跳示例改述（机制保留、示例去演示工具）、§3 审批示例与默认名单、§8.5 dsrouting 段（Python 侧现为纯路由库函数） |
| `README.md` | 审批默认名单、`/internal/*` 示例段、dsrouting 段 |

## 6. 验证结论

- **smoke_test.py**：39 项全 PASS（聚合器合并 it_ops 8 + common 8、审批挂起/批准/驳回、
  exec_failed 追溯、幂等重放、错误分类回归、ITOM 三测试、util_* 命名与错误码治理、
  Host/Origin 防护）。
- **run_ops_test.py**：真实部署链路全 PASS（网关 16 条 Python 路由 + Go 17、审批流、
  审计、kv 语义、错误码契约、目录体积保护）——需服务器 `git pull` 部署新代码后复跑。
- 全项目 `*.md` 与代码残留旧工具名扫描：零残留。
- go_datahub（Go 层）未动：`data_batch_process` 等重活工具全部保留。
