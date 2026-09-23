package internalapi

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func newServer(t *testing.T) *httptest.Server {
	t.Helper()
	mux := http.NewServeMux()
	Register(mux)
	srv := httptest.NewServer(mux)
	t.Cleanup(srv.Close)
	return srv
}

func postJSON(t *testing.T, srv *httptest.Server, path string, body any) (*http.Response, map[string]any) {
	t.Helper()
	b, err := json.Marshal(body)
	if err != nil {
		t.Fatal(err)
	}
	resp, err := http.Post(srv.URL+path, "application/json", bytes.NewReader(b))
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatalf("响应不是合法 JSON: %v", err)
	}
	return resp, out
}

func TestHealthz(t *testing.T) {
	srv := newServer(t)
	resp, err := http.Get(srv.URL + "/internal/healthz")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("healthz 状态码: %d", resp.StatusCode)
	}
	var out map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out["ok"] != true {
		t.Fatalf("healthz 应返回 ok=true: %v", out)
	}
}

func TestListDSNRefs(t *testing.T) {
	t.Setenv("DSN_DEMO_PG", "postgres://u:p@h/db")
	srv := newServer(t)
	resp, err := http.Get(srv.URL + "/internal/v1/dsn_refs")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		OK   bool     `json:"ok"`
		Refs []string `json:"refs"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if !out.OK {
		t.Fatal("ok 应为 true")
	}
	found := false
	for _, r := range out.Refs {
		if r == "demo_pg" {
			found = true
		}
		if strings.Contains(r, "://") {
			t.Fatalf("refs 不应包含连接串本体: %v", out.Refs)
		}
	}
	if !found {
		t.Fatalf("DSN_DEMO_PG 未出现在 refs: %v", out.Refs)
	}
}

func TestBatchImportParamErrors(t *testing.T) {
	srv := newServer(t)

	// 空 rows → 400
	resp, out := postJSON(t, srv, "/internal/v1/batch/import", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none", "table": "t", "rows": []any{},
	})
	if resp.StatusCode != http.StatusBadRequest || out["ok"] != false {
		t.Fatalf("空 rows 应 400: code=%d body=%v", resp.StatusCode, out)
	}

	// 非法表名（注入特征）→ 400，不触库
	resp, out = postJSON(t, srv, "/internal/v1/batch/import", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none",
		"table": "t; DROP TABLE x", "rows": []any{map[string]any{"a": 1}},
	})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("非法表名应 400: code=%d body=%v", resp.StatusCode, out)
	}

	// 未知 dsn_ref → 400，错误信息不泄露连接串
	resp, out = postJSON(t, srv, "/internal/v1/batch/import", map[string]any{
		"db_type": "pg", "dsn_ref": "no_such_ref", "table": "t",
		"rows": []any{map[string]any{"a": 1}},
	})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("未知 dsn_ref 应 400: code=%d body=%v", resp.StatusCode, out)
	}
	if strings.Contains(out["error"].(string), "://") {
		t.Fatalf("错误信息疑似泄露连接串: %v", out)
	}

	// 缺 dsn/dsn_ref → 400
	resp, _ = postJSON(t, srv, "/internal/v1/batch/import", map[string]any{
		"db_type": "pg", "table": "t", "rows": []any{map[string]any{"a": 1}},
	})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("缺 dsn/dsn_ref 应 400: code=%d", resp.StatusCode)
	}
}

func TestBatchImportUnreachableDBIsStructured(t *testing.T) {
	srv := newServer(t)
	// 不可达库 → 200 + ok=false（与 heavy.batch_import MCP 工具行为一致），错误脱敏
	resp, out := postJSON(t, srv, "/internal/v1/batch/import", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:secret@127.0.0.1:1/none", "table": "t",
		"rows": []any{map[string]any{"a": 1}},
	})
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("库侧失败应 200: code=%d body=%v", resp.StatusCode, out)
	}
	if out["ok"] != false {
		t.Fatalf("ok 应为 false: %v", out)
	}
	errText, _ := out["error"].(string)
	if errText == "" {
		t.Fatal("失败应带可读错误")
	}
	if strings.Contains(errText, "secret") || strings.Contains(errText, "://") {
		t.Fatalf("错误信息疑似泄露连接串: %s", errText)
	}
}

func TestBatchQueryValidationAndFailure(t *testing.T) {
	srv := newServer(t)

	// 非法列名 → 400
	resp, _ := postJSON(t, srv, "/internal/v1/batch/query", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none", "table": "t",
		"filters": map[string]any{"a; DROP TABLE x": "1"},
	})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("非法列名应 400: code=%d", resp.StatusCode)
	}

	// 不可达库 → 200 + ok=false
	resp, out := postJSON(t, srv, "/internal/v1/batch/query", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:p@127.0.0.1:1/none", "table": "t",
	})
	if resp.StatusCode != http.StatusOK || out["ok"] != false {
		t.Fatalf("不可达库应 200+ok=false: code=%d body=%v", resp.StatusCode, out)
	}
}

func TestListTablesFailureIsStructured(t *testing.T) {
	srv := newServer(t)
	resp, err := http.Get(srv.URL + "/internal/v1/tables?db_type=pg&dsn=" + "postgres://u:p@127.0.0.1:1/none")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		OK    bool   `json:"ok"`
		Error string `json:"error"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if out.OK {
		t.Fatal("不可达库竟然列出表成功")
	}
	if out.Error == "" {
		t.Fatal("失败应带可读错误")
	}
}

