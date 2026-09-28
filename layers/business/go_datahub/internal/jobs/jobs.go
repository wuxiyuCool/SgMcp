// Package jobs 提供采集任务的内存注册表与状态机。
//
// 中层重活（大批量采集）不能在一次 MCP 工具调用里同步完成：
// submit_collect_job 立即返回 job_id，后台 goroutine 执行，
// 客户端通过 get_job_status 轮询进度。
package jobs

import (
	"context"
	crand "crypto/rand"
	"encoding/hex"
	"errors"
	"os"
	"strconv"
	"sync"
	"time"
)

type Status string

const (
	StatusPending   Status = "pending"
	StatusRunning   Status = "running"
	StatusCompleted Status = "completed"
	StatusFailed    Status = "failed"
	StatusCancelled Status = "cancelled"
)

// Job 是任务的可导出快照，同时用作工具输出的数据结构。
type Job struct {
	ID         string    `json:"id"`
	Source     string    `json:"source"`
	Status     Status    `json:"status"`
	Rows       int       `json:"rows"`
	Message    string    `json:"message,omitempty"`
	Error      string    `json:"error,omitempty"`
	StartedAt  time.Time `json:"started_at,omitzero"`
	FinishedAt time.Time `json:"finished_at,omitzero"`
}

// Handle 是运行中任务的可变控制句柄，采集器通过它上报进度。
type Handle struct {
	mu     sync.Mutex
	id     string
	job    Job
	cancel context.CancelFunc
}

func (h *Handle) ID() string { return h.id }

// AddRows 由采集器在每产出一批行后调用，用于进度上报。
func (h *Handle) AddRows(n int) {
	h.mu.Lock()
	h.job.Rows += n
	h.mu.Unlock()
}

func (h *Handle) SetMessage(msg string) {
	h.mu.Lock()
	h.job.Message = msg
	h.mu.Unlock()
}

func (h *Handle) snapshot() Job {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.job
}

func newID() string {
	b := make([]byte, 8)
	_, _ = crand.Read(b)
	return "job-" + hex.EncodeToString(b)
}

var ErrNotFound = errors.New("任务不存在")
var ErrNotRunning = errors.New("任务已结束，无法取消")

type Registry struct {
	mu      sync.RWMutex
	hands   map[string]*Handle
	order   []string // 按启动顺序记录，用于保留窗口裁剪
	maxKeep int      // 已结束任务最多保留多少条（含运行中）
}

func NewRegistry() *Registry {
	return NewRegistryWithLimit(envInt("GO_DATAHUB_JOB_HISTORY", 500))
}

// NewRegistryWithLimit 指定历史保留条数（<=0 表示不裁剪）。
func NewRegistryWithLimit(maxKeep int) *Registry {
	return &Registry{hands: map[string]*Handle{}, maxKeep: maxKeep}
}

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			return n
		}
	}
	return def
}

// defaultRegistry 供进程级 AbortAll 使用（各 server 自建 Registry 时也可调 trim/List 等方法）。
var defaultRegistry = NewRegistry()

// abortAllLocked 取消所有 pending/running 任务（调用方持写锁）。
func (r *Registry) abortAllLocked() {
	for _, h := range r.hands {
		st := h.snapshot().Status
		if st != StatusPending && st != StatusRunning {
			continue
		}
		h.mu.Lock()
		if h.cancel != nil {
			h.cancel()
		}
		h.job.Status = StatusCancelled
		h.job.FinishedAt = time.Now()
		h.job.Message = "服务停机，任务已中止"
		h.mu.Unlock()
	}
}

