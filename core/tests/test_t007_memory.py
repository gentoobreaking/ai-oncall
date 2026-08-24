"""T007 測試：§D.1 混合檢索、§D.2 三來源、§D.5 遮蔽、provider 切換。"""

from __future__ import annotations

from pathlib import Path

import pytest

from oncall_core.memory import (
    HashEmbeddingProvider,
    KnowledgeIndexer,
    SearchFilters,
    search_knowledge,
)
from oncall_core.memory.embeddings import cosine_similarity
from oncall_core.redact import contains_secret, redact_text
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t007.db")


@pytest.fixture()
def indexer(store: Store) -> KnowledgeIndexer:
    return KnowledgeIndexer(store)


# ---------------------------------------------------------------------------
# HashEmbeddingProvider：離線可測、確定性
# ---------------------------------------------------------------------------


def test_hash_embedding_deterministic_and_normalized() -> None:
    p = HashEmbeddingProvider()
    a = p.embed("postgres connection pool exhausted")
    b = p.embed("postgres connection pool exhausted")
    assert a == b, "同文字必得同向量-可重現評測"
    norm = sum(x * x for x in a) ** 0.5
    assert abs(norm - 1.0) < 1e-6 or a == [0.0] * len(a)
    assert len(a) == 256


def test_cosine_similarity_ranking() -> None:
    p = HashEmbeddingProvider()
    q = p.embed("redis timeout")
    near = p.embed("redis timeout on cache tier")
    far = p.embed("kafka consumer lag")
    assert cosine_similarity(q, near) > cosine_similarity(q, far)


# ---------------------------------------------------------------------------
# §D.5 遮蔽層
# ---------------------------------------------------------------------------


def test_redact_masks_common_secret_patterns() -> None:
    samples = {
        "github": "token ghp_abcdefghijklmnopqrstuvwxyz0123456789 in log",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c",
        "conn": "postgres://admin:s3cretpw@db.prod:5432/app",
        "aws": "AKIAIOSFODNN7EXAMPLE",
    }
    for name, s in samples.items():
        masked = redact_text(s)
        assert not contains_secret(masked), f"{name} 未被打碼: {masked}"
        assert "<REDACTED:" in masked


def test_indexing_goes_through_redaction(store: Store, indexer: KnowledgeIndexer) -> None:
    indexer.index_postmortem(
        incident_id="inc-1",
        conclusion="root cause was leaked key ghp_abcdefghijklmnopqrstuvwxyz0123456789",
        service="api",
    )
    chunks = store.query_knowledge_chunks(service="api")
    assert len(chunks) == 1
    assert "ghp_" not in chunks[0].text, "金鑰不得進入向量庫原文"


# ---------------------------------------------------------------------------
# §D.1 檢索：metadata 過濾 + 相似度排名
# ---------------------------------------------------------------------------


def test_search_metadata_filter_service(store: Store, indexer: KnowledgeIndexer) -> None:
    indexer.index_postmortem(
        incident_id="inc-a",
        conclusion="redis memory full evicted keys",
        service="cache",
        cluster="prod",
    )
    indexer.index_postmortem(
        incident_id="inc-b",
        conclusion="postgres deadlock on migration",
        service="db",
        cluster="prod",
    )

    # 純語意會兩者都撈；service 過濾後只剩 cache 相關
    hits_all = search_knowledge(store, "memory eviction redis", SearchFilters(), top_k=10)
    assert len(hits_all) == 2

    hits_cache = search_knowledge(
        store, "memory eviction redis", SearchFilters(service="cache"), top_k=10
    )
    assert len(hits_cache) == 1
    assert hits_cache[0].chunk.service == "cache"


def test_search_time_range_filter(store: Store, indexer: KnowledgeIndexer) -> None:
    indexer.index_postmortem(incident_id="inc-old", conclusion="old incident about disk")
    # 把第一筆推到很早
    store._conn.execute("UPDATE knowledge_chunks SET created_at = created_at - 86400*30")
    store._conn.commit()
    indexer.index_postmortem(incident_id="inc-new", conclusion="new incident about disk")

    import time

    now = time.time()
    hits = search_knowledge(
        store,
        "disk incident",
        SearchFilters(time_range=(now - 3600, now)),
        top_k=10,
    )
    assert len(hits) == 1
    assert hits[0].chunk.ref_id == "inc-new"


