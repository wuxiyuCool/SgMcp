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
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/sg/mcp/go-datahub/internal/config"
	"github.com/sg/mcp/go-datahub/internal/dbhub"
	"github.com/sg/mcp/go-datahub/internal/filetools"
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

// dangerousSQL 以词边界匹配写操作/危险函数关键字（列名如 update_time、delete_flag
// 不会误伤：下划线是单词字符，不构成词边界）。
var dangerousSQL = regexp.MustCompile(`(?i)\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|merge|exec|execute|copy|vacuum|outfile|dumpfile|load_file|infile|pg_sleep|dbms_lock|benchmark|lo_import|lo_export|pg_read_file|shutdown)\b`)

// dangerousTables 明确禁止读取的账号/权限类系统对象（凭证泄露面）。
var dangerousTables = regexp.MustCompile(`(?i)\b(mysql\.user|mysql\.db|information_schema\.(user|user_privileges|columns_priv)|pg_shadow|pg_authid|sys\.sql_logins|sys\.server_principals|sys\.database_principals|dba_users|all_users)\b`)

// GuardSelect 校验「只读单条查询」：允许 SELECT 与 WITH…SELECT（CTE 只读），
// 拒绝多语句、注释伪装与写/系统表访问关键字。
func GuardSelect(query string) error {
	body := stripSQLComments(strings.TrimSpace(query))
	if body == "" {
		return fmt.Errorf("sql 不能为空")
	}
	if len(query) > 20000 {
		return fmt.Errorf("sql 过长（%d 字符 > 20000），请简化查询", len(query))
	}
	trimmed := strings.TrimRight(body, " ;")
	if strings.Contains(trimmed, ";") {
		return fmt.Errorf("只允许单条 SELECT 语句（检测到分号，疑似拼接多条）")
	}
	if !(strings.HasPrefix(strings.ToLower(trimmed), "select") || strings.HasPrefix(strings.ToLower(trimmed), "with")) {
		return fmt.Errorf("sql 只允许以 SELECT 或 WITH 开头（只读查询），写操作请用库表工具并走审批闸门")
	}
	if got := dangerousSQL.FindString(trimmed); got != "" {
		return fmt.Errorf("sql 含被禁止的写法 %q（本工具只允许只读查询）", got)
	}
	if got := dangerousTables.FindString(trimmed); got != "" {
		return fmt.Errorf("sql 不允许访问账号/权限系统对象 %q", got)
	}
	return nil
}

// stripSQLComments 去掉 -- 行注释与 /* */ 块注释（防注释伪装关键字绕过检查）。
func stripSQLComments(q string) string {
	var sb strings.Builder
	for i := 0; i < len(q); {
		if i+1 < len(q) && q[i] == '-' && q[i+1] == '-' {
			for i < len(q) && q[i] != '\n' {
				i++
			}
			continue
		}
		if i+1 < len(q) && q[i] == '/' && q[i+1] == '*' {
			j := strings.Index(q[i+2:], "*/")
			if j < 0 {
				break
			}
			i += 2 + j + 2
			sb.WriteByte(' ')
			continue
		}
		sb.WriteByte(q[i])
		i++
	}
	return strings.TrimSpace(sb.String())
}

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
	path, err = filetools.CheckPath(path, false) // 路径围栏：AI 给的 path 不能读任意文件
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

// httpCollectClient 采集用 HTTP 客户端：必须自带超时——http.DefaultClient 无超时，
// 对端挂起会让 job 永久 running 并占住连接。
var httpCollectClient = &http.Client{
	Timeout: 30 * time.Second,
	Transport: &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		MaxIdleConns:          20,
		IdleConnTimeout:       60 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: 1 * time.Second,
	},
}

// MaxHTTPBody 单次接口采集的响应体上限（8MB）：超限截断并报错，防 OOM。
const MaxHTTPBody = 8 << 20

