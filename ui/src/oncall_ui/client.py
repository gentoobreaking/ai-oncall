"""readapi 客戶端——oncall-ui 的唯一資料源。

安全鐵律（spec §2.2）：ui 不碰 SQLite 檔案、不碰 executor，
一切取數走 core 的唯讀 HTTP API。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field


def default_readapi_url() -> str:
    return os.environ.get("READAPI_URL", "http://127.0.0.1:8090")


@dataclass(slots=True)
class ReadApiClient:
    base_url: str = field(default_factory=default_readapi_url)

    def get_json(self, path: str) -> dict:
        req = urllib.request.Request(
            self.base_url + path,
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())  # type: ignore[no-any-return]

    # ------------------------------------------------------------------
    # 型別化端點封裝
    # ------------------------------------------------------------------

    def incidents(
        self, status: str | None = None, page: int = 1, page_size: int = 20, sort: str = "newest"
    ) -> dict:
        from urllib.parse import urlencode

        qs = {"page": page, "page_size": page_size, "sort": sort}
        if status:
            qs["status"] = status
        return self.get_json(f"/api/incidents?{urlencode(qs)}")

    def incident(self, incident_id: str) -> dict | None:
        try:
            return self.get_json(f"/api/incidents/{incident_id}")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            raise

    def action_items(self) -> dict:
        return self.get_json("/api/action-items")

    def runbooks(self) -> dict:
        return self.get_json("/api/runbooks")

    def stats(self) -> dict:
        return self.get_json("/api/stats")
