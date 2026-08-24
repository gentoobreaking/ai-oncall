package tgtransport

import (
	"context"
	"fmt"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// ---------------------------------------------------------------------------
// EscapeMarkdownV2
// ---------------------------------------------------------------------------

func TestEscapeMarkdownV2(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"純文字不動", "latency high", "latency high"},
		{"底線", "service_api", "service\\_api"},
		{"星號與點", "v1.2*alpha", "v1\\.2\\*alpha"},
		{"括號與驚嘆號", "rollback(now)!", "rollback\\(now\\)\\!"},
		{"全集合", "_*[]()~`>#+-=|{}!", "\\_\\*\\[\\]\\(\\)\\~\\`\\>\\#\\+\\-\\=\\|\\{\\}\\!"},
		{"中文不受影響", "延遲過高：API 服務", "延遲過高：API 服務"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			assert.Equal(t, tt.want, EscapeMarkdownV2(tt.in))
		})
	}
}

// ---------------------------------------------------------------------------
// SendMessage：重試 / fatal 錯誤 / log-only
// ---------------------------------------------------------------------------

// newFakeTG 啟一個假 Telegram Bot API，回應可控且計數。
func newFakeTG(t *testing.T, status func(attempt int64) int) (*httptest.Server, *atomic.Int64) {
	t.Helper()
	var hits atomic.Int64
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		n := hits.Add(1)
		code := status(n)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(code)
		if code == http.StatusInternalServerError {
			fmt.Fprint(w, `{"ok":false,"description":"internal error"}`)
		} else {
			fmt.Fprint(w, `{"ok":true}`)
		}
	}))
	t.Cleanup(srv.Close)
	return srv, &hits
}

func fastSender(token, apiBase string) *Sender {
	s := NewSender(token, nil)
	s.apiBase = apiBase
	s.sleep = func(time.Duration) {} // 測試不等退避
	return s
}

func TestSendMessage_RetriesThenSucceeds(t *testing.T) {
	srv, hits := newFakeTG(t, func(n int64) int {
		if n < 3 {
			return http.StatusInternalServerError // 前兩次失敗
		}
		return http.StatusOK
	})
	s := fastSender("tok", srv.URL)

	err := s.SendMessage(context.Background(), "123", "report", "MarkdownV2",
		[]Button{{CallbackID: "cb-1", Text: "✅批准"}})
	require.NoError(t, err)
	assert.Equal(t, int64(3), hits.Load(), "第三次成功")
}

func TestSendMessage_GivesUpAfterThreeRetries(t *testing.T) {
	srv, hits := newFakeTG(t, func(int64) int { return http.StatusBadGateway })
	s := fastSender("tok", srv.URL)

	err := s.SendMessage(context.Background(), "123", "x", "", nil)
	require.Error(t, err)
	assert.Equal(t, int64(sendAttempts), hits.Load(), "首次 + 重試 3 次後放棄")
	assert.Contains(t, err.Error(), "重試 3 次後仍失敗")
}

func TestSendMessage_Fatal4xxNoRetry(t *testing.T) {
	srv, hits := newFakeTG(t, func(int64) int {
		return http.StatusBadRequest // chat not found——重試無益
	})
	s := fastSender("tok", srv.URL)

	err := s.SendMessage(context.Background(), "123", "x", "", nil)
	require.Error(t, err)
	assert.Equal(t, int64(1), hits.Load(), "4xx 不重試")
}

func TestSendMessage_LogOnlyModeWhenTokenEmpty(t *testing.T) {
	var touched atomic.Bool
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		touched.Store(true)
		w.WriteHeader(http.StatusOK)
	}))
	t.Cleanup(srv.Close)

	s := NewSender("", nil) // token 空 → log-only
	require.True(t, s.LogOnly())

	err := s.SendMessage(context.Background(), "123", "shadow report", "", nil)
	require.NoError(t, err, "log-only 模式送訊息應無聲成功")
	assert.False(t, touched.Load(), "log-only 不得觸網")
}

// ---------------------------------------------------------------------------
// callback 轉發：gRPC ActionCallback、core 掛掉時快取重試
// ---------------------------------------------------------------------------

type fakeCoreForwarder struct {
	failNext  atomic.Bool
	received  chan *oncallv1.ActionCallbackRequest
	callCount atomic.Int64
}

func newFakeCore() *fakeCoreForwarder {
	return &fakeCoreForwarder{received: make(chan *oncallv1.ActionCallbackRequest, 16)}
}

func (f *fakeCoreForwarder) ActionCallback(_ context.Context, req *oncallv1.ActionCallbackRequest) (*oncallv1.ActionCallbackResponse, error) {
	if f.failNext.Load() {
		return nil, fmt.Errorf("core unavailable")
	}
	f.callCount.Add(1)
	f.received <- req
	return &oncallv1.ActionCallbackResponse{Accepted: true}, nil
}

