package config

import (
	"strings"
	"testing"
	"time"
)

func setEnv(t *testing.T, kv map[string]string) {
	t.Helper()
	// 先清空所有 config 相關環境變數，隔離宿主機 shell 的洩漏
	//（例如全域 export 的 TELEGRAM_BOT_TOKEN），再套用測試值。
	for _, k := range EnvKeys {
		t.Setenv(k, "")
	}
	for k, v := range kv {
		t.Setenv(k, v)
	}
}

// 預設值：只給必填的 SHARED_SECRET/CORE_ADDR，其餘應為內建預設。
func TestLoad_Defaults(t *testing.T) {
	setEnv(t, map[string]string{
		"SHARED_SECRET": "s3cret",
		"CORE_ADDR":     "core.internal:50051",
	})
	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() err = %v", err)
	}
	if cfg.ListenAddr != "127.0.0.1:8080" {
		t.Errorf("ListenAddr = %q, want default 127.0.0.1:8080", cfg.ListenAddr)
	}
	if cfg.PrometheusURL != "http://127.0.0.1:9090" {
		t.Errorf("PrometheusURL = %q, want default", cfg.PrometheusURL)
	}
	if cfg.LokiURL != "http://127.0.0.1:3100" {
		t.Errorf("LokiURL = %q, want default", cfg.LokiURL)
	}
	if cfg.CollectTimeout != 20*time.Second {
		t.Errorf("CollectTimeout = %s, want 20s", cfg.CollectTimeout)
	}
	if cfg.TelegramBotToken != "" || cfg.DeploymentsPath != "" {
		t.Errorf("選用欄位預設應為空字串")
	}
}

// 環境變數覆寫所有欄位。
func TestLoad_Override(t *testing.T) {
	setEnv(t, map[string]string{
		"SHARED_SECRET":      "env-secret",
		"CORE_ADDR":          "10.0.0.5:50051",
		"LISTEN_ADDR":        "0.0.0.0:9000",
		"PROMETHEUS_URL":     "https://prom.example.com",
		"LOKI_URL":           "https://loki.example.com",
		"DEPLOYMENTS_PATH":   "/var/lib/deployments.jsonl",
		"COLLECT_TIMEOUT":    "45s",
		"TELEGRAM_BOT_TOKEN": "tok-123",
	})
	cfg, err := Load()
	if err != nil {
		t.Fatalf("Load() err = %v", err)
	}
	if cfg.ListenAddr != "0.0.0.0:9000" ||
		cfg.SharedSecret != "env-secret" ||
		cfg.CoreAddr != "10.0.0.5:50051" ||
		cfg.PrometheusURL != "https://prom.example.com" ||
		cfg.LokiURL != "https://loki.example.com" ||
		cfg.DeploymentsPath != "/var/lib/deployments.jsonl" ||
		cfg.CollectTimeout != 45*time.Second ||
		cfg.TelegramBotToken != "tok-123" {
		t.Errorf("環境變數未正確覆寫: %+v", cfg)
	}
}

// 缺少任一必填欄位 → 錯誤且指出欄位名稱；兩者皆缺 → 一併列出。
func TestLoad_MissingRequired(t *testing.T) {
	tests := []struct {
		name    string
		env     map[string]string
		wantErr string
	}{
		{
			name:    "缺 SHARED_SECRET",
			env:     map[string]string{"CORE_ADDR": "c:1"},
			wantErr: "SHARED_SECRET",
		},
		{
			name:    "缺 CORE_ADDR",
			env:     map[string]string{"SHARED_SECRET": "x"},
			wantErr: "CORE_ADDR",
		},
		{
			name:    "全缺",
			env:     map[string]string{},
			wantErr: "CORE_ADDR",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			setEnv(t, tt.env)
			_, err := Load()
			if err == nil {
				t.Fatal("Load() 應回傳錯誤，卻成功了")
			}
			if !contains(err.Error(), tt.wantErr) || !contains(err.Error(), "缺少必填設定") {
				t.Errorf("err = %v, 應包含 %q 與「缺少必填設定」", err, tt.wantErr)
			}
		})
	}
}

// COLLECT_TIMEOUT 格式錯誤與非正值都必須被拒絕。
func TestLoad_BadCollectTimeout(t *testing.T) {
	setEnv(t, map[string]string{"SHARED_SECRET": "x", "CORE_ADDR": "c:1", "COLLECT_TIMEOUT": "soon"})
	if _, err := Load(); err == nil {
		t.Error("COLLECT_TIMEOUT=soon 應解析失敗")
	}
	setEnv(t, map[string]string{"SHARED_SECRET": "x", "CORE_ADDR": "c:1", "COLLECT_TIMEOUT": "-3s"})
	if _, err := Load(); err == nil {
		t.Error("COLLECT_TIMEOUT=-3s 應被拒絕")
	}
}

// URL 格式驗證（Validate 由前往後檢查，先回報第一個錯）。
func TestValidate_BadURL(t *testing.T) {
	cfg := Config{SharedSecret: "x", CoreAddr: "c:1", ListenAddr: "a:1", GRPCListenAddr: "g:1", PrometheusURL: "ftp://p", LokiURL: "http://l"}
	if err := cfg.Validate(); err == nil || !contains(err.Error(), "PROMETHEUS_URL") {
		t.Errorf("Validate() err = %v, 應指出 PROMETHEUS_URL", err)
	}
	cfg.PrometheusURL = "http://p"
	cfg.LokiURL = "no-scheme"
	if err := cfg.Validate(); err == nil || !contains(err.Error(), "LOKI_URL") {
		t.Errorf("Validate() err = %v, 應指出 LOKI_URL", err)
	}
}

func contains(s, sub string) bool {
	return strings.Contains(s, sub)
}
