"""Token 預算護欄（algs/triage-pipeline.md §A.4 / F11）。

每 Incident 的 LLM 呼叫次數上限（預設 6）與 token 上限；
超限拋 BudgetExceeded，由 triage 降級為純 context 推播。
"""

from __future__ import annotations

import threading

from oncall_core.logging import get_logger

log = get_logger(__name__)

DEFAULT_MAX_CALLS = 6
DEFAULT_MAX_TOKENS = 20_000


class BudgetExceeded(Exception):
    """超過預算——呼叫端不得再打 LLM。"""

    def __init__(self, incident_id: str, reason: str) -> None:
        super().__init__(f"budget exceeded for {incident_id}: {reason}")
        self.incident_id = incident_id
        self.reason = reason


class TokenBudget:
    """單一 Incident 的預算帳本。"""

    def __init__(
        self,
        incident_id: str,
        *,
        max_calls: int = DEFAULT_MAX_CALLS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.incident_id = incident_id
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.calls_used = 0
        self.tokens_used = 0
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 檢查點：LLM 呼叫前必查（§A.4 超限降級）
    # ------------------------------------------------------------------

    def assert_can_spend(self, estimated_tokens: int = 0) -> None:
        with self._lock:
            if self.calls_used >= self.max_calls:
                raise BudgetExceeded(
                    self.incident_id, f"call limit {self.calls_used}/{self.max_calls}"
                )
            # 已達 token 上限：任何後續呼叫都拒絕
            if self.tokens_used >= self.max_tokens:
                raise BudgetExceeded(
                    self.incident_id,
                    f"token limit {self.tokens_used}/{self.max_tokens}",
                )
            projected = self.tokens_used + estimated_tokens
            if projected > self.max_tokens:
                raise BudgetExceeded(
                    self.incident_id,
                    f"token limit {projected}/{self.max_tokens}",
                )

    def record(self, tokens_used: int) -> None:
        """LLM 呼叫後記帳——取消/失敗時已耗 token 仍計入成本統計（§A.3）。"""
        with self._lock:
            self.calls_used += 1
            self.tokens_used += tokens_used

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.max_tokens - self.tokens_used)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "calls_used": self.calls_used,
                "max_calls": self.max_calls,
                "tokens_used": self.tokens_used,
                "max_tokens": self.max_tokens,
            }


class BudgetLedger:
    """全進程的預算登記簿：incident → TokenBudget，供 /metrics 彙報（F12）。"""

    def __init__(
        self,
        *,
        max_calls: int = DEFAULT_MAX_CALLS,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self._defaults = {"max_calls": max_calls, "max_tokens": max_tokens}
        self._budgets: dict[str, TokenBudget] = {}
        self._lock = threading.Lock()

    def budget_for(self, incident_id: str) -> TokenBudget:
        with self._lock:
            if incident_id not in self._budgets:
                self._budgets[incident_id] = TokenBudget(incident_id, **self._defaults)
            return self._budgets[incident_id]

    def totals(self) -> dict[str, int]:
        with self._lock:
            budgets = list(self._budgets.values())
        return {
            "incidents_tracked": len(budgets),
            "llm_calls_total": sum(b.calls_used for b in budgets),
            "llm_tokens_total": sum(b.tokens_used for b in budgets),
            "budget_exceeded_count": sum(
                1 for b in budgets if b.calls_used >= b.max_calls or b.tokens_used >= b.max_tokens
            ),
        }
