// callback 轉發：Telegram update（long-polling）→ gRPC ActionCallback → core。
//
// core 不可達時：事件進入待重試佇列，背景迴圈持續重送直到成功——
// 使用者的批准/拒絕不可因 core 暫時掛掉而靜默消失。
package tgtransport

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// CoreForwarder 是 core ActionCallback 的最小介面。
type CoreForwarder interface {
	ActionCallback(ctx context.Context, req *oncallv1.ActionCallbackRequest) (*oncallv1.ActionCallbackResponse, error)
}

// Router 接收 Telegram updates、抽出 callback_query、轉發 core。
type Router struct {
	token   string
	apiBase string
	client  *http.Client
	core    CoreForwarder
	logger  *slog.Logger

	mu         sync.Mutex
	offset     int64 // getUpdates offset（最後處理到的 update_id + 1）
	pending    []*oncallv1.ActionCallbackRequest
	maxPending int
}

// NewRouter 建立 callback router。token 為空時 Run 直接返回（log-only）。
func NewRouter(token, apiBase string, core CoreForwarder, logger *slog.Logger) *Router {
	if logger == nil {
		logger = slog.Default()
	}
	if apiBase == "" {
		apiBase = defaultAPIBase
	}
	return &Router{
		token:      token,
		apiBase:    apiBase,
		client:     &http.Client{Timeout: 35 * time.Second}, // long-polling 需長逾時
		core:       core,
		logger:     logger,
		maxPending: 1000,
	}
}

// telegramUpdate 只取需要的欄位。
type telegramUpdate struct {
	UpdateID      int64 `json:"update_id"`
	CallbackQuery *struct {
		ID   string `json:"id"`
		From struct {
			ID int64 `json:"id"`
		} `json:"from"`
		Data string `json:"data"`
	} `json:"callback_query"`
}

// FetchOnce 執行一輪 getUpdates 並處理收到的 callbacks。
// 測試可直接注入 update JSON；正式由 Run 迴圈呼叫。
func (r *Router) FetchOnce(ctx context.Context) error {
	if r.token == "" {
		return nil // log-only：不觸網
	}
	url := fmt.Sprintf("%s/bot%s/getUpdates?timeout=25&offset=%d", r.apiBase, r.token, r.offset)
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	resp, err := r.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("getUpdates 回 %d", resp.StatusCode)
	}

	var body struct {
		OK     bool             `json:"ok"`
		Result []telegramUpdate `json:"result"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return err
	}
	for _, u := range body.Result {
		r.offset = u.UpdateID + 1
		if u.CallbackQuery == nil {
			continue
		}
		req := &oncallv1.ActionCallbackRequest{
			Action: &oncallv1.CallbackAction{
				CallbackId:     u.CallbackQuery.ID,
				Kind:           parseKind(u.CallbackQuery.Data),
				Reason:         parseReason(u.CallbackQuery.Data),
				RequestId:      parseRequestID(u.CallbackQuery.Data),
				TelegramUserId: fmt.Sprintf("%d", u.CallbackQuery.From.ID),
			},
		}
		r.dispatch(ctx, req)
	}
	return nil
}

// dispatch 轉發單一 callback；失敗則入佇列等待重試。
func (r *Router) dispatch(ctx context.Context, req *oncallv1.ActionCallbackRequest) {
	if _, err := r.core.ActionCallback(ctx, req); err != nil {
		r.enqueue(req)
		r.logger.Warn("core 不可達，callback 進入重試佇列",
			"callback_id", req.Action.CallbackId, "error", err)
	}
}

func (r *Router) enqueue(req *oncallv1.ActionCallbackRequest) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if len(r.pending) >= r.maxPending {
		// 佇列滿：丟最舊（保留時間線一致性已不可能，至少保最新決策）
		r.pending = r.pending[1:]
	}
	r.pending = append(r.pending, req)
}

// FlushPending 重送佇列內所有 pending callbacks；成功者移除。
// 回傳剩餘數量。測試與 RetryLoop 共用。
func (r *Router) FlushPending(ctx context.Context) int {
	r.mu.Lock()
	remaining := make([]*oncallv1.ActionCallbackRequest, 0, len(r.pending))
	queue := r.pending
	r.pending = nil
	r.mu.Unlock()

	for _, req := range queue {
		if ctx.Err() != nil {
			remaining = append(remaining, req)
			continue
		}
		if _, err := r.core.ActionCallback(ctx, req); err != nil {
			remaining = append(remaining, req)
		}
	}
	r.mu.Lock()
	r.pending = append(remaining, r.pending...) // 新到的排後面
	n := len(r.pending)
	r.mu.Unlock()
	return n
}

// Run 是 long-polling 主迴圈：拉 updates + 定期 flush pending。
// token 為空（log-only）時直接返回。ctx 取消即結束。
func (r *Router) Run(ctx context.Context) error {
	if r.token == "" {
		r.logger.Info("tgtransport log-only：callback 迴圈未啟動")
		return nil
	}
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		if err := r.FetchOnce(ctx); err != nil && ctx.Err() == nil {
			r.logger.Error("getUpdates 失敗", "error", err)
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			r.FlushPending(ctx)
		}
	}
}

// parseKind 從 callback_data 解析動作種類："approve:..."/"reject:..."/"snooze:..."
func parseKind(data string) oncallv1.CallbackAction_Kind {
	switch {
	case hasPrefixFold(data, "approve"):
		return oncallv1.CallbackAction_KIND_APPROVE
	case hasPrefixFold(data, "reject"):
		return oncallv1.CallbackAction_KIND_REJECT
	case hasPrefixFold(data, "snooze"):
		return oncallv1.CallbackAction_KIND_SNOOZE
	default:
		return oncallv1.CallbackAction_KIND_UNSPECIFIED
	}
}

// parseReason 取 "kind:" 之後的一句話原因（F9 強制捕獲用）。
func parseReason(data string) string {
	for i := 0; i < len(data); i++ {
		if data[i] == ':' {
			return data[i+1:]
		}
	}
	return ""
}

// parseRequestID 取 verb: 之後、下一個冒號前的 request_id。
// data 格式："approve:{request_id}" / "reject:{request_id}:{reason}"
func parseRequestID(data string) string {
	first := -1
	for i := 0; i < len(data); i++ {
		if data[i] == ':' {
			if first == -1 {
				first = i
			} else {
				return data[first+1 : i]
			}
		}
	}
	if first != -1 && first+1 < len(data) {
		return data[first+1:]
	}
	return ""
}

func hasPrefixFold(s, prefix string) bool {
	if len(s) < len(prefix) {
		return false
	}
	for i := 0; i < len(prefix); i++ {
		a, b := s[i], prefix[i]
		if 'A' <= a && a <= 'Z' {
			a += 'a' - 'A'
		}
		if a != b {
			return false
		}
	}
	return true
}
