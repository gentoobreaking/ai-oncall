// AlertManager webhook payload → proto AlertEvent 正規化（F1）。
//
// 缺欄位容錯：AM payload 欄位皆視為可缺席——
//   - status 省略 → 視為 firing（AM 只送 firing/resolved）
//   - fingerprint 省略 → 以排序後的 labels 雜湊導出（E.2 冪等仍可用）
//   - startsAt 解析失敗 → 以收到時間代替
//   - severity label 省略 → SEVERITY_UNSPECIFIED
package ingest

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"sort"
	"strings"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// amPayload 是 AlertManager webhook 的 JSON 結構（僅取需要的欄位）。
type amPayload struct {
	Version  string    `json:"version"`
	Alerts   []amAlert `json:"alerts"`
	Receiver string    `json:"receiver"`
}

type amAlert struct {
	Status       string            `json:"status"`
	Labels       map[string]string `json:"labels"`
	Annotations  map[string]string `json:"annotations"`
	StartsAt     string            `json:"startsAt"`
	EndsAt       string            `json:"endsAt"`
	GeneratorURL string            `json:"generatorURL"`
	Fingerprint  string            `json:"fingerprint"`
}

// Normalize 將 AM payload JSON 正規化為一或多筆 AlertEvent。
// 回傳錯誤僅代表 payload 本身不可解析（非 JSON/結構錯誤）；
// 個別警報的缺欄位以上述容錯規則處理，不使整批失敗。
func Normalize(data []byte, receivedAt time.Time) ([]*oncallv1.AlertEvent, error) {
	var p amPayload
	if err := json.Unmarshal(data, &p); err != nil {
		return nil, fmt.Errorf("payload 非合法 JSON: %w", err)
	}
	if len(p.Alerts) == 0 {
		return nil, fmt.Errorf("payload 無 alerts 陣列")
	}
	events := make([]*oncallv1.AlertEvent, 0, len(p.Alerts))
	for i, a := range p.Alerts {
		events = append(events, normalizeOne(a, receivedAt))
		_ = i
	}
	return events, nil
}

func normalizeOne(a amAlert, receivedAt time.Time) *oncallv1.AlertEvent {
	ev := &oncallv1.AlertEvent{
		Status:       oncallv1.AlertStatus_ALERT_STATUS_FIRING,
		Severity:     severityFromLabels(a.Labels),
		Labels:       a.Labels,
		Annotations:  a.Annotations,
		GeneratorUrl: a.GeneratorURL,
		Fingerprint:  strings.TrimSpace(a.Fingerprint),
		Summary:      a.Annotations["summary"],
		Description:  a.Annotations["description"],
	}
	if strings.EqualFold(a.Status, "resolved") {
		ev.Status = oncallv1.AlertStatus_ALERT_STATUS_RESOLVED
	}
	if ev.Fingerprint == "" {
		ev.Fingerprint = deriveFingerprint(a.Labels)
	}
	if ts, err := time.Parse(time.RFC3339, a.StartsAt); err == nil {
		ev.StartsAtUnix = ts.Unix()
	} else {
		ev.StartsAtUnix = receivedAt.Unix()
	}
	return ev
}

func severityFromLabels(labels map[string]string) oncallv1.Severity {
	switch strings.ToLower(labels["severity"]) {
	case "critical", "crit", "page":
		return oncallv1.Severity_SEVERITY_CRITICAL
	case "warning", "warn":
		return oncallv1.Severity_SEVERITY_WARNING
	case "info", "none":
		return oncallv1.Severity_SEVERITY_INFO
	default:
		return oncallv1.Severity_SEVERITY_UNSPECIFIED
	}
}

// deriveFingerprint：labels 排序後 k=v 串接取 SHA-256 前 16 hex。
func deriveFingerprint(labels map[string]string) string {
	keys := make([]string, 0, len(labels))
	for k := range labels {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		b.WriteString(k)
		b.WriteByte('=')
		b.WriteString(labels[k])
		b.WriteByte(';')
	}
	sum := sha256.Sum256([]byte(b.String()))
	return "derived-" + hex.EncodeToString(sum[:8])
}