def test_search_severity_and_cluster_filters(store: Store, indexer: KnowledgeIndexer) -> None:
    indexer.index_postmortem(
        incident_id="inc-c",
        conclusion="cpu throttling",
        service="api",
        cluster="staging",
        severity="warning",
    )
    hits = search_knowledge(store, "cpu", SearchFilters(severity="critical"), top_k=5)
    assert len(hits) == 0
    hits = search_knowledge(
        store, "cpu", SearchFilters(cluster="staging", severity="warning"), top_k=5
    )
    assert len(hits) == 1


def test_search_returns_ranked_results(store: Store, indexer: KnowledgeIndexer) -> None:
    indexer.index_override(
        incident_id="inc-1", actual_action="restart redis replica", service="cache"
    )
    indexer.index_override(
        incident_id="inc-2", actual_action="raise redis maxmemory", service="cache"
    )
    hits = search_knowledge(store, "redis restart replica fix", top_k=2)
    assert len(hits) == 2
    scores = [h.score for h in hits]
    assert scores == sorted(scores, reverse=True), "應依相似度降冪"


# ---------------------------------------------------------------------------
# §D.2 三個入库來源各有介面與測試
# ---------------------------------------------------------------------------


def test_source_postmortem(store: Store, indexer: KnowledgeIndexer) -> None:
    chunk = indexer.index_postmortem(
        incident_id="inc-pm", conclusion="root cause: bad deploy rev a1b2c3", service="api"
    )
    assert chunk.source == "postmortem"
    assert chunk.ref_id == "inc-pm"
    assert store.count_knowledge_chunks() == 1


def test_source_override(store: Store, indexer: KnowledgeIndexer) -> None:
    """F9：人類否決 AI 建議的一句話即時入庫。"""
    chunk = indexer.index_override(
        incident_id="inc-ov", actual_action="其實是 quota 觸頂-已申請提額", service="api"
    )
    assert chunk.source == "override"
    stored = search_knowledge(store, "quota 提額", top_k=1)
    assert len(stored) == 1 and "override:" in stored[0].chunk.text


def test_source_runbook_reindex(store: Store, indexer: KnowledgeIndexer, tmp_path: Path) -> None:
    rb_dir = tmp_path / "runbooks"
    rb_dir.mkdir()
    (rb_dir / "restart-pod.md").write_text("---\nservice: api\n---\nsteps: kubectl rollout restart")
    (rb_dir / "rollback.yml").write_text("action: rollback\nrisk: mutating")
    (rb_dir / "notes.txt").write_text("should be ignored")

    chunks = indexer.reindex_runbooks(rb_dir)
    assert len(chunks) == 2, ".txt 不入索引"

    # 變更觸發重新索引：同名 runbook 不重複累積
    (rb_dir / "rollback.yml").write_text("action: rollback\nrisk: mutating\nv2: true")
    chunks2 = indexer.reindex_runbooks(rb_dir)
    assert len(chunks2) == 2
    assert store.count_knowledge_chunks() == 2  # 同名覆寫不累積


def test_reindex_nonexistent_dir_is_noop(store: Store, indexer: KnowledgeIndexer) -> None:
    assert indexer.reindex_runbooks("/nonexistent/path") == []
    assert store.count_knowledge_chunks() == 0


# ---------------------------------------------------------------------------
# provider 可切換（hash ↔ openai）
# ---------------------------------------------------------------------------


def test_provider_switchable_openai_construct_offline(store: Store) -> None:
    """openai provider 建構不觸網；embed 才需要端點。"""
    from oncall_core.memory.embeddings import OpenAIEmbeddingProvider

    provider = OpenAIEmbeddingProvider(api_key="test-key", base_url="http://127.0.0.1:9/v1")
    assert provider.name.startswith("openai:")
    # indexer 接受任意 provider（介面注入）
    indexer = KnowledgeIndexer(store, provider=provider)
    import pytest as _pytest

    with _pytest.raises(Exception):  # noqa: B017
        indexer.index_postmortem(incident_id="inc-x", conclusion="offline, no server")


def test_index_and_search_use_same_provider_namespace(store: Store) -> None:
    """不同 provider 的向量不可混檢：search 以 provider 一致性為前提。"""
    indexer = KnowledgeIndexer(store, provider=HashEmbeddingProvider())
    indexer.index_runbook(name="rb-hash", content="restart the pod")
    hits = search_knowledge(store, "restart pod", top_k=3, provider=HashEmbeddingProvider())
    assert all(h.chunk.embedding_provider == "hash" for h in hits)
