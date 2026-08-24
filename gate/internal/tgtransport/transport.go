// Package tgtransport 是 Telegram 的純收發傳輸層（spec §2.2）。
//
// 直推中心定案：本層是唯一 Telegram 出口——core 不直接碰 Telegram API，
// 一律經 gRPC DeliverNotification 請求 gate 代發。
// 本套件不含任何決策語意：批准/拒絕該做什麼是 core/interact 的事；
// 這裡只負責把 callback 原樣轉發給 core。
package tgtransport

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"time"
)

const (
	defaultAPIBase   = "https://api.telegram.org"
	sendAttempts     = 4 // 首次 + 失敗退避重試 3 次
	baseBackoffDelay = 150 * time.Millisecond
	httpTimeout      = 10 * time.Second
)

// Button 是 inline keyboard 按鈕；callback_data 即 proto NotificationButton.callback_id。
type Button struct {
	CallbackID string `json:"callback_data"`
	Text       string `json:"text"`
}

// Sender 送訊息出口。token 為空時進入 log-only 降級模式（驗收標準 3）：
// 所有送訊息呼叫僅記 log，不回錯、不觸網。
type Sender struct {
	token   string
	apiBase string
	client  *http.Client
	logger  *slog.Logger
	now     func() time.Time
	sleep   func(time.Duration)
}

// NewSender 建立 Sender。token 為空字串即啟用 log-only 模式。
func NewSender(token string, logger *slog.Logger) *Sender {
	if logger == nil {
		logger = slog.Default()
	}
	s := &Sender{
		token:   token,
		apiBase: defaultAPIBase,
		client:  &http.Client{Timeout: httpTimeout},
		logger:  logger,
		now:     time.Now,
		sleep:   time.Sleep,
	}
	if token == "" {
		logger.Info("TELEGRAM_BOT_TOKEN 未設定，tgtransport 降級為 log-only 模式")
	}
	return s
}

// LogOnly 回報是否處於降級模式（觀測/測試用）。
func (s *Sender) LogOnly() bool { return s.token == "" }

// telegramResponse 是 Bot API 共用回應殼。
type telegramResponse struct {
	OK          bool   `json:"ok"`
	Description string `json:"description"`
}

// SendMessage 送出訊息（可含 inline 按鈕）。
//
// 失敗指數退避重試 3 次（150ms→300ms→600ms）；
// 僅對暫態錯誤（網路/HTTP 5xx/429）重試，4xx 參數錯誤立即放棄。
func (s *Sender) SendMessage(ctx context.Context, chatID, text, parseMode string, buttons []Button) error {
	if s.LogOnly() {
		s.logger.Info("[log-only] Telegram 訊息未送出",
			"chat_id", chatID, "bytes", len(text), "buttons", len(buttons))
		return nil
	}

	payload := map[string]any{
		"chat_id": chatID,
		"text":    text,
	}
	if parseMode != "" {
		payload["parse_mode"] = parseMode
	}
	if len(buttons) > 0 {
		rows := make([][]map[string]string, 1)
		for _, b := range buttons {
			rows[0] = append(rows[0], map[string]string{
				"text": b.Text, "callback_data": b.CallbackID,
			})
		}
		payload["reply_markup"] = map[string]any{"inline_keyboard": rows}
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("payload marshal: %w", err)
	}

	var lastErr error
	for attempt := 0; attempt < sendAttempts; attempt++ {
		if attempt > 0 {
			delay := baseBackoffDelay << (attempt - 1) // 150ms, 300ms, 600ms
			select {
			case <-ctx.Done():
				return ctx.Err()
			case <-time.After(delay):
			}
		}
		lastErr = s.postJSON(ctx, "/sendMessage", body)
		if lastErr == nil {
			return nil
		}
		var fatal *fatalTGError
		if errors.As(lastErr, &fatal) {
			return lastErr // 參數類錯誤，重試無益
		}
	}
	return fmt.Errorf("sendMessage 重試 %d 次後仍失敗: %w", sendAttempts-1, lastErr)
}

// fatalTGError 標記不可重試的 Telegram API 錯誤（HTTP 4xx 非 429）。
type fatalTGError struct{ desc string }

func (e *fatalTGError) Error() string { return "telegram api 拒絕: " + e.desc }

func (s *Sender) postJSON(ctx context.Context, method string, body []byte) error {
	url := fmt.Sprintf("%s/bot%s%s", s.apiBase, s.token, method)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(body))
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/json")

	resp, err := s.client.Do(req)
	if err != nil {
		return err // 網路層錯誤 → 可重試
	}
	defer resp.Body.Close()

	switch {
	case resp.StatusCode >= 200 && resp.StatusCode < 300:
		return nil
	case resp.StatusCode == http.StatusTooManyRequests:
		return fmt.Errorf("telegram 429 rate limited") // 可重試
	case resp.StatusCode >= 500:
		return fmt.Errorf("telegram %d", resp.StatusCode) // 可重試
	default:
		var tr telegramResponse
		_ = json.NewDecoder(resp.Body).Decode(&tr)
		return &fatalTGError{desc: fmt.Sprintf("HTTP %d %s", resp.StatusCode, tr.Description)}
	}
}

// EscapeMarkdownV2 將純文字轉義為 MarkdownV2 安全文字（Telegram Bot API）。
// 必須轉義集合：'_','*','[',']','(',')','~','`','>','#','+','-','=','|','.','!'
// （位於一般文字段落時；code 區塊內規則不同，本層不產生 code 區塊內文）
func EscapeMarkdownV2(s string) string {
	const special = "_*[]()~`>#+-=.|{}!"
	var b strings.Builder
	b.Grow(len(s))
	for _, r := range s {
		if strings.ContainsRune(special, r) {
			b.WriteByte('\\')
		}
		b.WriteRune(r)
	}
	return b.String()
}
