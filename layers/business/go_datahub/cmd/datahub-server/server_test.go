package main

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/sg/mcp/go-datahub/internal/config"
)

func connectInMemory(t *testing.T) *mcp.ClientSession {
	t.Helper()
	server, _ := buildServer()
	client := mcp.NewClient(&mcp.Implementation{Name: "test-client", Version: "0"}, nil)
	ct, st := mcp.NewInMemoryTransports()
	ctx := context.Background()
	if _, err := server.Connect(ctx, st, nil); err != nil {
		t.Fatalf("server connect: %v", err)
	}
	sess, err := client.Connect(ctx, ct, nil)
	if err != nil {
		t.Fatalf("client connect: %v", err)
	}
	t.Cleanup(func() { sess.Close() })
	return sess
}

type jobView struct {
	Status string `json:"status"`
	Rows   int    `json:"rows"`
}

func callStructured[T any](t *testing.T, sess *mcp.ClientSession, tool string, args map[string]any) T {
	t.Helper()
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{Name: tool, Arguments: args})
	if err != nil {
		t.Fatalf("call %s: %v", tool, err)
	}
	if res.IsError {
		t.Fatalf("call %s 返回工具错误: %v", tool, res.Content)
	}
	var out T
	b, _ := json.Marshal(res.StructuredContent)
	if err := json.Unmarshal(b, &out); err != nil {
		t.Fatalf("call %s 解析结构化结果失败: %v", tool, err)
	}
	return out
}

// contentText 提取工具错误里的可读文本（Content 是接口切片，需取 TextContent.Text）。
func contentText(res *mcp.CallToolResult) string {
	var sb strings.Builder
	for _, c := range res.Content {
		if tc, ok := c.(*mcp.TextContent); ok {
			sb.WriteString(tc.Text)
		}
	}
	return sb.String()
}

func TestToolsRegistered(t *testing.T) {
	sess := connectInMemory(t)
	res, err := sess.ListTools(context.Background(), nil)
	if err != nil {
		t.Fatal(err)
	}
	names := map[string]bool{}
	for _, tool := range res.Tools {
		names[tool.Name] = true
	}
	// 架构约定：重活层对 AI 暴露的工具统一 data. 前缀
	for _, want := range []string{
		"data.list_sources", "data.submit_collect_job", "data.get_job_status",
		"data.list_jobs", "data.cancel_job", "data.db_ping", "data.list_dsn_refs",
		"data.list_tables", "data.batch_import", "data.batch_query", "data.db_query_preview",
	} {
		if !names[want] {
			t.Errorf("缺少工具: %s", want)
		}
	}
}

func TestListSources(t *testing.T) {
	sess := connectInMemory(t)
	out := callStructured[struct {
		Sources []struct {
			Name string `json:"name"`
		} `json:"sources"`
	}](t, sess, "data.list_sources", nil)
	if len(out.Sources) < 4 {
		t.Fatalf("采集器数量异常: %d", len(out.Sources))
	}
}

