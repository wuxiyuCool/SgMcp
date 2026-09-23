// 【中层·数据重活】go-datahub MCP server（Go 官方 SDK 实现）。
//
// 定位：与 it_ops（Python 轻活）分工，承担大批量数据采集与多数据库交互。
// 长任务采用异步 job 模式：submit_collect_job 立即返回 job_id，
// get_job_status 轮询进度，cancel_job 中止 —— 避免网关 HTTP 长连接超时。
//
// 运行：
//
//	go run ./cmd/datahub-server                 # 默认 127.0.0.1:9300 /mcp
//	go build -o datahub-server.exe ./cmd/datahub-server
package main

import (
	"context"
	"crypto/subtle"
	"flag"
	"fmt"
	"log"
	"net/http"
	"strconv"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/sg/mcp/go-datahub/internal/collector"
	"github.com/sg/mcp/go-datahub/internal/config"
	"github.com/sg/mcp/go-datahub/internal/dbhub"
	"github.com/sg/mcp/go-datahub/internal/jobs"
)

// ---------------------------------------------------------------------------
// 工具入出参结构（jsonschema 标签即参数描述，SDK 自动校验）
// ---------------------------------------------------------------------------

type listSourcesOut struct {
	Sources []collector.Info `json:"sources"`
}

type submitIn struct {
	Source string         `json:"source" jsonschema:"采集器名称，见 list_sources"`
	Params map[string]any `json:"params,omitempty" jsonschema:"采集参数，各采集器定义见其 description"`
}

type submitOut struct {
	JobID  string `json:"job_id"`
	Status string `json:"status"`
	Source string `json:"source"`
}

type jobIn struct {
	JobID string `json:"job_id" jsonschema:"submit_collect_job 返回的任务 ID"`
}

type listJobsIn struct {
	Status string `json:"status,omitempty" jsonschema:"按状态过滤：pending/running/completed/failed/cancelled，留空为全部"`
	Limit  int    `json:"limit,omitempty" jsonschema:"最多返回条数，默认 20"`
}

type listJobsOut struct {
	Jobs []jobs.Job `json:"jobs"`
}

type dbPingIn struct {
	DBType string `json:"db_type" jsonschema:"数据库类型：oracle/mysql/pg/mssql"`
	DSN    string `json:"dsn,omitempty" jsonschema:"连接串（与 dsn_ref 二选一；推荐优先 dsn_ref 避免密码经网络传输）"`
	DSNRef string `json:"dsn_ref,omitempty" jsonschema:"配置文件 DSN_<名称> 的引用名，如 order_pg（优先于 dsn），可用 list_dsn_refs 查看"`
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

// ---------------------------------------------------------------------------

func buildServer() (*mcp.Server, *http.ServeMux) {
	server := mcp.NewServer(&mcp.Implementation{
		Name:    "go-datahub",
		Title:   "数据重活 MCP（Go）",
		Version: "0.1.0",
	}, nil)

	reg := jobs.NewRegistry()
	cols := collector.Default()

	mcp.AddTool(server, &mcp.Tool{
		Name:        "list_sources",
		Description: "列出可用采集器（数据库/HTTP/文件/演示）及其参数说明。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
	}, func(ctx context.Context, req *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, listSourcesOut, error) {
		return nil, listSourcesOut{Sources: cols.List()}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "submit_collect_job",
		Description: "提交一个异步批量采集任务，立即返回 job_id；大规模采集由此发起，不阻塞调用。",
	}, func(ctx context.Context, req *mcp.CallToolRequest, in submitIn) (*mcp.CallToolResult, submitOut, error) {
		if _, ok := cols.Get(in.Source); !ok {
			return nil, submitOut{}, fmt.Errorf("未知采集器: %s（用 list_sources 查看可用）", in.Source)
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
		Name:        "get_job_status",
		Description: "查询采集任务状态与已采集行数（rows 随进度增长）。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
	}, func(ctx context.Context, req *mcp.CallToolRequest, in jobIn) (*mcp.CallToolResult, jobs.Job, error) {
		job, ok := reg.Get(in.JobID)
		if !ok {
			return nil, jobs.Job{}, fmt.Errorf("任务不存在: %s", in.JobID)
		}
		return nil, *job, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "list_jobs",
		Description: "列出采集任务，可按状态过滤。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
	}, func(ctx context.Context, req *mcp.CallToolRequest, in listJobsIn) (*mcp.CallToolResult, listJobsOut, error) {
		limit := in.Limit
		if limit <= 0 {
			limit = 20
		}
		return nil, listJobsOut{Jobs: reg.List(jobs.Status(in.Status), limit)}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "cancel_job",
		Description: "中止一个运行中的采集任务。",
	}, func(ctx context.Context, req *mcp.CallToolRequest, in jobIn) (*mcp.CallToolResult, cancelOut, error) {
		if err := reg.Cancel(in.JobID); err != nil {
			return nil, cancelOut{}, err
		}
		job, _ := reg.Get(in.JobID)
		return nil, cancelOut{JobID: job.ID, Status: string(job.Status)}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "db_ping",
		Description: fmt.Sprintf("数据库连通性探测并返回版本。支持: %v；推荐用 dsn_ref 引用服务端配置，免传密码", dbhub.Kinds()),
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
	}, func(ctx context.Context, req *mcp.CallToolRequest, in dbPingIn) (*mcp.CallToolResult, dbPingOut, error) {
		dsn := in.DSN
		if in.DSNRef != "" {
			d, ok := config.DSN(in.DSNRef)
			if !ok {
				return nil, dbPingOut{}, fmt.Errorf("配置中不存在 DSN 引用: %s（已配置: %v）", in.DSNRef, config.DSNRefs())
			}
			dsn = d
		}
		if dsn == "" {
			return nil, dbPingOut{}, fmt.Errorf("dsn 与 dsn_ref 至少提供一个")
		}
		v, ms, err := dbhub.Ping(ctx, in.DBType, dsn)
		if err != nil {
			return nil, dbPingOut{OK: false, Error: err.Error()}, nil
		}
		return nil, dbPingOut{OK: true, Version: v, LatencyMS: ms}, nil
	})

	mcp.AddTool(server, &mcp.Tool{
		Name:        "list_dsn_refs",
		Description: "列出服务端配置文件（config/datahub.env）中已登记的 DSN 引用名（只回名称，不回连接串）。只读。",
		Annotations: &mcp.ToolAnnotations{ReadOnlyHint: true},
	}, func(ctx context.Context, req *mcp.CallToolRequest, _ struct{}) (*mcp.CallToolResult, listDsnOut, error) {
		return nil, listDsnOut{Refs: config.DSNRefs(), ConfigFound: config.ConfigPath() != ""}, nil
	})

	streamable := mcp.NewStreamableHTTPHandler(func(*http.Request) *mcp.Server { return server }, nil)
	mux := http.NewServeMux()
	mux.Handle("/mcp", streamable)
	return server, mux
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