func (HttpSource) Collect(ctx context.Context, p Params, emit func(Record) error) error {
	url, err := strParam(p, "url")
	if err != nil {
		return err
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := httpCollectClient.Do(req)
	if err != nil {
		return fmt.Errorf("请求失败: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	// 骨架实现：整个响应体作为一条记录，真实业务在此换成解码 + 分页拉取。
	body, err := io.ReadAll(io.LimitReader(resp.Body, MaxHTTPBody+1))
	if err != nil {
		return err
	}
	if int64(len(body)) > MaxHTTPBody {
		return fmt.Errorf("接口响应超过 %d 字节上限，请改用分页采集", MaxHTTPBody)
	}
	return emit(Record{"url": url, "status": resp.StatusCode, "body": string(body)})
}

// DbQuerySource 从 Oracle/MySQL/PG/MSSQL 流式执行查询并逐行采集。
type DbQuerySource struct{}

func (DbQuerySource) Name() string { return "db_query" }
func (DbQuerySource) Kind() string { return "database" }
func (DbQuerySource) Description() string {
	return "对源库执行 SELECT 并流式采集（params: dsn_ref=配置文件中数据源名（推荐，类型自动推断，见 list_dsn_refs）或 dsn=裸连接串, db_type=可选覆盖（oracle/mysql/pg/mssql）, sql=必填且须以 SELECT 开头, max_rows=可选上限）"
}

// ResolveSource 解析数据源：优先 dsn_ref（连接串存服务端配置，密码不经网络），
// db_type 缺省时按 DSN scheme 自动推断，支持同类型多数据源。
func ResolveSource(p Params) (kind, dsn string, err error) {
	kind, _ = strParam(p, "db_type")
	if ref, rerr := strParam(p, "dsn_ref"); rerr == nil && ref != "" {
		d, ok := config.DSN(ref)
		if !ok {
			return "", "", fmt.Errorf("配置中不存在 DSN 引用: %s（已配置: %v）", ref, config.DSNRefList())
		}
		dsn = d
	} else {
		d, derr := strParam(p, "dsn")
		if derr != nil {
			return "", "", fmt.Errorf("dsn_ref 与 dsn 至少提供一个")
		}
		dsn = d
	}
	if kind == "" {
		if kind = dbhub.InferKind(dsn); kind == "" {
			return "", "", fmt.Errorf("无法从 DSN 推断数据库类型，请显式传 db_type（可选 %v）", dbhub.Kinds())
		}
	}
	return kind, dsn, nil
}

func (DbQuerySource) Collect(ctx context.Context, p Params, emit func(Record) error) error {
	kind, dsn, err := ResolveSource(p)
	if err != nil {
		return err
	}
	query, err := strParam(p, "sql")
	if err != nil {
		return err
	}
	if err := GuardSelect(query); err != nil {
		return err
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

// Preview 同步小查询（"看数据"场景；大批量搬运仍走 job）。
// 只允许单条 SELECT；最多返回 limit 行，truncated 标识是否被截断。
func Preview(ctx context.Context, p Params, limit int) (kind string, cols []string, rows []Record, truncated bool, err error) {
	kind, dsn, err := ResolveSource(p)
	if err != nil {
		return "", nil, nil, false, err
	}
	query, err := strParam(p, "sql")
	if err != nil {
		return "", nil, nil, false, err
	}
	if err := GuardSelect(query); err != nil {
		return "", nil, nil, false, err
	}
	db, err := dbhub.Open(kind, dsn)
	if err != nil {
		return kind, nil, nil, false, dbhub.Scrub(err, dsn)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	rs, err := db.QueryContext(ctx, query)
	if err != nil {
		return kind, nil, nil, false, dbhub.Scrub(err, dsn)
	}
	defer rs.Close()

	cols, err = rs.Columns()
	if err != nil {
		return kind, nil, nil, false, err
	}
	rows = []Record{}
	for rs.Next() {
		if len(rows) >= limit {
			truncated = true
			break
		}
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rs.Scan(ptrs...); err != nil {
			return kind, cols, rows, truncated, dbhub.Scrub(err, dsn)
		}
		rec := Record{}
		for i, c := range cols {
			// []byte 统一转字符串，避免 JSON 序列化成 base64
			if b, ok := vals[i].([]byte); ok {
				rec[c] = string(b)
			} else {
				rec[c] = vals[i]
			}
		}
		rows = append(rows, rec)
	}
	return kind, cols, rows, truncated, rs.Err()
}
