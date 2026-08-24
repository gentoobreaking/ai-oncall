"""警報風暴聚合（algs/triage-pipeline.md §A.2，v1 最簡版）。

- 新警報與過去 5 分鐘內的未結 Incident（open/investigating/mitigated）
  比較 cluster/service/severity 標籤交集 ≥2 → 併入
- 併入 open/investigating：時間線追加、不重跑分診
- 併入 mitigated：只記錄、不重開（狀態不變、不觸發分診）
- 無命中才新建 Incident（status=open）
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from oncall_core.incident.hashchain import HashChain
from oncall_core.logging import get_logger
from oncall_core.store import Incident, Store

log = get_logger(__name__)

# §A.2 聚合比較標籤
CORRELATION_LABELS: tuple[str, ...] = ("cluster", "service", "severity")
# 時間窗（分鐘）
WINDOW_MINUTES = 5

# 不參與聚合比對的 severity 值（未標注者視為空字串）


class CorrelateAction(StrEnum):
    CREATED = "created"  # 新建 Incident → 觸發分診
    MERGED = "merged"  # 併入活躍 Incident → 不重跑分診
    RECORDED = "recorded"  # 併入 mitigated Incident → 只記錄


@dataclass(slots=True)
class CorrelateResult:
    incident: Incident
    action: CorrelateAction


class Correlator:
    def __init__(self, store: Store, chain: HashChain | None = None) -> None:
        self._store = store
        self._chain = chain or HashChain(store)

    def ingest_alert(
        self,
        *,
        fingerprint: str,
        labels: dict[str, str],
        summary: str = "",
        severity: int = 0,
    ) -> CorrelateResult:
        candidates = self._store.recent_unresolved_incidents(WINDOW_MINUTES * 60)
        for candidate in candidates:
            if label_intersection(candidate.labels, labels) >= 2:
                return self._merge(candidate, fingerprint, summary, labels)

        incident, created = self._store.create_incident(
            fingerprint=fingerprint,
            severity=severity,
            title=summary,
            labels=labels,
        )
        if not created:
            # gate 冪等漏接的同指紋重送——視為 merged，不新建不重跑
            return CorrelateResult(incident, CorrelateAction.MERGED)
        log.info("correlate: new incident", incident_id=incident.id, fingerprint=fingerprint)
        return CorrelateResult(incident, CorrelateAction.CREATED)

    def _merge(
        self,
        candidate: Incident,
        fingerprint: str,
        summary: str,
        labels: dict[str, str],
    ) -> CorrelateResult:
        self._chain.append(
            candidate.id,
            "alert_merged",
            {
                "fingerprint": fingerprint,
                "summary": summary,
                "matched_labels": {k: labels.get(k, "") for k in CORRELATION_LABELS},
            },
        )
        self._store.touch_incident(candidate.id)

        if candidate.status == "mitigated":
            # §A.2：mitigated 後只記錄不重開——時間線已記，狀態不動、分診不跑
            log.info(
                "correlate: recorded into mitigated",
                incident_id=candidate.id,
                fingerprint=fingerprint,
            )
            return CorrelateResult(candidate, CorrelateAction.RECORDED)

        log.info("correlate: merged", incident_id=candidate.id, fingerprint=fingerprint)
        return CorrelateResult(candidate, CorrelateAction.MERGED)


def label_intersection(a: dict[str, str], b: dict[str, str]) -> int:
    """三個聚合標籤中「兩邊都有且值相同」的數量。"""
    hits = 0
    for key in CORRELATION_LABELS:
        va, vb = a.get(key), b.get(key)
        if va and vb and va == vb:
            hits += 1
    return hits
