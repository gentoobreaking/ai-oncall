# ai-oncall — AI On-call 副駕駛

接收 AlertManager 警報後自動完成「進入狀況前的 context 收集」：拉取相關指標、
近期部署、HPA 擴縮容軌跡與錯誤 log，以 RAG 對照歷史事故與 runbook，產出含
原因假說排序的結構化分診報告推播到 Telegram。破壞性動作一律需要人類批准；
事故結束後自動產出 postmortem 草稿並將結論沉澱回知識庫。

核心原則：**AI 只建議與執行被批准的動作；判斷責任在人。**

## Features

- 警報接收：shared secret 認證＋fingerprint 冪等（AM 重試不產生重複 Incident）
- Context 收集：Prometheus／部署紀錄／HPA 副本數軌跡／Loki error log 四路併發，失敗降級標注
- 警報風暴聚合：5 分鐘窗內 `cluster`/`service`/`severity` 交集 ≥2 併入同一 Incident
- 歷史比對（RAG）：hash embedding 起步（離線可測），可切換 OpenAI embedding；
  metadata 過濾（service/cluster/severity/time_range）
- 分診管線：LLM 輸出 JSON schema 驗證→帶錯誤重問一次→再失敗降級為純 context 推播
- 批准閘門：read-only 自動執行；mutating 三段式（dry-run→批准→執行），
  逾時沿排班升級鏈 primary→secondary→manager
- Runbook 執行器：冪等、逐步回報失敗即停、輸出金鑰樣式打碼、原始輸出存加密稽核檔
- Postmortem：時間線彙整草稿、action items 追蹤與逾期提醒、定稿 commit 至 incidents repo
- 知識飛輪：拒絕當下一句話原因即時入 RAG；定稿結論入库供下次分診引用
- 時間線防篡改：SHA256 鏈式雜湊，`verify_chain()` 可定位竄改事件
- Shadow Mode：`SHADOW_MODE=1` 全管線運行但不推播不執行；≥30 份人工評分達標才允許關閉
- 成本護欄：每 Incident LLM 呼叫次數（預設 6）與 token 上限，超限自動降級

## Architecture

```
┌──────────────────────────────┐
│ oncall-gate (Go)             │  生產網段 sidecar：無 AI、無狀態依賴
│  AlertManager webhook ──▶ ingest（認證/冪等/正規化）
│  context 收集器 fan-out ──▶ Prometheus / deployments / HPA / Loki
│  Telegram 傳輸層（送訊息/callback 轉發）
└──────────┬────────── ▲───────┘
           │ gRPC (proto/oncall/v1)   │ DeliverNotification / CollectContext
┌──────────▼──────────┐       └───────┐
│ oncall-core (Python) │              │
│  incident 狀態機＋SQLite store      │
│  memory RAG ＋ brain 分診引擎       │
│  runbook 批准閘門 ＋ executor ★    │  ← 全系統唯一碰生產環境的模組
│  postmortem ＋ evalkit 評測         │
└──────────┬───────────┘
┌──────────▼───────────┐
│ oncall-ui (Python)    │  唯讀網頁，綁 127.0.0.1，資料源僅 core 的 readapi
└───────────────────────┘
```

- 語言邊界：Go 負責管線（常駐小、goroutine fan-out）、Python 負責智能（RAG/LLM 生態）
- 兩側只透過 proto 契約通訊；gate 內禁止 AI/RAG 套件、core 內禁止直接碰 Prometheus/Loki
- 斷線韌性：gate 掛了警報暫存由 AM 重試；core 掛了 gate 回 502，恢復後補分診

## Project Structure

