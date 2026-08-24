"""T006 測試：狀態機、§A.2 聚合、§E.3 雜湊鏈。"""

from __future__ import annotations

import pytest

from oncall_core.incident import (
    CorrelateAction,
    Correlator,
    HashChain,
    can_transition,
    transition,
    verify_chain,
)
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t006.db")


@pytest.fixture()
def chain(store: Store) -> HashChain:
    return HashChain(store)


# ---------------------------------------------------------------------------
# 狀態機
# ---------------------------------------------------------------------------


def test_legal_transition_path(store: Store, chain: HashChain) -> None:
    inc, _ = store.create_incident(fingerprint="fp-sm")
    assert transition(store, inc.id, "investigating")
    assert transition(store, inc.id, "mitigated")
    assert transition(store, inc.id, "resolved")
    assert store.get_incident(inc.id).status == "resolved"  # type: ignore[union-attr]
    kinds = [r["kind"] for r in store.timeline(inc.id)]
    assert kinds.count("status_changed") == 3


def test_illegal_transitions_rejected_and_recorded(store: Store) -> None:
    inc, _ = store.create_incident(fingerprint="fp-illegal")

    # open → mitigated 跳級：拒絕
    assert not transition(store, inc.id, "mitigated")
    # open → resolved：拒絕
    assert not transition(store, inc.id, "resolved")
    # 狀態不變
    assert store.get_incident(inc.id).status == "open"  # type: ignore[union-attr]

    # 拒絕事件已入時間線（含雜湊鏈）
    events = store.timeline(inc.id)
    rejected = [e for e in events if e["kind"] == "illegal_transition_rejected"]
    assert len(rejected) == 2
    assert verify_chain(store, inc.id).ok


def test_resolved_is_terminal(store: Store) -> None:
    inc, _ = store.create_incident(fingerprint="fp-term")
    for status in ("investigating", "mitigated", "resolved"):
        assert transition(store, inc.status if False else inc.id, status)
    # resolved 為終態
    assert not can_transition("resolved", "open")
    assert not transition(store, inc.id, "open")


def test_transition_table_shape() -> None:
    assert can_transition("open", "investigating")
    assert can_transition("investigating", "mitigated")
    assert can_transition("mitigated", "resolved")
    assert can_transition("investigating", "investigating")  # 迭代分診允許
    assert not can_transition("open", "mitigated")
    assert not can_transition("mitigated", "investigating")


def test_transition_unknown_incident(store: Store) -> None:
    assert not transition(store, "inc-nope", "investigating")


# ---------------------------------------------------------------------------
# §A.2 風暴聚合
# ---------------------------------------------------------------------------


def make_correlator(store: Store) -> Correlator:
    return Correlator(store)


def test_correlate_intersection_ge2_merges(store: Store) -> None:
    c = make_correlator(store)
    r1 = c.ingest_alert(
        fingerprint="fp-a",
        labels={"cluster": "prod", "service": "api", "severity": "critical"},
        summary="latency high",
    )
    assert r1.action is CorrelateAction.CREATED

    # cluster+service 相同（交集 2）→ 併入同一 Incident
    r2 = c.ingest_alert(
        fingerprint="fp-b",
        labels={"cluster": "prod", "service": "api", "severity": "warning"},
        summary="error rate up",
    )
    assert r2.action is CorrelateAction.MERGED
    assert r2.incident.id == r1.incident.id

    # 時間線有 alert_merged，且不重跑分診（此處以時間線種類驗證）
    kinds = [row["kind"] for row in store.timeline(r1.incident.id)]
    assert kinds.count("alert_merged") == 1


def test_correlate_intersection_lt2_creates_new(store: Store) -> None:
    c = make_correlator(store)
    r1 = c.ingest_alert(
        fingerprint="fp-x",
        labels={"cluster": "prod", "service": "api"},
    )
    r2 = c.ingest_alert(
        fingerprint="fp-y",
        labels={"cluster": "prod", "service": "db"},  # 只交集 cluster=1
    )
    assert r2.action is CorrelateAction.CREATED
    assert r2.incident.id != r1.incident.id


