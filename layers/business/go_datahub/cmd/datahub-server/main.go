// 【中层·数据重活】go-datahub MCP server（Go 官方 SDK 实现）。
//
// 定位：与业务层（Python 轻活）分工，承担大批量数据采集与多数据库交互。
// 对 AI 暴露的工具统一 data. 前缀（架构约定：data.xxx = 重活层）。
// 长任务采用异步 job 模式：data_submit_collect_job 立即返回 job_id，
// data_get_job_status 轮询进度，data_cancel_job 中止 —— 避免网关 HTTP 长连接超时。
//
// 库表操作（data_list_tables / data_batch_import / data_batch_query）基于
// 服务端 DSN 注册表（DSN_<名称>），值参数绑定、连接串不进 MCP 结果。
// 业务层 Python 亦经 MCP 协议直连本 server 复用这些能力（不经网关）。
//
// 运行：
//
//	go run ./cmd/datahub-server                 # 默认 127.0.0.1:9300 /mcp
//	go build -o datahub-server.exe ./cmd/datahub-server
package main

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"flag"
	"fmt"
	"log"
	"net/http"
	"strconv"
	"strings"

	"github.com/google/jsonschema-go/jsonschema"
	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/sg/mcp/go-datahub/internal/batchproc"
	"github.com/sg/mcp/go-datahub/internal/collector"
	"github.com/sg/mcp/go-datahub/internal/config"
	"github.com/sg/mcp/go-datahub/internal/dbhub"
	"github.com/sg/mcp/go-datahub/internal/dsrouting"
	"github.com/sg/mcp/go-datahub/internal/filetools"
	"github.com/sg/mcp/go-datahub/internal/internalapi"
	"github.com/sg/mcp/go-datahub/internal/jobs"
)

// ---------------------------------------------------------------------------
// 工具入出参结构（jsonschema 标签即参数描述，SDK 自动校验）
// ---------------------------------------------------------------------------

type listSourcesOut struct {
	Sources []collector.Info `json:"sources"`
}

type submitIn struct {
	Source string         `json:"source" jsonschema:"采集器名称，见 data_list_sources"`
	Params map[string]any `json:"params,omitempty" jsonschema:"采集参数，各采集器定义见其 description"`
}

type submitOut struct {
	JobID  string `json:"job_id"`
	Status string `json:"status"`
	Source string `json:"source"`
}

type jobIn struct {
	JobID string `json:"job_id" jsonschema:"data_submit_collect_job 返回的任务 ID"`
}

type listJobsIn struct {
	Status string `json:"status,omitempty" jsonschema:"按状态过滤：pending/running/completed/failed/cancelled，留空为全部"`
	Limit  int    `json:"limit,omitempty" jsonschema:"最多返回条数，默认 20"`
}

type listJobsOut struct {
	Jobs []jobs.Job `json:"jobs"`
}

// DBTargetIn 是「指定一个库」的公共入参：db_type 可省略（按 DSN 连接串自动推断）+ dsn/dsn_ref 二选一。
type DBTargetIn struct {
	DBType string `json:"db_type,omitempty" jsonschema:"数据库类型 oracle/mysql/pg/mssql；用 dsn_ref 时可省略（自动按连接串推断）"`
	DSN    string `json:"dsn,omitempty" jsonschema:"连接串（与 dsn_ref 二选一；推荐优先 dsn_ref 避免密码经网络传输）"`
	DSNRef string `json:"dsn_ref,omitempty" jsonschema:"配置文件 DSN_<名称> 的引用名，如 order_pg（优先于 dsn），可用 data_list_dsn_refs 查看"`
}

type dbPingOut struct {
	OK        bool   `json:"ok"`
	Version   string `json:"version,omitempty"`
	LatencyMS int64  `json:"latency_ms"`
	Error     string `json:"error,omitempty"`
}

type cancelOut struct {
	JobID  string `json:"job_id"`
	Status string `json:"status"`
}

type listDsnOut struct {
	Refs        []string `json:"refs"`
	ConfigFound bool     `json:"config_found"`
}

type listTablesOut struct {
	OK     bool     `json:"ok"`
	Tables []string `json:"tables,omitempty"`
	Error  string   `json:"error,omitempty"`
}

type batchImportIn struct {
	DBTargetIn
	Table string           `json:"table" jsonschema:"目标表名，允许 [schema.]table 形态；可用 data_list_tables 查看"`
	Rows  []map[string]any `json:"rows" jsonschema:"要写入的行（对象数组，键即列名）；单次上限 50000 行，更大量请分批"`
}

