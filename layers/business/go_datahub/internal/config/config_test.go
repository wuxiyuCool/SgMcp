package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestFileAndEnvPrecedence(t *testing.T) {
	dir := t.TempDir()
	cfg := filepath.Join(dir, "datahub.env")
	content := "# comment\nexport GO_DATAHUB_TOKEN=file-token\nDSN_ORDER_PG=postgres://u:p@h/db\nDSN_WMS_MSSQL=\"sqlserver://u:p@h:1433\"\nBAD_LINE_NO_EQUALS\n"
	if err := os.WriteFile(cfg, []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("GO_DATAHUB_CONFIG", cfg)
	Reload()

	if got := Value("GO_DATAHUB_TOKEN"); got != "file-token" {
		t.Errorf("文件值未生效: %q", got)
	}
	// OS 环境变量优先于文件
	t.Setenv("GO_DATAHUB_TOKEN", "env-token")
	if got := Value("GO_DATAHUB_TOKEN"); got != "env-token" {
		t.Errorf("环境变量应优先: %q", got)
	}

	if dsn, ok := DSN("order_pg"); !ok || dsn != "postgres://u:p@h/db" {
		t.Errorf("DSN(order_pg) = %q, %v", dsn, ok)
	}
	if dsn, ok := DSN("wms_mssql"); !ok || dsn != "sqlserver://u:p@h:1433" {
		t.Errorf("DSN(wms_mssql) 引号剥离失败: %q, %v", dsn, ok)
	}
	if _, ok := DSN("nope"); ok {
		t.Error("未知 ref 不应命中")
	}

	refs := DSNRefs()
	found := map[string]bool{}
	for _, r := range refs {
		found[r] = true
	}
	if !found["order_pg"] || !found["wms_mssql"] {
		t.Errorf("DSNRefs 不全: %v", refs)
	}
}

func TestMissingFileIsNoop(t *testing.T) {
	t.Setenv("GO_DATAHUB_CONFIG", filepath.Join(t.TempDir(), "absent.env"))
	Reload()
	if Value("GO_DATAHUB_TOKEN") != "" {
		t.Error("不存在的配置不应产生值")
	}
}
