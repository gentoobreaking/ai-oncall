"""T013 測試：草稿內容、action items CRUD/逾期提醒、定稿入 RAG、git commit。"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from oncall_core.memory import KnowledgeIndexer
from oncall_core.postmortem import ActionItemStatus, PostmortemWriter
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t013.db")


@pytest.fixture()
def indexer(store: Store) -> KnowledgeIndexer:
    return KnowledgeIndexer(store)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "incidents_repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    return repo


@pytest.fixture()
def writer(store: Store, indexer: KnowledgeIndexer, git_repo: Path) -> PostmortemWriter:
    return PostmortemWriter(store, indexer, incidents_repo_dir=git_repo)


def seed_resolved_incident(store: Store, fingerprint: str = "fp-pm") -> str:
    inc, _ = store.create_incident(
        fingerprint=fingerprint, title="latency spike", labels={"service": "api"}
    )
    store.save_prediction(
        incident_id=inc.id,
        prompt_version="2.1.0",
        hypotheses=[{"cause": "bad deploy rev-99", "confidence": 0.8}],
        actions=[{"action": "rollback", "risk": "mutating"}],
        missing_context=["logs"],
        tokens_used=800,
    )
    store.append_chained_event(inc.id, "step_completed", {"step": "step-0-rollback"})
    for status in ("investigating", "mitigated", "resolved"):
        from oncall_core.incident import transition

        transition(store, inc.id, status)
    return inc.id


# ---------------------------------------------------------------------------
# 草稿內容
# ---------------------------------------------------------------------------


def test_draft_contains_required_sections(
    store: Store, indexer: KnowledgeIndexer, git_repo: Path
) -> None:
    writer = PostmortemWriter(store, indexer, incidents_repo_dir=git_repo)
    incident_id = seed_resolved_incident(store)
    path = writer.draft(incident_id, impact="API p99 延遲 12 分鐘")

    content = path.read_text(encoding="utf-8")
    assert "# Postmortem draft" in content
    # 四個必要區塊
    assert "## Timeline" in content
    assert "## Root cause (manual)" in content and "<TODO" in content  # 人工修正欄
    assert "## Actions taken" in content and "step_completed" in content
    assert "## Impact" in content and "p99" in content
    # 時間線與分診假設有帶入
    assert "bad deploy rev-99" in content
    assert "status_changed" in content


def test_draft_manual_root_cause_filled(writer: PostmortemWriter, store: Store) -> None:
    incident_id = seed_resolved_incident(store)
    path = writer.draft(incident_id, root_cause_manual="quota 觸頂導致限流")
    assert "quota 觸頂導致限流" in path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# action items CRUD + 逾期提醒（F19）
# ---------------------------------------------------------------------------


def test_action_item_crud(writer: PostmortemWriter, store: Store) -> None:
    incident_id = seed_resolved_incident(store)
    item_id = writer.add_action_item(
        incident_id,
        description="補上 quota 告警",
        owner="david",
        due_ts=time.time() + 86400,
    )
    item = writer.get_action_item(item_id)
    assert item is not None and item.status == "open" and item.owner == "david"

    assert writer.update_action_item(item_id, status=ActionItemStatus.DONE)
    assert writer.get_action_item(item_id).status == "done"  # type: ignore[union-attr]
    assert writer.update_action_item(item_id, owner="erin")
    assert writer.get_action_item(item_id).owner == "erin"  # type: ignore[union-attr]


def test_overdue_items_and_reminders(writer: PostmortemWriter, store: Store) -> None:
    incident_id = seed_resolved_incident(store)
    overdue_id = writer.add_action_item(
        incident_id,
        description="逾期事項",
        owner="david",
        due_ts=time.time() - 3600,
    )
    writer.add_action_item(
        incident_id, description="未到期", owner="erin", due_ts=time.time() + 86400
    )

    overdue = writer.overdue_items()
    assert [i.id for i in overdue] == [overdue_id]

    reminded: list[str] = []
    writer.remind_overdue(notify=lambda item: reminded.append(item.id))
    assert reminded == [overdue_id], "只提醒逾期未結者"


# ---------------------------------------------------------------------------
# 定稿：RAG 入庫 + git commit
# ---------------------------------------------------------------------------


def test_finalize_indexes_to_rag_and_commits(
    writer: PostmortemWriter, store: Store, git_repo: Path
) -> None:
    incident_id = seed_resolved_incident(store)
    writer.draft(incident_id, root_cause_manual="quota 觸頂")

    commit = writer.finalize(
        incident_id, conclusion="root cause: quota 觸頂導致限流", service="api"
    )
    assert commit is not None and len(commit) >= 7, "應產生 git commit"
    log = subprocess.run(
        ["git", "-C", str(git_repo), "log", "--oneline"], capture_output=True, text=True
    ).stdout
    assert f"postmortem: {incident_id}" in log

    # 知識飛輪：結論立即可檢索
    from oncall_core.memory import SearchFilters, search_knowledge

    hits = search_knowledge(store, "quota 觸頂限流", SearchFilters(service="api"), top_k=3)
    assert any(h.chunk.source == "postmortem" for h in hits)


def test_finalize_without_repo_still_indexes(store: Store, indexer: KnowledgeIndexer) -> None:
    writer = PostmortemWriter(store, indexer, incidents_repo_dir=None)
    incident_id = seed_resolved_incident(store)
    commit = writer.finalize(incident_id, conclusion="conclusion only")
    assert commit is None
    assert store.count_knowledge_chunks() == 1