type batchImportOut struct {
	OK       bool   `json:"ok"`
	Imported int    `json:"imported,omitempty"`
	Error    string `json:"error,omitempty"`
}

type batchQueryIn struct {
	DBTargetIn
	Table   string         `json:"table" jsonschema:"目标表名，允许 [schema.]table 形态"`
	Filters map[string]any `json:"filters,omitempty" jsonschema:"等值过滤条件，键为列名、值为匹配值；留空返回任意行"`
	Limit   int            `json:"limit,omitempty" jsonschema:"最多返回行数（默认 100，上限 1000），防全表拖库"`
}

type batchQueryOut struct {
	OK    bool             `json:"ok"`
	Rows  []map[string]any `json:"rows,omitempty"`
	Count int              `json:"count,omitempty"`
	Error string           `json:"error,omitempty"`
}

// dbQueryPreviewIn：任意单条 SELECT（JOIN/聚合场景；单表条件查询优先 data_batch_query）。
type dbQueryPreviewIn struct {
	DBTargetIn
	SQL   string `json:"sql" jsonschema:"单条 SELECT 语句"`
	Limit int    `json:"limit,omitempty" jsonschema:"返回行数上限，默认 50，最大 500"`
}

type dbQueryPreviewOut struct {
	Kind      string             `json:"kind"`
	Columns   []string           `json:"columns"`
	Rows      []collector.Record `json:"rows"`
	Returned  int                `json:"returned"`
	Truncated bool               `json:"truncated"`
}

// batchProcessIn 对应架构示例：AI 调 data_batch_process(dataset_id=业务名,
// period=周期, tenant_id=租户) → 网关转发到 Go → 内部 routeDB/routeTable →
// 异步执行 → 立即返回 task_id（AI 回复用户"任务已提交"）。
type batchProcessIn struct {
	DatasetID string `json:"dataset_id" jsonschema:"业务数据集名（不是表名），如 orders；可用 data_list_datasets 查看"`
	Period    string `json:"period" jsonschema:"处理周期，格式 YYYY-MM（按月分表），如 2024-05"`
	TenantID  string `json:"tenant_id" jsonschema:"租户 ID，用于按租户路由数据源，如 t1"`
	Mode      string `json:"mode,omitempty" jsonschema:"simulate 演练（不触库，默认）；real 真实执行（连路由库统计目标表行数）"`
	Rows      int    `json:"rows,omitempty" jsonschema:"simulate 模式的模拟行数（默认 1000）"`
}

type batchProcessOut struct {
	TaskID    string         `json:"task_id"`
	Status    string         `json:"status"`
	DatasetID string         `json:"dataset_id"`
	Period    string         `json:"period"`
	TenantID  string         `json:"tenant_id"`
	Routed    map[string]any `json:"routed"`
}

type listDatasetsOut struct {
	Datasets []dsrouting.DatasetInfo `json:"datasets"`
}

// queryDatasetIn 第 3 层·全局数据平台（细粒度）：dataset_id 枚举为全量数据集，
// 中文说明在工具描述里动态生成（构建期从 DATASET_*_DESC 汇总），AI 看名知义。
type queryDatasetIn struct {
	DatasetID string         `json:"dataset_id" jsonschema:"业务数据集名（全量枚举与中文说明见工具描述）"`
	TenantID  string         `json:"tenant_id" jsonschema:"租户 ID，用于按租户路由数据源"`
	Period    string         `json:"period,omitempty" jsonschema:"周期 YYYY-MM（周期数据集按月分表）；非周期数据集留空"`
	Filters   map[string]any `json:"filters,omitempty" jsonschema:"等值过滤条件，键为列名、值为匹配值；留空返回任意行"`
	Limit     int            `json:"limit,omitempty" jsonschema:"最多返回行数（默认 100，上限 1000）"`
}

type queryDatasetOut struct {
	OK     bool             `json:"ok"`
	Routed map[string]any   `json:"routed,omitempty"`
	Rows   []map[string]any `json:"rows,omitempty"`
	Count  int              `json:"count,omitempty"`
	Error  string           `json:"error,omitempty"`
}

// ---- 文件重工具（filetools）----

