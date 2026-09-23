package dsrouting

import (
	"encoding/json"
	"testing"

	"github.com/sg/mcp/go-datahub/internal/config"
)

func TestRouteDBTenantOverrideAndDefault(t *testing.T) {
	t.Setenv("DATASET_ORDERS_DBTYPE", "pg")
	t.Setenv("DATASET_ORDERS_DSN_T1", "order_t1_pg")
	t.Setenv("DATASET_ORDERS_DSN_DEFAULT", "order_pg")
	config.Reload()
	t.Cleanup(config.Reload)

	// 租户专属路由优先
	dbType, ref, err := RouteDB("orders", "t1")
	if err != nil || dbType != "pg" || ref != "order_t1_pg" {
		t.Fatalf("租户路由错误: dbType=%s ref=%s err=%v", dbType, ref, err)
	}

	// 未命中租户 → DEFAULT 兜底
	_, ref, err = RouteDB("orders", "t9")
	if err != nil || ref != "order_pg" {
		t.Fatalf("兜底路由错误: ref=%s err=%v", ref, err)
	}

	// 大小写归一：租户 T1 与 t1 等价
	_, ref, err = RouteDB("ORDERS", "T1")
	if err != nil || ref != "order_t1_pg" {
		t.Fatalf("大小写归一失败: ref=%s err=%v", ref, err)
	}
}

func TestRouteDBUnconfiguredIsReadableError(t *testing.T) {
	config.Reload()
	t.Cleanup(config.Reload)
	_, _, err := RouteDB("no_such_ds", "t1")
	if err == nil {
		t.Fatal("未配置数据集应报错")
	}
	// 错误信息应引导配置（包含期望的 env 键名），且不含连接串
	if !contains(err.Error(), "DATASET_NO_SUCH_DS_DSN_DEFAULT") {
		t.Fatalf("错误应列出期望的配置键: %v", err)
	}
}

func TestRouteTableMonthlySharding(t *testing.T) {
	tbl, err := RouteTable("orders", "2024-05")
	if err != nil || tbl != "orders_202405" {
		t.Fatalf("按月分表错误: table=%s err=%v", tbl, err)
	}
	if _, err := RouteTable("orders", "2024-13"); err == nil {
		t.Fatal("非法月份应报错")
	}
	if _, err := RouteTable("orders", "202405"); err == nil {
		t.Fatal("缺连字符应报错")
	}
	if _, err := RouteTable("orders", "'; DROP TABLE x"); err == nil {
		t.Fatal("注入特征应报错")
	}
}

func TestTableOfFixedDataset(t *testing.T) {
	tbl, err := TableOf("incidents")
	if err != nil || tbl != "incidents" {
		t.Fatalf("固定表名错误: %s %v", tbl, err)
	}
}

func TestListDatasets(t *testing.T) {
	t.Setenv("DATASET_ORDERS_DBTYPE", "pg")
	t.Setenv("DATASET_ORDERS_DSN_T1", "order_t1_pg")
	t.Setenv("DATASET_ORDERS_DSN_DEFAULT", "order_pg")
	t.Setenv("DATASET_INCIDENTS_DSN_DEFAULT", "itsm_pg")
	config.Reload()
	t.Cleanup(config.Reload)

	sets := ListDatasets()
	byName := map[string]DatasetInfo{}
	for _, s := range sets {
		byName[s.Dataset] = s
	}
	o, ok := byName["orders"]
	if !ok {
		t.Fatalf("orders 未出现在清单: %+v", sets)
	}
	if o.DBType != "pg" || o.DSNDefault != "order_pg" {
		t.Fatalf("orders 路由概览错误: %+v", o)
	}
	foundT1 := false
	for i, tn := range o.Tenants {
		if tn == "t1" && o.DSNTenants[i] == "order_t1_pg" {
			foundT1 = true
		}
	}
	if !foundT1 {
		t.Fatalf("租户 t1 路由未出现在概览: %+v", o)
	}
	if _, ok := byName["incidents"]; !ok {
		t.Fatalf("incidents 未出现在清单: %+v", sets)
	}
	// 概览绝不能包含连接串本体
	for _, s := range sets {
		b := marshal(t, s)
		if containsStr(b, "://") {
			t.Fatalf("数据集概览疑似泄露连接串: %s", b)
		}
	}
}

func marshal(t *testing.T, v any) string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func contains(s, sub string) bool {
	return len(sub) > 0 && len(s) >= len(sub) && indexOf(s, sub) >= 0
}

func containsStr(s, sub string) bool { return contains(s, sub) }

func indexOf(s, sub string) int {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return i
		}
	}
	return -1
}
