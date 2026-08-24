// Prometheus 指標收集器：以警報標籤查相關時間序列（F2）。
//
// 邊界鐵律（spec §2.2）：只有 gate 允許直接呼叫 Prometheus HTTP。
package collect

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// PrometheusClient 以標籤選擇器拉取近期指標序列。
type PrometheusClient struct {
	BaseURL string
	Client  *http.Client
}

func (p *PrometheusClient) Name() string { return "prometheus" }

// Collect 查詢與 service/alertname 相關的請求速率與延遲序列。
// 任一 query 失敗即回傳錯誤（該路整體視為失敗，§A.5）。
func (p *PrometheusClient) Collect(ctx context.Context, labels map[string]string, since, until time.Time) (bundleFragment, error) {
	var frag bundleFragment
	client := p.Client
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}

	queries := promQueries(labels)
	for _, q := range queries {
		series, err := p.queryRange(ctx, client, q.Expr, since, until)
		if err != nil {
			return bundleFragment{}, fmt.Errorf("query %q: %w", q.Name, err)
		}
		frag.metrics = append(frag.metrics, series)
	}
	return frag, nil
}

type promQuery struct{ Name, Expr string }

// promQueries 依警報標籤組出預設查詢集：
// 有 service 標籤才查 per-service 指標；否則退回全域。
func promQueries(labels map[string]string) []promQuery {
	svc := labels["service"]
	match := "up" // 最小可用查詢；有 service 時聚焦該服務
	var exprs []promQuery
	if svc != "" {
		match = fmt.Sprintf(`up{service=%q}`, svc)
	}
	exprs = append(exprs,
		promQuery{"availability", match},
	)
	if svc != "" {
		exprs = append(exprs,
			promQuery{"request_rate", fmt.Sprintf(`sum(rate(http_requests_total{service=%q}[5m]))`, svc)},
			promQuery{"latency_p99", fmt.Sprintf(`histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket{service=%q}[5m])) by (le))`, svc)},
		)
	}
	return exprs
}

// queryRange 呼叫 /api/v1/query_range，只保留數值樣本。
func (p *PrometheusClient) queryRange(ctx context.Context, client *http.Client, expr string, since, until time.Time) (*oncallv1.MetricSeries, error) {
	u, err := url.Parse(p.BaseURL + "/api/v1/query_range")
	if err != nil {
		return nil, err
	}
	step := (until.Sub(since)) / 60
	if step < 15*time.Second {
		step = 15 * time.Second
	}
	q := u.Query()
	q.Set("query", expr)
	q.Set("start", since.Format(time.RFC3339))
	q.Set("end", until.Format(time.RFC3339))
	q.Set("step", step.String())
	u.RawQuery = q.Encode()

	req, err := http.NewRequestWithContext(ctx, http.MethodGet, u.String(), nil)
	if err != nil {
		return nil, err
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("prometheus 回 %d", resp.StatusCode)
	}

	var body struct {
		Status string `json:"status"`
		Data   struct {
			Result []struct {
				Metric map[string]string `json:"metric"`
				Values [][]any           `json:"values"` // [ts, "value"]
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	if body.Status != "success" {
		return nil, fmt.Errorf("prometheus status=%s", body.Status)
	}

	series := &oncallv1.MetricSeries{
		Query:  expr,
		Labels: firstResultLabels(body.Data.Result),
	}
	for _, r := range body.Data.Result {
		for _, v := range r.Values {
			if len(v) != 2 {
				continue
			}
			ts, ok1 := v[0].(float64)
			valStr, ok2 := v[1].(string)
			if !ok1 || !ok2 {
				continue
			}
			var val float64
			fmt.Sscanf(valStr, "%g", &val)
			series.Points = append(series.Points, &oncallv1.Point{
				TimestampUnix: ts,
				Value:         val,
			})
		}
	}
	return series, nil
}

func firstResultLabels(results []struct {
	Metric map[string]string `json:"metric"`
	Values [][]any           `json:"values"`
}) map[string]string {
	if len(results) == 0 {
		return nil
	}
	return results[0].Metric
}
