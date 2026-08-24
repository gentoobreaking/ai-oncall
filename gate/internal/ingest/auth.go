// 實作 algs/integrity-auth.md §E.1：webhook shared secret 驗證。
//
// - /alerts 強制 Authorization: Bearer <shared_secret>；不符 → 401
// - secret 來自 env（config），與 Telegram token 分開保管
// - 未認證請求計入 /metrics，且不得觸及任何下游資源（解析/core 轉發）
package ingest

import (
	"crypto/subtle"
	"net/http"
	"strings"

	"github.com/david/ai-oncall/gate/internal/metrics"
)

const bearerPrefix = "Bearer "

// verifyAuth 驗證 Bearer token。回傳是否通過。
// 使用 constant-time 比較避免 timing side-channel。
func verifyAuth(r *http.Request, sharedSecret string) bool {
	h := r.Header.Get("Authorization")
	if !strings.HasPrefix(h, bearerPrefix) {
		return false
	}
	got := strings.TrimSpace(strings.TrimPrefix(h, bearerPrefix))
	if got == "" || sharedSecret == "" {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(sharedSecret)) == 1
}

// writeUnauthorized 回 401 並計入 metrics（E.1）。
func writeUnauthorized(w http.ResponseWriter, m *metrics.Metrics) {
	m.IncUnauthorized()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusUnauthorized)
	_, _ = w.Write([]byte(`{"error":"unauthorized"}`))
}
