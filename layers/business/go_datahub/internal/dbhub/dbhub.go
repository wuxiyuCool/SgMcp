// Package dbhub 统一多数据库的连接与探活（Oracle / MySQL / PostgreSQL / SQLServer）。
package dbhub

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"strings"
	"time"

	_ "github.com/go-sql-driver/mysql"
	_ "github.com/jackc/pgx/v5/stdlib"
	_ "github.com/microsoft/go-mssqldb"
	_ "github.com/sijms/go-ora/v2"
)

// kinds 登记每种库的驱动名、探测 SQL 与 DSN 示例。
var kinds = map[string]struct {
	driver   string
	probeSQL string
	dsnHint  string
}{
	"oracle": {driver: "oracle", probeSQL: "SELECT banner FROM v$version WHERE ROWNUM = 1", dsnHint: `oracle://user:pass@host:1521/servicename`},
	"mysql":  {driver: "mysql", probeSQL: "SELECT VERSION()", dsnHint: "user:pass@tcp(host:3306)/dbname?parseTime=true"},
	"pg":     {driver: "pgx", probeSQL: "SHOW server_version", dsnHint: "postgres://user:pass@host:5432/db?sslmode=disable"},
	"mssql":  {driver: "sqlserver", probeSQL: "SELECT @@VERSION", dsnHint: "sqlserver://user:pass@host:1433?database=db"},
}

// Kinds 返回支持的数据库类型列表。
func Kinds() []string {
	out := make([]string, 0, len(kinds))
	for k := range kinds {
		out = append(out, k)
	}
	return out
}

// InferKind 从 DSN 形态推断数据库类型（oracle/pg/mssql 自带 scheme；
// user:pass@tcp(...) 形态为 mysql）。无法判定返回空串，由调用方要求显式 db_type。
func InferKind(dsn string) string {
	switch {
	case strings.HasPrefix(dsn, "oracle://"):
		return "oracle"
	case strings.HasPrefix(dsn, "postgres://"), strings.HasPrefix(dsn, "postgresql://"):
		return "pg"
	case strings.HasPrefix(dsn, "sqlserver://"):
		return "mssql"
	case strings.Contains(dsn, "@tcp("):
		return "mysql"
	default:
		return ""
	}
}

// Open 按类型打开连接池（调用方负责 Close / 复用）。
func Open(kind, dsn string) (*sql.DB, error) {
	k, ok := kinds[kind]
	if !ok {
		return nil, fmt.Errorf("不支持的数据库类型: %s（可选 %v）", kind, Kinds())
	}
	db, err := sql.Open(k.driver, dsn)
	if err != nil {
		return nil, fmt.Errorf("打开连接失败: %w", err)
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(30 * time.Minute)
	return db, nil
}

// Ping 连通性探测：建立连接并读取版本信息。
func Ping(ctx context.Context, kind, dsn string) (version string, latencyMS int64, err error) {
	k, ok := kinds[kind]
	if !ok {
		return "", 0, fmt.Errorf("不支持的数据库类型: %s（可选 %v）", kind, Kinds())
	}
	start := time.Now()
	db, err := sql.Open(k.driver, dsn)
	if err != nil {
		return "", 0, fmt.Errorf("打开连接失败: %w", Scrub(err, dsn))
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		return "", 0, fmt.Errorf("连接失败: %w", Scrub(err, dsn))
	}
	var v string
	if err := db.QueryRowContext(ctx, k.probeSQL).Scan(&v); err != nil {
		return "", 0, fmt.Errorf("读取版本失败: %w", Scrub(err, dsn))
	}
	return v, time.Since(start).Milliseconds(), nil
}

// Scrub 把错误文本里出现的 DSN（含密码）替换为 ***，防止凭证进入 MCP 结果/审计日志。
func Scrub(err error, dsn string) error {
	msg := err.Error()
	if dsn != "" && strings.Contains(msg, dsn) {
		msg = strings.ReplaceAll(msg, dsn, "***")
	}
	// 驱动报错不一定回显完整 DSN，但密码可能单独出现在错误里（如 go-ora 回显 URI 片段）：
	// 从 DSN 中抽出密码再替换一次。
	if pw := passwordOf(dsn); pw != "" && strings.Contains(msg, pw) {
		msg = strings.ReplaceAll(msg, pw, "***")
	}
	return errors.New(msg)
}

// passwordOf 从常见 DSN 形态中提取密码（user:pass@... 或 scheme://user:pass@...）。
func passwordOf(dsn string) string {
	at := strings.LastIndex(dsn, "@")
	if at <= 0 {
		return ""
	}
	prefix := dsn[:at]
	if colon := strings.LastIndex(prefix, ":"); colon >= 0 {
		if slash := strings.LastIndex(prefix[:colon], "/"); slash < 0 {
			return prefix[colon+1:]
		}
	}
	return ""
}
