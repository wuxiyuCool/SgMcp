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
	"sort"
	"strings"
	"sync"

	"github.com/sg/mcp/go-datahub/internal/dbhub"
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

// DSNRef 一个已登记数据源（名称 + 由连接串推断的类型）。
type DSNRef struct {
	Name string `json:"name"`
	Type string `json:"type"`
}

// DSNRefs 只回名称列表（dsrouting/internalapi 等调用点用；带类型清单用 DSNRefList）。
func DSNRefs() []string {
	list := DSNRefList()
	out := make([]string, len(list))
	for i, r := range list {
		out[i] = r.Name
	}
	return out
}

// DSNRefList 列出所有已配置数据源（文件 + OS 环境变量，只回名称与类型不回值）。
func DSNRefList() []DSNRef {
	load()
	seen := map[string]string{}
	collect := func(key, val string) {
		if !strings.HasPrefix(key, "DSN_") {
			return
		}
		name := strings.ToLower(strings.TrimPrefix(key, "DSN_"))
		if _, dup := seen[name]; !dup {
			seen[name] = dbhub.InferKind(val)
		}
	}
	for k, v := range fileV {
		collect(k, v)
	}
	for _, kv := range os.Environ() {
		k, v, _ := strings.Cut(kv, "=")
		collect(k, v)
	}
	out := make([]DSNRef, 0, len(seen))
	for name, typ := range seen {
		out = append(out, DSNRef{Name: name, Type: typ})
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Name < out[j].Name })
	return out
}

// Keys 列出匹配前缀的配置键（文件 + OS 环境变量，供数据集路由等注册表枚举用）。
func Keys(prefix string) []string {
	load()
	seen := map[string]bool{}
	out := []string{}
	for k := range fileV {
		if strings.HasPrefix(k, prefix) && !seen[k] {
			seen[k] = true
			out = append(out, k)
		}
	}
	for _, kv := range os.Environ() {
		k, _, _ := strings.Cut(kv, "=")
		if strings.HasPrefix(k, prefix) && !seen[k] {
			seen[k] = true
			out = append(out, k)
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
