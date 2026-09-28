package collector

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/sg/mcp/go-datahub/internal/jobs"
)

// TestRegistryTrimsFinished 已结束任务按保留上限裁剪，运行中任务不受影响
// （不裁剪时长跑进程会一直攒 job 记录）。
func TestRegistryTrimsFinished(t *testing.T) {
	reg := jobs.NewRegistryWithLimit(3)
	release := make(chan struct{})
	running := reg.Start("synthetic", func(ctx context.Context, h *jobs.Handle) error {
		<-release
		return ctx.Err()
	})
	for i := 0; i < 8; i++ {
		reg.Start("synthetic", func(ctx context.Context, h *jobs.Handle) error { return nil })
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if _, ok := reg.Get(running.ID()); !ok {
			t.Fatalf("运行中的任务被裁掉了")
		}
		list := reg.List("", 0)
		if len(list) <= 5 {
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if got := len(reg.List("", 0)); got > 6 {
		t.Fatalf("历史任务未被裁剪，仍有 %d 条", got)
	}
	if _, ok := reg.Get(running.ID()); !ok {
		t.Fatal("运行中的任务必须保留")
	}
	close(release)
	time.Sleep(100 * time.Millisecond)
}

// TestListNewestFirst data_list_jobs 的 limit 必须给出「最近 N 条」，
// 而不是 map 随机序里捞到的任意 N 条（否则 AI 找回不到自己的 job）。
func TestListNewestFirst(t *testing.T) {
	reg := jobs.NewRegistryWithLimit(0)
	var ids []string
	for i := 0; i < 5; i++ {
		ids = append(ids, reg.Start("synthetic", func(ctx context.Context, h *jobs.Handle) error { return nil }).ID())
	}
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) && len(reg.List(jobs.StatusCompleted, 0)) < 5 {
		time.Sleep(20 * time.Millisecond)
	}
	list := reg.List("", 2)
	if len(list) != 2 {
		t.Fatalf("limit 未生效: %d", len(list))
	}
	if list[0].ID != ids[4] || list[1].ID != ids[3] {
		t.Fatalf("应按最新在前返回, got %s %s want %s %s", list[0].ID, list[1].ID, ids[4], ids[3])
	}
}

func TestGuardSelectAllowsReadOnly(t *testing.T) {
	for _, q := range []string{
		"SELECT * FROM t WHERE id = 1",
		"select a, update_time, delete_flag from t limit 10", // 列名含关键字不误伤
		"WITH s AS (SELECT 1 AS x) SELECT * FROM s",
		"SELECT count(*) FROM orders /* 注释 */ GROUP BY cid;",
	} {
		if err := GuardSelect(q); err != nil {
			t.Fatalf("只读查询被误拒 %q: %v", q, err)
		}
	}
}

func TestGuardSelectBlocksDangerous(t *testing.T) {
	for _, q := range []string{
		"",
		"DELETE FROM t",
		"SELECT 1; DROP TABLE t",
		"SELECT * INTO OUTFILE '/tmp/x' FROM t",
		"SELECT pg_sleep(60)",
		"SELECT user, host FROM mysql.user",
		"UPDATE t SET a=1",
		"SELECT * FROM t -- x\n; INSERT INTO t VALUES(1)",
		"EXPLAIN DELETE FROM t",
	} {
		err := GuardSelect(q)
		if err == nil {
			t.Fatalf("危险写法竟然通过: %q", q)
		}
		if strings.Contains(err.Error(), "panic") {
			t.Fatalf("错误信息异常: %v", err)
		}
	}
}
