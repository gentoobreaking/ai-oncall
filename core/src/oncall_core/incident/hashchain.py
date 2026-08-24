"""時間線防篡改雜湊鏈（algs/integrity-auth.md §E.3 / spec F21）。

    event_n.hash = SHA256(event_n.payload + event_{n-1}.hash)

- 鏈頭 genesis = SHA256(incident_id + 建立時間戳)
- verify_chain()：竄改任一筆可偵測並標記損毀位置（spec §5 標準 15）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from oncall_core.store import Store


def _sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def genesis_hash(incident_id: str, created_at: float) -> str:
    """鏈頭：incident_id 與建立時間戳共同決定。"""
    return _sha256_hex(f"{incident_id}:{created_at!r}")


def compute_event_hash(prev_hash: str, kind: str, payload_json: str) -> str:
    # §E.3 公式：payload（含 kind 一併綁定）+ 前筆 hash
    return _sha256_hex(f"{kind}:{payload_json}:{prev_hash}")


@dataclass(slots=True)
class ChainVerdict:
    ok: bool
    # 損毀位置（timeline.id）；ok=True 時為 None
    corrupt_id: int | None = None


class HashChain:
    """以雜湊鏈寫入 timeline 的唯一入口。"""

    def __init__(self, store: Store) -> None:
        self._store = store

    def append(self, incident_id: str, kind: str, payload: dict[str, object]) -> int:
        incident = self._store.get_incident(incident_id)
        if incident is None:
            raise ValueError(f"unknown incident {incident_id}")

        prev = self._store.last_chained_hash(incident_id)
        if prev is None:
            prev = genesis_hash(incident_id, incident.created_at)

        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        event_hash = compute_event_hash(prev, kind, payload_json)
        return self._store.append_chained_event(
            incident_id, kind, payload, prev_hash=prev, event_hash=event_hash
        )

    def verify(self, incident_id: str) -> ChainVerdict:
        rows = self._store.timeline(incident_id)
        incident = self._store.get_incident(incident_id)
        if incident is None:
            raise ValueError(f"unknown incident {incident_id}")

        expected_prev = genesis_hash(incident_id, incident.created_at)
        for row in rows:
            stored_prev = row["prev_hash"]
            stored_hash = row["hash"]
            # 鏈斷裂：prev 不接上，或 hash 重算不符 → 標記此筆損毀
            if stored_prev != expected_prev:
                return ChainVerdict(ok=False, corrupt_id=row["id"])
            recomputed = compute_event_hash(stored_prev, row["kind"], row["payload_json"])
            if recomputed != stored_hash:
                return ChainVerdict(ok=False, corrupt_id=row["id"])
            expected_prev = stored_hash
        return ChainVerdict(ok=True)


# 模組級便捷函式（驗收標準以 verify_chain() 稱呼）
def verify_chain(store: Store, incident_id: str) -> ChainVerdict:
    return HashChain(store).verify(incident_id)
