"""T017 測試：三頁面、GET-only 白名單、資料源僅 readapi。"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from oncall_core.readapi import ReadApiServer
from oncall_core.store import Store

from oncall_ui.app import GET_ROUTES, create_app


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "ui.db")


@pytest.fixture()
def readapi(store: Store):
    srv = ReadApiServer(store, port=0)
    thread = srv.start_background()
    yield srv
    srv.stop()
    thread.join(timeout=5)


@pytest.fixture()
def client(readapi: ReadApiServer) -> TestClient:
    app = create_app(readapi_url=readapi.url)
    return TestClient(app)


def seed(store: Store) -> str:
    inc, _ = store.create_incident(
        fingerprint="fp-ui", title="latency high", labels={"service": "api"}
    )
    store.append_chained_event(inc.id, "step_completed", {"step": "s"})
    from oncall_core.memory import KnowledgeIndexer

    KnowledgeIndexer(store).index_runbook(name="rollback", content="steps: undo")
    return inc.id


def test_incidents_page_lists_and_filters(store: Store, client: TestClient) -> None:
    seed(store)
    resp = client.get("/incidents")
    assert resp.status_code == 200
    assert "latency high" in resp.text
    assert "fp-ui" in resp.text or "inc-" in resp.text

    # 搜尋無命中
    resp_empty = client.get("/incidents", params={"q": "no-such-thing"})
    assert "無資料" in resp_empty.text


def test_incident_detail_page(store: Store, client: TestClient) -> None:
    incident_id = seed(store)
    resp = client.get(f"/incidents/{incident_id}")
    assert resp.status_code == 200
    assert "Timeline" in resp.text
    assert "Triage report" in resp.text


def test_incident_detail_404(client: TestClient) -> None:
    assert client.get("/incidents/inc-none").status_code == 404


def test_runbooks_page(store: Store, client: TestClient) -> None:
    seed(store)
    resp = client.get("/runbooks")
    assert resp.status_code == 200
    assert "rollback" in resp.text
    assert "Stats" in resp.text


def test_get_only_routes_whitelist() -> None:
    """spec §5 標準 6：滲透測試確認無寫入端點——路由白名單全為 GET。"""
    app = create_app(readapi_url="http://127.0.0.1:9")
    all_paths = sorted({getattr(r, "path", "") for r in app.routes} - {""})
    routes: tuple[str, ...] = ("/", *GET_ROUTES)
    allowed_prefixes = (*routes, "/openapi.json", "/docs", "/redoc", "/static")
    for path in all_paths:
        assert path.startswith(allowed_prefixes), f"未預期路由 {path}"
    for r in app.routes:
        methods = getattr(r, "methods", None)
        if methods is not None:
            assert methods <= {
                "GET", "HEAD"
            }, f"路由 {getattr(r, 'path', '?')} 含非唯讀方法 {methods}"


def test_data_source_is_readapi_only() -> None:
    """整合斷言：ui 原始碼不得 import sqlite / oncall_core.store。"""
    src_root = Path(__file__).resolve().parents[1] / "src" / "oncall_ui"
    violations: list[str] = []
    for py in src_root.rglob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = getattr(node, "module", "") or ""
                names = [a.name for a in node.names]
                if "sqlite3" in mod or any(
                    n.startswith("oncall_core") or n == "sqlite3" for n in names
                ):
                    violations.append(f"{py.name}: import {mod} {names}")
    assert violations == [], f"ui 必須只依賴 readapi，違規:\n{violations}"


def test_htmx_and_css_present(client: TestClient) -> None:
    """htmx + 極簡 CSS：模板引用 htmx script 與樣式。"""
    base_path = Path(__file__).parents[1] / "src" / "oncall_ui" / "templates" / "base.html"
    content = base_path.read_text(encoding="utf-8")
    assert "htmx.org" in content or "htmx.min.js" in content
    style_path = Path(__file__).parents[1] / "src" / "oncall_ui" / "static" / "style.css"
    assert style_path.exists()
