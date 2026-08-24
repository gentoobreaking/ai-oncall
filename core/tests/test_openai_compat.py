"""OpenAI 相容 provider 測試：請求形狀、回應解析、錯誤轉譯、env 工廠。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from oncall_core.brain.providers import (
    CompletionRequest,
    OpenAICompatibleProvider,
    ProviderChain,
    ProviderError,
    provider_chain_from_env,
)


class FakeOpenAIServer:
    """假 /chat/completions 端點：記錄請求、回應可控。"""

    def __init__(self, status: int = 200, body: dict | None = None, dynamic_report: bool = False):
        import re

        self.requests: list[dict] = []
        self.status = status
        self.dynamic_report = dynamic_report
        self.body = (
            body
            if body is not None
            else {
                "choices": [{"message": {"content": "triage result"}}],
                "usage": {"total_tokens": 321},
            }
        )
        self._shutdown = False
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                req_body = json.loads(self.rfile.read(length))
                outer.requests.append(
                    {
                        "auth": self.headers.get("Authorization"),
                        "body": req_body,
                    }
                )
                if outer.dynamic_report:
                    # 從 prompt 抽 Incident ID，回傳符合分診 schema 的合法報告
                    msgs = req_body.get("messages", [])
                    text = " ".join(m.get("content", "") for m in msgs)
                    m = re.search(r"Incident: (\S+)", text)
                    incident_id = m.group(1) if m else "unknown"
                    content = json.dumps(
                        {
                            "incident_id": incident_id,
                            "hypotheses": [
                                {"cause": "e2e root cause", "confidence": 0.9, "evidence": ["ctx"]}
                            ],
                            "suggested_actions": [
                                {"action": "investigate logs", "risk": "read-only"}
                            ],
                            "missing_context": [],
                            "prompt_version": "9.9.9",
                        }
                    )
                    payload_body = {
                        "choices": [{"message": {"content": content}}],
                        "usage": {"total_tokens": 100},
                    }
                else:
                    payload_body = outer.body
                payload = json.dumps(payload_body).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format, *args):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture()
def fake_server():
    srv = FakeOpenAIServer()
    yield srv
    srv.stop()


def make_provider(base_url: str) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key="test-key",
        model="test-model",
        base_url=base_url,
    )


def test_complete_parses_response(fake_server: FakeOpenAIServer) -> None:
    p = make_provider(fake_server.url)
    req = CompletionRequest(prompt="triage", system="be brief", max_tokens=100)
    result = p.complete(req)

    assert result.text == "triage result"
    assert result.tokens_used == 321
    assert result.provider_name == "openai-compat:test-model"

    sent = fake_server.requests[0]
    assert sent["auth"] == "Bearer test-key"
    assert sent["body"]["model"] == "test-model"
    assert sent["body"]["messages"][0]["role"] == "system"
    assert sent["body"]["messages"][1]["content"] == "triage"


def test_http_error_translated_to_provider_error(fake_server: FakeOpenAIServer) -> None:
    fake_server.status = 429
    fake_server.body = {"error": "rate limited"}
    p = make_provider(fake_server.url)

    with pytest.raises(ProviderError, match="HTTP 429"):
        p.complete(CompletionRequest(prompt="x"))


def test_malformed_body_raises_provider_error(fake_server: FakeOpenAIServer) -> None:
    fake_server.body = {"unexpected": True}
    p = make_provider(fake_server.url)

    with pytest.raises(ProviderError, match="response shape"):
        p.complete(CompletionRequest(prompt="x"))


def test_requires_api_key() -> None:
    with pytest.raises(ValueError, match="api_key"):
        OpenAICompatibleProvider(api_key="", model="m")


# ---------------------------------------------------------------------------
# 透過 ProviderChain 使用（備援鏈相容）
# ---------------------------------------------------------------------------


def test_works_inside_provider_chain(fake_server: FakeOpenAIServer) -> None:
    chain = ProviderChain([make_provider(fake_server.url)])
    result = chain.complete(CompletionRequest(prompt="hello"))
    assert result.text == "triage result"
    assert result.attempts == ["openai-compat:test-model"]


# ---------------------------------------------------------------------------
# env 工廠：LLM_PROVIDERS 多備援設定
# ---------------------------------------------------------------------------


def test_provider_chain_from_env_multi(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        "deepseek|https://api.deepseek.com/v1|deepseek-chat|sk-1,"
        "ollama|http://100.64.0.5:11434/v1|llama3|ollama",
    )
    chain = provider_chain_from_env()
    states = chain.provider_states()
    assert set(states) == {"deepseek", "ollama"}, "任何 OpenAI 相容端點皆可並列為備援"


def test_provider_chain_from_env_empty_gives_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDERS", raising=False)
    chain = provider_chain_from_env({})
    assert list(chain.provider_states()) == ["fake-default"]


def test_provider_chain_from_env_bad_format(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="LLM_PROVIDERS"):
        provider_chain_from_env({"LLM_PROVIDERS": "only-name"})


def test_real_request_through_env_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：env 設定 → 假 server 實際收到請求。"""
    srv = FakeOpenAIServer()
    monkeypatch.setenv(
        "LLM_PROVIDERS",
        f"local|{srv.url}|llama3|ollama",
    )
    chain = provider_chain_from_env()
    result = chain.complete(CompletionRequest(prompt="ping"))
    assert result.text == "triage result"
    assert srv.requests[0]["body"]["model"] == "llama3"
    srv.stop()
