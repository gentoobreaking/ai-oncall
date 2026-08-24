package grpcserver

import (
	"context"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
	"github.com/david/ai-oncall/gate/internal/tgtransport"
)

type fakeSender struct {
	calls   int
	failErr error
	last    struct {
		chatID, text, parseMode string
		buttons                 int
	}
}

func (f *fakeSender) SendMessage(
	_ context.Context, chatID, text, parseMode string, buttons []tgtransport.Button,
) error {
	f.calls++
	if f.failErr != nil {
		return f.failErr
	}
	f.last.chatID = chatID
	f.last.text = text
	f.last.parseMode = parseMode
	f.last.buttons = len(buttons)
	return nil
}

type fakeCoreProxy struct {
	reportCalled   bool
	callbackCalled bool
}

func (f *fakeCoreProxy) ForwardReportIncident(
	_ context.Context, _ *oncallv1.ReportIncidentRequest,
) (*oncallv1.ReportIncidentResponse, error) {
	f.reportCalled = true
	return &oncallv1.ReportIncidentResponse{Accepted: true}, nil
}

func (f *fakeCoreProxy) ForwardActionCallback(
	_ context.Context, _ *oncallv1.ActionCallbackRequest,
) (*oncallv1.ActionCallbackResponse, error) {
	f.callbackCalled = true
	return &oncallv1.ActionCallbackResponse{Accepted: true}, nil
}

func TestDeliverNotification_SendsMessage(t *testing.T) {
	sender := &fakeSender{}
	srv := New(sender, nil, nil)

	resp, err := srv.DeliverNotification(context.Background(),
		&oncallv1.DeliverNotificationRequest{
			Notification: &oncallv1.Notification{
				IncidentId: "inc-1",
				ChatId:     "chat-9",
				Text:       "report text",
				ParseMode:  "MarkdownV2",
				Buttons: []*oncallv1.NotificationButton{
					{CallbackId: "cb-1", Text: "批准"},
					{CallbackId: "cb-2", Text: "拒絕"},
				},
			},
		})
	require.NoError(t, err)
	assert.True(t, resp.Accepted)
	assert.Equal(t, 1, sender.calls)
	assert.Equal(t, "chat-9", sender.last.chatID)
	assert.Equal(t, 2, sender.last.buttons)
}

func TestDeliverNotification_EmptyTextRejected(t *testing.T) {
	srv := New(&fakeSender{}, nil, nil)
	resp, err := srv.DeliverNotification(context.Background(),
		&oncallv1.DeliverNotificationRequest{
			Notification: &oncallv1.Notification{ChatId: "chat-9"},
		})
	require.NoError(t, err)
	assert.False(t, resp.Accepted)
}

func TestCollectContext_ReturnsBundle(t *testing.T) {
	bundle := &oncallv1.ContextBundle{
		Metrics:         []*oncallv1.MetricSeries{{Query: "up"}},
		DegradedSources: []string{"logs: unavailable"},
	}
	var gotLabels map[string]string
	collect := func(_ context.Context, labels map[string]string, _, _ time.Time,
	) (*oncallv1.ContextBundle, error) {
		gotLabels = labels
		return bundle, nil
	}
	srv := New(&fakeSender{}, collect, nil)

	resp, err := srv.CollectContext(context.Background(), &oncallv1.CollectContextRequest{
		IncidentId: "inc-c",
		Labels:     map[string]string{"service": "api"},
		SinceUnix:  1000,
		UntilUnix:  2000,
	})
	require.NoError(t, err)
	assert.Len(t, resp.Bundle.Metrics, 1)
	assert.Equal(t, []string{"logs: unavailable"}, resp.Bundle.DegradedSources)
	assert.Equal(t, "api", gotLabels["service"])
}

func TestCollectContext_NotConfigured_Degraded(t *testing.T) {
	srv := New(&fakeSender{}, nil, nil)
	resp, err := srv.CollectContext(context.Background(), &oncallv1.CollectContextRequest{})
	require.NoError(t, err)
	assert.NotEmpty(t, resp.Bundle.DegradedSources, "未設定收集器應標注降級")
}

func TestProxy_ForwardsToCore(t *testing.T) {
	core := &fakeCoreProxy{}
	srv := New(&fakeSender{}, nil, core)

	r1, err := srv.ReportIncident(context.Background(), &oncallv1.ReportIncidentRequest{})
	require.NoError(t, err)
	assert.True(t, r1.Accepted)
	assert.True(t, core.reportCalled)

	r2, err := srv.ActionCallback(context.Background(), &oncallv1.ActionCallbackRequest{})
	require.NoError(t, err)
	assert.True(t, r2.Accepted)
	assert.True(t, core.callbackCalled)
}

func TestProxy_NilCore_GracefulReject(t *testing.T) {
	srv := New(&fakeSender{}, nil, nil)
	r1, err := srv.ReportIncident(context.Background(), &oncallv1.ReportIncidentRequest{})
	require.NoError(t, err)
	assert.False(t, r1.Accepted)
	r2, err := srv.ActionCallback(context.Background(), &oncallv1.ActionCallbackRequest{})
	require.NoError(t, err)
	assert.False(t, r2.Accepted)
}