```
proto/oncall/v1/        gRPC 契約（單一事實來源）
gate/                   Go 管線層
  cmd/gate/             進入點（HTTP server + gRPC client）
  internal/config/      環境變數設定載入
  internal/ingest/      webhook 認證/冪等/正規化
  internal/collect/     context 收集器（prometheus/deploys/scaling/logs）
  internal/tgtransport/ Telegram 傳輸層
  internal/metrics/     /metrics 計數器
core/                   Python 智能層（uv 管理）
  src/oncall_core/
    store.py            SQLite WAL store＋migration 機制
    grpc_servicer.py    gate→core gRPC 介面
    incident/           狀態機、風暴聚合、雜湊鏈
    memory/             RAG indexer/search/embeddings
    brain/              分診編排、schema 驗證、providers、token 預算
    runbook/            YAML 解析、批准閘門
    executor/           冪等執行器＋redaction（唯一碰生產環境）
    interact/, schedule/, postmortem/, evalkit/, shadow/, readapi/
ui/                     唯讀 Web（FastAPI + Jinja2 + htmx）
deploy/                 systemd units / docker-compose / .env 範本
docs/deploy.md          從零部署指南（含 WireGuard/Tailscale 組網與安全驗證）
```

## Requirements

| 工具 | 版本 |
|---|---|
| Go | ≥ 1.24 |
| Python | ≥ 3.11（搭配 [uv](https://docs.astral.sh/uv/)） |
| protoc / buf | 最新版 |
| protoc-gen-go / protoc-gen-go-grpc | 安裝於 `~/go/bin` |

```bash
go install google.golang.org/protobuf/cmd/protoc-gen-go@latest
go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.5.1
```

> 若沙箱/權限限制無法寫入預設 build cache，專案 Makefile 已使用
> `GOCACHE=$HOME/go/.cache/go-build`。

## Quick Start

```bash
# 1. proto 契約檢查與雙側 stubs 產生
make proto-lint && make proto-gen

# 2. 啟動 core（gRPC :50051）
cd core && uv sync
uv run python -m oncall_core --db data/oncall.db --addr 127.0.0.1:50051

# 3. 另開終端：建置並啟動 gate（webhook :8080）
cd gate && make gate-build
SHARED_SECRET=dev-secret CORE_ADDR=127.0.0.1:50051 ./bin/gate

# 4. 送一筆警報
curl -X POST http://127.0.0.1:8080/alerts \
  -H "Authorization: Bearer dev-secret" \
  -d '{"alerts":[{"fingerprint":"demo-1","status":"firing",
       "labels":{"alertname":"HighLatency","service":"api","severity":"critical"}}]}'

# 5. 唯讀 UI（需 core 先啟動 readapi；見下方 Configuration）
```

未設定 `TELEGRAM_BOT_TOKEN` 時推播自動降級為 log-only，不影響其餘流程。

## Configuration

### gate（環境變數）

| 變數 | 必填 | 預設 | 說明 |
|---|---|---|---|
| `SHARED_SECRET` | ✅ | — | webhook Bearer token（缺漏拒絕啟動） |
| `CORE_ADDR` | ✅ | — | core gRPC 位址 |
| `LISTEN_ADDR` | | `127.0.0.1:8080` | webhook HTTP 監聽 |
| `PROMETHEUS_URL` | | `http://127.0.0.1:9090` | |
| `LOKI_URL` | | `http://127.0.0.1:3100` | |
| `DEPLOYMENTS_PATH` | | （停用） | 部署清單 JSONL 檔 |
| `COLLECT_TIMEOUT` | | `20s` | context 收集總逾時 |
| `TELEGRAM_BOT_TOKEN` | | （log-only 降級） | |

### core / ui

- `SHADOW_MODE=1`：影子模式（上線前必開）
- `READAPI_URL`（ui）：readapi 位址，預設 `http://127.0.0.1:8090`
- 完整範本見 `deploy/*.env.example`

## API

readapi（唯讀，僅綁 127.0.0.1，所有路由僅 GET）：

| Endpoint | 說明 |
|---|---|
| `GET /api/incidents?status=&page=&page_size=&sort=` | 事故清單（分頁/篩選/排序） |
| `GET /api/incidents/{id}` | 詳情（時間線＋最新分診假設） |
| `GET /api/action-items` | 追蹤清單 |
| `GET /api/runbooks` | 已索引 runbook 清單 |
| `GET /api/stats` | 狀態統計／逾期數／知識庫大小 |
| `GET /metrics`（gate） | 請求/認證失敗/冪等/上游錯誤計數 |

gRPC service `oncall.v1.OncallService`：`ReportIncident` /
`DeliverNotification` / `ActionCallback` / `CollectContext`
契約定義見 `proto/oncall/v1/oncall.proto`。

## Data Model

SQLite（WAL）主要表：

- `incidents` — id/fingerprint(冪等)/status(open→investigating→mitigated→resolved)/labels
- `timeline` — 逐筆事件含 `prev_hash`/`hash` 防篡改鏈
- `predictions` — 分診紀錄（hypotheses/actions/prompt_version/tokens_used）
- `knowledge_chunks` — RAG 片段（source/ref_id/metadata/embedding）
- `action_items` — 修正事項追蹤（owner/due_ts/status）
- `executed_actions` — executor 冪等登記
- `shadow_scores` — 影子報告人工評分

Migration 以 `schema_migrations` 表記版本，啟動時自動補齊。

## Testing

```bash
make test                          # gate（go test）
cd core && uv run pytest -q        # core 144 tests（單元＋整合＋e2e）
cd ui   && uv run pytest -q        # ui 7 tests
```

- 全套離線可跑：LLM 以腳本化 FakeProvider 注入，不打真 API
- `tests/test_t019_e2e.py` 對照 spec §5 十五條上線標準，模組 docstring 含對照表
- 跨 process 契約測試會啟動真實 core daemon 與 Go gate binary
  （需先 `make gate-build`，否則自動 skip）
- gate 測試支援 `-race`

## Deployment

完整從零部署步驟（WireGuard/Tailscale 組網、systemd units、AlertManager 對接、
公網掃描安全驗證）見 [`docs/deploy.md`](docs/deploy.md)；容器範本見
[`deploy/docker-compose.yml`](deploy/docker-compose.yml)。

要點：

- gate 部署於看得到生產環境的網段；core/ui 可跑在家裡/NAS
- gate↔core gRPC 必須走 WireGuard/Tailscale 內網或 mTLS，**禁止明文暴露公網**
- ui 對外一律經反向代理認證；未授權直掃不得觸及

## Security

- webhook 強制 `Authorization: Bearer <SHARED_SECRET>`（constant-time 比較），
  失敗計入 `/metrics` 且不消耗下游資源
- payload 上限 1MB、per-IP rate limiting
- mutating 動作三段式鐵律：dry-run → 人類批准 → 執行；executor 對未通過
  schema 驗證的輸入硬拒絕，即使人類手動批准
- executor 出口遮蔽：Bearer/AWS/GitHub/JWT/連線字串等 11 類金鑰樣式打碼；
  原始輸出僅存本地 Fernet 加密稽核檔（保留期預設 90 天）
- 時間線 SHA256 雜湊鏈，竄改可偵測並定位
- secrets 一律走環境變數；`.env*`/`*.pem`/`*.key` 已列 gitignore 與沙箱 denyWrite

## Limitations

- LLM provider 目前僅有 OpenAI 相容介面定義與 fake 實作；線上端點未經生產驗證
- kubectl/shell 執行 adapter 提供介面與 dry-run 注入邏輯，實際部署需提供生產版 runner
- 風暴聚合 v1 僅標籤交集，文字嵌入相似度留待 v2
- ICS 排班為最小解析器；API 型排班來源未實作
- `/metrics` 為 text/plain 計數器，尚未接入 Prometheus client 完整格式
- 冪等儲存為 process 內 TTL map，gate 重啟後依賴 AM 重試與 core 端去重兜底

## Development Guide

```bash
make help        # 列出所有目標
make lint        # buf lint + go vet (+golangci-lint)
make test        # go test
make proto-gen   # 改 proto 後同步重生雙側 stubs（同一 commit 內更新兩側）
```

品質閘門：core/ui 以 `ruff check` + `ruff format` + `pyright` 歸零為準；
gate 以 `go vet` + `go test -race` 為準。

架構鐵律（違者 CI 測試會擋下）：

- `executor/` 是唯一碰生產環境的套件，其他模組禁止 import
- gate 內禁止 AI/RAG 套件；core 內禁止直接呼叫 Prometheus/Loki HTTP
- `ui/` 無寫入端點、不碰 SQLite 檔案，取數只走 readapi

## License

本專案採用 Apache License 2.0 授權，完整條款見 [`LICENSE`](LICENSE)。
