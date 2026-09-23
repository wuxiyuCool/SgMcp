// Package dsrouting 实现「业务名 → 数据源」路由（对应架构例子：
//
//	db    = routeDB("orders", "t1")      # 按租户路由
//	table = routeTable("orders", "2024-05")  # 按月分表
//
// dataset_id 是业务名（如 orders / incidents），不是表名——AI 与业务层都只认
// 业务名，物理库表由这里的路由规则决定。
//
// 配置约定（env / config/datahub.env，键统一大写）：
//
//	DATASET_<NAME>_DBTYPE=<pg|mysql|mssql|oracle>   # 库类型（默认 pg）
//	DATASET_<NAME>_DSN_<租户大写>=<dsn_ref>          # 租户专属路由（如 t1 → _DSN_T1）
//	DATASET_<NAME>_DSN_DEFAULT=<dsn_ref>            # 兜底路由（未命中租户时）
//	DATASET_<NAME>_DOMAIN=<域名>                     # 域归属（如 itops/order），领域查询工具按域限定枚举
//	DATASET_<NAME>_DESC=<中文说明>                   # 数据集中文说明（工具枚举描述用，让 AI 看名知义）
//
// 值是 dsn_ref（引用 DSN_<名称> 注册表），不存连接串本体。
// Python 侧 mcp_shared/dsrouting.py 用同一套 env 约定，两端路由结果一致。
package dsrouting

import (
	"fmt"
	"regexp"
	"sort"
	"strings"

	"github.com/sg/mcp/go-datahub/internal/config"
)

// periodRe 合法周期：YYYY-MM（按月分表的分片键）。
var periodRe = regexp.MustCompile(`^\d{4}-(0[1-9]|1[0-2])$`)

// identRe 业务名/租户的合法形态（拼 env 键前先校验，防注入）。
var identRe = regexp.MustCompile(`^[a-z][a-z0-9_]{0,62}$`)

func norm(s string) string { return strings.ToUpper(strings.TrimSpace(s)) }

// RouteDB 按业务名 + 租户路由数据源：返回 (db_type, dsn_ref)。
// 键约定：租户专属 = DATASET_<NAME>_DSN_<租户大写>（如 t1 → DATASET_ORDERS_DSN_T1），
// 未命中回退 DATASET_<NAME>_DSN_DEFAULT，都没有则报可读错误（列出期望的键名）。
// 租户名保留字 DEFAULT 不允许（与兜底键冲突）。
func RouteDB(dataset, tenant string) (dbType, dsnRef string, err error) {
	if !identRe.MatchString(strings.TrimSpace(strings.ToLower(dataset))) {
		return "", "", fmt.Errorf("数据集名非法（小写字母数字下划线）: %q", dataset)
	}
	tn := norm(tenant)
	if strings.TrimSpace(tenant) == "" {
		return "", "", fmt.Errorf("tenant_id 不能为空")
	}
	if tn == "DEFAULT" {
		return "", "", fmt.Errorf("tenant_id 不能为保留字 default")
	}
	base := "DATASET_" + norm(dataset)
	dbType = config.Value(base + "_DBTYPE")
	if dbType == "" {
		dbType = "pg"
	}
	if v := config.Value(base + "_DSN_" + tn); v != "" {
		return dbType, v, nil
	}
	if v := config.Value(base + "_DSN_DEFAULT"); v != "" {
		return dbType, v, nil
	}
	return "", "", fmt.Errorf(
		"数据集 %q 未配置路由：需要 %s_DSN_%s 或 %s_DSN_DEFAULT（值为 dsn_ref，见 heavy.list_dsn_refs）",
		dataset, base, tn, base)
}

// RouteTable 按业务名 + 周期路由表名（按月分表）：orders + 2024-05 → orders_202405。
func RouteTable(dataset, period string) (string, error) {
	ds := strings.TrimSpace(strings.ToLower(dataset))
	if !identRe.MatchString(ds) {
		return "", fmt.Errorf("数据集名非法（小写字母数字下划线）: %q", dataset)
	}
	if !periodRe.MatchString(strings.TrimSpace(period)) {
		return "", fmt.Errorf("period 格式应为 YYYY-MM（如 2024-05）: %q", period)
	}
	return fmt.Sprintf("%s_%s", ds, strings.ReplaceAll(strings.TrimSpace(period), "-", "")), nil
}

