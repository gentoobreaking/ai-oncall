"""readapi：oncall-ui 專用唯讀查詢 HTTP 端點。

安全鐵律（spec §2.2）：
- 僅綁 127.0.0.1；改綁非 loopback 啟動時印警告
- 所有路由僅 GET；不碰執行面（executor/runbook 寫入路徑）
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from oncall_core.logging import get_logger
from oncall_core.store import Store

log = get_logger(__name__)

# 路由白名單（唯讀斷言測試據此掃描）
GET_ROUTES: tuple[str, ...] = (
    "/healthz",
    "/api/incidents",
    "/api/incidents/{id}",
    "/api/action-items",
    "/api/runbooks",
    "/api/stats",
)


def _incident_to_dict(inc) -> dict[str, object]:
    return {
        "id": inc.id,
        "fingerprint": inc.fingerprint,
        "status": inc.status,
        "severity": inc.severity,
        "title": inc.title,
        "labels": inc.labels,
        "created_at": inc.created_at,
        "updated_at": inc.updated_at,
    }


class ReadApiHandler(BaseHTTPRequestHandler):
    """僅 GET 的 JSON API。store 以唯讀方法存取。"""

    store: Store  # 由 server_class 注入

    def log_message(self, format: str, *args: Any) -> None:
        """覆寫預設 stderr 記錄，改走 structlog。"""
        log.debug("http", message=format % args if args else format)

    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        try:
            if path == "/healthz":
                self._json({"ok": True})
            elif path == "/api/incidents":
                self._list_incidents(qs)
            elif path.startswith("/api/incidents/"):
                self._get_incident(path.rsplit("/", 1)[-1])
            elif path == "/api/action-items":
                self._list_action_items(qs)
            elif path == "/api/runbooks":
                self._list_runbooks()
            elif path == "/api/stats":
                self._stats()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            log.error("readapi error", path=path, error=str(exc))
            self._json({"error": "internal error"}, 500)

    # 刻意不定義 do_POST/do_PUT/do_DELETE/do_PATCH：
    # BaseHTTPRequestHandler 對未支援方法回 501，路由表天然僅 GET。
    # ------------------------------------------------------------------

    def _list_incidents(self, qs: dict[str, list[str]]) -> None:
        status = qs.get("status", [None])[0]  # type: ignore[list-item]
        page = max(1, int(qs.get("page", ["1"])[0]))
        page_size = min(100, max(1, int(qs.get("page_size", ["20"])[0])))
        sort = qs.get("sort", ["newest"])[0]
        order = {"newest": "created_at DESC", "oldest": "created_at ASC"}.get(
            str(sort), "created_at DESC"
        )
        items = self.store.list_incidents(
            status=status, limit=page_size, offset=(page - 1) * page_size, order=order
        )
        total = self.store.count_incidents(status=status)
        self._json(
            {
                "items": [_incident_to_dict(i) for i in items],
                "page": page,
                "page_size": page_size,
                "total": total,
            }
        )

    def _get_incident(self, incident_id: str) -> None:
        inc = self.store.get_incident(incident_id)
        if inc is None:
            self._json({"error": "not found"}, 404)
            return
        timeline = [
            {
                "id": r["id"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"]),
                "created_at": r["created_at"],
            }
            for r in self.store.timeline(incident_id)
        ]
        prediction = self.store.latest_prediction(incident_id)
        self._json(
            {
                **_incident_to_dict(inc),
                "timeline": timeline,
                "prediction": json.loads(prediction["hypotheses_json"]) if prediction else None,
            }
        )

    def _list_action_items(self, qs: dict[str, list[str]]) -> None:
        status = qs.get("status", [None])[0]  # type: ignore[list-item]
        rows = [
            r for r in self.store.list_action_items() if status is None or r["status"] == status
        ]
        self._json({"items": [dict(r) for r in rows], "total": len(rows)})

    def _list_runbooks(self) -> None:
        chunks = self.store.query_knowledge_chunks(sources=["runbook"])
        self._json(
            {
                "items": [
                    {"ref_id": c.ref_id, "text": c.text[:500], "indexed_at": c.created_at}
                    for c in chunks
                ],
                "total": len(chunks),
            }
        )

    def _stats(self) -> None:
        by_status = self.store.count_incidents_by_status()
        overdue = len(self.store.overdue_action_items(time.time()))
        self._json(
            {
                "incidents_by_status": by_status,
                "action_items_overdue": overdue,
                "knowledge_chunks": self.store.count_knowledge_chunks(),
            }
        )

    def _json(self, data: object, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ReadApiServer:
    """唯讀 API server。預設只聽 127.0.0.1。"""

    def __init__(self, store: Store, host: str = "127.0.0.1", port: int = 8090) -> None:
        if host not in ("127.0.0.1", "localhost"):
            log.warning(
                "readapi binding to non-loopback address——UI 必須經反向代理認證對外",
                host=host,
            )
        handler = type("BoundHandler", (ReadApiHandler,), {"store": store})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.host, self.port = self.httpd.server_address[:2]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def serve_forever(self) -> None:
        self.httpd.serve_forever()

    def start_background(self) -> threading.Thread:
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        t.start()
        self._thread = t
        return t

    def stop(self) -> None:
        # shutdown() 需在 serve_forever 已運行時呼叫，否則永久等待
        if getattr(self, "_thread", None) is not None:
            self.httpd.shutdown()
            self._thread.join(timeout=5)
        self.httpd.server_close()
