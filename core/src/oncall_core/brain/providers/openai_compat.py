"""OpenAI 相容 LLM provider——任何支援 /chat/completions 的端點皆可。

涵蓋：OpenAI、DeepSeek、Groq、vLLM、Ollama（/v1）、LM Studio 等。
base_url 可配置，因此「接其他 provider」＝改環境變數，不需改程式碼。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from oncall_core.brain.providers.base import (
    CompletionRequest,
    CompletionResult,
    ProviderError,
)


class OpenAICompatibleProvider:
    """以 OpenAI Chat Completions 協定呼叫任意相容端點。"""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        name: str | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key is required")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._name = name or f"openai-compat:{model}"

    @property
    def name(self) -> str:
        return self._name

    def complete(self, request: CompletionRequest) -> CompletionResult:
        payload = {
            "model": self._model,
            "messages": [],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if request.system:
            payload["messages"].append({"role": "system", "content": request.system})
        payload["messages"].append({"role": "user", "content": request.prompt})

        req = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
            raise ProviderError(f"{self._name}: HTTP {exc.code} {detail}") from None
        except urllib.error.URLError as exc:
            raise ProviderError(f"{self._name}: {exc.reason}") from exc

        try:
            text = body["choices"][0]["message"]["content"]
            tokens = int(body.get("usage", {}).get("total_tokens", 0))
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderError(f"{self._name}: unexpected response shape") from exc
        return CompletionResult(
            text=text or "",
            tokens_used=tokens,
            provider_name=self._name,
            model=self._model,
        )
