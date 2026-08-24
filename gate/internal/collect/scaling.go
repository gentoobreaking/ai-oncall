// HPA/autoscaler 活動軌跡收集器（spec F2）。
//
// 「量暴漲事故」的關鍵訊號：事故前副本數變化（如 4→12）。
// v1 從 Prometheus 查 kube_deployment 系列重組軌跡；
// 之後可換 Kubernetes API client，介面不變。
package collect

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"sort"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// ScalingClient 以 Prometheus instant query 取得
// 事故窗前後的 deployment 副本數變化軌跡。
type ScalingClient struct {
	// PromBaseURL Prometheus 端點（與 PrometheusClient 同叢集）
	PromBaseURL string
	Client      *http.Client

	// ServiceLabel 對應警報 labels 的服務務鍵（預設 "service"）
	ServiceLabel string
}

func (s *ScalingClient) Name() string { return "scaling" }

// Collect 重組 since 前後每個 replica 變化點：
//
//	max_over_time(kube_deployment_status_replicas{service="x"}[30s]) 的離散取樣，
//	相鄰樣本不同即視為一次擴縮容事件。
func (s *ScalingClient) Collect(ctx context.Context, labels map[string]string, since, until time.Time) (bundleFragment, error) {
	var frag bundleFragment
	svc := labels[s.serviceLabel()]
	if svc == "" {
		// 無 service 標籤無法定位 workload——不算失敗，只是無資料
		return frag, nil
	}

	client := s.Client
	if client == nil {
		client = &http.Client{Timeout: 10 * time.Second}
	}

	expr := fmt.Sprintf(`kube_deployment_status_replicas{service=%q}`, svc)
	points, err := s.queryRangePoints(ctx, client, expr, since.Add(-5*time.Minute), until)
	if err != nil {
		return bundleFragment{}, err
	}
	if len(points) < 2 {
		return frag, nil
	}

	sort.Slice(points, func(i, j int) bool { return points[i].ts < points[j].ts })
	for i := 1; i < len(points); i++ {
		from, to := points[i-1].replicas, points[i].replicas
		if from != to {
			frag.scaling = append(frag.scaling, &oncallv1.ScalingEvent{
				Service:      svc,
				ReplicasFrom: int32(from),
				ReplicasTo:   int32(to),
				AtUnix:       int64(points[i].ts),
				Reason:       scalingReason(from, to),
			})
		}
	}
	return frag, nil
}

func (s *ScalingClient) serviceLabel() string {
	if s.ServiceLabel != "" {
		return s.ServiceLabel
	}
	return "service"
}

func scalingReason(from, to float64) string {
	if to > from {
		return fmt.Sprintf("scaled out %d→%d (HPA 或手動擴容)", int64(from), int64(to))
	}
	return fmt.Sprintf("scaled in %d→%d", int64(from), int64(to))
}

type replicaPoint struct {
	ts       float64
	replicas float64
}

func (s *ScalingClient) queryRangePoints(ctx context.Context, client *http.Client, expr string, since, until time.Time) ([]replicaPoint, error) {
	u, err := url.Parse(s.PromBaseURL + "/api/v1/query_range")
	if err != nil {
		return nil, err
	}
	q := u.Query()
	q.Set("query", expr)
	q.Set("start", since.Format(time.RFC3339))
	q.Set("end", until.Format(time.RFC3339))
	q.Set("step", "15s")
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
				Values [][]any `json:"values"`
			} `json:"result"`
		} `json:"data"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
		return nil, err
	}
	if body.Status != "success" {
		return nil, fmt.Errorf("prometheus status=%s", body.Status)
	}

	var pts []replicaPoint
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
			var replicas float64
			fmt.Sscanf(valStr, "%g", &replicas)
			pts = append(pts, replicaPoint{ts: ts, replicas: replicas})
		}
	}
	return pts, nil
}
