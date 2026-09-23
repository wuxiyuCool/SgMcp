// tableops.go 提供贴近真实业务的批量库表操作：列表 / 批量导入 / 条件查询。
//
// 设计要点（与平台安全约定一致）：
//   - 表名/列名只允许安全标识符并做白名单校验（防 SQL 注入），值一律参数绑定；
//   - dsn 经 dbhub.Scrub 参与错误脱敏，连接串不进 MCP 结果 / 审计日志；
//   - 方言差异集中在此处处理：占位符 pg=$n、mysql/mssql=?、oracle=:n；
//     分页 pg/mysql=LIMIT、oracle=FETCH FIRST、mssql=TOP。
package dbhub

import (
	"context"
	"fmt"
	"regexp"
	"strings"
	"time"
)

// identRe 安全标识符：字母/下划线开头，字母数字下划线组成（防注入的第一道闸）。
var identRe = regexp.MustCompile(`^[A-Za-z_][A-Za-z0-9_]{0,62}$`)

// MaxBatchRows 单次批量导入上限（防一次工具调用拖垮库 / 网关超时）。
const MaxBatchRows = 50000

// validateTable 校验表名，允许 schema.table 两段形态，返回规范化表名。
func validateTable(table string) (string, error) {
	table = strings.TrimSpace(table)
	if table == "" {
		return "", fmt.Errorf("表名不能为空")
	}
	parts := strings.Split(table, ".")
	if len(parts) > 2 {
		return "", fmt.Errorf("表名格式非法（应为 [schema.]table）: %s", table)
	}
	for _, p := range parts {
		if !identRe.MatchString(p) {
			return "", fmt.Errorf("表名包含非法字符（仅允许字母数字下划线）: %s", table)
		}
	}
	return table, nil
}

// validateColumn 校验列名（单段标识符）。
func validateColumn(col string) error {
	if !identRe.MatchString(col) {
		return fmt.Errorf("列名非法（仅允许字母数字下划线）: %q", col)
	}
	return nil
}

// placeholder 按方言生成第 i 个（1 起）参数占位符。
func placeholder(kind string, i int) string {
	switch kind {
	case "pg":
		return fmt.Sprintf("$%d", i)
	case "oracle":
		return fmt.Sprintf(":%d", i)
	default: // mysql / mssql
		return "?"
	}
}

// listTablesSQL 各库列出用户表（排除系统 schema）。
var listTablesSQL = map[string]string{
	"pg":     `SELECT table_schema || '.' || table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1`,
	"mysql":  `SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE' ORDER BY 1`,
	"mssql":  `SELECT table_schema + '.' + table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE' AND table_schema NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest') ORDER BY 1`,
	"oracle": `SELECT owner || '.' || table_name FROM all_tables WHERE owner NOT IN ('SYS', 'SYSTEM', 'XDB') ORDER BY 1`,
}

