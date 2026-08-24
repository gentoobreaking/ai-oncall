"""LLM providers——多 provider 備援、逾時、熔斷（algs/triage-pipeline.md §A.3/A.4）。

獨立子套件鐵律：這裡只做「怎麼呼叫 LLM」，不做「叫 LLM 做什麼」。
prompt 組裝與 schema 驗證在 brain/triage.py / brain/schema_validator.py（T009）。
"""

from oncall_core.brain.providers.base import (
    CompletionRequest,
    CompletionResult,
    LLMProvider,
    ProviderError,
)
from oncall_core.brain.providers.chain import ProviderChain
from oncall_core.brain.providers.circuit import CircuitBreaker
from oncall_core.brain.providers.fake import FakeProvider

__all__ = [
    "CircuitBreaker",
    "CompletionRequest",
    "CompletionResult",
    "FakeProvider",
    "LLMProvider",
    "ProviderChain",
    "ProviderError",
]
