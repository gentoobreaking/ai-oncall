// Package collect 依警報標籤併發收集現場 context（F2）。
//
// 四路 goroutine fan-out：prometheus / deploys / scaling / logs，
// 整體逾時保護；任一路失敗不拖垮其他——失敗區塊在 ContextBundle
// 的 degraded_sources 標注 unavailable（algs/triage-pipeline.md §A.5 降級模式），
// 分診報告必須明列缺漏，禁止 LLM 幻覺補完。
package collect

import (
	"context"
	"sync"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

// Collector 是單路收集器的介面（各實作注入，測試以 fake 取代）。
type Collector interface {
	// Name 回傳來源名稱（degraded_sources 標注用）。
	Name() string
	// Collect 執行收集。回傳錯誤即視為該路失敗，結果不採用。
	Collect(ctx context.Context, labels map[string]string, since, until time.Time) (bundleFragment, error)
}

// bundleFragment 是單一收集器產出的 ContextBundle 片段。
// 各收集器只填自己的欄位，聚合時合併。
type bundleFragment struct {
	metrics     []*oncallv1.MetricSeries
	deployments []*oncallv1.DeploymentEvent
	scaling     []*oncallv1.ScalingEvent
	logs        []*oncallv1.LogSummary

	// 各收集器只填自己的欄位，聚合時合併
}

func (f bundleFragment) merge(b *oncallv1.ContextBundle) {
	b.Metrics = append(b.Metrics, f.metrics...)
	b.Deployments = append(b.Deployments, f.deployments...)
	b.ScalingEvents = append(b.ScalingEvents, f.scaling...)
	b.LogSummaries = append(b.LogSummaries, f.logs...)
}

// Result 是 fan-out 的總結果。
type Result struct {
	Bundle *oncallv1.ContextBundle
	// Degraded 為 true 表示至少一路失敗（§A.5）
	Degraded bool
}

// FanOut 併發執行所有 collector，等待全部完成或整體逾時。
//
// 整體逾時保護：每路繼承帶 deadline 的 ctx；單路慢不拖垮其他——
// 先完成者照常收錄，逾時/出錯者標注 unavailable。
func FanOut(ctx context.Context, collectors []Collector, labels map[string]string, since, until time.Time, timeout time.Duration) *Result {
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	bundle := &oncallv1.ContextBundle{
		CollectedAtUnix: time.Now().Unix(),
	}

	var (
		wg          sync.WaitGroup
		mu          sync.Mutex
		degraded    degradedList
	)

	for _, c := range collectors {
		wg.Add(1)
		go func(c Collector) {
			defer wg.Done()
			frag, err := c.Collect(ctx, labels, since, until)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				degraded.add(c.Name(), err)
				return
			}
			frag.merge(bundle)
		}(c)
	}
	wg.Wait()

	bundle.DegradedSources = degraded.messages()
	return &Result{Bundle: bundle, Degraded: len(degraded.items) > 0}
}

type degradedItem struct {
	source string
	err    error
	msg    string
}

type degradedList struct {
	mu    sync.Mutex
	items []degradedItem
}

func (d *degradedList) add(source string, err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.items = append(d.items, degradedItem{source: source, err: err})
}

func (d *degradedList) messages() []string {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]string, 0, len(d.items))
	for _, it := range d.items {
		out = append(out, it.source+": unavailable ("+it.err.Error()+")")
	}
	return out
}
