// oncall-gate 進入點：webhook HTTP server + core gRPC client。
//
// T001 僅確立骨架：載入設定、建立結構化 logger（log/slog）、
// 監聽 /healthz；ingest/collect/tgtransport 由後續任務接入。
package main

import (
	"context"
	"errors"
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/david/ai-oncall/gate/internal/config"
)

func main() {
	logger := slog.New(slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{Level: slog.LevelInfo}))
	slog.SetDefault(logger)

	cfg, err := config.Load()
	if err != nil {
		logger.Error("設定載入失敗，gate 拒絕啟動", "error", err)
		os.Exit(1)
	}
	logger.Info("oncall-gate 啟動",
		"listen", cfg.ListenAddr,
		"core", cfg.CoreAddr,
		"prometheus", cfg.PrometheusURL,
		"loki", cfg.LokiURL,
	)

	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	// /alerts 端點由 T002（ingest 認證/冪等）實作佔位
	mux.HandleFunc("/alerts", func(w http.ResponseWriter, _ *http.Request) {
		http.Error(w, "not implemented", http.StatusNotImplemented)
	})

	srv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() {
		if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
			errCh <- err
		}
	}()

	select {
	case err := <-errCh:
		logger.Error("HTTP server 異常退出", "error", err)
		os.Exit(1)
	case <-ctx.Done():
		logger.Info("收到停止訊號，優雅關機")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil {
			logger.Error("優雅關機失敗", "error", err)
		}
	}
}
