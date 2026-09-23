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
	mu    sync.RWMutex
	hands map[string]*Handle
}

func NewRegistry() *Registry {
	return &Registry{hands: map[string]*Handle{}}
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
	r.mu.Unlock()

	go func() {
		h.mu.Lock()
		h.job.Status = StatusRunning
		h.job.StartedAt = time.Now()
		h.mu.Unlock()

		err := run(ctx, h)

		h.mu.Lock()
		defer h.mu.Unlock()
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

func (r *Registry) List(status Status, limit int) []Job {
	r.mu.RLock()
	hs := make([]*Handle, 0, len(r.hands))
	for _, h := range r.hands {
		hs = append(hs, h)
	}
	r.mu.RUnlock()

	out := make([]Job, 0, len(hs))
	for _, h := range hs {
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
