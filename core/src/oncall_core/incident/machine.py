"""Incident 狀態機（spec.md §3.3）。

    [*] → open → investigating ⇄（迭代）→ mitigated → resolved → [*]

非法遷移一律拒絕：回 False、記 log、並在時間線記錄被拒的遷移嘗試。
"""

from __future__ import annotations

from oncall_core.incident.hashchain import HashChain
from oncall_core.logging import get_logger
from oncall_core.store import Store

log = get_logger(__name__)

# 合法遷移表；resolved 為終態（不在任何 value 中）
TRANSITIONS: dict[str, set[str]] = {
    "open": {"investigating"},
    "investigating": {"mitigated", "investigating"},
    "mitigated": {"resolved"},
}


def can_transition(current: str, target: str) -> bool:
    return target in TRANSITIONS.get(current, set())


def transition(store: Store, incident_id: str, target: str) -> bool:
    """嘗試狀態遷移。成功回 True；非法遷移拒絕、記錄、回 False。"""
    incident = store.get_incident(incident_id)
    if incident is None:
        log.warning("transition on unknown incident", incident_id=incident_id, target=target)
        return False

    current = incident.status
    if not can_transition(current, target):
        log.warning(
            "illegal transition rejected",
            incident_id=incident_id,
            current=current,
            target=target,
        )
        # 非法遷移也要留紀錄——postmortem 追因時需要知道有過這次嘗試
        HashChain(store).append(
            incident_id,
            "illegal_transition_rejected",
            {"current": current, "target": target},
        )
        return False

    if not store.update_status(incident_id, target):
        return False
    chain = HashChain(store)
    chain.append(
        incident_id,
        "status_changed",
        {"from": current, "to": target},
    )
    log.info("incident transition", incident_id=incident_id, **{"from": current}, to=target)
    return True
