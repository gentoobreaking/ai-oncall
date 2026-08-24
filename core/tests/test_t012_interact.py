"""T012 測試：inline 三分支、排班升級鏈降級/依序升級、RBAC。"""

from __future__ import annotations

from datetime import UTC

import pytest

from oncall_core.interact import CallbackEvent, InteractionRouter, RBACError
from oncall_core.memory import KnowledgeIndexer
from oncall_core.runbook.approval import ApprovalGate, ApprovalState, FixedAdminEscalation
from oncall_core.runbook.parse import Runbook, RunbookStep
from oncall_core.schedule import Roster, load_roster, roster_from_ics, roster_from_static
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t012.db")


@pytest.fixture()
def indexer(store: Store) -> KnowledgeIndexer:
    return KnowledgeIndexer(store)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, target: str, text: str) -> None:
        self.sent.append((target, text))


def make_gate(
    store: Store, indexer: KnowledgeIndexer, notifier: FakeNotifier, roster: Roster
) -> ApprovalGate:
    return ApprovalGate(
        store,
        indexer,
        notifier=notifier,
        escalation=FixedAdminEscalation(admin=roster.manager),
        initial_target=roster.primary,
    )


def submit_mutating(store: Store, gate: ApprovalGate) -> str:
    incident, _ = store.create_incident(fingerprint="fp-t012")
    rb = Runbook(name="rb", service="api", description="d")
    step = RunbookStep(name="do", action="kubectl rollout undo", risk="mutating")
    outcome = gate.submit(incident.id, rb, step)
    assert outcome.request_id is not None
    return outcome.request_id


# ---------------------------------------------------------------------------
# inline 按鈕三分支
# ---------------------------------------------------------------------------


def test_approve_branch(store: Store, indexer: KnowledgeIndexer) -> None:
    roster = roster_from_static("alice-p", "bob-s", "carol-m")
    notifier = FakeNotifier()
    gate = make_gate(store, indexer, notifier, roster)
    router = InteractionRouter(gate, roster, notifier=notifier)
    req = submit_mutating(store, gate)

    outcome = router.handle(CallbackEvent(request_id=req, kind="approve", user="alice-p"))
    assert outcome.state is ApprovalState.APPROVED


def test_reject_branch_requires_reason_and_indexes(store: Store, indexer: KnowledgeIndexer) -> None:
    roster = roster_from_static("p", "s", "m")
    gate = make_gate(store, indexer, FakeNotifier(), roster)
    router = InteractionRouter(gate, roster)
    req = submit_mutating(store, gate)

    # 無原因 → 拒絕處理（F9 要求一句話）
    no_reason = router.handle(CallbackEvent(request_id=req, kind="reject", user="p"))
    assert "reason required" in no_reason.detail

    # 有原因 → 拒絕並入 RAG
    ok = router.handle(
        CallbackEvent(request_id=req, kind="reject", user="p", reason="quota 已提額")
    )
    assert ok.state is ApprovalState.REJECTED
    hits = store.query_knowledge_chunks(sources=["override"])
    assert len(hits) == 1 and "quota" in hits[0].text


def test_snooze_branch_keeps_pending(store: Store, indexer: KnowledgeIndexer) -> None:
    roster = roster_from_static("p", "s", "m")
    gate = make_gate(store, indexer, FakeNotifier(), roster)
    router = InteractionRouter(gate, roster)
    req = submit_mutating(store, gate)

    outcome = router.handle(CallbackEvent(request_id=req, kind="snooze", user="anyone"))
    assert outcome.state is ApprovalState.PENDING and outcome.detail == "snoozed"


# ---------------------------------------------------------------------------
# 排班：v1 固定 admin 降級；設定後依序升級
# ---------------------------------------------------------------------------


def test_roster_default_admin_degradation() -> None:
    roster = Roster()  # 未設定
    assert roster.chain() == ["admin"]


def test_roster_chain_order_dedup() -> None:
    roster = roster_from_static("alice", "bob", "alice")  # manager 與 primary 同人
    assert roster.chain() == ["alice", "bob"], "去重保序"


