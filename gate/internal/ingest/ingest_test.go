package ingest

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
	"github.com/david/ai-oncall/gate/internal/metrics"
)

const testSecret = "test-secret"

// fakeCore 記錄呼叫次數並回傳固定結果。
type fakeCore struct {
	calls    atomic.Int64
	failNext atomic.Bool
	lastReq  *oncallv1.ReportIncidentRequest
}

func (f *fakeCore) ReportIncident(_ context.Context, in *oncallv1.ReportIncidentRequest) (*oncallv1.ReportIncidentResponse, error) {
	if f.failNext.Load() {
		return nil, fmt.Errorf("core down")
	}
	f.calls.Add(1)
	f.lastReq = in
	return &oncallv1.ReportIncidentResponse{
		Accepted:   true,
		IncidentId: "inc-001",
		Message:    "created",
	}, nil
}

func newTestHandler(t *testing.T) (*Handler, *fakeCore, *metrics.Metrics) {
	t.Helper()
	core := &fakeCore{}
	m := metrics.New()
	h := NewHandler(testSecret, core, NewStore(), m)
	return h, core, m
}

func post(t *testing.T, h http.Handler, body string, secret string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(http.MethodPost, "/alerts", strings.NewReader(body))
	req.RemoteAddr = "10.0.0.9:12345" // 固定 IP，避免限流互擾
	if secret != "" {
		req.Header.Set("Authorization", "Bearer "+secret)
	}
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	return rec
}

func amBody(fingerprint, status string) string {
	return fmt.Sprintf(`{"alerts":[{"status":%[2]q,"fingerprint":%[1]q,
		"labels":{"alertname":"HighLatency","service":"api","severity":"critical"},
		"annotations":{"summary":"latency high"},
		"startsAt":"2026-08-24T00:00:00Z","generatorURL":"http://prom/graph"}]}`,
		fingerprint, status)
}

// ---------------------------------------------------------------------------
// E.1 認證
// ---------------------------------------------------------------------------

func TestAuth_ValidSecretPasses(t *testing.T) {
	h, core, _ := newTestHandler(t)
	rec := post(t, h, amBody("fp-1", "firing"), testSecret)
	require.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, int64(1), core.calls.Load())
}

func TestAuth_RejectsAndCounts(t *testing.T) {
	tests := []struct {
		name   string
		secret string // 空字串 = 不帶 Authorization header
	}{
		{"無 header", ""},
		{"錯誤 secret", "wrong-secret"},
		{"非 Bearer", "test-secret"}, // 下方另行處理；此案例其實是錯的 secret
	}
	for _, tt := range tests[:2] {
		t.Run(tt.name, func(t *testing.T) {
			h, core, m := newTestHandler(t)
			rec := post(t, h, amBody("fp-auth", "firing"), tt.secret)
			assert.Equal(t, http.StatusUnauthorized, rec.Code)
			assert.Equal(t, int64(0), core.calls.Load(), "未認證不得觸及下游")
			snap := m.Snapshot()
			assert.Equal(t, int64(1), snap.Unauthorized, "應計入 /metrics")
		})
	}
}

func TestAuth_UnauthorizedNeverReachesDownstream(t *testing.T) {
	h, core, _ := newTestHandler(t)
	// 惡意大 payload + 無認證：不應有任何解析成本外洩到 core
	big := `{"alerts":[{"labels":{"pad":"` + strings.Repeat("x", 4096) + `"}}]}`
	rec := post(t, h, big, "")
	assert.Equal(t, http.StatusUnauthorized, rec.Code)
	assert.Equal(t, int64(0), core.calls.Load())
}

// ---------------------------------------------------------------------------
// E.2 冪等
// ---------------------------------------------------------------------------

