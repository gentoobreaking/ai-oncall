package grpcserver

import (
	"context"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// coreClientAdapter 讓產生的 gRPC client 符合 CoreProxy 介面。
type coreClientAdapter struct {
	client oncallv1.OncallServiceClient
}

// NewCoreProxy 將 core 的 gRPC client 包裝為 CoreProxy。
func NewCoreProxy(client oncallv1.OncallServiceClient) CoreProxy {
	return coreClientAdapter{client: client}
}

func (a coreClientAdapter) ForwardReportIncident(
	ctx context.Context, req *oncallv1.ReportIncidentRequest,
) (*oncallv1.ReportIncidentResponse, error) {
	return a.client.ReportIncident(ctx, req)
}

func (a coreClientAdapter) ForwardActionCallback(
	ctx context.Context, req *oncallv1.ActionCallbackRequest,
) (*oncallv1.ActionCallbackResponse, error) {
	return a.client.ActionCallback(ctx, req)
}
