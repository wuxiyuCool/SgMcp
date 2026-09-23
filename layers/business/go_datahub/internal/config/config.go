// Package config 提供 Go 侧独立的敏感配置入口（与 Python 侧 config/platform.env 分离，
// 适配 Go 服务单独部署在其他机器的现实）。
//
// 规则：
//   - 文件查找：环境变量 GO_DATAHUB_CONFIG 显式指定 → 从可执行文件目录与 cwd
//     逐级向上找 config/datahub.env（最多 6 级）。
//   - 优先级：OS 环境变量 > 配置文件（打包后改文件重启即生效）。
//   - 键约定：GO_DATAHUB_TOKEN / GO_DATAHUB_ADDR / GO_DATAHUB_PORT（服务自身参数）、
//     DSN_<名称>（数据库连接串注册表，工具参数用 dsn_ref 引用，密码不经 MCP 消息流转）。
//   - 仓库只保留 config/datahub.env.example 模板，真实文件不进 git。
package config

import (
	"bufio"
	"os"
	"path/filepath"
	"strings"
	"sync"
)

var (
	once  = &sync.Once{}
	fileV map[string]string
)

func load() {
	once.Do(func() {
		fileV = map[string]string{}
		if p := ConfigPath(); p != "" {
			fileV = parseFile(p)
		}
	})
}

// Reload 丢弃缓存重读配置文件（测试与配置热更新场景）。
func Reload() {
	once = &sync.Once{}
	fileV = nil
	load()
}

// Value 读取配置项：OS 环境变量优先，其次配置文件。
func Value(key string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	load()
	return fileV[key]
}

// DSN 按名称取连接串：dsn_ref="order_pg" → 键 DSN_ORDER_PG。
func DSN(ref string) (string, bool) {
	v := Value("DSN_" + strings.ToUpper(strings.TrimSpace(ref)))
	return v, v != ""
}

// DSNRefs 列出已配置的 DSN 名称（不含值，供工具返回给调用方浏览）。
func DSNRefs() []string {
	load()
	out := []string{}
	for k := range fileV {
		if strings.HasPrefix(k, "DSN_") {
			out = append(out, strings.ToLower(strings.TrimPrefix(k, "DSN_")))
		}
	}
	// OS 环境变量里的 DSN_ 也纳入
	for _, kv := range os.Environ() {
		k, _, _ := strings.Cut(kv, "=")
		if strings.HasPrefix(k, "DSN_") {
			name := strings.ToLower(strings.TrimPrefix(k, "DSN_"))
			found := false
			for _, o := range out {
				if o == name {
					found = true
					break
				}
			}
			if !found {
				out = append(out, name)
			}
		}
	}
	return out
}

// ConfigPath 定位配置文件，找不到返回 ""。
func ConfigPath() string {
	if p := os.Getenv("GO_DATAHUB_CONFIG"); p != "" {
		if fi, err := os.Stat(p); err == nil && !fi.IsDir() {
			return p
		}
		return ""
	}
	roots := []string{}
	if exe, err := os.Executable(); err == nil {
		roots = append(roots, filepath.Dir(exe))
	}
	if cwd, err := os.Getwd(); err == nil {
		roots = append(roots, cwd)
	}
	for _, root := range roots {
		dir := root
		for range 6 {
			p := filepath.Join(dir, "config", "datahub.env")
			if fi, err := os.Stat(p); err == nil && !fi.IsDir() {
				return p
			}
			parent := filepath.Dir(dir)
			if parent == dir {
				break
			}
			dir = parent
		}
	}
	return ""
}

func parseFile(path string) map[string]string {
	m := map[string]string{}
	f, err := os.Open(path)
	if err != nil {
		return m
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		k = strings.TrimSpace(k)
		v = strings.Trim(strings.TrimSpace(v), `"'`)
		if k != "" {
			m[k] = v
		}
	}
	return m
}