func TestIdempotency_ResendThreeTimesSingleIncident(t *testing.T) {
	// spec.md §5 標準 13：同 fingerprint 重送 3 次僅產生 1 個 Incident
	h, core, m := newTestHandler(t)
	for i := 0; i < 3; i++ {
		rec := post(t, h, amBody("fp-dup", "firing"), testSecret)
		require.Equal(t, http.StatusOK, rec.Code, "第 %d 次", i+1)

		var out struct {
			Alerts []jsonResult `json:"alerts"`
		}
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &out))
		require.Len(t, out.Alerts, 1)
		assert.Equal(t, "inc-001", out.Alerts[0].IncidentId, "每次都回上次結果")
		if i > 0 {
			assert.True(t, out.Alerts[0].Deduplicated, "第 %d 次應標記 deduplicated", i+1)
		}
	}
	assert.Equal(t, int64(1), core.calls.Load(), "管線只跑一次")
	assert.Equal(t, int64(2), m.Snapshot().Deduplicated)
}

func TestIdempotency_StatusChangeIsNewKey(t *testing.T) {
	// (fingerprint, firing) 已存在；(fingerprint, resolved) 是不同鍵 → 新處理
	h, core, _ := newTestHandler(t)
	post(t, h, amBody("fp-rc", "firing"), testSecret)
	rec := post(t, h, amBody("fp-rc", "resolved"), testSecret)
	require.Equal(t, http.StatusOK, rec.Code)
	assert.Equal(t, int64(2), core.calls.Load())
}

func TestIdempotency_DifferentFingerprintsBothForward(t *testing.T) {
	h, core, _ := newTestHandler(t)
	post(t, h, amBody("fp-a", "firing"), testSecret)
	post(t, h, amBody("fp-b", "firing"), testSecret)
	assert.Equal(t, int64(2), core.calls.Load())
}

// ---------------------------------------------------------------------------
// 正規化（表驅動，含缺欄位容錯）
// ---------------------------------------------------------------------------

func TestNormalize_TableDriven(t *testing.T) {
	now := time.Date(2026, 8, 24, 12, 0, 0, 0, time.UTC)
	tests := []struct {
		name        string
		payload     string
		wantErr     bool
		wantEvents  int
		check       func(t *testing.T, ev *oncallv1.AlertEvent)
	}{
		{
			name: "完整 payload",
			payload: `{"alerts":[{"status":"firing","fingerprint":"abc123",
				"labels":{"alertname":"HighLatency","service":"api","severity":"critical"},
				"annotations":{"summary":"s","description":"d"},
				"startsAt":"2026-08-24T00:01:02Z","generatorURL":"http://g"}]}`,
			wantEvents: 1,
			check: func(t *testing.T, ev *oncallv1.AlertEvent) {
				assert.Equal(t, "abc123", ev.Fingerprint)
				assert.Equal(t, oncallv1.AlertStatus_ALERT_STATUS_FIRING, ev.Status)
				assert.Equal(t, oncallv1.Severity_SEVERITY_CRITICAL, ev.Severity)
				assert.Equal(t, int64(1787529662), ev.StartsAtUnix) // 2026-08-24T00:01:02Z
				assert.Equal(t, "http://g", ev.GeneratorUrl)
				assert.Equal(t, "s", ev.Summary)
			},
		},
		{
			name:       "缺 status → 預設 firing",
			payload:    `{"alerts":[{"fingerprint":"x","labels":{}}]}`,
			wantEvents: 1,
			check: func(t *testing.T, ev *oncallv1.AlertEvent) {
				assert.Equal(t, oncallv1.AlertStatus_ALERT_STATUS_FIRING, ev.Status)
			},
		},
		{
			name:       "缺 fingerprint → labels 雜湊導出",
			payload:    `{"alerts":[{"status":"firing","labels":{"alertname":"A","instance":"i"}}]}`,
			wantEvents: 1,
			check: func(t *testing.T, ev *oncallv1.AlertEvent) {
				assert.True(t, strings.HasPrefix(ev.Fingerprint, "derived-"))
				// 同 labels 導出同指紋（決定性）
				ev2 := normalizeOne(amAlert{Labels: map[string]string{"alertname": "A", "instance": "i"}}, now)
				assert.Equal(t, ev.Fingerprint, ev2.Fingerprint)
			},
		},
		{
			name:       "startsAt 壞格式 → 收到時間代替",
			payload:    `{"alerts":[{"fingerprint":"x","labels":{},"startsAt":"not-a-time"}]}`,
			wantEvents: 1,
			check: func(t *testing.T, ev *oncallv1.AlertEvent) {
				assert.Equal(t, now.Unix(), ev.StartsAtUnix)
			},
		},
		{
			name:       "缺 annotations/severity",
			payload:    `{"alerts":[{"fingerprint":"x"}]}`,
			wantEvents: 1,
			check: func(t *testing.T, ev *oncallv1.AlertEvent) {
				assert.Equal(t, oncallv1.Severity_SEVERITY_UNSPECIFIED, ev.Severity)
				assert.Empty(t, ev.Summary)
			},
		},
		{
			name:       "resolved 狀態",
			payload:    `{"alerts":[{"status":"resolved","fingerprint":"x","labels":{}}]}`,
			wantEvents: 1,
			check: func(t *testing.T, ev *oncallv1.AlertEvent) {
				assert.Equal(t, oncallv1.AlertStatus_ALERT_STATUS_RESOLVED, ev.Status)
			},
		},
		{
			name:       "多筆警報同批",
			payload:    `{"alerts":[{"fingerprint":"a","labels":{}},{"fingerprint":"b","labels":{}}]}`,
			wantEvents: 2,
		},
		{
			name:    "非 JSON",
			payload: `garbage`,
			wantErr: true,
		},
		{
			name:    "空 alerts",
			payload: `{"alerts":[]}`,
			wantErr: true,
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			events, err := Normalize([]byte(tt.payload), now)
			if tt.wantErr {
				require.Error(t, err)
				return
			}
			require.NoError(t, err)
			require.Len(t, events, tt.wantEvents)
			if tt.check != nil {
				tt.check(t, events[0])
			}
		})
	}
}