func TestJobLifecycle(t *testing.T) {
	sess := connectInMemory(t)
	sub := callStructured[struct {
		JobID  string `json:"job_id"`
		Status string `json:"status"`
	}](t, sess, "data.submit_collect_job", map[string]any{
		"source": "synthetic",
		"params": map[string]any{"rows": 300, "batch_pause_ms": 1},
	})
	if sub.JobID == "" {
		t.Fatal("job_id 为空")
	}

	deadline := time.Now().Add(10 * time.Second)
	var last jobView
	for time.Now().Before(deadline) {
		last = callStructured[jobView](t, sess, "data.get_job_status", map[string]any{"job_id": sub.JobID})
		if last.Status == "completed" || last.Status == "failed" {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if last.Status != "completed" {
		t.Fatalf("任务未完成: %+v", last)
	}
	if last.Rows != 300 {
		t.Fatalf("行数不对: got %d want 300", last.Rows)
	}
}

func TestUnknownSourceIsToolError(t *testing.T) {
	sess := connectInMemory(t)
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "data.submit_collect_job",
		Arguments: map[string]any{"source": "nope"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError {
		t.Fatal("未知采集器应返回 tool error")
	}
}

func TestDbPingDsnValidation(t *testing.T) {
	sess := connectInMemory(t)

	// dsn 与 dsn_ref 都缺 → tool error
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "data.db_ping", Arguments: map[string]any{"db_type": "pg"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError {
		t.Fatal("缺 dsn/dsn_ref 竟然成功")
	}

	// 未知 dsn_ref → 错误信息只列名称，不含任何连接串
	res, err = sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "data.db_ping", Arguments: map[string]any{"db_type": "pg", "dsn_ref": "no_such_ref"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError {
		t.Fatal("未知 dsn_ref 竟然成功")
	}
	text := contentText(res)
	if strings.Contains(text, "://") {
		t.Fatalf("错误信息疑似泄露连接串: %s", text)
	}
}

func TestListDsnRefs(t *testing.T) {
	t.Setenv("DSN_DEMO_PG", "postgres://u:p@h/db")
	config.Reload()
	t.Cleanup(config.Reload)

	sess := connectInMemory(t)
	out := callStructured[struct {
		Refs        []string `json:"refs"`
		ConfigFound bool     `json:"config_found"`
	}](t, sess, "data.list_dsn_refs", nil)
	found := false
	for _, r := range out.Refs {
		if r == "demo_pg" {
			found = true
		}
	}
	if !found {
		t.Fatalf("DSN_DEMO_PG 环境变量未出现在 refs: %v", out.Refs)
	}
	for _, r := range out.Refs {
		if strings.Contains(r, "://") {
			t.Fatalf("refs 不应包含连接串本体: %v", out.Refs)
		}
	}
}

// ---------------------------------------------------------------------------
// 库表操作（data.list_tables / data.batch_import / data.batch_query）
// 真库成功路径需要真实 DSN；本组用例覆盖参数校验与失败路径（任意环境可跑）。
// ---------------------------------------------------------------------------

// batchImportView / batchQueryView 与 main.go 的出参对应（仅取断言所需字段）。
type batchImportView struct {
	OK       bool   `json:"ok"`
	Imported int    `json:"imported"`
	Error    string `json:"error"`
}

type batchQueryView struct {
	OK    bool             `json:"ok"`
	Rows  []map[string]any `json:"rows"`
	Count int              `json:"count"`
	Error string           `json:"error"`
}

func callRaw(t *testing.T, sess *mcp.ClientSession, tool string, args map[string]any) *mcp.CallToolResult {
	t.Helper()
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{Name: tool, Arguments: args})
	if err != nil {
		t.Fatalf("call %s: %v", tool, err)
	}
	return res
}

func TestBatchImportValidation(t *testing.T) {
	sess := connectInMemory(t)

	// 空 rows → tool error
	res := callRaw(t, sess, "data.batch_import", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none", "table": "t", "rows": []any{},
	})
	if !res.IsError {
		t.Fatal("空 rows 竟然成功")
	}

	// 非法表名（注入特征）→ tool error，且不执行任何库操作
	res = callRaw(t, sess, "data.batch_import", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none",
		"table": "t; DROP TABLE x", "rows": []any{map[string]any{"a": 1}},
	})
	if !res.IsError {
		t.Fatal("非法表名竟然成功")
	}

	// 未知 dsn_ref → tool error，错误信息不泄露连接串
	res = callRaw(t, sess, "data.batch_import", map[string]any{
		"db_type": "pg", "dsn_ref": "no_such_ref", "table": "t",
		"rows": []any{map[string]any{"a": 1}},
	})
	if !res.IsError {
		t.Fatal("未知 dsn_ref 竟然成功")
	}
	if strings.Contains(contentText(res), "://") {
		t.Fatalf("错误信息疑似泄露连接串: %s", res.Content)
	}

	// 不可达库 → 结构化 ok=false（不抛异常），错误信息已脱敏
	out := callStructured[batchImportView](t, sess, "data.batch_import", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:secret@127.0.0.1:1/none", "table": "t",
		"rows": []any{map[string]any{"a": 1}},
	})
	if out.OK {
		t.Fatal("不可达库竟然导入成功")
	}
	if out.Error == "" {
		t.Fatal("失败应带可读错误")
	}
	if strings.Contains(out.Error, "secret") || strings.Contains(out.Error, "://") {
		t.Fatalf("失败信息疑似泄露连接串: %s", out.Error)
	}
}

func TestBatchQueryValidation(t *testing.T) {
	sess := connectInMemory(t)

	// 非法列名（注入特征）→ tool error
	res := callRaw(t, sess, "data.batch_query", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none", "table": "t",
		"filters": map[string]any{"a; DROP TABLE x": "1"},
	})
	if !res.IsError {
		t.Fatal("非法列名竟然成功")
	}

	// 不可达库 → 结构化 ok=false
	out := callStructured[batchQueryView](t, sess, "data.batch_query", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none", "table": "t",
	})
	if out.OK {
		t.Fatal("不可达库竟然查询成功")
	}
	if out.Error == "" {
		t.Fatal("失败应带可读错误")
	}
}

