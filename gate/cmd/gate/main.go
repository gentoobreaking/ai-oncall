// oncall-gate 進入點：webhook HTTP server + core gRPC client/server。
//
// 管線：
//
//	AlertManager → /alerts（認證/冪等/正規化）→ gRPC ReportIncident → core
//	core → gate gRPC DeliverNotification（Telegram 推播）
//	core → gate gRPC CollectContext（context 收集 fan-out）
//	Telegram callback long-polling → gRPC ActionCallback → core
package main

import (
	"context"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
	"github.com/david/ai-oncall/gate/internal/collect"
	"github.com/david/ai-oncall/gate/internal/config"
	"github.com/david/ai-oncall/gate/internal/grpcserver"
	"github.com/david/ai-oncall/gate/internal/ingest"
	"github.com/david/ai-oncall/gate/internal/metrics"
	"github.com/david/ai-oncall/gate/internal/tgtransport"
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
		"grpc_listen", cfg.GRPCListenAddr,
		"core", cfg.CoreAddr,
		"prometheus", cfg.PrometheusURL,
		"loki", cfg.LokiURL,
	)

	// ---- core gRPC client ----
	coreConn, err := grpc.NewClient(cfg.CoreAddr,
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		logger.Error("core gRPC client 建立失敗", "error", err)
		os.Exit(1)
	}
	defer func() { _ = coreConn.Close() }()
	coreClient := oncallv1.NewOncallServiceClient(coreConn)

	m := metrics.New()

	// ---- Telegram 傳輸層（token 未設定時 log-only 降級）----
	sender := tgtransport.NewSender(cfg.TelegramBotToken, logger)
	router := tgtransport.NewRouter(cfg.TelegramBotToken, "", coreClientAdapter{c: coreClient}, logger)

	// ---- gate 端 gRPC server（core 反向呼叫推播/context 收集）----
	collectors := buildCollectors(cfg)
	collectFn := func(ctx context.Context, labels map[string]string,
		since, until time.Time) (*oncallv1.ContextBundle, error) {
		result := collect.FanOut(ctx, collectors, labels, since, until, cfg.CollectTimeout)
		return result.Bundle, nil
	}

	grpcSrv := grpc.NewServer()
	oncallv1.RegisterOncallServiceServer(
		grpcSrv,
		grpcserver.New(sender, collectFn, grpcserver.NewCoreProxy(coreClient)),
	)

	grpcLis, err := net.Listen("tcp", cfg.GRPCListenAddr)
	if err != nil {
		logger.Error("gate gRPC 監聽失敗", "addr", cfg.GRPCListenAddr, "error", err)
		os.Exit(1)
	}
	go func() { _ = grpcSrv.Serve(grpcLis) }()
	logger.Info("gate gRPC server ready", "addr", cfg.GRPCListenAddr)

	// callback long-polling（token 未設定時 Run 內部直接返回）
	go func() { _ = router.Run(context.Background()) }()

	// ---- webhook HTTP server ----
	mux := http.NewServeMux()
	mux.Handle("GET /healthz", http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	}))
	mux.Handle("GET /metrics", m.Handler())
	mux.Handle("POST /alerts", ingest.NewHandler(
		cfg.SharedSecret,
		coreClientAdapter{c: coreClient},
		ingest.NewStore(),
		m,
	))

	httpSrv := &http.Server{
		Addr:              cfg.ListenAddr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
	}

	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()

	errCh := make(chan error, 1)
	go func() {
		if err := httpSrv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
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
		if err := httpSrv.Shutdown(shutdownCtx); err != nil {
			logger.Error("HTTP 關機失敗", "error", err)
		}
		grpcSrv.GracefulStop()
	}
}

// coreClientAdapter 讓產生的 gRPC client 符合 ingest.CoreReporter 最小介面。
type coreClientAdapter struct{ c oncallv1.OncallServiceClient }

func (a coreClientAdapter) ReportIncident(
	ctx context.Context, in *oncallv1.ReportIncidentRequest,
) (*oncallv1.ReportIncidentResponse, error) {
	return a.c.ReportIncident(ctx, in)
}

// ActionCallback 讓 tgtransport.Router 滿足 CoreForwarder 介面。
func (a coreClientAdapter) ActionCallback(
	ctx context.Context, req *oncallv1.ActionCallbackRequest,
) (*oncallv1.ActionCallbackResponse, error) {
	return a.c.ActionCallback(ctx, req)
}

// buildCollectors 依設定建構 context 收集器集合。
func buildCollectors(cfg config.Config) []collect.Collector {
	cs := []collect.Collector{
		&collect.PrometheusClient{BaseURL: cfg.PrometheusURL, ClusterURLs: cfg.ClusterPromURLs},
		&collect.ScalingClient{PromBaseURL: cfg.PrometheusURL, ClusterURLs: cfg.ClusterPromURLs},
		&collect.LokiClient{BaseURL: cfg.LokiURL},
	}
	if cfg.DeploymentsPath != "" {
		cs = append(cs, &collect.DeploymentsFile{Path: cfg.DeploymentsPath})
	}
	return cs
}
