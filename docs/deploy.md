# ai-oncall 上線部署指南

> 三服務架構：`oncall-gate`（Go，生產網段）＋ `oncall-core`／`oncall-ui`
> （Python，家裡/NAS）。gate↔core 以 gRPC 通訊，**必須走 WireGuard/Tailscale
> 內網或 mTLS，禁止明文 gRPC 暴露公網**（spec §2.3 鐵律、§5 標準 9）。

## 0. 前置需求

| 節點 | 需求 |
|---|---|
| 生產網段主機 | Go ≥1.24（建置）或直接部署 `gate/bin/gate` binary；可連 Prometheus/Loki/AlertManager |
| 家裡/NAS | Python ≥3.11 + uv；SQLite/RAG 資料目錄；可連外網 LLM API |
| 兩端 | WireGuard 或 Tailscale 已組網、互 ping 得到內網 IP |

---

## 1. 網路：WireGuard/Tailscale 組網

### Tailscale（最快路徑）

1. 兩台機器各自 `tailscale up` 並登入同一 tailnet
2. 取得內網 IP：
   ```bash
   tailscale ip -4   # 例如 gate=100.64.0.2, core=100.64.0.3
   ```
3. **驗證連通**（在 core 機）：
   ```bash
   ping -c 3 100.64.0.2          # gate 的 tailscale IP
   ```

### WireGuard（自架）

標準 wg-quick 配置即可，重點：

```ini
# core 端僅允許來自 gate 內網位址的 50051 連線
[Peer]
PublicKey = <gate-pubkey>
AllowedIPs = 10.8.0.2/32
```

### 上線前安全驗證（spec §5 標準 9）

從公網側（手機熱線/外部 VPS）掃描兩台機器：

```bash
nmap -p 50051,8090,8091 <公網IP>
```

**預期：全部 filtered/closed。** gRPC(50051) 只聽 tailscale/wg 內網 IP；
readapi(8090)/ui(8091) 只聽 127.0.0.1。

---

## 1.5 容器化（推薦路徑）

```bash
make docker-build    # 建置 oncall-gate/core/ui 三映像（alpine base）
SHARED_SECRET=<secret> make docker-up
curl http://127.0.0.1:8080/healthz   # gate
curl http://127.0.0.1:8091/healthz   # ui
```

- 映像：multi-stage、non-root、alpine base；gate 最終層僅單一 binary
- core 資料落 `core-data` volume；readapi/gRPC 端口不映射公網
- 生產環境變數以 env_file/外部環境注入（見 deploy/docker-compose.yml 註解）

以下為 systemd 直接部署路徑。

## 2. oncall-core（Python daemon）

```bash
cd ~/Projects/ai-oncall/core
uv sync                                   # 安裝依賴
mkdir -p data shadow_reports eval_reports audit

# 必填環境變數見 deploy/core.env.example
cp ../deploy/core.env.example .env        # 填入實際值後 source
```

### systemd unit（範本：`deploy/oncall-core.service`）

```bash
sudo cp deploy/oncall-core.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now oncall-core
systemctl status oncall-core              # active (running)
journalctl -u oncall-core -f              # 看 JSON 日誌
```

**驗證**：core 啟動後 log 出現 `oncall-core started`。

---

## 3. oncall-gate（Go daemon）

```bash
cd ~/Projects/ai-oncall/gate
GOCACHE=$HOME/go/.cache/go-build go build -o bin/gate ./cmd/gate
```

環境變數（範本 `deploy/gate.env.example`）：

```bash
SHARED_SECRET=<長隨機字串，與 AM webhook 配置一致>
CORE_ADDR=<core 的 tailscale/wg IP>:50051    # 如 100.64.0.3:50051
LISTEN_ADDR=0.0.0.0:8080                     # webhook 需被 AM 觸及
PROMETHEUS_URL=http://prometheus:9090
LOKI_URL=http://loki:3100
# 多叢集分流（T022）：alert 帶 cluster label 時查詢導向對應端點；
# 未帶 cluster 或查無此叢集 → 用 PROMETHEUS_URL 預設端點
# PROMETHEUS_CLUSTERS=aws-prod=http://prom-aws:9090,gcp-prod=http://prom-gcp:9090
# TELEGRAM_BOT_TOKEN 未設定時自動 log-only 降級
```

### systemd unit（範本：`deploy/oncall-gate.service`）

同上 `systemctl enable --now oncall-gate`。

### AlertManager 對接

```yaml
# alertmanager.yml
route:
  receivers: [oncall]
receivers:
  - name: oncall
    webhook_configs:
      - url: http://<gate>:8080/alerts
        http_config:
          authorization:
            type: Bearer
            credentials: <與 SHARED_SECRET 相同>
```

**驗證**：

```bash
# 無 secret → 401（F17-A）
curl -i -X POST http://gate:8080/alerts -d '{"alerts":[]}' | head -1
# 有 secret → 200/502（core 是否在線）
curl -i -X POST http://gate:8080/alerts -H "Authorization: Bearer $SECRET" \
     -d '{"alerts":[{"fingerprint":"t","status":"firing","labels":{"alertname":"T"}}]}' | head -1
```

---

## 4. oncall-ui（唯讀 Web）

```bash
cd ~/Projects/ai-oncall/ui
uv sync
READAPI_URL=http://127.0.0.1:8090 uv run python -m oncall_ui   # 預設 127.0.0.1:8091
```

對外一律經反向代理認證（nginx/Caddy + SSO/basic auth）。
**驗證**：未經代理直掃 ui port 不得觸及（§5 標準 6）。

---

## 5. proto 版本管理與雙側同步

1. 契約唯一來源：`proto/oncall/v1/oncall.proto`
2. 改契約流程：
   ```bash
   make proto-lint    # buf lint（含 breaking 檢查設定）
   make proto-gen     # 同步重生 Go stubs + Python stubs
   ```
3. Go 側 `go build ./...`、Python 側 `uv run pytest` 都過才能提交；
   兩側 stub 必須在同一 commit 內更新（單一事實來源）
4. `core/src/oncall_core/_proto/oncall/v1/oncall_pb2_grpc.py` 為手寫樣板，
   重生後以 `diff` 對照確認介面語意一致

---

## 6. Shadow Mode 上線流程（F15/標準 11）

1. core 環境加 `SHADOW_MODE=1`——全管線運行但不推播不執行
2. 累積 ≥30 份影子報告於 `shadow_reports/`
3. 人工逐份評分（原因正確/建議可用），寫回統計庫
4. 通過門檻才移除旗標正式上線；品質不達標系統會明確拒絕並說明差距
