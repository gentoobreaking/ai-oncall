"""provider 介面與資料模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ProviderError(Exception):
    """單一 provider 呼叫失敗（網路/逾時/拒絕）。"""


@dataclass(slots=True)
class CompletionRequest:
    prompt: str
    system: str = ""
    max_tokens: int = 1024
    temperature: float = 0.2


@dataclass(slots=True)
class CompletionResult:
    text: str
    tokens_used: int
    provider_name: str
    model: str
    # 備援鏈中實際嘗試過的 providers（觀測/除錯用）
    attempts: list[str] = field(default_factory=list)


class LLMProvider(Protocol):
    """所有 provider 的最小介面。"""

    @property
    def name(self) -> str: ...

    def complete(self, request: CompletionRequest) -> CompletionResult:
        """同步補全。失敗拋 ProviderError；逾時由實作自行保護。"""
        ...
