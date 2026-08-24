// 實作 algs/integrity-auth.md §E.2：alert fingerprint 冪等鍵。
//
// - 冪等鍵 = (fingerprint, status)
// - 同鍵重送 → 直接回上次處理結果，不新建 Incident、不重跑管線
// - spec.md §5 標準 13：同 fingerprint 重送 3 次僅產生 1 個 Incident
//
// v1 以 process 內 TTL map 實作；gate 為單實例 sidecar，重啟後遺失
// 冪等狀態的後果僅是重跑一次分診（core 端仍有 incident 聚合兜底）。
package ingest

import (
	"sync"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// defaultTTL 冪等紀錄保留時間。需 > AM retry/backoff 窗；
// firing→resolved 的正常生命週期也在此窗口內各記一筆。
const defaultTTL = 24 * time.Hour

// maxEntries 懶清理上限：超過時先清過期，仍超量則拒絕寫入（保守不覆蓋歷史）。
const maxEntries = 100_000

// Entry 是冪等命中時回放的上次處理結果。
type Entry struct {
	Response *oncallv1.ReportIncidentResponse
	expires  time.Time
}

// Store 是 (fingerprint, status) → 上次結果 的併發安全儲存。
type Store struct {
	mu   sync.RWMutex
	m    map[string]Entry
	ttl  time.Duration
	now  func() time.Time // 測試注入用
	full bool             // 達到上限標記（測試斷言用）
}

// NewStore 建立冪等儲存。
func NewStore() *Store {
	return &Store{
		m:   make(map[string]Entry),
		ttl: defaultTTL,
		now: time.Now,
	}
}

// IdempotencyKey 組出冪等鍵：fingerprint + AlertStatus（E.2）。
func IdempotencyKey(fingerprint string, status oncallv1.AlertStatus) string {
	return fingerprint + "\x00" + status.String()
}

// Get 回傳鍵對應的上次結果；不存在或已過期回 nil。
func (s *Store) Get(key string) *oncallv1.ReportIncidentResponse {
	s.mu.RLock()
	e, ok := s.m[key]
	s.mu.RUnlock()
	if !ok || s.now().After(e.expires) {
		return nil
	}
	return e.Response
}

// Put 記錄處理結果。只在鍵不存在時寫入——併發同鍵以先到者為準。
func (s *Store) Put(key string, resp *oncallv1.ReportIncidentResponse) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if _, exists := s.m[key]; exists {
		return
	}
	if len(s.m) >= maxEntries {
		s.evictLocked()
	}
	if len(s.m) >= maxEntries {
		s.full = true
		return
	}
	s.m[key] = Entry{Response: resp, expires: s.now().Add(s.ttl)}
}

// evictLocked 清除所有過期條目（呼叫端須持寫鎖）。
func (s *Store) evictLocked() {
	now := s.now()
	for k, e := range s.m {
		if now.After(e.expires) {
			delete(s.m, k)
		}
	}
}

// Len 回傳目前條目數（測試/觀測用）。
func (s *Store) Len() int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return len(s.m)
}