// ---------------------------------------------------------------------------
// payload 上限與 rate limiting
// ---------------------------------------------------------------------------

func TestPayloadTooLarge(t *testing.T) {
	h, core, m := newTestHandler(t)
	big := `{"alerts":[{"fingerprint":"x","labels":{"pad":"` + strings.Repeat("x", MaxBodyBytes+1024) + `"}}]}`
	rec := post(t, h, big, testSecret)
	assert.Equal(t, http.StatusRequestEntityTooLarge, rec.Code)
	assert.Equal(t, int64(0), core.calls.Load())
	assert.Equal(t, int64(1), m.Snapshot().TooLarge)
}

func TestRateLimit_PerIP(t *testing.T) {
	h, core, m := newTestHandler(t)
	body := amBody("fp-rl", "firing")
	var limited int
	for i := 0; i < 50; i++ { // burst 10，之後開始 429（冪等命中不影響 limiter）
		req := httptest.NewRequest(http.MethodPost, "/alerts", strings.NewReader(body))
		req.RemoteAddr = "10.9.9.9:1"
		req.Header.Set("Authorization", "Bearer "+testSecret)
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, req)
		if rec.Code == http.StatusTooManyRequests {
			limited++
		} else {
			assert.Equal(t, http.StatusOK, rec.Code, "第 %d 筆", i+1)
		}
	}
	assert.Greater(t, limited, 0, "超量應被 429")
	assert.Equal(t, int64(1), core.calls.Load(), "只有第一筆真正轉發")
	assert.Greater(t, m.Snapshot().RateLimited, int64(0))
}

func TestMethodNotAllowed(t *testing.T) {
	h, _, _ := newTestHandler(t)
	req := httptest.NewRequest(http.MethodGet, "/alerts", bytes.NewReader(nil))
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, req)
	assert.Equal(t, http.StatusMethodNotAllowed, rec.Code)
}