// Start 注册任务并在后台执行 run。run 收到可取消的 context，
// 结束（正常/出错/被取消）后状态自动流转为 completed/failed/cancelled。
func (r *Registry) Start(source string, run func(ctx context.Context, h *Handle) error) *Handle {
	h := &Handle{id: newID(), job: Job{Source: source, Status: StatusPending}}
	h.job.ID = h.id
	ctx, cancel := context.WithCancel(context.Background())
	h.cancel = cancel

	r.mu.Lock()
	r.hands[h.id] = h
	r.order = append(r.order, h.id)
	r.trimLocked()
	r.mu.Unlock()

	go func() {
		h.mu.Lock()
		h.job.Status = StatusRunning
		h.job.StartedAt = time.Now()
		h.mu.Unlock()

		err := run(ctx, h)

		h.mu.Lock()
		h.job.FinishedAt = time.Now()
		switch {
		case errors.Is(ctx.Err(), context.Canceled):
			h.job.Status = StatusCancelled
		case err != nil:
			h.job.Status = StatusFailed
			h.job.Error = err.Error()
		default:
			h.job.Status = StatusCompleted
		}
		h.cancel = nil
		h.mu.Unlock()

		// 任务落定后再裁一次：只在 Start 时裁剪的话，最后几个任务会永远留在表里
		// （它们之后没有新的 Start 触发清理）。锁序保持 r.mu → h.mu，勿在此持 h.mu 调用。
		r.mu.Lock()
		r.trimLocked()
		r.mu.Unlock()
	}()
	return h
}

func (r *Registry) Get(id string) (*Job, bool) {
	r.mu.RLock()
	h := r.hands[id]
	r.mu.RUnlock()
	if h == nil {
		return nil, false
	}
	snap := h.snapshot()
	return &snap, true
}

func (r *Registry) Cancel(id string) error {
	r.mu.RLock()
	h := r.hands[id]
	r.mu.RUnlock()
	if h == nil {
		return ErrNotFound
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.job.Status != StatusPending && h.job.Status != StatusRunning {
		return ErrNotRunning
	}
	if h.cancel != nil {
		h.cancel()
	}
	return nil
}

// trimLocked 按启动顺序裁剪已结束的历史任务，保留最多 maxKeep 条（运行中的永不裁）。
// 不裁剪的话，长时间运行的服务里 job 记录只增不减，最终成为内存泄漏点。
func (r *Registry) trimLocked() {
	if r.maxKeep <= 0 {
		return
	}
	drop := map[string]bool{}
	kept := 0
	for i := len(r.order) - 1; i >= 0; i-- { // 从最新往回看
		id := r.order[i]
		h := r.hands[id]
		if h == nil {
			continue
		}
		if st := h.snapshot().Status; st == StatusPending || st == StatusRunning {
			continue // 在途任务不受保留窗口影响
		}
		kept++
		if kept > r.maxKeep {
			drop[id] = true
		}
	}
	if len(drop) == 0 {
		return
	}
	next := make([]string, 0, len(r.order))
	for _, id := range r.order {
		if drop[id] {
			delete(r.hands, id)
			continue
		}
		next = append(next, id)
	}
	r.order = next
}

// Stats 任务概览（探活/巡检用，不含任务明细与任何入参）。
func (r *Registry) Stats() map[string]any {
	r.mu.RLock()
	defer r.mu.RUnlock()
	byStatus := map[string]int{}
	for _, h := range r.hands {
		byStatus[string(h.snapshot().Status)]++
	}
	return map[string]any{"jobs_total": len(r.hands), "jobs_by_status": byStatus,
		"job_history_limit": r.maxKeep}
}

func (r *Registry) List(status Status, limit int) []Job {
	r.mu.RLock()
	hs := make([]*Handle, 0, len(r.order))
	for _, id := range r.order { // 启动顺序 -> 后面反转成「最新在前」
		if h := r.hands[id]; h != nil {
			hs = append(hs, h)
		}
	}
	r.mu.RUnlock()

	out := make([]Job, 0, len(hs))
	for i := len(hs) - 1; i >= 0; i-- {
		h := hs[i]
		snap := h.snapshot()
		if status != "" && snap.Status != status {
			continue
		}
		out = append(out, snap)
		if limit > 0 && len(out) >= limit {
			break
		}
	}
	return out
}

// AbortAll 停机时把所有在途任务标记为 cancelled（否则进程退了任务还"在跑"，
// 客户端轮询会拿到已完成/找不到的错乱状态）。
func AbortAll() {
	defaultRegistry.mu.Lock()
	defer defaultRegistry.mu.Unlock()
	defaultRegistry.abortAllLocked()
}
