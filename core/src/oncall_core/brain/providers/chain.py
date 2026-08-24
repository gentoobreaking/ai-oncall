"""備援鏈：依序嘗試 providers；單一 provider 包逾時＋熔斷。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError

from oncall_core.brain.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    ProviderError,
)
from oncall_core.brain.providers.circuit import CircuitBreaker
from oncall_core.logging import get_logger

log = get_logger(__name__)


class _GuardedProvider:
    """包一層：熔斷 + 呼叫逾時（thread pool 隔離，provider 內部可無逾時）。"""

    def __init__(self, inner: LLMProvider, timeout: float) -> None:
        self.inner = inner
        self.timeout = timeout
        self.breaker = CircuitBreaker()
        self._pool = ThreadPoolExecutor(max_workers=2)

    def complete(self, request: CompletionRequest) -> CompletionResult:
        if not self.breaker.allow_request():
            raise ProviderError(f"{self.inner.name}: circuit open")
        future = self._pool.submit(self.inner.complete, request)
        try:
            result = future.result(timeout=self.timeout)
        except FutureTimeoutError:
            future.cancel()
            self.breaker.record_failure()
            raise ProviderError(f"{self.inner.name}: timeout after {self.timeout}s") from None
        except ProviderError:
            self.breaker.record_failure()
            raise
        self.breaker.record_success()
        return result


class AllProvidersFailedError(ProviderError):
    """備援鏈全數失敗——呼叫端（triage）應降級為純 context 推播。"""

    def __init__(self, attempts: list[str]) -> None:
        super().__init__(f"all providers failed: {', '.join(attempts)}")
        self.attempts = attempts


class ProviderChain:
    """多 provider 備援：主 provider 失敗自動切下一個。"""

    def __init__(
        self,
        providers: list[LLMProvider],
        *,
        per_provider_timeout: float = 30.0,
    ) -> None:
        if not providers:
            raise ValueError("ProviderChain 需要至少一個 provider")
        self._guarded = [_GuardedProvider(p, per_provider_timeout) for p in providers]

    def complete(self, request: CompletionRequest) -> CompletionResult:
        errors: list[str] = []
        for guarded in self._guarded:
            name = guarded.inner.name
            try:
                result = guarded.complete(request)
                result.attempts = [*errors, name]
                return result
            except ProviderError as exc:
                log.warning("provider failed, falling back", provider=name, error=str(exc))
                errors.append(name)
        raise AllProvidersFailedError(errors)

    def provider_states(self) -> dict[str, str]:
        return {g.inner.name: g.breaker.state for g in self._guarded}
