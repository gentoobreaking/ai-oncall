package collect

// cluster_test.go（T022）：cluster 感知端點分流。

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestPromEndpointResolution(t *testing.T) {
	urls := map[string]string{
		"aws-prod": "http://prom-aws:9090",
		"gcp-prod": "http://prom-gcp:9090",
	}
	cases := []struct {
		name   string
		labels map[string]string
		want   string
	}{
		{"known cluster", map[string]string{"cluster": "aws-prod", "service": "api"}, "http://prom-aws:9090"},
		{"unknown cluster falls back", map[string]string{"cluster": "moon"}, "http://default:9090"},
		{"no cluster label falls back", map[string]string{"service": "api"}, "http://default:9090"},
		{"empty cluster value falls back", map[string]string{"cluster": ""}, "http://default:9090"},
	}
	for _, tc := range cases {
		if got := promEndpoint("http://default:9090", urls, tc.labels); got != tc.want {
			t.Fatalf("%s: got %s, want %s", tc.name, got, tc.want)
		}
	}
	// nil 分流表 → 恆為預設（回歸：單叢集部署行為不變）
	if got := promEndpoint("http://default:9090", nil, map[string]string{"cluster": "aws-prod"}); got != "http://default:9090" {
		t.Fatalf("nil table must use default, got %s", got)
	}
}

// 驗收 2：alert 帶已知 cluster → 打到對應端點；未知／無 cluster → 預設端點。
func TestCollectRoutesToClusterEndpoint(t *testing.T) {
	var awsHits, defaultHits int
	aws := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		awsHits++
		w.Write([]byte(`{"status":"success","data":{"result":[]}}`))
	}))
	defer aws.Close()
	def := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		defaultHits++
		w.Write([]byte(`{"status":"success","data":{"result":[]}}`))
	}))
	defer def.Close()

	p := &PrometheusClient{
		BaseURL:     def.URL,
		ClusterURLs: map[string]string{"aws-prod": aws.URL},
	}
	ctx := context.Background()
	since, until := time.Now().Add(-time.Hour), time.Now()

	// 已知叢集
	if _, err := p.Collect(ctx, map[string]string{"cluster": "aws-prod", "service": "api"}, since, until); err != nil {
		t.Fatal(err)
	}
	if awsHits == 0 || defaultHits != 0 {
		t.Fatalf("known cluster must route to its endpoint: aws=%d default=%d", awsHits, defaultHits)
	}

	// 無 cluster → 預設
	if _, err := p.Collect(ctx, map[string]string{"service": "api"}, since, until); err != nil {
		t.Fatal(err)
	}
	if defaultHits == 0 {
		t.Fatal("no-cluster alert must hit the default endpoint")
	}
}

// 驗收 3：scaling 收集器同樣分流。
func TestScalingRoutesToClusterEndpoint(t *testing.T) {
	var awsHits int
	aws := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		awsHits++
		w.Write([]byte(`{"status":"success","data":{"result":[{"metric":{"service":"api"},"values":[[1,"4"],[2,"4"],[3,"9"]]}]}}`))
	}))
	defer aws.Close()

	s := &ScalingClient{
		PromBaseURL: "http://127.0.0.1:1", // 故意不可達——打到這裡即失敗
		ClusterURLs: map[string]string{"k8s-east": aws.URL},
	}
	frag, err := s.Collect(context.Background(),
		map[string]string{"cluster": "k8s-east", "service": "api"},
		time.Now().Add(-time.Hour), time.Now())
	if err != nil {
		t.Fatalf("must route to cluster endpoint: %v", err)
	}
	if awsHits == 0 || len(frag.scaling) == 0 {
		t.Fatalf("expected scaling events from cluster endpoint: hits=%d events=%d", awsHits, len(frag.scaling))
	}
}