// TableOf 非周期数据集（如 incidents）的固定表名 = 数据集名。
func TableOf(dataset string) (string, error) {
	ds := strings.TrimSpace(strings.ToLower(dataset))
	if !identRe.MatchString(ds) {
		return "", fmt.Errorf("数据集名非法（小写字母数字下划线）: %q", dataset)
	}
	return ds, nil
}

// Domain 数据集的域归属（DATASET_<NAME>_DOMAIN，未配置返回空串 = 未分域）。
// 领域查询工具（如 itops.query_dataset）用它限定本域枚举，越域时引导去
// 全局数据平台（data.query_dataset）。
func Domain(dataset string) string {
	return config.Value("DATASET_" + norm(dataset) + "_DOMAIN")
}

// Desc 数据集中文说明（DATASET_<NAME>_DESC，工具枚举描述用）。
func Desc(dataset string) string {
	return config.Value("DATASET_" + norm(dataset) + "_DESC")
}

// DatasetInfo 数据集路由概览（只含名称与引用名，不含任何连接串）。
type DatasetInfo struct {
	Dataset    string   `json:"dataset"`
	DBType     string   `json:"db_type"`
	Domain     string   `json:"domain,omitempty"`
	Desc       string   `json:"desc,omitempty"`
	Tenants    []string `json:"tenants,omitempty"`
	DSNDefault string   `json:"dsn_default,omitempty"`
	DSNTenants []string `json:"dsn_tenant_refs,omitempty"` // 与 Tenants 一一对应
}

// acc 数据集配置的聚合中间态。
type acc struct {
	dbType  string
	domain  string
	desc    string
	tenants map[string]string // tenant（小写，"__default__"=兜底）→ dsn_ref（小写）
}

// ListDatasets 枚举已配置的数据集路由（扫描 DATASET_* 键，不回连接串）。
func ListDatasets() []DatasetInfo {
	sets := map[string]*acc{}
	for _, k := range config.Keys("DATASET_") {
		rest := strings.TrimPrefix(k, "DATASET_")
		switch {
		case strings.HasSuffix(rest, "_DBTYPE"):
			name := strings.ToLower(strings.TrimSuffix(rest, "_DBTYPE"))
			ensure(sets, name).dbType = config.Value(k)
		case strings.HasSuffix(rest, "_DOMAIN"):
			name := strings.ToLower(strings.TrimSuffix(rest, "_DOMAIN"))
			ensure(sets, name).domain = strings.ToLower(config.Value(k))
		case strings.HasSuffix(rest, "_DESC"):
			name := strings.ToLower(strings.TrimSuffix(rest, "_DESC"))
			ensure(sets, name).desc = config.Value(k)
		case strings.HasSuffix(rest, "_DSN_DEFAULT"):
			name := strings.ToLower(strings.TrimSuffix(rest, "_DSN_DEFAULT"))
			a := ensure(sets, name)
			a.tenants["__default__"] = strings.ToLower(config.Value(k))
		default:
			// DATASET_<NAME>_DSN_<租户>（保留字 DEFAULT 已在上面的分支处理）
			if idx := strings.Index(rest, "_DSN_"); idx > 0 {
				name := strings.ToLower(rest[:idx])
				tenant := rest[idx+len("_DSN_"):]
				if tenant != "" && tenant != "DEFAULT" {
					ensure(sets, name).tenants[strings.ToLower(tenant)] = strings.ToLower(config.Value(k))
				}
			}
		}
	}
	out := []DatasetInfo{}
	for name, a := range sets {
		info := DatasetInfo{Dataset: name, DBType: a.dbType, Domain: a.domain, Desc: a.desc,
			Tenants: []string{}, DSNTenants: []string{}}
		if info.DBType == "" {
			info.DBType = "pg"
		}
		// 稳定输出：default 最后，其余按租户名排序
		tenants := make([]string, 0, len(a.tenants))
		for t := range a.tenants {
			tenants = append(tenants, t)
		}
		sort.Strings(tenants)
		for _, t := range tenants {
			if t == "__default__" {
				info.DSNDefault = a.tenants[t]
				continue
			}
			info.Tenants = append(info.Tenants, t)
			info.DSNTenants = append(info.DSNTenants, a.tenants[t])
		}
		out = append(out, info)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Dataset < out[j].Dataset })
	return out
}

func ensure(m map[string]*acc, name string) *acc {
	a, ok := m[name]
	if !ok {
		a = &acc{tenants: map[string]string{}}
		m[name] = a
	}
	return a
}
