// Package collector 定义采集器（Source）抽象与注册表。
//
// 采集器是"重活"的落点：从混合数据源（数据库 / HTTP 接口 / 文件）
// 流式读出记录，每读一批就通过 emit 回调交给下游（落库/汇总），
// 并响应 context 取消，全程内存占用与数据量解耦。
package collector

import (
	"context"
	"encoding/csv"
	"fmt"
	"io"
	"net/http"
	"os"
	"sort"
	"strconv"
	"time"

	"github.com/sg/mcp/go-datahub/internal/config"
	"github.com/sg/mcp/go-datahub/internal/dbhub"
	"github.com/sg/mcp/go-datahub/internal/jobs"
)

// Record 一条采集到的记录（列名 -> 值）。
type Record map[string]any

// Params 采集参数，来自 submit_collect_job 的 params 字段。
type Params map[string]any

// Source 一个可注册的数据采集器。
type Source interface {
	Name() string
	Kind() string
	Description() string
	// Collect 流式采集：每产出一条记录调用 emit，emit 返回错误应立即中止。
	Collect(ctx context.Context, p Params, emit func(Record) error) error
}

// Registry 采集器注册表。
type Registry struct {
	sources map[string]Source
}

func NewRegistry(sources ...Source) *Registry {
	m := map[string]Source{}
	for _, s := range sources {
		m[s.Name()] = s
	}
	return &Registry{sources: m}
}

func (r *Registry) Get(name string) (Source, bool) {
	s, ok := r.sources[name]
	return s, ok
}

type Info struct {
	Name        string `json:"name"`
	Kind        string `json:"kind"`
	Description string `json:"description"`
}

func (r *Registry) List() []Info {
	out := make([]Info, 0, len(r.sources))
	for _, s := range r.sources {
		out = append(out, Info{Name: s.Name(), Kind: s.Kind(), Description: s.Description()})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// Run 在 job 上下文中执行采集，把产出的行数累计到 job 进度。
func (r *Registry) Run(ctx context.Context, job *jobs.Handle, name string, p Params) error {
	s, ok := r.Get(name)
	if !ok {
		return fmt.Errorf("未知采集器: %s（用 list_sources 查看可用）", name)
	}
	job.SetMessage(fmt.Sprintf("collector=%s", name))
	return s.Collect(ctx, p, func(rec Record) error {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		job.AddRows(1)
		return nil
	})
}

// --- 参数小工具 -----------------------------------------------------------

func intParam(p Params, key string, def int) int {
	if v, ok := p[key]; ok {
		switch n := v.(type) {
		case float64:
			return int(n)
		case int:
			return n
		case string:
			if i, err := strconv.Atoi(n); err == nil {
				return i
			}
		}
	}
	return def
}

func strParam(p Params, key string) (string, error) {
	v, ok := p[key]
	if !ok {
		return "", fmt.Errorf("缺少参数: %s", key)
	}
	s, ok := v.(string)
	if !ok || s == "" {
		return "", fmt.Errorf("参数 %s 必须是非空字符串", key)
	}
	return s, nil
}

// --- 内置采集器 -----------------------------------------------------------

// SyntheticSource 压测/演示用：流式生成 N 条模拟指标行。
type SyntheticSource struct{}

func (SyntheticSource) Name() string { return "synthetic" }
func (SyntheticSource) Kind() string { return "demo" }
func (SyntheticSource) Description() string {
	return "生成模拟指标数据（params: rows=行数默认1000, batch_pause_ms=每100行暂停毫秒默认0），用于验证大批量采集链路"
}

func (SyntheticSource) Collect(ctx context.Context, p Params, emit func(Record) error) error {
	rows := intParam(p, "rows", 1000)
	pause := time.Duration(intParam(p, "batch_pause_ms", 0)) * time.Millisecond
	base := time.Now()
	for i := range rows {
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}
		rec := Record{
			"ts":     base.Add(time.Duration(i) * time.Second).Format(time.RFC3339),
			"seq":    i,
			"metric": "demo_cpu",
			"value":  float64(i%97) + 0.5,
		}
		if err := emit(rec); err != nil {
			return err
		}
		if pause > 0 && i%100 == 99 {
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(pause):
			}
		}
	}
	return nil
}

// CsvSource 从本地 CSV 文件流式读取。
type CsvSource struct{}

func (CsvSource) Name() string { return "csv" }
func (CsvSource) Kind() string { return "file" }
func (CsvSource) Description() string {
	return "读取本地 CSV 文件（params: path=文件路径，首行为表头；max_rows=可选上限）"
}