func TestMethodMismatch(t *testing.T) {
	srv := newServer(t)
	// GET 打到只接受 POST 的路由 → 405（Go 1.22 方法路由自动返回）
	resp, err := http.Get(srv.URL + "/internal/v1/batch/process")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusMethodNotAllowed {
		t.Fatalf("GET /internal/v1/batch/process 应 405: code=%d", resp.StatusCode)
	}
}

func TestBatchProcessEndpoint(t *testing.T) {
	srv := newServer(t)

	// simulate：不触库免 dsn → 200 + ok=true + processed
	resp, out := postJSON(t, srv, "/internal/v1/batch/process", map[string]any{
		"table": "orders_202405", "mode": "simulate", "rows": 100,
	})
	if resp.StatusCode != http.StatusOK || out["ok"] != true {
		t.Fatalf("simulate 批处理应 200+ok=true: code=%d body=%v", resp.StatusCode, out)
	}
	if out["processed"] != float64(100) {
		t.Fatalf("processed 应为 100: %v", out)
	}

	// 非法表名 → 400
	resp, _ = postJSON(t, srv, "/internal/v1/batch/process", map[string]any{
		"table": "t; DROP TABLE x", "mode": "simulate",
	})
	if resp.StatusCode != http.StatusBadRequest {
		t.Fatalf("非法表名应 400: code=%d", resp.StatusCode)
	}

	// real + 不可达库 → 200 + ok=false（错误脱敏）
	resp, out = postJSON(t, srv, "/internal/v1/batch/process", map[string]any{
		"db_type": "pg", "dsn": "postgres://u:secret@127.0.0.1:1/none",
		"table": "orders_202405", "mode": "real",
	})
	if resp.StatusCode != http.StatusOK || out["ok"] != false {
		t.Fatalf("real 不可达库应 200+ok=false: code=%d body=%v", resp.StatusCode, out)
	}
	if strings.Contains(out["error"].(string), "secret") {
		t.Fatalf("错误疑似泄露连接串: %v", out)
	}
}

func TestListDatasetsEndpoint(t *testing.T) {
	t.Setenv("DATASET_ORDERS_DBTYPE", "pg")
	t.Setenv("DATASET_ORDERS_DSN_DEFAULT", "order_pg")
	srv := newServer(t)
	resp, err := http.Get(srv.URL + "/internal/v1/datasets")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var out struct {
		OK       bool `json:"ok"`
		Datasets []struct {
			Dataset    string `json:"dataset"`
			DBType     string `json:"db_type"`
			DSNDefault string `json:"dsn_default"`
		} `json:"datasets"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		t.Fatal(err)
	}
	if !out.OK {
		t.Fatal("ok 应为 true")
	}
	found := false
	for _, d := range out.Datasets {
		if d.Dataset == "orders" && d.DSNDefault == "order_pg" {
			found = true
		}
	}
	if !found {
		t.Fatalf("orders 数据集未出现在端点结果: %+v", out)
	}
}
