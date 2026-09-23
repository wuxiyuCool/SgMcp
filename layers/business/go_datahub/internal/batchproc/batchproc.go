// Package batchproc 批处理核心：对路由后的库表执行一次批量处理作业。
//
// 双模式：
//   - simulate（默认）：演练模式，不触库，按节奏推进进度——任意环境可跑通
//     「业务名路由 → 异步任务 → task_id → 轮询」全流程（含测试与演示）；
//   - real：真实执行——连路由库，统计目标表行数作为处理规模（生产在此接入
//     真实的批处理逻辑：ETL / 汇总 / 归档等）。
//
// 调用方（heavy.batch_process 工具 / /internal/v1/batch/process 端点）负责
// 上游的 dsrouting 路由，本包只面对「已解析的库表」。
package batchproc

import (
	"context"
	"fmt"
	"time"

	"github.com/sg/mcp/go-datahub/internal/dbhub"
)

// Request 一次批处理作业的入参。real 模式必须能解析出 DSN；simulate 不触库可留空。
type Request struct {
	DBType string // pg/mysql/mssql/oracle
	DSN    string // real 模式必填（由 dsn_ref 解析而来）
	Table  string // 目标表（已由 routeTable/TableOf 解析）
	Mode   string // simulate | real（空 = simulate）
	Rows   int    // simulate 模式的模拟行数（默认 1000）
}

// Progress 进度上报回调（异步作业据此累计 rows 字段）。
type Progress func(processed int)

// Run 同步执行一次批处理，返回 (处理行数, 过程描述, 错误)。
// 错误一律经 dbhub.Scrub 脱敏（real 模式），连接串不进任务消息。
func Run(ctx context.Context, req Request, progress Progress) (int, string, error) {
	// 两种模式都先做参数级校验（表名白名单），坏参数不该等到触库才暴露
	if err := dbhub.ValidateTable(req.Table); err != nil {
		return 0, "", err
	}
	mode := req.Mode
	if mode == "" {
		mode = "simulate"
	}
	switch mode {
	case "simulate":
		return runSimulate(ctx, req, progress)
	case "real":
		return runReal(ctx, req, progress)
	default:
		return 0, "", fmt.Errorf("mode 只支持 simulate/real: %q", mode)
	}
}

func runSimulate(ctx context.Context, req Request, progress Progress) (int, string, error) {
	total := req.Rows
	if total <= 0 {
		total = 1000
	}
	processed := 0
	batch := 100
	for processed < total {
		select {
		case <-ctx.Done(): // 任务被取消
			return processed, "", ctx.Err()
		case <-time.After(20 * time.Millisecond):
		}
		n := batch
		if processed+n > total {
			n = total - processed
		}
		processed += n
		if progress != nil {
			progress(processed)
		}
	}
	detail := fmt.Sprintf("simulate 模式：模拟处理 %d 行（不触库）", total)
	return processed, detail, nil
}

func runReal(ctx context.Context, req Request, progress Progress) (int, string, error) {
	if req.DSN == "" {
		return 0, "", fmt.Errorf("real 模式需要可解析的 DSN（检查数据集路由的 dsn_ref 是否已在 DSN_<名称> 登记）")
	}
	db, err := dbhub.Open(req.DBType, req.DSN)
	if err != nil {
		return 0, "", dbhub.Scrub(err, req.DSN)
	}
	defer db.Close()

	ctx, cancel := context.WithTimeout(ctx, 60*time.Second)
	defer cancel()
	if err := db.PingContext(ctx); err != nil {
		return 0, "", dbhub.Scrub(fmt.Errorf("路由库不可达: %w", err), req.DSN)
	}

	// 处理规模 = 目标表行数（生产替换为真实批处理逻辑；表不存在/无权限 → 可读错误）
	n, err := dbhub.CountTable(ctx, req.DBType, req.DSN, req.Table)
	if err != nil {
		return 0, "", err
	}
	if progress != nil {
		progress(n)
	}
	detail := fmt.Sprintf("real 模式：目标表 %s 共 %d 行（连接 %s 路由库成功）", req.Table, n, req.DBType)
	return n, detail, nil
}
