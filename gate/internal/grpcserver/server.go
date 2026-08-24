// Package grpcserver 提供 gate 端的 OncallService gRPC server：
// 讓 core 能反向呼叫 DeliverNotification（Telegram 推播）與
// CollectContext（context 收集 fan-out），並代理 ActionCallback/ReportIncident。
package grpcserver

import (
	"context"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
	"github.com/david/ai-oncall/gate/internal/tgtransport"
)

// NotificationSender 是 tgtransport.Sender 的最小介面（測試以 fake 注入）。
type NotificationSender interface {
	SendMessage(ctx context.Context, chatID, text, parseMode string, buttons []tgtransport.Button) error
}

// CollectFunc 執行 context 收集 fan-out，回傳合成後的 bundle。
type CollectFunc func(ctx context.Context, labels map[string]string,
	since, until time.Time) (*oncallv1.ContextBundle, error)

// CoreProxy 轉發 core 負責的 RPC（gate 作為單一入口時使用）。
type CoreProxy interface {
	ForwardReportIncident(ctx context.Context, req *oncallv1.ReportIncidentRequest) (*oncallv1.ReportIncidentResponse, error)
	ForwardActionCallback(ctx context.Context, req *oncallv1.ActionCallbackRequest) (*oncallv1.ActionCallbackResponse, error)
}

// Server 實作 oncallv1.OncallService 的 gate 端。
type Server struct {
	oncallv1.UnimplementedOncallServiceServer
	Sender  NotificationSender
	Collect CollectFunc
	Core    CoreProxy // 可為 nil：未設定 core 時回報失敗
}

// New 建立 gate 端 server。
func New(sender NotificationSender, collect CollectFunc, core CoreProxy) *Server {
	return &Server{Sender: sender, Collect: collect, Core: core}
}

// ---------------------------------------------------------------------------
// DeliverNotification — core → gate：請求發送 Telegram 訊息
// ---------------------------------------------------------------------------

func (s *Server) DeliverNotification(
	ctx context.Context,
	req *oncallv1.DeliverNotificationRequest,
) (*oncallv1.DeliverNotificationResponse, error) {
	n := req.GetNotification()
	if n == nil || n.GetText() == "" {
		return &oncallv1.DeliverNotificationResponse{
			Accepted: false, Message: "missing notification text",
		}, nil
	}
	buttons := make([]tgtransport.Button, 0, len(n.GetButtons()))
	for _, b := range n.GetButtons() {
		buttons = append(buttons, tgtransport.Button{CallbackID: b.GetCallbackId(), Text: b.GetText()})
	}
	parseMode := n.GetParseMode()
	if err := s.Sender.SendMessage(ctx, n.GetChatId(), n.GetText(), parseMode, buttons); err != nil {
		return &oncallv1.DeliverNotificationResponse{
			Accepted: false, Message: "send failed: " + err.Error(),
		}, nil
	}
	return &oncallv1.DeliverNotificationResponse{Accepted: true}, nil
}

// ---------------------------------------------------------------------------
// CollectContext — core → gate：併發收集現場 context
// ---------------------------------------------------------------------------

func (s *Server) CollectContext(
	ctx context.Context,
	req *oncallv1.CollectContextRequest,
) (*oncallv1.CollectContextResponse, error) {
	if s.Collect == nil {
		return &oncallv1.CollectContextResponse{
			Bundle: &oncallv1.ContextBundle{
				DegradedSources: []string{"collector: unavailable (not configured)"},
			},
		}, nil
	}
	since := time.Unix(req.GetSinceUnix(), 0)
	until := time.Unix(req.GetUntilUnix(), 0)
	bundle, err := s.Collect(ctx, req.GetLabels(), since, until)
	if err != nil {
		return nil, err
	}
	return &oncallv1.CollectContextResponse{Bundle: bundle}, nil
}

// ---------------------------------------------------------------------------
// ReportIncident / ActionCallback — 代理轉發給 core
// ---------------------------------------------------------------------------

func (s *Server) ReportIncident(
	ctx context.Context,
	req *oncallv1.ReportIncidentRequest,
) (*oncallv1.ReportIncidentResponse, error) {
	if s.Core == nil {
		return &oncallv1.ReportIncidentResponse{Accepted: false, Message: "core not configured"}, nil
	}
	return s.Core.ForwardReportIncident(ctx, req)
}

func (s *Server) ActionCallback(
	ctx context.Context,
	req *oncallv1.ActionCallbackRequest,
) (*oncallv1.ActionCallbackResponse, error) {
	if s.Core == nil {
		return &oncallv1.ActionCallbackResponse{Accepted: false, Message: "core not configured"}, nil
	}
	return s.Core.ForwardActionCallback(ctx, req)
}
