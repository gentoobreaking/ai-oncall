SHELL := /bin/bash
GO := go
GOCACHE ?= $(HOME)/go/.cache/go-build
export GOCACHE

.PHONY: help proto-lint proto-gen gate-build gate-vet gate-test gate-lint build lint test clean

help: ## 列出可用目標
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-14s %s\n", $$1, $$2}'

proto-lint: ## buf lint 檢查 proto 契約
	buf lint

proto-gen: ## 產生 Go stubs（gate/gen）與 Python stubs（core/src/oncall_core/_proto）
	buf generate
	PATH=$(HOME)/go/bin:$(PATH) protoc \
		-I proto \
		--python_out=core/src/oncall_core/_proto \
		--pyi_out=core/src/oncall_core/_proto \
		proto/oncall/v1/oncall.proto

gate-build: ## 建置 gate binary
	cd gate && $(GO) build -o bin/gate ./cmd/gate

gate-vet: ## go vet
	cd gate && $(GO) vet ./...

gate-test: ## gate 單元測試
	cd gate && $(GO) test ./...

gate-lint: gate-vet ## gate lint（vet；golangci-lint 若存在則加跑）
	cd gate && command -v golangci-lint >/dev/null && golangci-lint run || true

build: gate-build ## 全部建置
lint: proto-lint gate-lint ## 全部 lint
test: gate-test ## 全部測試

clean: ## 清除建置產物
	rm -rf gate/bin
