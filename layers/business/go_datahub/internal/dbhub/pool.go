// pool.go 连接池复用：按 (kind, dsn) 缓存 *sql.DB，避免每次工具调用重新握手。
//
// 为什么需要：原来每个库表操作都 Open → 用完 Close，等于每次调用重做一次
// TCP + 认证（Oracle/PG 还要 TLS）。AI 一次提问常触发 list_tables → query →
// import 三连，跨机房时这几百毫秒到几秒的纯握手开销会直接变成"网关很慢"；
// 高频场景下还可能在数据库侧留下大量 TIME_WAIT。
//
// 规则：
//   - 键为 kind+dsn（dsn 只作为 map key，不写日志不回显）；
//   - 池上限 GO_DATAHUB_POOL_MAX（默认 8）：超出按最久空闲的先关（LRU 驱逐）；
//   - 空闲超过 GO_DATAHUB_POOL_IDLE_SECONDS（默认 600）由后台清理协程关闭；
//   - Acquire 返回 release 回调：调用方**不要** Close(db)，只 release。
package dbhub

import (
	"database/sql"
	"log"
	"os"
	"sort"
	"strconv"
	"sync"
	"time"
)

type poolKey struct{ kind, dsn string }

type poolEntry struct {
	db     *sql.DB
	last   time.Time
	closed bool
}

var (
	poolMu      sync.Mutex
	poolMap     = map[poolKey]*poolEntry{}
	poolOnce    sync.Once
	poolMax     = envInt("GO_DATAHUB_POOL_MAX", 8)
	poolIdleTTL = time.Duration(envInt("GO_DATAHUB_POOL_IDLE_SECONDS", 600)) * time.Second
)

func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

// Acquire 取（必要时创建）一个共享连接池；release 归还引用（不关闭连接）。
func Acquire(kind, dsn string) (db *sql.DB, release func(), err error) {
	key := poolKey{kind, dsn}
	poolOnce.Do(startPoolSweeper)

	poolMu.Lock()
	e, ok := poolMap[key]
	if !ok || e.closed {
		if !ok {
			evictLocked() // 到达上限：先驱逐最久空闲的池
		}
		opened, oerr := Open(kind, dsn)
		if oerr != nil {
			poolMu.Unlock()
			return nil, nil, oerr
		}
		e = &poolEntry{db: opened}
		poolMap[key] = e
	}
	e.last = time.Now()
	poolMu.Unlock()

	ref := true
	var refMu sync.Mutex
	return e.db, func() {
		refMu.Lock()
		defer refMu.Unlock()
		if !ref {
			return
		}
		ref = false
		poolMu.Lock()
		if cur, ok := poolMap[key]; ok && !cur.closed {
			cur.last = time.Now()
		}
		poolMu.Unlock()
	}, nil
}

// evictLocked 在池数达上限时关掉最久未用的一批（调用方持锁）。
func evictLocked() {
	if len(poolMap) < poolMax {
		return
	}
	list := make([]struct {
		k poolKey
		e *poolEntry
	}, 0, len(poolMap))
	for k, e := range poolMap {
		list = append(list, struct {
			k poolKey
			e *poolEntry
		}{k, e})
	}
	sort.Slice(list, func(i, j int) bool { return list[i].e.last.Before(list[j].e.last) })
	if len(list) > 0 {
		k := list[0].k
		e := list[0].e
		delete(poolMap, k)
		e.closed = true
		_ = e.db.Close()
		log.Printf("[dbhub] 连接池达上限 %d，驱逐最久空闲的 %s 池", poolMax, k.kind)
	}
}

// startPoolSweeper 后台清理空闲超时的池（每分钟一轮）。
func startPoolSweeper() {
	go func() {
		for range time.Tick(time.Minute) {
			now := time.Now()
			var stale []*poolEntry
			poolMu.Lock()
			for k, e := range poolMap {
				if !e.closed && now.Sub(e.last) > poolIdleTTL {
					delete(poolMap, k)
					e.closed = true
					stale = append(stale, e)
				}
			}
			poolMu.Unlock()
			for _, e := range stale {
				_ = e.db.Close()
			}
		}
	}()
}

// CloseAllPools 优雅停机时释放所有池（幂等）。
func CloseAllPools() {
	poolMu.Lock()
	entries := make([]*poolEntry, 0, len(poolMap))
	for k, e := range poolMap {
		delete(poolMap, k)
		if !e.closed {
			e.closed = true
			entries = append(entries, e)
		}
	}
	poolMu.Unlock()
	for _, e := range entries {
		_ = e.db.Close()
	}
}

// PoolStats 连接池概览（/internal/healthz 与运维自检用；不含任何 dsn）。
func PoolStats() map[string]any {
	poolMu.Lock()
	defer poolMu.Unlock()
	byKind := map[string]int{}
	for k := range poolMap {
		byKind[k.kind]++
	}
	return map[string]any{"pools": len(poolMap), "by_kind": byKind, "max": poolMax,
		"idle_ttl_s": int(poolIdleTTL.Seconds())}
}
