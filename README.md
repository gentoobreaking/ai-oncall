# ai-oncall — AI On-call 分身

三服務架構（spec.md §2.2）：gate(Go) / core(Python) / ui(Python)，以 proto 契約通訊。

## 目錄

```
proto/          gRPC 契約（單一事實來源）
gate/           oncall-gate：Go 管線層 sidecar
core/           oncall-core：Python 智能層（T005 起）
ui/             oncall-ui：唯讀 Web 服務（T017）
```

## 快速開始

```bash
make proto-lint   # buf lint
make proto-gen    # 產生 Go + Python stubs
make gate-test    # gate 測試
make gate-build   # gate 建置
```

## 需求工具

go ≥1.24、protoc、buf、~/go/bin/{protoc-gen-go,protoc-gen-go-grpc}、python3

---
## License

本專案採用 **Apache License 2.0** 授權。

- 完整授權條款見 [`LICENSE`](LICENSE)（專案根目錄）
- Apache-2.0 官方條款：<https://www.apache.org/licenses/LICENSE-2.0>
- 版權與貢獻者資訊以 LICENSE 檔案為準

> 本專案為研究/模擬用途，授權條款不構成任何投資建議或保證；
> 使用/修改/再散佈前請詳閱 LICENSE 全文。

本專案僅供個人量化研究與教育用途。資料來源（FinMind、TWSE、TPEX）之使用請遵守各平台之服務條款。

Proprietary - All rights reserved.