type hashFileIn struct {
	Path string `json:"path" jsonschema:"datahub-server 本机文件绝对路径"`
	Algo string `json:"algo,omitempty" jsonschema:"哈希算法，可选 md5/sha1/sha256/sha512，默认 sha256"`
}

type hashFileOut struct {
	Algo      string `json:"algo"`
	Hex       string `json:"hex"`
	SizeBytes int64  `json:"size_bytes"`
}

type fileStatsIn struct {
	Path         string `json:"path" jsonschema:"datahub-server 本机文件绝对路径"`
	PreviewLines int    `json:"preview_lines,omitempty" jsonschema:"预览行数，默认 5，上限 50"`
}

type convertFileIn struct {
	Src     string `json:"src" jsonschema:"源文件绝对路径（.csv/.jsonl/.ndjson，按扩展名自动识别方向）"`
	Dst     string `json:"dst" jsonschema:"目标文件绝对路径（已存在会被覆盖）"`
	MaxRows int64  `json:"max_rows,omitempty" jsonschema:"最多转换行数，留空或 0=全部"`
}

// datasetEnumDescription 构建期生成全量数据集枚举描述（带中文说明），随配置变化。
func datasetEnumDescription() string {
	sets := dsrouting.ListDatasets()
	if len(sets) == 0 {
		return "（尚未配置任何数据集，见 DATASET_* 路由配置）"
	}
	parts := make([]string, 0, len(sets))
	for _, s := range sets {
		p := s.Dataset
		if s.Desc != "" {
			p += "=" + s.Desc
		}
		if s.Domain != "" {
			p += fmt.Sprintf("（域:%s）", s.Domain)
		}
		parts = append(parts, p)
	}
	return strings.Join(parts, "；")
}

// resolveDSN 解析库目标：dsn_ref 优先，其次 dsn 明文。
func resolveDSN(in DBTargetIn) (string, error) {
	if in.DSNRef != "" {
		d, ok := config.DSN(in.DSNRef)
		if !ok {
			return "", fmt.Errorf("配置中不存在 DSN 引用: %s（已配置: %v）", in.DSNRef, config.DSNRefs())
		}
		return d, nil
	}
	if in.DSN != "" {
		return in.DSN, nil
	}
	return "", fmt.Errorf("dsn 与 dsn_ref 至少提供一个")
}

// resolveTarget 在 resolveDSN 基础上补 db_type：缺省按连接串 scheme 推断，支持同类型多数据源。
func resolveTarget(in DBTargetIn) (kind, dsn string, err error) {
	dsn, err = resolveDSN(in)
	if err != nil {
		return "", "", err
	}
	kind = in.DBType
	if kind == "" {
		if kind = dbhub.InferKind(dsn); kind == "" {
			return "", "", fmt.Errorf("无法推断数据库类型，请显式传 db_type（可选 %v）", dbhub.Kinds())
		}
	}
	return kind, dsn, nil
}

// ---------------------------------------------------------------------------

