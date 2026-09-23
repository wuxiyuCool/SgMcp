package main

import (
	"context"
	"encoding/json"
	"fmt"
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
	for _, want := range []string{"list_sources", "submit_collect_job", "get_job_status", "list_jobs", "cancel_job", "db_ping", "list_dsn_refs"} {
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
	}](t, sess, "list_sources", nil)
	if len(out.Sources) < 4 {
		t.Fatalf("采集器数量异常: %d", len(out.Sources))
	}
}

func TestJobLifecycle(t *testing.T) {
	sess := connectInMemory(t)
	sub := callStructured[struct {
		JobID  string `json:"job_id"`
		Status string `json:"status"`
	}](t, sess, "submit_collect_job", map[string]any{
		"source": "synthetic",
		"params": map[string]any{"rows": 300, "batch_pause_ms": 1},
	})
	if sub.JobID == "" {
		t.Fatal("job_id 为空")
	}

	deadline := time.Now().Add(10 * time.Second)
	var last jobView
	for time.Now().Before(deadline) {
		last = callStructured[jobView](t, sess, "get_job_status", map[string]any{"job_id": sub.JobID})
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
		Name:      "submit_collect_job",
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
		Name: "db_ping", Arguments: map[string]any{"db_type": "pg"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError {
		t.Fatal("缺 dsn/dsn_ref 竟然成功")
	}

	// 未知 dsn_ref → 错误信息只列名称，不含任何连接串
	res, err = sess.CallTool(context.Background(), &mcp.CallToolParams{
		Name: "db_ping", Arguments: map[string]any{"db_type": "pg", "dsn_ref": "no_such_ref"},
	})
	if err != nil {
		t.Fatal(err)
	}
	if !res.IsError {
		t.Fatal("未知 dsn_ref 竟然成功")
	}
	text := fmt.Sprint(res.Content)
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
	}](t, sess, "list_dsn_refs", nil)
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
