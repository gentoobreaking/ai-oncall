// Loki log 收集器：拉取錯誤 log 並做行數統計與取樣摘要（F2）。
package collect

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

const (
	logSampleLines  = 50 // 摘要最多保留行數（供 LLM 用，控制 token）
	maxLogLineBytes = 512
)

// LokiClient 以 query_range 拉 error 層級 log。
type LokiClient struct {
	BaseURL string
	Client  *http.Client

	// QueryTemplate 可覆寫預設 LogQL；%s 為 service 名
	QueryTemplate string
}

func (l *LokiClient) Name() string { return "logs" }

// Collect 查詢時間窗內 level=error 的 log，回傳總行數與取樣行。
func (l *LokiClient) Collect(ctx context.Context, labels map[string]string, since, until time.Time) (bundleFragment, error) {
	var frag bundleFragment
	client := l.Client
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}

	query := l.logql(labels)
	u, err := url.Parse(l.BaseURL + "/loki/api/v1/query_range")
	if err != nil {
		return bundleFragment{}, err
	}
	q := u.Query()
	q.Set("query", query)
	q.Set("start", fmt.Sprintf("%d", since.UnixNano()))
	q.Set("end", fmt.Sprintf("%d", until.UnixNano()))
	q.Set("limit", "5000")
	q.Set("direction", "backward")
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return bundleFragment{}, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return bundleFragment{}, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return bundleFragment{}, fmt.Errorf("loki 回 %d", resp.StatusCode)
	}

	var body struct {
		Status string `json:"status"`
		Data   struct {
			Result []struct {
				Values [][]any `json:"values"` // [tsNs, line]
			} `json:"result"`
		} `json:"data"`
	}
	// Loki 可能回大 payload，限制解碼大小防灌爆
	if err := json.NewDecoder(io.LimitReader(resp.Body, 32<<20)).Decode(&body); err != nil {
		return bundleFragment{}, err
	}
	if body.Status != "success" {
		return bundleFragment{}, fmt.Errorf("loki status=%s", body.Status)
	}

	summary := &oncallv1.LogSummary{
		Query: query,
	}
	total := int64(0)
	for _, stream := range body.Data.Result {
		for _, v := range stream.Values {
			if len(v) != 2 {
				continue
			}
			line, ok := v[1].(string)
			if !ok {
				continue
			}
			total++
			if len(summary.SampleLines) < logSampleLines {
				summary.SampleLines = append(summary.SampleLines, truncateLine(line))
			}
		}
	}
	summary.TotalLines = total
	frag.logs = append(frag.logs, summary)
	return frag, nil
}

func (l *LokiClient) logql(labels map[string]string) string {
	if l.QueryTemplate != "" {
		svc := labels["service"]
		if strings.Contains(l.QueryTemplate, "%s") && svc != "" {
			return fmt.Sprintf(l.QueryTemplate, svc)
		}
		return l.QueryTemplate
	}
	if svc := labels["service"]; svc != "" {
		return fmt.Sprintf(`{service=%q} |= level=error`, svc)
	}
	return `{job=~".+"} |= level=error`
}

func truncateLine(s string) string {
	if len(s) > maxLogLineBytes {
		return s[:maxLogLineBytes] + "…[truncated]"
	}
	return s
}
