// /alerts webhook 端點的完整管線：
//
//	payload 大小上限 → rate limiting（防灌爆）→ shared secret 驗證（E.1）
//	→ JSON 解析與正規化（F1）→ 冪等檢查（E.2）→ 轉發 core
//
// 401 之後才允許解析/正規化/core 轉發等下游資源消耗。
package ingest

import (
	"context"
	"encoding/json"
	"errors"
	"net"
	"net/http"
	"sync"
	"time"

	"golang.org/x/time/rate"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
	"github.com/david/ai-oncall/gate/internal/metrics"
)

// MaxBodyBytes 單一 webhook payload 上限（1MB；AM payload 通常 <100KB）。
const MaxBodyBytes = 1 << 20

// 每來源 IP 的速率限制。
const (
	ratePerIP   = rate.Limit(5) // 每秒 5 請求
	burstPerIP  = 10            // 瞬間爆量容許 10
	ipCacheSize = 65_536        // 追蹤的 IP 數上限，超過即重置防記憶體無界
)

// CoreReporter 是 core gRPC client 的最小介面（測試以 fake 實作）。
type CoreReporter interface {
	ReportIncident(ctx context.Context, in *oncallv1.ReportIncidentRequest) (*oncallv1.ReportIncidentResponse, error)
}

// Handler 是 /alerts 的 http.Handler。
type Handler struct {
	secret string
	core   CoreReporter
	store  *Store
	m      *metrics.Metrics
	now    func() time.Time

	mu       sync.Mutex
	limiters map[string]*rate.Limiter
}

// NewHandler 建立 webhook handler。
func NewHandler(sharedSecret string, core CoreReporter, store *Store, m *metrics.Metrics) *Handler {
	return &Handler{
		secret:   sharedSecret,
		core:     core,
		store:    store,
		m:        m,
		now:      time.Now,
		limiters: make(map[string]*rate.Limiter),
	}
}

func (h *Handler) allow(ip string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	if len(h.limiters) >= ipCacheSize {
		h.limiters = make(map[string]*rate.Limiter)
	}
	l, ok := h.limiters[ip]
	if !ok {
		l = rate.NewLimiter(ratePerIP, burstPerIP)
		h.limiters[ip] = l
	}
	return l.Allow()
}

func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	h.m.IncReceived()

	if r.Method != http.MethodPost {
		http.Error(w, `{"error":"method not allowed"}`, http.StatusMethodNotAllowed)
		return
	}

	// 大小上限：讀取側硬限制
	r.Body = http.MaxBytesReader(w, r.Body, MaxBodyBytes)

	// rate limiting（防灌爆）
	if !h.allow(clientIP(r)) {
		h.m.IncRateLimited()
		writeJSONError(w, `{"error":"rate limited"}`, http.StatusTooManyRequests)
		return
	}

	// E.1 shared secret 驗證——401 之後才允許下游資源消耗
	if !verifyAuth(r, h.secret) {
		writeUnauthorized(w, h.m)
		return
	}

	var raw json.RawMessage
	if err := json.NewDecoder(r.Body).Decode(&raw); err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) || len(raw) > MaxBodyBytes {
			h.m.IncTooLarge()
			writeJSONError(w, `{"error":"payload too large"}`, http.StatusRequestEntityTooLarge)
			return
		}
		writeJSONError(w, `{"error":"invalid json"}`, http.StatusBadRequest)
		return
	}
	if int64(len(raw)) > MaxBodyBytes {
		h.m.IncTooLarge()
		writeJSONError(w, `{"error":"payload too large"}`, http.StatusRequestEntityTooLarge)
		return
	}

	events, err := Normalize(raw, h.now())
	if err != nil {
		writeJSONError(w, `{"error":"unnormalizable payload"}`, http.StatusBadRequest)
		return
	}

	results := make([]jsonResult, 0, len(events))
	for _, ev := range events {
		key := IdempotencyKey(ev.Fingerprint, ev.Status)

		// E.2 冪等：同鍵回上次結果，不新建 Incident 不重跑管線
		if prev := h.store.Get(key); prev != nil {
			h.m.IncDeduplicated()
			results = append(results, jsonResult{
				Accepted:     prev.Accepted,
				Deduplicated: true,
				IncidentId:   prev.IncidentId,
				Message:      prev.Message,
			})
			continue
		}

		resp, err := h.core.ReportIncident(r.Context(), &oncallv1.ReportIncidentRequest{Event: ev})
		if err != nil {
			h.m.IncUpstreamError()
			writeJSONError(w, `{"error":"core unavailable"}`, http.StatusBadGateway)
			return
		}
		// 只快取成功受理的結果；失敗不寫入，讓 AM 重試可再進
		if resp.Accepted {
			h.store.Put(key, resp)
			h.m.IncForwarded()
		}
		results = append(results, jsonResult{
			Accepted:     resp.Accepted,
			Deduplicated: resp.Deduplicated,
			IncidentId:   resp.IncidentId,
			Message:      resp.Message,
		})
	}
	h.m.IncNormalized(int64(len(events)))

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"alerts": results})
}

func clientIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil || host == "" {
		return r.RemoteAddr
	}
	return host
}

func writeJSONError(w http.ResponseWriter, msg string, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_, _ = w.Write([]byte(msg))
}

// jsonResult 是 HTTP 回應中的單筆警報處理結果。
type jsonResult struct {
	Accepted     bool   `json:"accepted"`
	Deduplicated bool   `json:"deduplicated"`
	IncidentId   string `json:"incident_id,omitempty"`
	Message      string `json:"message,omitempty"`
}
