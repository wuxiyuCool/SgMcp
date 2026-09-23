// Package internalapi 提供重活层的**内部 REST 面**（给业务 Python 服务间直调，
// 不经网关、不暴露给 AI），与 MCP 面（/mcp，给网关聚合）共存于同一端口：
//
//	/mcp                  -> 给网关聚合，暴露给 AI（MCP 协议）
//	/internal/v1/...      -> 给业务 Python 内部调（HTTP + JSON）
//	/internal/healthz     -> 存活探针
//
// 设计约定：
//   - 与 heavy.* 工具共用同一套核心实现（internal/dbhub），行为一致：
//     表名/列名白名单 + 值参数绑定 + 错误经 Scrub 脱敏；
//   - 参数错误返回 4xx（ok=false + 可读 error）；库侧执行失败返回 200 +
//     ok=false（与 MCP 工具的结构化结果对齐，调用方统一检查 ok 字段）；
//   - 鉴权与 /mcp 一致：配置了 GO_DATAHUB_TOKEN 时整端口（含 internal）都要
//     Bearer 头，业务 Python 复用 MCP_GODATAHUB_TOKEN（约定同值）。
package internalapi

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/sg/mcp/go-datahub/internal/batchproc"
	"github.com/sg/mcp/go-datahub/internal/config"
	"github.com/sg/mcp/go-datahub/internal/dbhub"
	"github.com/sg/mcp/go-datahub/internal/dsrouting"
)

// batchImportReq / batchQueryReq 与 MCP 工具入参字段一致，业务层两边可无缝切换。
type batchImportReq struct {
	DBType string           `json:"db_type"`
	DSN    string           `json:"dsn,omitempty"`
	DSNRef string           `json:"dsn_ref,omitempty"`
	Table  string           `json:"table"`
	Rows   []map[string]any `json:"rows"`
}

type batchQueryReq struct {
	DBType  string         `json:"db_type"`
	DSN     string         `json:"dsn,omitempty"`
	DSNRef  string         `json:"dsn_ref,omitempty"`
	Table   string         `json:"table"`
	Filters map[string]any `json:"filters,omitempty"`
	Limit   int            `json:"limit,omitempty"`
}

// Register 把内部 REST 路由挂到 mux（与 /mcp 共存）。
func Register(mux *http.ServeMux) {
	mux.HandleFunc("GET /internal/healthz", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusOK, map[string]any{"ok": true})
	})
	mux.HandleFunc("GET /internal/v1/dsn_refs", handleDSNRefs)
	mux.HandleFunc("GET /internal/v1/datasets", handleListDatasets)
	mux.HandleFunc("GET /internal/v1/tables", handleListTables)
	mux.HandleFunc("POST /internal/v1/batch/import", handleBatchImport)
	mux.HandleFunc("POST /internal/v1/batch/query", handleBatchQuery)
	mux.HandleFunc("POST /internal/v1/batch/process", handleBatchProcess)
}

// ---------------------------------------------------------------------------
// handlers
// ---------------------------------------------------------------------------

func handleDSNRefs(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{
		"ok":           true,
		"refs":         config.DSNRefs(),
		"config_found": config.ConfigPath() != "",
	})
}

// batchProcessReq 内部批处理入参：路由（db_type/dsn_ref/table）由调用方解析好，
// 本端点只负责执行（与 heavy.batch_process 工具共用 batchproc 核心）。
type batchProcessReq struct {
	DBType string `json:"db_type,omitempty"`
	DSN    string `json:"dsn,omitempty"`
	DSNRef string `json:"dsn_ref,omitempty"`
	Table  string `json:"table"`
	Mode   string `json:"mode,omitempty"` // simulate（默认，不触库免 dsn）| real
	Rows   int    `json:"rows,omitempty"`
}