func (CsvSource) Collect(ctx context.Context, p Params, emit func(Record) error) error {
	path, err := strParam(p, "path")
	if err != nil {
		return err
	}
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("打开文件失败: %w", err)
	}
	defer f.Close()

	br := csv.NewReader(f)
	br.FieldsPerRecord = -1
	header, err := br.Read()
	if err != nil {
		return fmt.Errorf("读取表头失败: %w", err)
	}
	maxRows := intParam(p, "max_rows", 0)
	for n := 0; ; n++ {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if maxRows > 0 && n >= maxRows {
			return nil
		}
		row, err := br.Read()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fmt.Errorf("第 %d 行解析失败: %w", n+2, err)
		}
		rec := Record{}
		for i, col := range header {
			if i < len(row) {
				rec[col] = row[i]
			}
		}
		if err := emit(rec); err != nil {
			return err
		}
	}
}

// HttpSource 抓取 HTTP JSON 接口（单 URL，用于接口类采集的最小骨架）。
type HttpSource struct{}

func (HttpSource) Name() string { return "http_json" }
func (HttpSource) Kind() string { return "http" }
func (HttpSource) Description() string {
	return "GET 一个 HTTP 接口并把响应体作为单条记录采集（params: url=必填, list_key=可选，按数组字段拆行）"
}

func (HttpSource) Collect(ctx context.Context, p Params, emit func(Record) error) error {
	url, err := strParam(p, "url")
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("请求失败: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	// 骨架实现：整个响应体作为一条记录，真实业务在此换成解码 + 分页拉取。
	body, err := io.ReadAll(io.LimitReader(resp.Body, 8<<20))
	if err != nil {
		return err
	}
	return emit(Record{"url": url, "status": resp.StatusCode, "body": string(body)})
}

// DbQuerySource 从 Oracle/MySQL/PG/MSSQL 流式执行查询并逐行采集。
type DbQuerySource struct{}

func (DbQuerySource) Name() string { return "db_query" }
func (DbQuerySource) Kind() string { return "database" }
func (DbQuerySource) Description() string {
	return fmt.Sprintf("对源库执行 SELECT 并流式采集（params: db_type=%v, dsn_ref=配置文件中 DSN_<名称> 的引用名（推荐，密码不经网络）或 dsn=裸连接串, sql=必填且须以 SELECT 开头, max_rows=可选上限）", dbhub.Kinds())
}

// resolveDSN 优先取 dsn_ref（连接串存服务端配置文件，密码不经 MCP 消息/审计日志流转）。
func resolveDSN(p Params) (string, error) {
	if ref, err := strParam(p, "dsn_ref"); err == nil && ref != "" {
		if dsn, ok := config.DSN(ref); ok {
			return dsn, nil
		}
		return "", fmt.Errorf("配置中不存在 DSN 引用: %s（已配置: %v）", ref, config.DSNRefs())
	}
	return strParam(p, "dsn")
}

func (DbQuerySource) Collect(ctx context.Context, p Params, emit func(Record) error) error {
	kind, err := strParam(p, "db_type")
	if err != nil {
		return err
	}
	dsn, err := resolveDSN(p)
	if err != nil {
		return err
	}
	query, err := strParam(p, "sql")
	if err != nil {
		return err
	}
	// 简单护栏：只允许单条 SELECT，写操作必须走审批闸门的其他通道。
	if len(query) < 6 || query[:6] != "SELECT" && query[:6] != "select" {
		return fmt.Errorf("sql 参数只允许 SELECT 查询")
	}
	maxRows := intParam(p, "max_rows", 0)

	db, err := dbhub.Open(kind, dsn)
	if err != nil {
		return dbhub.Scrub(err, dsn)
	}
	defer db.Close()

	rows, err := db.QueryContext(ctx, query)
	if err != nil {
		return dbhub.Scrub(err, dsn)
	}
	defer rows.Close()

	cols, err := rows.Columns()
	if err != nil {
		return err
	}
	for n := 0; rows.Next(); n++ {
		if ctx.Err() != nil {
			return ctx.Err()
		}
		if maxRows > 0 && n >= maxRows {
			return nil
		}
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			return fmt.Errorf("第 %d 行读取失败: %w", n+1, err)
		}
		rec := Record{}
		for i, c := range cols {
			rec[c] = vals[i]
		}
		if err := emit(rec); err != nil {
			return err
		}
	}
	return rows.Err()
}

// Default 返回平台内置采集器注册表。
func Default() *Registry {
	return NewRegistry(
		SyntheticSource{},
		CsvSource{},
		HttpSource{},
		DbQuerySource{},
	)
}
