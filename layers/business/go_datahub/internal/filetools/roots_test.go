package filetools

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestCheckPathAllowsRoot(t *testing.T) {
	root := t.TempDir()
	t.Setenv(EnvFileRoots, root)
	file := filepath.Join(root, "data.csv")
	if err := os.WriteFile(file, []byte("a,b\n1,2\n"), 0o600); err != nil {
		t.Fatal(err)
	}

	got, err := CheckPath(file, false)
	if err != nil {
		t.Fatalf("根目录内文件应放行: %v", err)
	}
	if got != file && filepath.Base(got) != "data.csv" {
		t.Fatalf("返回应为解析后的绝对路径, got %s", got)
	}
}

func TestCheckPathRejectsOutside(t *testing.T) {
	root := t.TempDir()
	outside := filepath.Join(filepath.Dir(root), "secret.env")
	t.Setenv(EnvFileRoots, root)

	if _, err := CheckPath(outside, false); err == nil {
		t.Fatal("白名单外路径竟然放行")
	} else if !strings.Contains(err.Error(), EnvFileRoots) {
		t.Fatalf("错误应提示如何放开白名单, got %v", err)
	}
}

func TestCheckPathBlocksTraversal(t *testing.T) {
	root := t.TempDir()
	t.Setenv(EnvFileRoots, root)
	if _, err := CheckPath(filepath.Join(root, "..", "..", "etc", "passwd"), false); err == nil {
		t.Fatal("../ 穿越竟然放行")
	}
}

func TestCheckPathRefusesEnvFileWrite(t *testing.T) {
	root := t.TempDir()
	t.Setenv(EnvFileRoots, root)
	if _, err := CheckPath(filepath.Join(root, "config.env"), true); err == nil {
		t.Fatal("写 *.env（含 DSN 凭证）必须被拒")
	}
}

func TestFileRootsDefaultsWhenUnset(t *testing.T) {
	t.Setenv(EnvFileRoots, "")
	roots := FileRoots()
	if len(roots) == 0 {
		t.Fatal("未配置时应回退到默认安全目录（临时目录/工作目录/exe 目录）")
	}
	for _, r := range roots {
		if !filepath.IsAbs(r) {
			t.Fatalf("根目录必须是绝对路径: %s", r)
		}
	}
}
