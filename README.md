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
