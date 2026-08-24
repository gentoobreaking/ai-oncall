// Package metrics 提供 gate 自我可觀測的最小計數器（F12 的種子）。
// 以 text/plain 暴露於 /metrics；未來可換成 Prometheus client。
package metrics

import (
	"fmt"
	"net/http"
	"sync"
)

// Metrics 是 gate 內部計數器集合。所有方法併發安全。
type Metrics struct {
	mu sync.Mutex

	received      int64 // 收到的 /alerts 請求（通過驗證前即計）
	unauthorized  int64 // 驗證失敗次數（攻擊偵測訊號，E.1）
	rateLimited   int64 // 被限流拒絕次數
	tooLarge      int64 // payload 超限次數
	normalized    int64 // 成功正規化的警報數
	deduplicated  int64 // 冪等命中次數（E.2）
	forwarded     int64 // 轉發給 core 的次數（新建或首次處理）
	upstreamError int64 // core 呼叫失敗次數
}

// Inc 系列方法：語意化計數。
func (m *Metrics) IncReceived() { m.add(&m.received) }
func (m *Metrics) IncUnauthorized() {
	m.add(&m.unauthorized)
}
func (m *Metrics) IncRateLimited()       { m.add(&m.rateLimited) }
func (m *Metrics) IncTooLarge()          { m.add(&m.tooLarge) }
func (m *Metrics) IncNormalized(n int64) { m.addN(&m.normalized, n) }
func (m *Metrics) IncDeduplicated()      { m.add(&m.deduplicated) }
func (m *Metrics) IncForwarded()         { m.add(&m.forwarded) }
func (m *Metrics) IncUpstreamError()     { m.add(&m.upstreamError) }

func (m *Metrics) add(p *int64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	*p++
}

func (m *Metrics) addN(p *int64, n int64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	*p += n
}

// New 建立計數器集合。
func New() *Metrics { return &Metrics{} }

// Snapshot 回傳目前所有計數值。
type Snapshot struct {
	Received      int64 `json:"received"`
	Unauthorized  int64 `json:"unauthorized"`
	RateLimited   int64 `json:"rate_limited"`
	TooLarge      int64 `json:"too_large"`
	Normalized    int64 `json:"normalized"`
	Deduplicated  int64 `json:"deduplicated"`
	Forwarded     int64 `json:"forwarded"`
	UpstreamError int64 `json:"upstream_error"`
}

func (m *Metrics) Snapshot() Snapshot {
	m.mu.Lock()
	defer m.mu.Unlock()
	return Snapshot{
		Received:      m.received,
		Unauthorized:  m.unauthorized,
		RateLimited:   m.rateLimited,
		TooLarge:      m.tooLarge,
		Normalized:    m.normalized,
		Deduplicated:  m.deduplicated,
		Forwarded:     m.forwarded,
		UpstreamError: m.upstreamError,
	}
}

// Handler 回傳 /metrics 的 text/plain 端點。
func (m *Metrics) Handler() http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		s := m.Snapshot()
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		fmt.Fprintf(w, "# TYPE oncall_gate_requests_received counter\noncall_gate_requests_received %d\n", s.Received)
		fmt.Fprintf(w, "# TYPE oncall_gate_auth_failures counter\noncall_gate_auth_failures %d\n", s.Unauthorized)
		fmt.Fprintf(w, "# TYPE oncall_gate_rate_limited counter\noncall_gate_rate_limited %d\n", s.RateLimited)
		fmt.Fprintf(w, "# TYPE oncall_gate_payload_too_large counter\noncall_gate_payload_too_large %d\n", s.TooLarge)
		fmt.Fprintf(w, "# TYPE oncall_gate_alerts_normalized counter\noncall_gate_alerts_normalized %d\n", s.Normalized)
		fmt.Fprintf(w, "# TYPE oncall_gate_deduplicated counter\noncall_gate_deduplicated %d\n", s.Deduplicated)
		fmt.Fprintf(w, "# TYPE oncall_gate_forwarded counter\noncall_gate_forwarded %d\n", s.Forwarded)
		fmt.Fprintf(w, "# TYPE oncall_gate_upstream_errors counter\noncall_gate_upstream_errors %d\n", s.UpstreamError)
	})
}
