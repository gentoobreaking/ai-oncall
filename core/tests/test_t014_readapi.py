"""T014 測試：端點契約（分頁/篩選/排序）、唯讀斷言、bind 安全。"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

import pytest

from oncall_core.readapi import GET_ROUTES, ReadApiHandler, ReadApiServer
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t014.db")


@pytest.fixture()
def server(store: Store):
    srv = ReadApiServer(store, port=0)  # 隨機埠
    thread = srv.start_background()
    yield srv
    srv.stop()
    thread.join(timeout=5)


@pytest.fixture()
def base_url(server: ReadApiServer) -> str:
    return server.url


def get(base_url: str, path: str) -> tuple[int, dict]:  # type: ignore[type-arg]
    try:
        with urllib.request.urlopen(base_url + path, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def seed(store: Store, n: int = 5) -> list[str]:
    ids = []
    for i in range(n):
        inc, _ = store.create_incident(
            fingerprint=f"fp-{i}", title=f"incident {i}", labels={"service": "api"}
        )
        if i % 2 == 0:
            store.update_status(inc.id, "investigating")
        ids.append(inc.id)
    # 推開時間以驗證排序
    store._conn.execute("UPDATE incidents SET created_at = created_at - ? WHERE id = (?)*100", ())
    return ids


# ---------------------------------------------------------------------------
# 端點契約
# ---------------------------------------------------------------------------


def test_list_incidents_pagination_and_sort(
    store: Store, server: ReadApiServer, base_url: str
) -> None:
    for i in range(5):
        inc, _ = store.create_incident(fingerprint=f"fp-pg-{i}")
        # 人為錯開建立時間
        store._conn.execute(
            "UPDATE incidents SET created_at = ? WHERE id = ?",
            (time.time() - i * 60, inc.id),
        )
    store._conn.commit()

    code, body = get(base_url, "/api/incidents?page=1&page_size=2&sort=newest")
    assert code == 200
    assert body["total"] == 5 and len(body["items"]) == 2
    assert body["page"] == 1
    # created_at = now - i*60：fp-pg-0 最新、fp-pg-4 最舊
    assert body["items"][0]["fingerprint"] == "fp-pg-0"

    code, body2 = get(base_url, "/api/incidents?page=2&page_size=2&sort=newest")
    assert body2["items"][0]["fingerprint"] == "fp-pg-2"

    code, body_old = get(base_url, "/api/incidents?page=1&page_size=2&sort=oldest")
    assert body_old["items"][0]["fingerprint"] == "fp-pg-4"


def test_list_incidents_status_filter(store: Store, base_url: str) -> None:
    for i in range(4):
        inc, _ = store.create_incident(fingerprint=f"fp-f-{i}")
        if i < 2:
            store.update_status(inc.id, "investigating")

    code, body = get(base_url, "/api/incidents?status=open")
    assert code == 200
    assert body["total"] == 2
    assert all(item["status"] == "open" for item in body["items"])


def test_get_incident_detail_with_timeline(store: Store, base_url: str) -> None:
    inc, _ = store.create_incident(fingerprint="fp-detail")
    store.append_chained_event(inc.id, "step_completed", {"step": "s1"})
    store.save_prediction(
        incident_id=inc.id,
        prompt_version="2.1.0",
        hypotheses=[{"cause": "c", "confidence": 0.7}],
        actions=[],
        missing_context=[],
    )

    code, body = get(base_url, f"/api/incidents/{inc.id}")
    assert code == 200
    assert body["fingerprint"] == "fp-detail"
    assert any(t["kind"] == "step_completed" for t in body["timeline"])
    assert body["prediction"][0]["cause"] == "c"


def test_get_unknown_incident_404(base_url: str) -> None:
    code, _ = get(base_url, "/api/incidents/inc-nope")
    assert code == 404


def test_stats_endpoint(store: Store, server: ReadApiServer, base_url: str) -> None:
    store.create_incident(fingerprint="fp-stat")

    code, body = get(base_url, "/api/stats")
    assert code == 200
    assert body["incidents_by_status"].get("open", 0) >= 1
    assert "knowledge_chunks" in body


def test_runbooks_endpoint_lists_indexed_runbooks(store: Store, base_url: str) -> None:
    from oncall_core.memory import KnowledgeIndexer

    indexer = KnowledgeIndexer(store)
    indexer.index_runbook(name="rollback-api", content="steps: kubectl rollout undo")

    code, body = get(base_url, "/api/runbooks")
    assert code == 200
    assert body["total"] == 1
    assert body["items"][0]["ref_id"] == "rollback-api"


# ---------------------------------------------------------------------------
# 唯讀斷言：無寫入方法；路由白名單
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
def test_write_methods_rejected(base_url: str, method: str) -> None:
    """唯讀讀斷言：寫入方法不得存在對應 handler（501 = 未實作）。"""
    req = urllib.request.Request(base_url + "/api/incidents", data=b"{}", method=method)
    try:
        urllib.request.urlopen(req, timeout=5)
        raise AssertionError(f"{method} 應被拒絕")
    except urllib.error.HTTPError as e:
        assert e.code == 501, f"{method} 應回 501 Not Implemented"


def test_route_table_is_get_only_whitelist() -> None:
    """路由表白名單掃描：GET_ROUTES 不得含非唯讀動詞語意。"""
    forbidden = ("POST", "PUT", "DELETE", "PATCH", "create", "update", "delete")
    for route in GET_ROUTES:
        lowered = route.lower()
        assert not any(word in lowered for word in forbidden), f"路由 {route} 疑似寫入"
    handler_methods = [m for m in dir(ReadApiHandler) if m.startswith("do_")]
    assert set(handler_methods) <= {"do_GET"}, f"handler 僅允許 do_GET-發現 {handler_methods}"


# ---------------------------------------------------------------------------
# bind 安全
# ---------------------------------------------------------------------------


def test_default_bind_is_loopback(server: ReadApiServer) -> None:
    assert str(server.host) in ("127.0.0.1",), "readapi 預設必須綁 127.0.0.1"


def test_non_loopback_bind_warns(store: Store, capsys: pytest.CaptureFixture[str]) -> None:
    srv = ReadApiServer(store, host="0.0.0.0", port=0)
    try:
        out = capsys.readouterr().out
        assert "反向代理" in out, "非 loopback 綁定必須印警告"
    finally:
        srv.stop()
