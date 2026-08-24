"""Store 測試：migration 順序、冪等建立、WAL 併發寫入。"""

from __future__ import annotations

import threading

import pytest

from oncall_core.store import MIGRATIONS, Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "test.db")


def test_migrations_applied_in_order(store: Store) -> None:
    rows = store._conn.execute("SELECT name FROM schema_migrations ORDER BY name").fetchall()
    assert {r["name"] for r in rows} == {name for name, _ in MIGRATIONS}
    # incidents/timeline/predictions 三表存在
    tables = {
        r["name"] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"incidents", "timeline", "predictions"} <= tables


def test_migration_is_idempotent(tmp_path) -> None:
    path = tmp_path / "re.db"
    s1 = Store(path)
    ran = s1.migrate()  # 第二次呼叫不應重跑
    assert ran == []
    s2 = Store(path)  # 重開連線也不應炸
    assert s2.get_incident_by_fingerprint("nope") is None
    s1.close()
    s2.close()


def test_create_incident_idempotent_by_fingerprint(store: Store) -> None:
    a, created_a = store.create_incident(fingerprint="fp-1", title="t")
    b, created_b = store.create_incident(fingerprint="fp-1", title="t")
    assert created_a is True
    assert created_b is False, "同 fingerprint 不得新建"
    assert a.id == b.id

    c, created_c = store.create_incident(fingerprint="fp-2")
    assert created_c and c.id != a.id


def test_update_status_and_list(store: Store) -> None:
    inc, _ = store.create_incident(fingerprint="fp-s", title="svc down")
    assert store.update_status(inc.id, "investigating")
    got = store.get_incident(inc.id)
    assert got is not None and got.status == "investigating"

    other, _ = store.create_incident(fingerprint="fp-other")
    listed = store.list_incidents(status="open")
    assert [i.id for i in listed] == [other.id]


def test_timeline_append_and_read(store: Store) -> None:
    inc, _ = store.create_incident(fingerprint="fp-tl")
    id1 = store.append_timeline(inc.id, "incident_created", {"x": 1})
    id2 = store.append_timeline(inc.id, "note", {"msg": "hi"})
    assert id2 > id1
    events = store.timeline(inc.id)
    assert [e["kind"] for e in events] == ["incident_created", "note"]


def test_prediction_save(store: Store) -> None:
    inc, _ = store.create_incident(fingerprint="fp-pred")
    pred_id = store.save_prediction(
        incident_id=inc.id,
        prompt_version="v3",
        hypotheses=[{"cause": "bad deploy", "confidence": 0.8}],
        actions=[{"action": "rollback", "risk": "mutating"}],
        missing_context=["logs"],
        tokens_used=1234,
    )
    row = store._conn.execute("SELECT * FROM predictions WHERE id = ?", (pred_id,)).fetchone()
    assert row["prompt_version"] == "v3"
    assert row["tokens_used"] == 1234


def test_concurrent_writes_wal(tmp_path) -> None:
    """多執行緒同時寫入：WAL + 寫入鎖下不得 SQLITE_BUSY 崩潰。"""
    store = Store(tmp_path / "conc.db")
    errors: list[Exception] = []

    def worker(n: int) -> None:
        try:
            for i in range(20):
                inc, _ = store.create_incident(fingerprint=f"fp-{n}-{i}")
                store.append_timeline(inc.id, "ping", {"i": i})
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"併發寫入出現例外: {errors[:3]}"
    count = store._conn.execute("SELECT COUNT(*) AS c FROM incidents").fetchone()["c"]
    assert count == 80  # 4 threads × 20