func TestBatchProcessFlow(t *testing.T) {
	// 架构示例全流程：data.batch_process(orders, 2024-05, t1) → task_id → 轮询完成
	t.Setenv("DATASET_ORDERS_DBTYPE", "pg")
	t.Setenv("DATASET_ORDERS_DSN_T1", "order_t1_pg")
	t.Setenv("DATASET_ORDERS_DSN_DEFAULT", "order_pg")
	config.Reload()
	t.Cleanup(config.Reload)

	sess := connectInMemory(t)
	sub := callStructured[struct {
		TaskID    string         `json:"task_id"`
		Status    string         `json:"status"`
		DatasetID string         `json:"dataset_id"`
		Routed    map[string]any `json:"routed"`
	}](t, sess, "data.batch_process", map[string]any{
		"dataset_id": "orders", "period": "2024-05", "tenant_id": "t1", "rows": 100,
	})
	if !strings.HasPrefix(sub.TaskID, "job-") {
		t.Fatalf("task_id 形态错误: %s", sub.TaskID)
	}
	if sub.DatasetID != "orders" || sub.Routed["table"] != "orders_202405" {
		t.Fatalf("路由结果错误: %+v", sub.Routed)
	}
	if sub.Routed["db"] != "order_t1_pg" {
		t.Fatalf("租户路由错误，应命中 DATASET_ORDERS_DSN_T1: %+v", sub.Routed)
	}

	// 轮询到完成，任务消息应记录路由明细
	deadline := time.Now().Add(10 * time.Second)
	var last jobView
	var msg string
	for time.Now().Before(deadline) {
		res := callStructured[struct {
			jobView
			Message string `json:"message"`
		}](t, sess, "data.get_job_status", map[string]any{"job_id": sub.TaskID})
		last, msg = res.jobView, res.Message
		if last.Status == "completed" || last.Status == "failed" {
			break
		}
		time.Sleep(50 * time.Millisecond)
	}
	if last.Status != "completed" {
		t.Fatalf("simulate 批处理未完成: %+v msg=%s", last, msg)
	}
	if !strings.Contains(msg, "table=orders_202405") || !strings.Contains(msg, "db=order_t1_pg") {
		t.Fatalf("任务消息未记录路由明细: %s", msg)
	}
}

func TestBatchProcessUnconfiguredDatasetIsToolError(t *testing.T) {
	config.Reload()
	t.Cleanup(config.Reload)
	sess := connectInMemory(t)
	res := callRaw(t, sess, "data.batch_process", map[string]any{
		"dataset_id": "no_such_ds", "period": "2024-05", "tenant_id": "t1",
	})
	if !res.IsError {
		t.Fatal("未配置数据集竟然成功")
	}
}

func TestListDatasetsTool(t *testing.T) {
	t.Setenv("DATASET_ORDERS_DSN_DEFAULT", "order_pg")
	config.Reload()
	t.Cleanup(config.Reload)
	sess := connectInMemory(t)
	out := callStructured[struct {
		Datasets []struct {
			Dataset string `json:"dataset"`
		} `json:"datasets"`
	}](t, sess, "data.list_datasets", nil)
	found := false
	for _, d := range out.Datasets {
		if d.Dataset == "orders" {
			found = true
		}
	}
	if !found {
		t.Fatalf("data.list_datasets 未返回 orders: %+v", out)
	}
}

func TestListTablesFailureIsStructured(t *testing.T) {
	sess := connectInMemory(t)
	out := callStructured[struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}](t, sess, "data.list_tables", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none",
	})
	if out.OK {
		t.Fatal("不可达库竟然列出表成功")
	}
	if out.Error == "" {
		t.Fatal("失败应带可读错误")
	}
}

func TestDbQueryPreviewGuards(t *testing.T) {
	sess := connectInMemory(t)

	// 非 SELECT 必须拒绝
	res, err := sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "data.db_query_preview",
		Arguments: map[string]any{"dsn": "postgres://u:p@127.0.0.1:1/db", "sql": "DELETE FROM t"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError || !strings.Contains(contentText(res), "SELECT") {
		t.Fatalf("非 SELECT 应被拒绝: %s", contentText(res))
	}

	// 未知 dsn_ref → 可读错误且不泄露连接串
	res, err = sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name:      "data.db_query_preview",
		Arguments: map[string]any{"dsn_ref": "no_such", "sql": "SELECT 1"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError || strings.Contains(contentText(res), "://") {
		t.Fatalf("未知 dsn_ref 处理异常: %s", contentText(res))
	}
}
