// oncall-gate 進入點：webhook HTTP server + core gRPC client。
//
// 管線：AlertManager → /alerts（認證/冪等/正規化）→ gRPC ReportIncident → core
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

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
	"github.com/david/ai-oncall/gate/internal/config"
	"github.com/david/ai-oncall/gate/internal/ingest"
	"github.com/david/ai-oncall/gate/internal/metrics"
)

// coreClientAdapter 讓產生的 gRPC client 符合 ingest.CoreReporter 最小介面。
type coreClientAdapter struct{ c oncallv1.OncallServiceClient }

func (a coreClientAdapter) ReportIncident(ctx context.Context, in *oncallv1.ReportIncidentRequest) (*oncallv1.ReportIncidentResponse, error) {
	return a.c.ReportIncident(ctx, in)
}

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

	// core gRPC client（core 掛掉時 ingest 回 502，AM 會重試）
	grpcConn, err := grpc.NewClient(cfg.CoreAddr, grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		logger.Error("core gRPC client 建立失敗", "error", err)
		os.Exit(1)
	}
	defer func() { _ = grpcConn.Close() }()

	m := metrics.New()
	mux := http.NewServeMux()
	mux.Handle("GET /healthz", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}))
	mux.Handle("GET /metrics", m.Handler())
	mux.Handle("POST /alerts", ingest.NewHandler(
		cfg.SharedSecret,
		coreClientAdapter{c: oncallv1.NewOncallServiceClient(grpcConn)},
		ingest.NewStore(),
		m,
	))

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