def test_correlate_window_expiry_5min(store: Store) -> None:
    c = make_correlator(store)
    r1 = c.ingest_alert(
        fingerprint="fp-w1",
        labels={"cluster": "prod", "service": "api"},
    )
    # 人為把 updated_at 推回 6 分鐘前 → 落窗外
    store._conn.execute(
        "UPDATE incidents SET updated_at = updated_at - 360 WHERE id = ?",
        (r1.incident.id,),
    )
    store._conn.commit()

    r2 = c.ingest_alert(
        fingerprint="fp-w2",
        labels={"cluster": "prod", "service": "api"},
    )
    assert r2.action is CorrelateAction.CREATED
    assert r2.incident.id != r1.incident.id


def test_correlate_mitigated_recorded_not_reopened(store: Store) -> None:
    c = make_correlator(store)
    r1 = c.ingest_alert(
        fingerprint="fp-m0",
        labels={"cluster": "prod", "service": "api"},
    )
    assert transition(store, r1.incident.id, "investigating")
    assert transition(store, r1.incident.id, "mitigated")

    # 同根因新警報進來：只記錄，狀態維持 mitigated、不重開
    r2 = c.ingest_alert(
        fingerprint="fp-m1",
        labels={"cluster": "prod", "service": "api"},
    )
    assert r2.action is CorrelateAction.RECORDED
    assert r2.incident.id == r1.incident.id
    assert store.get_incident(r1.incident.id).status == "mitigated"  # type: ignore[union-attr]

    kinds = [row["kind"] for row in store.timeline(r1.incident.id)]
    assert "alert_merged" in kinds


def test_correlate_resolved_not_matched(store: Store) -> None:
    """resolved 不在聚合候選內——新警報應新建而非復活舊案。"""
    c = make_correlator(store)
    r1 = c.ingest_alert(
        fingerprint="fp-r0",
        labels={"cluster": "prod", "service": "api"},
    )
    for status in ("investigating", "mitigated", "resolved"):
        transition(store, r1.incident.id, status)

    r2 = c.ingest_alert(
        fingerprint="fp-r1",
        labels={"cluster": "prod", "service": "api"},
    )
    assert r2.action is CorrelateAction.CREATED
    assert r2.incident.id != r1.incident.id


# ---------------------------------------------------------------------------
# §E.3 雜湊鏈 + 竄改偵測（spec §5 標準 15）
# ---------------------------------------------------------------------------


def test_hashchain_verify_ok(store: Store, chain: HashChain) -> None:
    inc, _ = store.create_incident(fingerprint="fp-hc")
    for i in range(5):
        chain.append(inc.id, f"event_{i}", {"n": i})
    verdict = verify_chain(store, inc.id)
    assert verdict.ok and verdict.corrupt_id is None


def test_hashchain_tamper_detected_with_position(store: Store, chain: HashChain) -> None:
    inc, _ = store.create_incident(fingerprint="fp-tamper")
    ids = [chain.append(inc.id, f"event_{i}", {"n": i}) for i in range(4)]

    # 竄改第 3 筆（index 2）payload
    store.tamper_timeline_payload(ids[2], {"n": 999})

    verdict = verify_chain(store, inc.id)
    assert not verdict.ok
    assert verdict.corrupt_id == ids[2], "應精確標記損毀位置"


def test_hashchain_detects_deletion_via_break(store: Store, chain: HashChain) -> None:
    """刪除中間一筆會造成鏈斷裂，verify 應標記斷點。"""
    inc, _ = store.create_incident(fingerprint="fp-del")
    ids = [chain.append(inc.id, f"event_{i}", {"n": i}) for i in range(4)]
    store._conn.execute("DELETE FROM timeline WHERE id = ?", (ids[1],))
    store._conn.commit()

    verdict = verify_chain(store, inc.id)
    assert not verdict.ok
    assert verdict.corrupt_id == ids[2], "刪除後的下一筆 prev 對不上即損毀點"


def test_hashchain_genesis_binds_identity_and_time(store: Store, chain: HashChain) -> None:
    from oncall_core.incident.hashchain import genesis_hash

    inc_a, _ = store.create_incident(fingerprint="fp-g1")
    inc_b, _ = store.create_incident(fingerprint="fp-g2")
    # 不同 incident 的 genesis 必不同；同 incident 的 genesis 穩定
    assert genesis_hash(inc_a.id, inc_a.created_at) != genesis_hash(inc_b.id, inc_b.created_at)
    assert genesis_hash(inc_a.id, inc_a.created_at) == genesis_hash(inc_a.id, inc_a.created_at)


def test_hashchain_append_unknown_incident_raises(store: Store, chain: HashChain) -> None:
    with pytest.raises(ValueError, match="unknown incident"):
        chain.append("inc-none", "x", {})
