"""T008 測試：備援鏈、熔斷、budget、fake providers（不打真 API）。"""

from __future__ import annotations

import pytest

from oncall_core.brain.budget import BudgetExceeded, BudgetLedger, TokenBudget
from oncall_core.brain.providers import (
    CompletionRequest,
    CompletionResult,
    FakeProvider,
    ProviderChain,
)
from oncall_core.brain.providers.chain import AllProvidersFailedError
from oncall_core.brain.providers.circuit import CircuitBreaker


def make_request() -> CompletionRequest:
    return CompletionRequest(prompt="triage this incident", max_tokens=512)


# ---------------------------------------------------------------------------
# 備援鏈
# ---------------------------------------------------------------------------


def test_chain_primary_success_no_fallback() -> None:
    primary = FakeProvider("primary", default_reply="primary reply")
    secondary = FakeProvider("secondary")
    chain = ProviderChain([primary, secondary])

    result = chain.complete(make_request())
    assert result.text == "primary reply"
    assert primary.call_count == 1 and secondary.call_count == 0
    assert result.provider_name == "primary"


def test_chain_falls_back_on_failure() -> None:
    primary = FakeProvider("primary")
    primary.fail_next = 1
    secondary = FakeProvider("secondary", default_reply="secondary reply")
    chain = ProviderChain([primary, secondary])

    result = chain.complete(make_request())
    assert result.text == "secondary reply"
    assert primary.call_count == 1
    assert secondary.call_count == 1


def test_chain_all_fail_raises_clear_exception() -> None:
    def broken(name):
        p = FakeProvider(name)
        p.fail_next = 99
        return p

    a, b, c = broken("a"), broken("b"), broken("c")
    chain = ProviderChain([a, b, c])

    with pytest.raises(AllProvidersFailedError) as exc_info:
        chain.complete(make_request())
    # 明確例外：列出嘗試過的所有 providers
    assert exc_info.value.attempts == ["a", "b", "c"]
    assert all(p.call_count == 1 for p in (a, b, c))


def test_chain_requires_at_least_one_provider() -> None:
    with pytest.raises(ValueError, match="至少一個"):
        ProviderChain([])


def test_chain_per_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """單 provider 卡死 → 逾時切下一個，不拖垮整體。"""
    import time

    class SlowProvider(FakeProvider):
        def complete(self, request: CompletionRequest) -> CompletionResult:
            time.sleep(10)  # 遠超鏈的 per-provider timeout
            raise AssertionError("should not return")

    fast = FakeProvider("fast", default_reply="fast!")
    chain = ProviderChain([SlowProvider("slow"), fast], per_provider_timeout=0.2)
    result = chain.complete(make_request())
    assert result.text == "fast!"


def test_circuit_breaker_opens_after_threshold() -> None:
    clock = {"t": 0.0}
    breaker = CircuitBreaker(threshold=3, cooldown_seconds=30.0, now=lambda: clock["t"])
    assert breaker.allow_request()
    for _ in range(3):
        breaker.record_failure()
    assert breaker.state == CircuitBreaker.OPEN
    assert not breaker.allow_request(), "跳閘後不得再打"

    clock["t"] += 31.0  # 冷卻結束 → half_open 允許試探
    assert breaker.allow_request()
    breaker.record_success()
    assert breaker.state == CircuitBreaker.CLOSED


def test_circuit_open_provider_skipped_in_chain() -> None:
    flaky = FakeProvider("flaky")
    flaky.fail_next = 99
    stable = FakeProvider("stable", default_reply="stable ok")
    chain = ProviderChain([flaky, stable], per_provider_timeout=5.0)

    # 第一輪：flaky 失敗一次，stable 接手
    r1 = chain.complete(make_request())
    assert r1.text == "stable ok"
    # flaky 繼續失敗到熔斷門檻
    for _ in range(3):
        chain.complete(make_request())

    states = chain.provider_states()
    assert states["flaky"] == CircuitBreaker.OPEN
    # 熔斷後 flaky 不再被嘗試（call_count 停滯）
    calls_before = flaky.call_count
    r2 = chain.complete(make_request())
    assert r2.text == "stable ok"
    assert flaky.call_count == calls_before


# ---------------------------------------------------------------------------
# Token budget（§A.3/A.4）
# ---------------------------------------------------------------------------


def test_budget_call_limit_default_6_and_exceeded() -> None:
    budget = TokenBudget("inc-1")  # 預設 max_calls=6
    for _ in range(6):
        budget.assert_can_spend()
        budget.record(tokens_used=100)

    with pytest.raises(BudgetExceeded, match="call limit"):
        budget.assert_can_spend()


def test_budget_token_limit_exceeded() -> None:
    budget = TokenBudget("inc-2", max_calls=100, max_tokens=1000)
    budget.record(tokens_used=900)
    budget.assert_can_spend(estimated_tokens=100)  # 剛好到頂，這通可花

    budget.record(tokens_used=100)  # 實際耗用後已達上限
    with pytest.raises(BudgetExceeded, match="token limit"):
        budget.assert_can_spend()


def test_budget_records_tokens_even_after_failure_path() -> None:
    """§A.3：取消/失敗時已耗 token 仍計入成本統計。"""
    budget = TokenBudget("inc-3", max_tokens=500)
    budget.record(tokens_used=300)
    assert budget.snapshot()["tokens_used"] == 300
    assert budget.remaining_tokens == 200


def test_budget_ledger_totals_for_metrics() -> None:
    ledger = BudgetLedger(max_calls=2, max_tokens=1000)
    b1 = ledger.budget_for("inc-a")
    b1.record(tokens_used=10)
    b2 = ledger.budget_for("inc-b")
    b2.record(tokens_used=20)

    totals = ledger.totals()
    assert totals["incidents_tracked"] == 2
    assert totals["llm_calls_total"] == 2
    assert totals["llm_tokens_total"] == 30
    assert totals["budget_exceeded_count"] == 0


def test_budget_exceeded_is_catchable_for_degradation() -> None:
    """triage 捕捉 BudgetExceeded 後降級為純 context 推播（§A.4）。"""
    ledger = BudgetLedger(max_calls=1)
    budget = ledger.budget_for("inc-dg")
    budget.record(tokens_used=50)
    degraded = False
    try:
        budget.assert_can_spend()
    except BudgetExceeded:
        degraded = True  # 降級路徑：純 context 推播，不再打 LLM
    assert degraded