// toolInput 派生工具入参 schema 并放宽为容忍未知字段：
// AI 平台模型常在 arguments 里幻觉出多余键（如 "action"），严格校验会直接拒绝调用；
// 这里改为剥离忽略——未知字段不会进入 Go 结构体（encoding/json 天然丢弃）。
func toolInput[T any]() map[string]any {
	sch, err := jsonschema.For[T](nil)
	if err != nil {
		panic(err)
	}
	b, err := json.Marshal(sch)
	if err != nil {
		panic(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		panic(err)
	}
	m["additionalProperties"] = true
	return m
}

// toolInputDB 在 toolInput 基础上从 schema 中摘除 dsn 字段：
// AI 可见入参只保留 dsn_ref——模型幻觉拼接残缺连接串（如漏端口）是失败高发区，
// 值参数本身仍保留在 Go 结构体中，供内部 REST/受信调用直传。
func toolInputDB[T any]() map[string]any {
	m := toolInput[T]()
	if props, ok := m["properties"].(map[string]any); ok {
		delete(props, "dsn")
	}
	return m
}

func buildServer() (*mcp.Server, *http.ServeMux) {
	server := mcp.NewServer(&mcp.Implementation{
		Name:    "go-datahub",
		Title:   "数据重活 MCP（Go）",
		Version: "0.2.0",
	}, nil)

	reg := jobs.NewRegistry()
	cols := collector.Default()

	mcp.AddTool(server, &mcp.Tool{
		Name:        "data_list_sources",
		Description: "列出可用采集器（数据库/HTTP/文件/演示）及其参数说明。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInput[struct{}](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, listSourcesOut, error) {
		return nil, listSourcesOut{Sources: cols.List()}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_submit_collect_job",
		Description: "提交异步批量采集任务，立即返回 job_id（形如 \"job-4189f7c72a30a804\"），不阻塞调用。" +
			"参数：source=采集器名（必填，先用 data_list_sources 查可用名及其 params 说明）；" +
			"params=对象，各采集器定义不同，例：synthetic 用 {\"rows\":100000,\"batch_pause_ms\":0}，" +
			"db_query 用 {\"dsn_ref\":\"orcl_erp\",\"sql\":\"SELECT ...\",\"max_rows\":50000}，" +
			"csv 用 {\"path\":\"/data/x.csv\"}。" +
			"提交后用 data_get_job_status 轮询（status 走完 pending→running→completed/failed 即结束）。" +
			"经网关调用时本工具需人工审批，返回 request_id 而非 job_id 表示尚未执行。",
		InputSchema: toolInput[submitIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in submitIn) (*mcp.CallToolResult, submitOut, error) {
		if _, ok := cols.Get(in.Source); !ok {
			return nil, submitOut{}, fmt.Errorf("未知采集器: %s（用 data_list_sources 查看可用）", in.Source)
		}
		params := in.Params
		if params == nil {
			params = collector.Params{}
		}
		h := reg.Start(in.Source, func(jctx context.Context, job *jobs.Handle) error {
			return cols.Run(jctx, job, in.Source, params)
		})
		return nil, submitOut{JobID: h.ID(), Status: string(jobs.StatusPending), Source: in.Source}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_get_job_status",
		Description: "查询采集任务状态。参数：job_id=data_submit_collect_job 返回值（形如 \"job-xxx\"）。" +
			"返回：status 枚举 pending/running/completed/failed/cancelled；rows=已采集行数（running 期间持续增长）；" +
			"failed 时看 error 字段。轮询建议：间隔 1~2 秒直到 status 不再是 pending/running。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInput[jobIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in jobIn) (*mcp.CallToolResult, jobs.Job, error) {
		job, ok := reg.Get(in.JobID)
		if !ok {
			return nil, jobs.Job{}, fmt.Errorf("任务不存在: %s", in.JobID)
		}
		return nil, *job, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_list_jobs",
		Description: "列出近期采集任务。参数：status=可选过滤（pending/running/completed/failed/cancelled，留空为全部）；" +
			"limit=最多条数默认 20。忘记 job_id 时用它找回。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInput[listJobsIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in listJobsIn) (*mcp.CallToolResult, listJobsOut, error) {
		limit := in.Limit
		if limit <= 0 {
			limit = 20
		}
		return nil, listJobsOut{Jobs: reg.List(jobs.Status(in.Status), limit)}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_cancel_job",
		Description: "中止运行中的采集任务。参数：job_id。仅 pending/running 可取消；" +
			"已结束的任务返回\"任务已结束，无法取消\"错误。",
		InputSchema: toolInput[jobIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in jobIn) (*mcp.CallToolResult, cancelOut, error) {
		if err := reg.Cancel(in.JobID); err != nil {
			return nil, cancelOut{}, err
		}
		job, _ := reg.Get(in.JobID)
		return nil, cancelOut{JobID: job.ID, Status: string(job.Status)}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_db_ping",
		Description: fmt.Sprintf("数据库连通性探测并返回版本。参数：dsn_ref=数据源名（必填，见 data_list_dsn_refs；出于安全不开放直传连接串）；"+
			"db_type 可省略（自动按连接串推断，可选 %v）。返回：{ok, version, latency_ms} 或 {ok:false, error=脱敏后的失败原因}。"+
			"操作数据源前建议先 ping 确认连通。只读。", dbhub.Kinds()),
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInputDB[DBTargetIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in DBTargetIn) (*mcp.CallToolResult, dbPingOut, error) {
		kind, dsn, err := resolveTarget(in)
		if err != nil {
			return nil, dbPingOut{}, err
		}
		v, ms, err := dbhub.Ping(ctx, kind, dsn)
		if err != nil {
			return nil, dbPingOut{OK: false, Error: err.Error()}, nil
		}
		return nil, dbPingOut{OK: true, Version: v, LatencyMS: ms}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_list_dsn_refs",
		Description: "列出服务端已登记的数据源（无参数）。返回 refs=[{name, type}]——name 即其他工具的 dsn_ref 取值，" +
			"type 为自动推断的库类型（oracle/mysql/pg/mssql）；config_found=false 表示服务端未放配置文件。" +
			"只回名称不回连接串。访问任何数据库前先调本工具了解可用数据源。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInput[struct{}](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, listDsnOut, error) {
		return nil, listDsnOut{Refs: config.DSNRefs(), ConfigFound: config.ConfigPath() != ""}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "data_list_tables",
		Description: "列出目标库中的用户表（排除系统 schema）。库操作前先用本工具确认目标表存在。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInputDB[DBTargetIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in DBTargetIn) (*mcp.CallToolResult, listTablesOut, error) {
		kind, dsn, err := resolveTarget(in)
		if err != nil {
			return nil, listTablesOut{}, err
		}
		tables, err := dbhub.ListTables(ctx, kind, dsn)
		if err != nil {
			return nil, listTablesOut{OK: false, Error: err.Error()}, nil
		}
		return nil, listTablesOut{OK: true, Tables: tables}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "data_batch_import",
		Description: "事务批量写入目标表：行数组（键即列名），列名白名单校验、值全部参数绑定；返回实际写入行数。单次上限 50000 行。",
		InputSchema: toolInputDB[batchImportIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in batchImportIn) (*mcp.CallToolResult, batchImportOut, error) {
		kind, dsn, err := resolveTarget(in.DBTargetIn)
		if err != nil {
			return nil, batchImportOut{}, err
		}
		imported, err := dbhub.BatchImport(ctx, kind, dsn, in.Table, in.Rows)
		if err != nil {
			// 参数类错误（表名/行数非法）上抛为可读工具错误；库侧失败转结构化结果
			if isParamError(err) {
				return nil, batchImportOut{}, err
			}
			return nil, batchImportOut{OK: false, Error: err.Error()}, nil
		}
		return nil, batchImportOut{OK: true, Imported: imported}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "data_batch_query",
		Description: "条件查询目标表（等值过滤 + 行数上限，防全表拖库）：列名白名单、值参数绑定。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInputDB[batchQueryIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in batchQueryIn) (*mcp.CallToolResult, batchQueryOut, error) {
		kind, dsn, err := resolveTarget(in.DBTargetIn)
		if err != nil {
			return nil, batchQueryOut{}, err
		}
		rows, err := dbhub.BatchQuery(ctx, kind, dsn, in.Table, in.Filters, in.Limit)
		if err != nil {
			if isParamError(err) {
				return nil, batchQueryOut{}, err
			}
			return nil, batchQueryOut{OK: false, Error: err.Error()}, nil
		}
		return nil, batchQueryOut{OK: true, Rows: rows, Count: len(rows)}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "data_db_query_preview",
		Description: "同步小查询：对数据源执行任意单条 SELECT（含 JOIN/聚合）并直接返回行数据（默认 50 行，上限 500）。单表条件查询优先 data_batch_query；大批量搬运用 data_submit_collect_job。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInputDB[dbQueryPreviewIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in dbQueryPreviewIn) (*mcp.CallToolResult, dbQueryPreviewOut, error) {
		limit := in.Limit
		if limit <= 0 {
			limit = 50
		}
		if limit > 500 {
			limit = 500
		}
		params := collector.Params{"sql": in.SQL}
		if in.DSNRef != "" {
			params["dsn_ref"] = in.DSNRef
		}
		if in.DSN != "" {
			params["dsn"] = in.DSN
		}
		if in.DBType != "" {
			params["db_type"] = in.DBType
		}
		kind, cols, rows, truncated, err := collector.Preview(ctx, params, limit)
		if err != nil {
			return nil, dbQueryPreviewOut{}, err
		}
		return nil, dbQueryPreviewOut{Kind: kind, Columns: cols, Rows: rows, Returned: len(rows), Truncated: truncated}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "data_batch_process",
		Description: "按业务数据集名批量处理数据（内部按租户路由库、按月分表，异步执行立即返回 task_id，用 data_get_job_status 轮询）。",
		InputSchema: toolInput[batchProcessIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in batchProcessIn) (*mcp.CallToolResult, batchProcessOut, error) {
		// 路由失败立即报错（fail fast），不产生僵尸任务
		dbType, dbRef, err := dsrouting.RouteDB(in.DatasetID, in.TenantID)
		if err != nil {
			return nil, batchProcessOut{}, err
		}
		table, err := dsrouting.RouteTable(in.DatasetID, in.Period)
		if err != nil {
			return nil, batchProcessOut{}, err
		}
		mode := in.Mode
		if mode == "" {
			mode = "simulate"
		}
		routed := map[string]any{"db": dbRef, "db_type": dbType, "table": table, "mode": mode}
		h := reg.Start("batch_process:"+in.DatasetID, func(jctx context.Context, job *jobs.Handle) error {
			// real 模式才解析 dsn_ref → DSN 连接串；simulate 不触库
			dsn := ""
			if mode == "real" {
				d, ok := config.DSN(dbRef)
				if !ok {
					return fmt.Errorf("dsn_ref %s 未在 DSN_<名称> 登记（已配置: %v）", dbRef, config.DSNRefs())
				}
				dsn = d
			}
			_, detail, err := batchproc.Run(jctx, batchproc.Request{
				DBType: dbType, DSN: dsn, Table: table, Mode: mode, Rows: in.Rows,
			}, func(n int) { job.AddRows(n) })
			job.SetMessage(fmt.Sprintf("dataset=%s tenant=%s db=%s table=%s mode=%s; %s",
				in.DatasetID, in.TenantID, dbRef, table, mode, detail))
			return err
		})
		return nil, batchProcessOut{
			TaskID: h.ID(), Status: string(jobs.StatusPending),
			DatasetID: in.DatasetID, Period: in.Period, TenantID: in.TenantID, Routed: routed,
		}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "data_list_datasets",
		Description: "列出已配置的业务数据集及其路由规则（租户 → dsn_ref、按月分表）。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInput[struct{}](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, listDatasetsOut, error) {
		return nil, listDatasetsOut{Datasets: dsrouting.ListDatasets()}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_query_dataset",
		Description: "按业务数据集名做细粒度查询（第 3 层·全局数据平台，dataset_id 全量枚举：" +
			datasetEnumDescription() +
			"）。领域工具查不到的不常用数据集也在此查询；大批量处理用 data_batch_process。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInput[queryDatasetIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in queryDatasetIn) (*mcp.CallToolResult, queryDatasetOut, error) {
		dbType, dbRef, err := dsrouting.RouteDB(in.DatasetID, in.TenantID)
		if err != nil {
			return nil, queryDatasetOut{}, err
		}
		table := ""
		if in.Period != "" {
			table, err = dsrouting.RouteTable(in.DatasetID, in.Period)
		} else {
			table, err = dsrouting.TableOf(in.DatasetID)
		}
		if err != nil {
			return nil, queryDatasetOut{}, err
		}
		dsn, ok := config.DSN(dbRef)
		if !ok {
			return nil, queryDatasetOut{}, fmt.Errorf("dsn_ref %s 未在 DSN_<名称> 登记（已配置: %v）", dbRef, config.DSNRefs())
		}
		routed := map[string]any{"db": dbRef, "db_type": dbType, "table": table}
		rows, err := dbhub.BatchQuery(ctx, dbType, dsn, table, in.Filters, in.Limit)
		if err != nil {
			if isParamError(err) {
				return nil, queryDatasetOut{}, err
			}
			return nil, queryDatasetOut{OK: false, Routed: routed, Error: err.Error()}, nil
		}
		return nil, queryDatasetOut{OK: true, Routed: routed, Rows: rows, Count: len(rows)}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_hash_file",
		Description: "流式计算本机文件哈希（任意大小文件，内存恒定）。参数：path=datahub-server 所在机器的文件绝对路径（必填）；" +
			"algo=可选 md5/sha1/sha256/sha512，默认 sha256。返回：{algo, hex, size_bytes}。" +
			"用于核对传输完整性、比对采集数据文件指纹。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInput[hashFileIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in hashFileIn) (*mcp.CallToolResult, hashFileOut, error) {
		hexSum, size, err := filetools.HashFile(in.Path, in.Algo)
		if err != nil {
			return nil, hashFileOut{}, err
		}
		algo := strings.ToLower(in.Algo)
		if algo == "" {
			algo = "sha256"
		}
		return nil, hashFileOut{Algo: algo, Hex: hexSum, SizeBytes: size}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_file_stats",
		Description: "统计本机文件：大小/总行数/编码/修改时间/前 N 行预览（大文件流式扫描）。参数：path=文件绝对路径（必填）；" +
			"preview_lines=默认 5 上限 50。返回 encoding 枚举 ascii/utf-8/utf-8-bom/non-utf8(可能为GBK)/binary，" +
			"binary 文件预览为空。取数据前先调它确认编码与规模（GBK 需转码再采集）。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
		InputSchema: toolInput[fileStatsIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in fileStatsIn) (*mcp.CallToolResult, filetools.Stats, error) {
		st, err := filetools.FileStats(in.Path, in.PreviewLines)
		if err != nil {
			return nil, filetools.Stats{}, err
		}
		return nil, *st, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name: "data_convert_file",
		Description: "CSV↔JSONL 流式互转（GB 级可行，原子写：先 .tmp 再改名）。参数：src=源文件绝对路径（.csv/.jsonl/.ndjson，方向按扩展名自动判断）；" +
			"dst=目标文件绝对路径（已存在会被覆盖）；max_rows=可选截断。返回：{src,dst,from,to,rows_converted,truncated}。" +
			"CSV 首行为表头；JSONL 转 CSV 以首行键集为列、后续行未知列忽略。",
		InputSchema: toolInput[convertFileIn](),
	}, func(ctx context.Context, req *mcp.CallToolRequest, in convertFileIn) (*mcp.CallToolResult, filetools.ConvertResult, error) {
		res, err := filetools.ConvertFile(in.Src, in.Dst, "", in.MaxRows)
		if err != nil {
			return nil, filetools.ConvertResult{}, err
		}
		return nil, *res, nil
	})

	streamable := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil)
	mux := http.NewServeMux()
	mux.Handle("/mcp", streamable)
	// 双端点设计：/mcp 给网关聚合暴露给 AI；/internal/* 给业务 Python 服务间直调
	// （HTTP+JSON，不经网关；与 /mcp 同端口同 Bearer 鉴权，共用 dbhub 核心实现）
	internalapi.Register(mux)
	return server, mux
}

// isParamError 区分「调用方参数用错了」（上抛为可读工具错误，引导修正）与
// 「库侧执行失败」（转结构化 ok=false 结果，调用方可继续处理）。
func isParamError(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	for _, p := range []string{"表名", "列名", "rows 不能为空", "单次导入", "不支持的数据库类型", "dsn 与 dsn_ref"} {
		if strings.Contains(msg, p) {
			return true
		}
	}
	return false
}

func main() {
	// 默认值取自 Go 侧配置文件（config/datahub.env），OS 环境变量与命令行参数可覆盖——
	// 打包后无需重编译，改配置文件重启即生效。
	defaultPort, _ := strconv.Atoi(config.Value("GO_DATAHUB_PORT"))
	if defaultPort == 0 {
		defaultPort = 9300
	}
	defaultAddr := config.Value("GO_DATAHUB_ADDR")
	if defaultAddr == "" {
		defaultAddr = "127.0.0.1" // 跨机部署在配置里设 0.0.0.0，或命令行 -addr 0.0.0.0
	}
	addr := flag.String("addr", defaultAddr, "监听地址（跨机部署用 0.0.0.0）")
	port := flag.Int("port", defaultPort, "监听端口")
	token := flag.String("token", config.Value("GO_DATAHUB_TOKEN"), "Bearer 令牌；非空时所有请求必须携带 Authorization: Bearer <token>（配置文件 GO_DATAHUB_TOKEN 或同名环境变量提供）")
	flag.Parse()

	_, mux := buildServer()
	var handler http.Handler = mux
	if *token != "" {
		handler = bearerAuth(*token, mux)
		log.Printf("[go-datahub] 已启用 Bearer 鉴权")
	}
	logger := log.New(log.Writer(), "[go-datahub] ", log.LstdFlags)
	logger.Printf("启动 %s:%d/mcp", *addr, *port)
	srv := &http.Server{Addr: fmt.Sprintf("%s:%d", *addr, *port), Handler: handler}
	if err := srv.ListenAndServe(); err != nil {
		logger.Fatal(err)
	}
}

// bearerAuth 校验 Authorization: Bearer 头（常数时间比较，防时序侧信道）。
func bearerAuth(token string, next http.Handler) http.Handler {
	want := "Bearer " + token
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if subtle.ConstantTimeCompare([]byte(r.Header.Get("Authorization")), []byte(want)) != 1 {
			http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}
