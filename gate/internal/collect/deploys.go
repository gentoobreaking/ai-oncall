// 部署事件收集器：從 JSONL 檔讀取近期部署（F2「最近 deployments」）。
//
// v1 以檔案為來源（DEPLOYMENTS_PATH）；未來可換 CI API 實作同一介面。
// 檔案格式：每行一個 JSON 物件
//
//	{"service":"api","revision":"a1b2c3","deployer":"david","deployed_at":"2026-08-24T08:00:00Z","source":"github-actions"}
package collect

import (
	"bufio"
	"context"
	"encoding/json"
	"os"
	"strings"
	"time"

	oncallv1 "github.com/david/ai-oncall/gate/gen/oncall/v1"
)

type deployRecord struct {
	Service    string    `json:"service"`
	Revision   string    `json:"revision"`
	Deployer   string    `json:"deployer"`
	DeployedAt time.Time `json:"deployed_at"`
	Source     string    `json:"source"`
}

// DeploymentsFile 以 JSONL 檔案為部署來源。
type DeploymentsFile struct {
	Path string
}

func (d *DeploymentsFile) Name() string { return "deploys" }

// Collect 回傳時間窗內、與 service 標籤相關（若提供）的部署事件。
func (d *DeploymentsFile) Collect(_ context.Context, labels map[string]string, since, until time.Time) (bundleFragment, error) {
	var frag bundleFragment
	f, err := os.Open(d.Path) //nolint:gosec // 路徑來自設定檔
	if err != nil {
		return bundleFragment{}, err
	}
	defer f.Close()

	svc := labels["service"]
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 0, 64*1024), 1024*1024)
	for sc.Scan() {
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		var rec deployRecord
		if err := json.Unmarshal([]byte(line), &rec); err != nil {
			return bundleFragment{}, err
		}
		if rec.DeployedAt.Before(since) || rec.DeployedAt.After(until) {
			continue
		}
		if svc != "" && rec.Service != svc {
			continue
		}
		frag.deployments = append(frag.deployments, &oncallv1.DeploymentEvent{
			Service:        rec.Service,
			Revision:       rec.Revision,
			Deployer:       rec.Deployer,
			DeployedAtUnix: rec.DeployedAt.Unix(),
			Source:         rec.Source,
		})
	}
	return frag, sc.Err()
}