def test_static_roster_escalation_order(store: Store, indexer: KnowledgeIndexer) -> None:
    """設定排班後：primary → secondary → manager 依序升級。"""
    roster = roster_from_static("primary-alice", "secondary-bob", "manager-carol")
    notifier = FakeNotifier()

    class RosterChain(FixedAdminEscalation):
        def __init__(self) -> None:
            super().__init__(admin=roster.manager)
            self._chain = roster.chain()

        def next_target(self, previous_target: str) -> str | None:
            idx = self._chain.index(previous_target) if previous_target in self._chain else -1
            return self._chain[idx + 1] if 0 <= idx < len(self._chain) - 1 else None

    gate = ApprovalGate(
        store, indexer, notifier=notifier, escalation=RosterChain(), initial_target=roster.primary
    )
    req = submit_mutating(store, gate)

    o1 = gate.on_timeout(req)
    assert o1.state is ApprovalState.ESCALATED
    assert notifier.sent[-1][0] == "secondary-bob"

    o2 = gate.on_timeout(req)
    assert o2.state is ApprovalState.ESCALATED
    assert notifier.sent[-1][0] == "manager-carol"

    o3 = gate.on_timeout(req)
    assert o3.state is ApprovalState.ABANDONED


def test_ics_import_parses_oncall_rotation() -> None:
    """過去（正在值班）者為 primary，下一個未來區間為 secondary。"""
    from datetime import datetime, timedelta

    def dt(days: int) -> str:
        base = datetime.now(UTC) + timedelta(days=days)
        return base.strftime("%Y%m%dT%H%M%SZ")

    ics = "\n".join(
        [
            "BEGIN:VCALENDAR",
            "BEGIN:VEVENT",
            "SUMMARY:dave",
            f"DTSTART:{dt(-3)}",
            f"DTEND:{dt(4)}",
            "END:VEVENT",
            "BEGIN:VEVENT",
            "SUMMARY:erin",
            f"DTSTART:{dt(4)}",
            f"DTEND:{dt(11)}",
            "END:VEVENT",
            "END:VCALENDAR",
        ]
    )
    roster = roster_from_ics(ics)
    assert roster.source == "ics"
    assert roster.primary == "dave"
    assert roster.secondary == "erin"


def test_load_roster_missing_file_defaults_admin(tmp_path) -> None:
    roster = load_roster(tmp_path / "none.ics")
    assert roster.chain() == ["admin"]


# ---------------------------------------------------------------------------
# RBAC：僅 admin 角色可批准 mutating
# ---------------------------------------------------------------------------


def test_rbac_non_admin_cannot_approve(store: Store, indexer: KnowledgeIndexer) -> None:
    roster = roster_from_static("primary-dave", "sec-erin", "mgr-frank")
    gate = make_gate(store, indexer, FakeNotifier(), roster)
    router = InteractionRouter(gate, roster, admins={"boss"})
    req = submit_mutating(store, gate)

    with pytest.raises(RBACError, match="not allowed"):
        router.handle(CallbackEvent(request_id=req, kind="approve", user="random-user"))


def test_rbac_admin_roles_can_approve(store: Store, indexer: KnowledgeIndexer) -> None:
    roster = roster_from_static("primary-dave", "sec-erin", "mgr-frank")
    gate = make_gate(store, indexer, FakeNotifier(), roster)
    router = InteractionRouter(gate, roster, admins={"boss"})

    for user in ("primary-dave", "mgr-frank", "boss"):  # primary/manager/明列 admin
        req = submit_mutating(store, gate)
        outcome = router.handle(CallbackEvent(request_id=req, kind="approve", user=user))
        assert outcome.state is ApprovalState.APPROVED, f"{user} 應可批准"


def test_rbac_anyone_can_reject_with_reason(store: Store, indexer: KnowledgeIndexer) -> None:
    """拒絕不需 admin（記錄性質），但原因必須入 RAG。"""
    roster = roster_from_static("dave", "erin", "frank")
    gate = make_gate(store, indexer, FakeNotifier(), roster)
    router = InteractionRouter(gate, roster)
    req = submit_mutating(store, gate)

    outcome = router.handle(
        CallbackEvent(request_id=req, kind="reject", user="intern", reason="誤報-已自動恢復")
    )
    assert outcome.state is ApprovalState.REJECTED