func handleBatchProcess(w http.ResponseWriter, r *http.Request) {
	var req batchProcessReq
	if !decodeJSON(w, r, &req) {
		return
	}
	mode := req.Mode
	if mode == "" {
		mode = "simulate"
	}
	dsn := ""
	if mode == "real" {
		d, err := resolveDSN(req.DBType, req.DSN, req.DSNRef)
		if err != nil {
			writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		dsn = d
	}
	processed, detail, err := batchproc.Run(r.Context(), batchproc.Request{
		DBType: req.DBType, DSN: dsn, Table: req.Table, Mode: mode, Rows: req.Rows,
	}, nil)
	if err != nil {
		if isParamError(err) {
			writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "processed": processed, "detail": detail})
}

func handleListDatasets(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "datasets": dsrouting.ListDatasets()})
}

func handleListTables(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	dsn, err := resolveDSN(q.Get("db_type"), q.Get("dsn"), q.Get("dsn_ref"))
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	ctx, cancel := contextWithTimeout(r)
	defer cancel()
	tables, err := dbhub.ListTables(ctx, q.Get("db_type"), dsn)
	if err != nil {
		writeJSON(w, http.StatusOK, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "tables": tables})
}

func handleBatchImport(w http.ResponseWriter, r *http.Request) {
	var req batchImportReq
	if !decodeJSON(w, r, &req) {
		return
	}
	dsn, err := resolveDSN(req.DBType, req.DSN, req.DSNRef)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	ctx, cancel := contextWithTimeout(r)
	defer cancel()
	imported, err := dbhub.BatchImport(ctx, req.DBType, dsn, req.Table, req.Rows)
	if err != nil {
		// 参数类错误（表名/列名/行数非法）→ 400；库侧失败 → 200 + ok=false
		if isParamError(err) {
			writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "imported": imported})
}

func handleBatchQuery(w http.ResponseWriter, r *http.Request) {
	var req batchQueryReq
	if !decodeJSON(w, r, &req) {
		return
	}
	dsn, err := resolveDSN(req.DBType, req.DSN, req.DSNRef)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	ctx, cancel := contextWithTimeout(r)
	defer cancel()
	rows, err := dbhub.BatchQuery(ctx, req.DBType, dsn, req.Table, req.Filters, req.Limit)
	if err != nil {
		if isParamError(err) {
			writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"ok": false, "error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "rows": rows, "count": len(rows)})
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// resolveDSN 与 main.go 的 MCP 工具侧同规则：dsn_ref 优先，其次 dsn 明文。
func resolveDSN(dbType, dsn, dsnRef string) (string, error) {
	if dsnRef != "" {
		d, ok := config.DSN(dsnRef)
		if !ok {
			return "", fmt.Errorf("配置中不存在 DSN 引用: %s（已配置: %v）", dsnRef, config.DSNRefs())
		}
		return d, nil
	}
	if dsn != "" {
		return dsn, nil
	}
	return "", fmt.Errorf("dsn 与 dsn_ref 至少提供一个")
}

// isParamError 与 main.go 同规则：参数校验类错误 4xx，库侧失败 200+ok=false。
func isParamError(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	for _, p := range []string{"表名", "列名", "rows 不能为空", "单次导入", "不支持的数据库类型", "dsn 与 dsn_ref"} {
		if contains(msg, p) {
			return true
		}
	}
	return false
}

func contains(s, sub string) bool {
	return len(sub) > 0 && len(s) >= len(sub) && indexOf(s, sub) >= 0
}

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}

func decodeJSON(w http.ResponseWriter, r *http.Request, v any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<20)) // 64MB 上限防滥用
	if err := dec.Decode(v); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"ok": false, "error": fmt.Sprintf("请求体非法 JSON: %v", err)})
		return false
	}
	return true
}

func writeJSON(w http.ResponseWriter, code int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(v)
}

func contextWithTimeout(r *http.Request) (context.Context, context.CancelFunc) {
	// 库操作统一 60s 上限：内部调用不应无限等待（批量导入 5 万行的量级足够）
	return context.WithTimeout(r.Context(), 60*time.Second)
}
