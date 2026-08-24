package collect

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/stretchr/testify/assert"
	"github.com/stretchr/testify/require"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// fakeCollector：可控延遲與成敗的測試用收集器。
type fakeCollector struct {
	name    string
	delay   time.Duration
	err     error
	frag    bundleFragment
	started chan<- string // 收集開始時通知（驗證並行性）
}

func (f *fakeCollector) Name() string { return f.name }

func (f *fakeCollector) Collect(ctx context.Context, _ map[string]string, _, _ time.Time) (bundleFragment, error) {
	if f.started != nil {
		f.started <- f.name
	}
	select {
	case <-time.After(f.delay):
	case <-ctx.Done():
		return bundleFragment{}, ctx.Err()
	}
	if f.err != nil {
		return bundleFragment{}, f.err
	}
	return f.frag, nil
}


// 四路「並行」啟動：若為序列執行，總耗時會超過單路延遲總和的一半以上。
func TestFanOut_RunsInParallel(t *testing.T) {
	started := make(chan string, 4)
	slow := 300 * time.Millisecond
	collectors := []Collector{
		&fakeCollector{name: "a", delay: slow, started: started},
		&fakeCollector{name: "b", delay: slow, started: started},
		&fakeCollector{name: "c", delay: slow, started: started},
		&fakeCollector{name: "d", delay: slow, started: started},
	}
	begin := time.Now()
	res := FanOut(context.Background(), collectors, nil,
		time.Now().Add(-time.Hour), time.Now(), 10*time.Second)
	elapsed := time.Since(begin)

	require.False(t, res.Degraded)
	close(started)
	names := make(map[string]bool)
	for n := range started {
		names[n] = true
	}
	assert.Len(t, names, 4, "四路都應被啟動")
	assert.Less(t, elapsed, slow*time.Duration(3),
		"並行應在 ~slow 內完成；序列則需 %s", slow*4)
}

// 單路慢不拖垮其他：整體逾時後，快路結果仍保留、慢路標注 unavailable。
func TestFanOut_SlowPathDoesNotBlockOthers(t *testing.T) {
	fast := &fakeCollector{
		name: "fast",
		frag: bundleFragment{deployments: []*oncallv1.DeploymentEvent{{Service: "api", Revision: "r1"}}},
	}
	slow := &fakeCollector{
		name:  "slow",
		delay: 5 * time.Second, // 遠超整體逾時
		err:   errors.New("timeout"),
	}
	res := FanOut(context.Background(), []Collector{fast, slow}, nil,
		time.Now().Add(-time.Hour), time.Now(), 200*time.Millisecond)

	require.True(t, res.Degraded, "慢路逾時應標注降級")
	assert.NotEmpty(t, res.Bundle.Deployments, "快路結果不應被拖垮捨棄")
	require.Len(t, res.Bundle.DegradedSources, 1)
	assert.Contains(t, res.Bundle.DegradedSources[0], "slow")
	assert.Contains(t, res.Bundle.DegradedSources[0], "unavailable")
}

// §A.5 全失敗 → bundle 空、degraded_sources 全列。
func TestFanOut_AllFail_DegradedMode(t *testing.T) {
	collectors := []Collector{
		&fakeCollector{name: "prometheus", err: errors.New("connection refused")},
		&fakeCollector{name: "deploys", err: errors.New("file not found")},
		&fakeCollector{name: "scaling", err: errors.New("connection refused")},
		&fakeCollector{name: "logs", err: errors.New("connection refused")},
	}
	res := FanOut(context.Background(), collectors, map[string]string{"service": "api"},
		time.Now().Add(-2*time.Hour), time.Now(), time.Second)

	require.True(t, res.Degraded)
	b := res.Bundle
	assert.Empty(t, b.Metrics)
	assert.Empty(t, b.Deployments)
	assert.Empty(t, b.ScalingEvents)
	assert.Empty(t, b.LogSummaries)
	require.Len(t, b.DegradedSources, 4, "四路缺漏全列，供分診報告明示")
	for _, msg := range b.DegradedSources {
		assert.Contains(t, msg, "unavailable")
	}
}

// §A.5 部分失敗 → 缺漏區塊標注 unavailable，其餘正常合併。
func TestFanOut_PartialFailure(t *testing.T) {
	ok := &fakeCollector{
		name: "prometheus",
		frag: bundleFragment{metrics: []*oncallv1.MetricSeries{{Query: "up"}}},
	}
	bad := &fakeCollector{name: "logs", err: errors.New("loki down")}
	res := FanOut(context.Background(), []Collector{ok, bad}, nil,
		time.Now().Add(-time.Hour), time.Now(), time.Second)

	require.True(t, res.Degraded)
	require.Len(t, res.Bundle.Metrics, 1, "成功路正常收錄")
	require.Len(t, res.Bundle.DegradedSources, 1)
	assert.Contains(t, res.Bundle.DegradedSources[0], "logs")
}

