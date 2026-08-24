"""Embedding providers（§D.1）。

- HashEmbeddingProvider：token 雜湊 bag-of-words，離線可測、零依賴——預設起步
- OpenAIEmbeddingProvider：可切換的線上 provider（建構時才需要網路）

兩者輸出同維度正規化向量；呼叫端只依賴 EmbeddingProvider protocol。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

# 向量維度：hash provider 與 openai 相容層皆以此為準
EMBEDDING_DIM = 256

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]+")


class EmbeddingProvider(Protocol):
    def embed(self, text: str) -> list[float]: ...

    @property
    def name(self) -> str: ...


class HashEmbeddingProvider:
    """確定性雜湊嵌入：每個 token 雜湊到 [0, DIM) 桶並累加，最後 L2 正規化。"""

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def name(self) -> str:
        return "hash"

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            return vec
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % self._dim
            # 符號雜湊降低碰撞偏移
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class OpenAIEmbeddingProvider:
    """OpenAI text-embedding 相容 provider（含任何 OpenAI 格式端點）。

    建構不觸網；embed() 才會發請求。測試可指向假 server。
    """

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-3-small",
        base_url: str = "https://api.openai.com/v1",
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")

    @property
    def name(self) -> str:
        return f"openai:{self._model}"

    def embed(self, text: str) -> list[float]:
        import json as _json
        import urllib.request

        req = urllib.request.Request(
            f"{self._base_url}/embeddings",
            data=_json.dumps({"model": self._model, "input": text}).encode(),
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            import json as _json2

            body = _json2.loads(resp.read())
        vec = body["data"][0]["embedding"]
        return [float(x) for x in vec]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
