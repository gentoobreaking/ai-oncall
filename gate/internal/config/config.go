// Package config 載入 oncall-gate 設定。
//
// 來源優先序：環境變數 > 預設值。
// 必填欄位（shared secret、core gRPC 位址）缺漏時回傳錯誤，gate 拒絕啟動——
// 寧可不收警報，不可無認證地開著 webhook 燒 LLM 的錢（F17-A）。
package config

import (
	"fmt"
	"os"
	"strings"
	"time"
)

// Config 是 oncall-gate 的全部執行期設定。
type Config struct {
	// ListenAddr webhook HTTP server 監聽位址（含 :port）
	ListenAddr string
	// SharedSecret AlertManager → gate webhook 的 Bearer token（F17-A）
	SharedSecret string
	// PrometheusURL Prometheus API 端點，如 http://prometheus:9090
	PrometheusURL string
	// LokiURL Loki API 端點，如 http://loki:3100
	LokiURL string
	// CoreAddr oncall-core 的 gRPC 位址，如 localhost:50051
	CoreAddr string
	// DeploymentsPath 近期部署清單檔（JSON lines）；空字串 = 停用該收集器
	DeploymentsPath string
	// CollectTimeout 單次 context 收集的總逾時
	CollectTimeout time.Duration
	// TelegramBotToken Telegram Bot API token（tgtransport 用）；空字串 = 推播停用
	TelegramBotToken string
}

// 預設值。ListenAddr 預設只聽 loopback；生產環境再明確放行。
// 注意：SHARED_SECRET / CORE_ADDR 為必填，刻意不給預設——
// 缺漏時 Validate 必須報錯擋下啟動（F17-A）。
func defaults() Config {
	return Config{
		ListenAddr:     "127.0.0.1:8080",
		PrometheusURL:  "http://127.0.0.1:9090",
		LokiURL:        "http://127.0.0.1:3100",
		CollectTimeout: 20 * time.Second,
	}
}

// EnvKeys 是 config 讀取的全部環境變數名稱（測試隔離用）。
var EnvKeys = []string{
	"LISTEN_ADDR",
	"SHARED_SECRET",
	"PROMETHEUS_URL",
	"LOKI_URL",
	"CORE_ADDR",
	"DEPLOYMENTS_PATH",
	"COLLECT_TIMEOUT",
	"TELEGRAM_BOT_TOKEN",
}

// Load 自環境變數載入設定並驗證必填欄位。
// 每次呼叫獨立讀取 os.LookupEnv——無任何跨呼叫共享狀態。
func Load() (Config, error) {
	cfg := defaults()

	get := func(key string) string {
		v, _ := os.LookupEnv(key)
		return strings.TrimSpace(v)
	}
	if v := get("LISTEN_ADDR"); v != "" {
		cfg.ListenAddr = v
	}
	if v := get("SHARED_SECRET"); v != "" {
		cfg.SharedSecret = v
	}
	if v := get("PROMETHEUS_URL"); v != "" {
		cfg.PrometheusURL = v
	}
	if v := get("LOKI_URL"); v != "" {
		cfg.LokiURL = v
	}
	if v := get("CORE_ADDR"); v != "" {
		cfg.CoreAddr = v
	}
	if v := get("DEPLOYMENTS_PATH"); v != "" {
		cfg.DeploymentsPath = v
	}
	if v := get("TELEGRAM_BOT_TOKEN"); v != "" {
		cfg.TelegramBotToken = v
	}
	if v := get("COLLECT_TIMEOUT"); v != "" {
		d, err := time.ParseDuration(v)
		if err != nil {
			return Config{}, fmt.Errorf("COLLECT_TIMEOUT 無法解析 %q: %w", v, err)
		}
		if d <= 0 {
			return Config{}, fmt.Errorf("COLLECT_TIMEOUT 必須為正數，得到 %s", d)
		}
		cfg.CollectTimeout = d
	}

	if err := cfg.Validate(); err != nil {
		return Config{}, err
	}
	return cfg, nil
}

// Validate 檢查必填欄位與格式。
func (c Config) Validate() error {
	var missing []string
	if c.SharedSecret == "" {
		missing = append(missing, "SHARED_SECRET")
	}
	if c.CoreAddr == "" {
		missing = append(missing, "CORE_ADDR")
	}
	if len(missing) > 0 {
		return fmt.Errorf("缺少必填設定: %s", strings.Join(missing, ", "))
	}
	if !strings.Contains(c.ListenAddr, ":") {
		return fmt.Errorf("LISTEN_ADDR 格式錯誤: %q（需含 :port）", c.ListenAddr)
	}
	if !strings.HasPrefix(c.PrometheusURL, "http") {
		return fmt.Errorf("PROMETHEUS_URL 必須以 http(s) 開頭: %q", c.PrometheusURL)
	}
	if !strings.HasPrefix(c.LokiURL, "http") {
		return fmt.Errorf("LOKI_URL 必須以 http(s) 開頭: %q", c.LokiURL)
	}
	return nil
}