// 多路片段正確合併進同一個 bundle。
func TestFanOut_MergesFragments(t *testing.T) {
	a := &fakeCollector{name: "prometheus",
		frag: bundleFragment{metrics: []*oncallv1.MetricSeries{{Query: "q1"}, {Query: "q2"}}}}
	b := &fakeCollector{name: "deploys",
		frag: bundleFragment{deployments: []*oncallv1.DeploymentEvent{{Service: "api"}}}}
	c := &fakeCollector{name: "scaling",
		frag: bundleFragment{scaling: []*oncallv1.ScalingEvent{{Service: "api", ReplicasFrom: 4, ReplicasTo: 12}}}}
	d := &fakeCollector{name: "logs",
		frag: bundleFragment{logs: []*oncallv1.LogSummary{{TotalLines: 42}}}}

	res := FanOut(context.Background(), []Collector{a, b, c, d}, nil,
		time.Now().Add(-time.Hour), time.Now(), time.Second)

	assert.False(t, res.Degraded)
	assert.Len(t, res.Bundle.Metrics, 2)
	assert.Len(t, res.Bundle.Deployments, 1)
	assert.Len(t, res.Bundle.ScalingEvents, 1)
	assert.Len(t, res.Bundle.LogSummaries, 1)
	assert.Empty(t, res.Bundle.DegradedSources)
}

// ---------------------------------------------------------------------------
// scaling.go：HPA 副本數變化軌跡（spec F2）
// ---------------------------------------------------------------------------

func TestScalingReason(t *testing.T) {
	assert.Contains(t, scalingReason(4, 12), "4→12")
	assert.Contains(t, scalingReason(12, 4), "scaled in")
}

// fakeProm：內嵌 HTTP server 回固定 query_range 資料，驗證軌跡重組邏輯。
func TestScalingClient_TrajectoryFromPrometheus(t *testing.T) {
	// 模擬副本數 4→12→8 的時間序列
	values := [][]any{
		{float64(1787500000), "4"},
		{float64(1787500015), "4"},
		{float64(1787500030), "12"},
		{float64(1787500045), "12"},
		{float64(1787500060), "8"},
	}
	payload := fmt.Sprintf(`{"status":"success","data":{"result":[{"metric":{"service":"api"},"values":[%s]}]}}`,
		promValuesJSON(values))
	srv := newTestPromServer(t, payload)

	sc := &ScalingClient{PromBaseURL: srv.URL}
	labels := map[string]string{"service": "api"}
	since := time.Unix(1787500000-3600, 0)
	until := since.Add(2 * time.Hour)

	frag, err := sc.Collect(context.Background(), labels, since, until)
	require.NoError(t, err)
	require.Len(t, frag.scaling, 2, "4→12 與 12→8 各一筆")

	assert.Equal(t, int32(4), frag.scaling[0].ReplicasFrom)
	assert.Equal(t, int32(12), frag.scaling[0].ReplicasTo)
	assert.Contains(t, frag.scaling[0].Reason, "scaled out")
	assert.Equal(t, int32(12), frag.scaling[1].ReplicasFrom)
	assert.Equal(t, int32(8), frag.scaling[1].ReplicasTo)
}

func TestScalingClient_NoServiceLabel_NoError(t *testing.T) {
	sc := &ScalingClient{PromBaseURL: "http://127.0.0.1:1"}
	frag, err := sc.Collect(context.Background(), map[string]string{},
		time.Now().Add(-time.Hour), time.Now())
	require.NoError(t, err, "無 service 標籤是『無資料』而非失敗")
	assert.Empty(t, frag.scaling)
}

// ---------------------------------------------------------------------------
// 測試小工具
// ---------------------------------------------------------------------------

// newTestPromServer 回傳固定 payload 的假 Prometheus。
func newTestPromServer(t *testing.T, payload string) *httptest.Server {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(payload))
	}))
	t.Cleanup(srv.Close)
	return srv
}

func promValuesJSON(values [][]any) string {
	parts := make([]string, 0, len(values))
	for _, v := range values {
		parts = append(parts, fmt.Sprintf("[%v,%q]", v[0], v[1]))
	}
	return strings.Join(parts, ",")
}
