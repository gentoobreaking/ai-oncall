"""測試用 fake provider——可控成敗與回應，不打真 API。"""

from __future__ import annotations

import threading

from oncall_core.brain.providers.base import (
    CompletionRequest,
    CompletionResult,
    ProviderError,
)


class FakeProvider:
    """腳本化回應：responses 佇列逐筆彈出；空時用 default。

    fail_next > 0 時連續拋 ProviderError 模擬故障。
    """

    def __init__(
        self,
        name: str = "fake",
        responses: list[str] | None = None,
        default_reply: str = "ok",
        tokens_per_reply: int = 100,
    ) -> None:
        self._name = name
        self._responses = list(responses or [])
        self._default = default_reply
        self._tokens = tokens_per_reply
        self.fail_next = 0
        self.call_count = 0
        self.last_prompt: str | None = None
        self._lock = threading.Lock()

    @property
    def name(self) -> str:
        return self._name

    def complete(self, request: CompletionRequest) -> CompletionResult:
        with self._lock:
            self.call_count += 1
            if self.fail_next > 0:
                self.fail_next -= 1
                raise ProviderError(f"{self._name}: simulated failure")
            text = self._responses.pop(0) if self._responses else self._default
            self.last_prompt = request.prompt
        return CompletionResult(
            text=text,
            tokens_used=self._tokens,
            provider_name=self._name,
            model=f"{self._name}-model",
        )
