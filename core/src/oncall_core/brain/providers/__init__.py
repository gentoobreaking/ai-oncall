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
from oncall_core.brain.providers.openai_compat import OpenAICompatibleProvider

__all__ = [
    "CircuitBreaker",
    "CompletionRequest",
    "CompletionResult",
    "FakeProvider",
    "LLMProvider",
    "OpenAICompatibleProvider",
    "ProviderChain",
    "ProviderError",
    "provider_chain_from_env",
]


def provider_chain_from_env(env=None) -> ProviderChain:
    """自環境變數組裝備援鏈（逗號分隔多 provider，依序 fallback）。

    每組以 `|` 分隔欄位：name|base_url|model|api_key，例如：
        LLM_PROVIDERS="deepseek|https://api.deepseek.com/v1|deepseek-chat|sk-1,local|http://127.0.0.1:11434/v1|llama3|ollama"
    未設定 LLM_PROVIDERS 時回傳僅含 FakeProvider 的鏈（離線/開發模式）。
    """
    import os

    providers: list[LLMProvider] = []
    if env is not None:
        raw = env.get("LLM_PROVIDERS", "").strip()
    else:
        raw = os.environ.get("LLM_PROVIDERS", "").strip()
    if raw:
        for entry in raw.split(","):
            parts = entry.strip().split("|")
            if len(parts) != 4:
                raise ValueError(
                    f"LLM_PROVIDERS 格式錯誤: {entry!r} - 需 name|base_url|model|api_key"
                )
            name, base_url, model, api_key = parts
            providers.append(
                OpenAICompatibleProvider(api_key=api_key, model=model, base_url=base_url, name=name)
            )
    if not providers:
        providers.append(FakeProvider(name="fake-default"))
    return ProviderChain(providers)
