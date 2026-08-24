"""混合檢索（algs/knowledge-flywheel.md §D.1）。

語意嵌入相似度 ＋ metadata 過濾並用：
service / cluster / severity 為等值過濾；time_range 為 (start_ts, end_ts) 區間。
回傳附 cosine 相似度排名，top_k 由呼叫端決定。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from oncall_core.logging import get_logger
from oncall_core.memory.embeddings import (
    EmbeddingProvider,
    HashEmbeddingProvider,
    cosine_similarity,
)
from oncall_core.redact import redact_text
from oncall_core.store import KnowledgeChunk, Store

log = get_logger(__name__)


@dataclass(slots=True)
class SearchFilters:
    """§D.1 metadata 過濾器；None 表示不限制。"""

    service: str | None = None
    cluster: str | None = None
    severity: str | None = None
    time_range: tuple[float, float] | None = None  # (start_ts, end_ts) 含端點
    sources: list[str] = field(default_factory=list)  # 空 = 全部來源


@dataclass(slots=True)
class SearchResult:
    chunk: KnowledgeChunk
    score: float  # cosine 相似度 [-1, 1]，降冪排名


def search_knowledge(
    store: Store,
    query: str,
    filters: SearchFilters | None = None,
    top_k: int = 5,
    provider: EmbeddingProvider | None = None,
) -> list[SearchResult]:
    """以嵌入相似度檢索知識庫；metadata 過濾先於相似度計算。"""
    filters = filters or SearchFilters()
    provider = provider or HashEmbeddingProvider()

    candidates = store.query_knowledge_chunks(
        service=filters.service,
        cluster=filters.cluster,
        severity=filters.severity,
        time_range=filters.time_range,
        sources=filters.sources,
    )
    if not candidates:
        return []

    # 查詢文字同樣過遮蔽層再嵌入（一致性；也防查詢本身攜帶金鑰被記 log）
    qvec = provider.embed(redact_text(query))
    scored = [
        SearchResult(chunk=c, score=cosine_similarity(qvec, c.embedding_vector)) for c in candidates
    ]
    scored.sort(key=lambda r: r.score, reverse=True)
    top = scored[:top_k]
    log.debug(
        "knowledge search", query_len=len(query), candidates=len(candidates), returned=len(top)
    )
    return top
