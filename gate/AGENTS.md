# oncall-gate 開發

## 測試 / 建置

```bash
go test ./...
go vet ./...
go build ./cmd/gate
```

## 環境變數

| 變數 | 必填 | 預設 | 說明 |
|---|---|---|---|
| SHARED_SECRET | ✅ | — | webhook Bearer token（F17-A） |
| CORE_ADDR | ✅ | — | core gRPC 位址 |
| LISTEN_ADDR | | 127.0.0.1:8080 | webhook HTTP 監聽 |
| PROMETHEUS_URL | | http://127.0.0.1:9090 | 預設端點 |
| PROMETHEUS_CLUSTERS | | （停用） | 多叢集分流 name=url[,…]（T022） |
| LOKI_URL | | http://127.0.0.1:3100 | |
| DEPLOYMENTS_PATH | | （停用） | 部署清單 JSONL |
| COLLECT_TIMEOUT | | 20s | context 收集總逾時 |
| TELEGRAM_BOT_TOKEN | | （推播停用） | |

## 邊界鐵律（spec §2.2）

- gate 內禁止 import 任何 AI/RAG 套件
- core 內禁止直接呼叫 Prometheus/Loki HTTP——一律經 gate
- 跨服務只走 proto 契約