// ListTables 列出目标库中的用户表（只读）。
func ListTables(ctx context.Context, kind, dsn string) ([]string, error) {
	sqlText, ok := listTablesSQL[kind]
	if !ok {
		return nil, fmt.Errorf("不支持的数据库类型: %s（可选 %v）", kind, Kinds())
	}
	db, err := Open(kind, dsn)
	if err != nil {
		return nil, Scrub(err, dsn)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	rows, err := db.QueryContext(ctx, sqlText)
	if err != nil {
		return nil, Scrub(err, dsn)
	}
	defer rows.Close()

	out := []string{}
	for rows.Next() {
		var name string
		if err := rows.Scan(&name); err != nil {
			return nil, Scrub(err, dsn)
		}
		out = append(out, name)
	}
	return out, Scrub(rows.Err(), dsn)
}

// BatchImport 事务批量导入：列名取自 rows 各键并集（白名单校验），值全部参数绑定。
// 返回实际写入行数。行数超 MaxBatchRows 直接拒绝（重活应分批提交或走异步 job）。
func BatchImport(ctx context.Context, kind, dsn, table string, rows []map[string]any) (int, error) {
	tbl, err := validateTable(table)
	if err != nil {
		return 0, err
	}
	if len(rows) == 0 {
		return 0, fmt.Errorf("rows 不能为空")
	}
	if len(rows) > MaxBatchRows {
		return 0, fmt.Errorf("单次导入 %d 行超过上限 %d，请分批提交", len(rows), MaxBatchRows)
	}

	// 列 = 首行键序（缺失键补 NULL），全部过白名单
	cols := make([]string, 0, len(rows[0]))
	seen := map[string]bool{}
	for k := range rows[0] {
		if err := validateColumn(k); err != nil {
			return 0, err
		}
		cols = append(cols, k)
		seen[k] = true
	}

	ph := make([]string, len(cols))
	for i := range cols {
		ph[i] = placeholder(kind, i+1)
	}
	sqlText := fmt.Sprintf("INSERT INTO %s (%s) VALUES (%s)",
		tbl, strings.Join(cols, ", "), strings.Join(ph, ", "))

	db, err := Open(kind, dsn)
	if err != nil {
		return 0, Scrub(err, dsn)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	tx, err := db.BeginTx(ctx, nil)
	if err != nil {
		return 0, Scrub(err, dsn)
	}
	defer tx.Rollback() //nolint:errcheck — 提交成功后 rollback 是无害 no-op

	stmt, err := tx.PrepareContext(ctx, sqlText)
	if err != nil {
		return 0, Scrub(err, dsn)
	}
	defer stmt.Close()

	imported := 0
	for _, row := range rows {
		args := make([]any, len(cols))
		for i, c := range cols {
			args[i] = row[c] // 缺失键 → nil
		}
		if _, err := stmt.ExecContext(ctx, args...); err != nil {
			return 0, Scrub(err, dsn)
		}
		imported++
	}
	if err := tx.Commit(); err != nil {
		return 0, Scrub(err, dsn)
	}
	return imported, nil
}

// BatchQuery 条件查询（只读）：filters 为等值匹配（列名白名单 + 参数绑定），limit 兜底防全表拖库。
func BatchQuery(ctx context.Context, kind, dsn, table string, filters map[string]any, limit int) ([]map[string]any, error) {
	tbl, err := validateTable(table)
	if err != nil {
		return nil, err
	}
	if limit <= 0 || limit > 1000 {
		limit = 100
	}

	where := make([]string, 0, len(filters))
	args := make([]any, 0, len(filters))
	i := 0
	for col, val := range filters {
		if err := validateColumn(col); err != nil {
			return nil, err
		}
		i++
		where = append(where, fmt.Sprintf("%s = %s", col, placeholder(kind, i)))
		args = append(args, val)
	}

	var sqlText string
	cond := ""
	if len(where) > 0 {
		cond = " WHERE " + strings.Join(where, " AND ")
	}
	switch kind {
	case "mssql":
		sqlText = fmt.Sprintf("SELECT TOP (%d) * FROM %s%s", limit, tbl, cond)
	case "oracle":
		sqlText = fmt.Sprintf("SELECT * FROM %s%s FETCH FIRST %d ROWS ONLY", tbl, cond, limit)
	default: // pg / mysql
		sqlText = fmt.Sprintf("SELECT * FROM %s%s LIMIT %d", tbl, cond, limit)
	}

	db, err := Open(kind, dsn)
	if err != nil {
		return nil, Scrub(err, dsn)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	rs, err := db.QueryContext(ctx, sqlText, args...)
	if err != nil {
		return nil, Scrub(err, dsn)
	}
	defer rs.Close()

	cols, err := rs.Columns()
	if err != nil {
		return nil, Scrub(err, dsn)
	}
	out := []map[string]any{}
	for rs.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rs.Scan(ptrs...); err != nil {
			return nil, Scrub(err, dsn)
		}
		m := make(map[string]any, len(cols))
		for i, c := range cols {
			m[c] = normalizeValue(vals[i])
		}
		out = append(out, m)
	}
	return out, Scrub(rs.Err(), dsn)
}

// ValidateTable 对外暴露表名校验（批处理核心在触库前先做参数级校验）。
func ValidateTable(table string) error {
	_, err := validateTable(table)
	return err
}

// CountTable 统计目标表行数（real 批处理用它确定处理规模；表不存在/无权限 → 可读错误）。
func CountTable(ctx context.Context, kind, dsn, table string) (int, error) {
	tbl, err := validateTable(table)
	if err != nil {
		return 0, err
	}
	db, err := Open(kind, dsn)
	if err != nil {
		return 0, Scrub(err, dsn)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(ctx, 15*time.Second)
	defer cancel()
	var n int
	if err := db.QueryRowContext(ctx, fmt.Sprintf("SELECT COUNT(*) FROM %s", tbl)).Scan(&n); err != nil {
		return 0, Scrub(fmt.Errorf("统计目标表失败（表不存在或无权限？）: %w", err), dsn)
	}
	return n, nil
}

// normalizeValue 把驱动返回的 []byte 统一为 string，便于 JSON 序列化。
func normalizeValue(v any) any {
	switch t := v.(type) {
	case []byte:
		return string(t)
	default:
		return v
	}
}
