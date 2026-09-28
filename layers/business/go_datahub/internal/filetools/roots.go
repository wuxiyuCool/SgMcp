// roots.go 文件工具的路径围栏（企业部署必配项）。
//
// 为什么必须有：data_hash_file / data_file_stats / data_convert_file 的 path 参数
// 由 AI 生成。没有白名单时，一次提示注入就能让模型读 /etc/passwd、
// ~/.ssh/id_rsa、别的业务的数据库导出文件，或把 dst 指到任意可写路径覆盖文件——
// 这是数据外读 + 任意文件写，属于必须在网关层下面再兜一道的边界。
//
// 规则（与 Python 侧 mcp_shared.limits.safe_path 同语义）：
//   - GO_DATAHUB_FILE_ROOTS：逗号分隔的允许根目录；**未配置时回退到默认安全集**
//     （系统临时目录 + 工作目录 + 可执行文件目录），既不炸老部署也不放开全盘；
//   - 解析符号链接与 .. 之后再判定（防穿越）；
//   - 写操作额外要求目标目录存在且在根目录内，且不允许覆盖配置文件（*.env）。
package filetools

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/sg/mcp/go-datahub/internal/config"
)

// EnvFileRoots 是路径白名单的环境变量/配置文件键。
const EnvFileRoots = "GO_DATAHUB_FILE_ROOTS"

// FileRoots 返回允许访问的根目录（已解析为绝对路径）。
func FileRoots() []string {
	raw := strings.TrimSpace(config.Value(EnvFileRoots))
	var roots []string
	if raw == "" {
		roots = defaultRoots()
	} else {
		for _, item := range strings.Split(raw, ",") {
			if item = strings.TrimSpace(item); item != "" {
				roots = append(roots, abs(item))
			}
		}
	}
	out := make([]string, 0, len(roots))
	for _, r := range roots {
		if p, err := filepath.EvalSymlinks(r); err == nil {
			r = p
		}
		out = append(out, r)
	}
	return out
}

func defaultRoots() []string {
	roots := []string{abs(os.TempDir())} // 临时目录：采集/转换中间文件的常见落点
	if wd, err := os.Getwd(); err == nil {
		roots = append(roots, abs(wd))
	}
	if exe, err := os.Executable(); err == nil {
		roots = append(roots, abs(filepath.Dir(exe)))
	}
	return roots
}

func abs(p string) string {
	if !filepath.IsAbs(p) {
		if wd, err := os.Getwd(); err == nil {
			p = filepath.Join(wd, p)
		}
	}
	return filepath.Clean(p)
}

// CheckPath 校验路径是否在允许清单内。forWrite=true 表示该路径会被创建/覆盖。
// 返回解析后的绝对路径；越界时给可读错误（含当前允许清单，方便运维补配）。
func CheckPath(path string, forWrite bool) (string, error) {
	if strings.TrimSpace(path) == "" {
		return "", fmt.Errorf("path 不能为空")
	}
	resolved := abs(path)
	if p, err := filepath.EvalSymlinks(resolved); err == nil {
		resolved = p // 已存在的路径按真实目标判定，防符号链接逃逸
	}
	roots := FileRoots()
	for _, root := range roots {
		if within(resolved, root) {
			if forWrite {
				if err := checkWritableTarget(resolved); err != nil {
					return "", err
				}
			}
			return resolved, nil
		}
	}
	return "", fmt.Errorf("路径越界：%s 不在允许清单内（%s=%s）。请改用清单内目录，或由运维扩展该配置",
		resolved, EnvFileRoots, strings.Join(roots, ","))
}

func within(p, root string) bool {
	if p == root {
		return true
	}
	rel, err := filepath.Rel(root, p)
	if err != nil {
		return false
	}
	return rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

// checkWritableTarget 拒写敏感目标：配置文件（.env）与已存在的目录。
func checkWritableTarget(p string) error {
	if strings.EqualFold(filepath.Ext(p), ".env") || strings.Contains(strings.ToLower(filepath.Base(p)), ".env") {
		return fmt.Errorf("拒绝写入配置文件（*.env 含 DSN 凭证）：%s", p)
	}
	if fi, err := os.Stat(p); err == nil && fi.IsDir() {
		return fmt.Errorf("目标是目录，不能覆盖：%s", p)
	}
	return nil
}