func TestFetchOnce_ForwardsCallbackToCore(t *testing.T) {
	core := newFakeCore()
	r := NewRouter("tok", "fake://unused", core, nil)
	// 直接注入 update 處理（繞過 HTTP）
	req := &oncallv1.ActionCallbackRequest{Action: &oncallv1.CallbackAction{
		CallbackId:     "cb-9",
		Kind:           oncallv1.CallbackAction_KIND_REJECT,
		Reason:         "其實是 quota 觸頂，已提額",
		TelegramUserId: "42",
	}}
	r.dispatch(context.Background(), req)

	select {
	case got := <-core.received:
		assert.Equal(t, "cb-9", got.Action.CallbackId)
		assert.Equal(t, oncallv1.CallbackAction_KIND_REJECT, got.Action.Kind)
		assert.Contains(t, got.Action.Reason, "quota")
	default:
		t.Fatal("callback 未轉發到 core")
	}
}

func TestDispatch_CachesOnCoreFailureThenFlushRetries(t *testing.T) {
	core := newFakeCore()
	r := NewRouter("tok", "fake://unused", core, nil)
	core.failNext.Store(true)

	ctx := context.Background()
	req := &oncallv1.ActionCallbackRequest{Action: &oncallv1.CallbackAction{
		CallbackId: "cb-cache", Kind: oncallv1.CallbackAction_KIND_APPROVE,
	}}
	r.dispatch(ctx, req)
	assert.Zero(t, core.callCount.Load(), "core 掛掉不得視為已轉發")

	core.failNext.Store(false) // core 恢復
	remaining := r.FlushPending(ctx)
	assert.Zero(t, remaining, "恢復後 pending 應清空")
	select {
	case got := <-core.received:
		assert.Equal(t, "cb-cache", got.Action.CallbackId)
	default:
		t.Fatal("pending callback 未被重試送達")
	}
}

func TestFlushPending_CoreStillDown_KeepsQueue(t *testing.T) {
	core := newFakeCore()
	r := NewRouter("tok", "fake://unused", core, nil)
	core.failNext.Store(true)

	ctx := context.Background()
	r.dispatch(ctx, &oncallv1.ActionCallbackRequest{Action: &oncallv1.CallbackAction{CallbackId: "a"}})
	r.dispatch(ctx, &oncallv1.ActionCallbackRequest{Action: &oncallv1.CallbackAction{CallbackId: "b"}})

	assert.Equal(t, 2, r.FlushPending(ctx), "core 仍掛：2 筆都留在佇列")
	assert.Equal(t, 2, r.FlushPending(ctx), "持續保留待重試")
}

func TestParseKindAndReason(t *testing.T) {
	assert.Equal(t, oncallv1.CallbackAction_KIND_APPROVE, parseKind("approve:inc-1"))
	assert.Equal(t, oncallv1.CallbackAction_KIND_REJECT, parseKind("REJECT:不是這個原因"))
	assert.Equal(t, oncallv1.CallbackAction_KIND_SNOOZE, parseKind("snooze:"))
	assert.Equal(t, oncallv1.CallbackAction_KIND_UNSPECIFIED, parseKind("wat"))

	assert.Equal(t, "不是這個原因", parseReason("reject:不是這個原因"))
	assert.Empty(t, parseReason("approve"), "無冒號無原因")
}

func TestRun_LogOnlyReturnsImmediately(t *testing.T) {
	r := NewRouter("", "", newFakeCore(), nil)
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	require.NoError(t, r.Run(ctx), "log-only Run 應立即返回 nil")
}

func TestFetchOnce_ParsesUpdateJSON(t *testing.T) {
	// 驗證 getUpdates 回應的解析路徑（用假 Telegram server）
	var body string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		body = `{"ok":true,"result":[{"update_id":7,"callback_query":{"id":"cbX","from":{"id":99},"data":"approve:go"}}]}`
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprint(w, body)
	}))
	t.Cleanup(srv.Close)

	core := newFakeCore()
	r := NewRouter("tok", srv.URL, core, nil)
	require.NoError(t, r.FetchOnce(context.Background()))

	select {
	case got := <-core.received:
		assert.Equal(t, "cbX", got.Action.CallbackId)
		assert.Equal(t, oncallv1.CallbackAction_KIND_APPROVE, got.Action.Kind)
		assert.Equal(t, "99", got.Action.TelegramUserId)
		assert.Equal(t, "go", got.Action.Reason)
	default:
		t.Fatal("update 內的 callback 未被轉發")
	}
	assert.EqualValues(t, 8, r.offset, "offset 應推進到 update_id+1")
}
