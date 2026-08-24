"""T010 測試：§B.1 風險分級與三段式、§B.2 逾期升級鏈、§B.5 拒絕捕獲、YAML 驗證。"""

from __future__ import annotations

import pytest
import yaml

from oncall_core.memory import KnowledgeIndexer
from oncall_core.runbook import (
    ApprovalGate,
    ApprovalState,
    FixedAdminEscalation,
    RunbookValidationError,
    parse_runbook,
    parse_runbook_yaml,
)
from oncall_core.runbook.parse import Runbook, RunbookStep
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t010.db")


@pytest.fixture()
def indexer(store: Store) -> KnowledgeIndexer:
    return KnowledgeIndexer(store)


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, target: str, text: str) -> None:
        self.sent.append((target, text))


VALID_YAML = """
name: rollback-api
service: api
description: 回滾最近部署
steps:
  - name: check-revision
    action: kubectl get deployment api
    risk: read-only
  - name: rollout-undo
    action: kubectl rollout undo deployment/api
    risk: mutating
    dry_run_capable: true
"""


# ---------------------------------------------------------------------------
# YAML 解析與錯誤彙總
# ---------------------------------------------------------------------------


def test_parse_valid_yaml() -> None:
    rb = parse_runbook_yaml(VALID_YAML)
    assert rb.name == "rollback-api"
    assert rb.max_risk == "mutating"
    assert len(rb.steps) == 2
    assert rb.steps_by_risk("read-only")[0].name == "check-revision"


def test_parse_errors_aggregated_not_fail_fast() -> None:
    bad = yaml.safe_load("""
name: Bad Name!!
service: ""
steps:
  - name: ok-step
    action: echo hi
    risk: read-only
  - name: ok-step
    action: echo again
    risk: nuclear
  - name: no-action
    risk: mutating
""")
    with pytest.raises(RunbookValidationError) as exc_info:
        parse_runbook(bad)
    joined = "; ".join(exc_info.value.errors)
    # 所有錯誤一次回報：名稱格式、空 service、重複 step、幻覺 enum、缺 action
    assert "name must match" in joined
    assert "service" in joined
    assert "duplicated" in joined
    assert "nuclear" in joined
    assert "steps[2].action" in joined


def test_parse_non_mapping_rejected() -> None:
    with pytest.raises(RunbookValidationError):
        parse_runbook(["not", "a", "dict"])


def test_read_only_runbook_risk() -> None:
    rb = parse_runbook(
        yaml.safe_load("""
name: tail-logs
service: api
description: 看 log
steps:
  - name: tail
    action: kubectl logs --tail=100
    risk: read-only
""")
    )
    assert rb.max_risk == "read-only"


# ---------------------------------------------------------------------------
# §B.1 批准閘門語意
# ---------------------------------------------------------------------------


@pytest.fixture()
def gate(store: Store, indexer: KnowledgeIndexer):
    notifier = FakeNotifier()
    g = ApprovalGate(store, indexer, notifier=notifier)
    return g, notifier


def make_rb(steps_risk: str = "mutating") -> tuple[Runbook, RunbookStep]:
    rb = Runbook(name="rb-test", service="api", description="d")
    step = RunbookStep(name="do-it", action="kubectl rollout undo", risk=steps_risk)
    rb.steps.append(step)
    return rb, step


def make_incident(store: Store) -> str:
    inc, _ = store.create_incident(fingerprint="fp-gate")
    return inc.id


def test_read_only_auto_approved(store: Store, indexer: KnowledgeIndexer) -> None:
    gate = ApprovalGate(store, indexer)
    incident_id = make_incident(store)
    rb, _ = make_rb()
    ro_step = RunbookStep(name="peek", action="logs", risk="read-only")

    outcome = gate.submit(incident_id, rb, ro_step)
    assert outcome.state is ApprovalState.AUTO_APPROVED
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "approval_auto_approved" in kinds


def test_mutating_requires_approval_flow(store: Store, indexer: KnowledgeIndexer) -> None:
    notifier = FakeNotifier()
    gate = ApprovalGate(store, indexer, notifier=notifier)
    incident_id = make_incident(store)
    rb, step = make_rb("mutating")

    outcome = gate.submit(incident_id, rb, step)
    assert outcome.state is ApprovalState.PENDING
    assert outcome.request_id is not None
    assert notifier.sent, "mutating 必須發出批准請求"
    assert ("admin", notifier.sent[0][1]) == notifier.sent[0]

    # approve → 可交 executor
    granted = gate.on_approve(outcome.request_id, approved_by="david")
    assert granted.state is ApprovalState.APPROVED
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "approval_requested" in kinds and "approval_granted" in kinds


def test_non_dry_run_capable_flagged_stricter(store: Store, indexer: KnowledgeIndexer) -> None:
    gate = ApprovalGate(store, indexer)
    incident_id = make_incident(store)
    rb = Runbook(name="shell-rb", service="api", description="d")
    step = RunbookStep(
        name="restart-service",
        action="systemctl restart api",
        risk="mutating",
        dry_run_capable=False,
    )
    rb.steps.append(step)

    outcome = gate.submit(incident_id, rb, step)
    assert outcome.state is ApprovalState.PENDING
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "approval_dry_run_unavailable" in kinds, "無法預演者必須標注並提高門檻"


# ---------------------------------------------------------------------------
# §B.5 拒絕捕獲——即時入 RAG（整合測試）
# ---------------------------------------------------------------------------


def test_reject_reason_indexed_to_rag_immediately(
    store: Store, indexer: KnowledgeIndexer, tmp_path
) -> None:
    """拒絕當下的一句話原因，不等 postmortem 即可被檢索到。"""
    gate = ApprovalGate(store, indexer)
    incident_id = make_incident(store)
    rb, step = make_rb("mutating")

    submitted = gate.submit(incident_id, rb, step)
    assert submitted.request_id is not None
    rejected = gate.on_reject(
        submitted.request_id,
        rejected_by="david",
        reason="其實是 quota 觸頂-已提額不需 rollback",
    )
    assert rejected.state is ApprovalState.REJECTED

    # 整合驗證：RAG 立即可檢索
    from oncall_core.memory import SearchFilters, search_knowledge

    hits = search_knowledge(
        store, "quota 提額 rollback 不需要", SearchFilters(), top_k=3, provider=indexer._provider
    )
    assert any("override:" in h.chunk.text for h in hits), "拒絕原因應即時入 RAG"

    # 時間線含拒絕與入庫紀錄
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "approval_rejected" in kinds
    assert "override_indexed_to_rag" in kinds
    # 雜湊鏈完整
    from oncall_core.incident import verify_chain

    assert verify_chain(store, incident_id).ok


# ---------------------------------------------------------------------------
# §B.2 逾期升級鏈
# ---------------------------------------------------------------------------


def test_timeout_once_escalates_not_abandons(store: Store, indexer: KnowledgeIndexer) -> None:
    class RosterChain(FixedAdminEscalation):
        def next_target(self, previous_target: str) -> str | None:
            return {"admin": "secondary", "secondary": "manager"}.get(previous_target)

    notifier = FakeNotifier()
    gate = ApprovalGate(store, indexer, notifier=notifier, escalation=RosterChain())
    incident_id = make_incident(store)
    rb, step = make_rb("mutating")
    submitted = gate.submit(incident_id, rb, step)
    assert submitted.request_id is not None

    outcome = gate.on_timeout(submitted.request_id)
    assert outcome.state is ApprovalState.ESCALATED
    assert outcome.detail == "escalated to secondary"
    # 再提醒送達新目標
    assert notifier.sent[-1][0] == "secondary"
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "approval_escalated" in kinds


def test_timeout_twice_abandons_with_full_trail(store: Store, indexer: KnowledgeIndexer) -> None:
    class RosterChain(FixedAdminEscalation):
        def next_target(self, previous_target: str) -> str | None:
            return {"admin": "secondary", "secondary": "manager"}.get(previous_target)

    notifier = FakeNotifier()
    gate = ApprovalGate(store, indexer, notifier=notifier, escalation=RosterChain())
    incident_id = make_incident(store)
    rb, step = make_rb("mutating")
    req = gate.submit(incident_id, rb, step).request_id
    assert req is not None

    gate.on_timeout(req)  # 第一次：升級
    final = gate.on_timeout(req)  # 第二次：棄單
    assert final.state is ApprovalState.ABANDONED

    events = store.timeline(incident_id)
    trail = [r["kind"] for r in events]
    for kind in ("approval_requested", "approval_escalated", "approval_abandoned"):
        assert kind in trail, f"軌跡缺 {kind}"
    abandoned_event = next(r for r in events if r["kind"] == "approval_abandoned")
    payload = __import__("json").loads(abandoned_event["payload_json"])
    assert set(payload["attempts"]) == {"admin", "secondary"}
    assert verify_chain_ok(store, incident_id)


def test_v1_no_schedule_fixed_admin_noop_chain(store: Store, indexer: KnowledgeIndexer) -> None:
    """v1 無排班表：固定 admin，升級鏈為空操作。"""
    notifier = FakeNotifier()
    gate = ApprovalGate(
        store, indexer, notifier=notifier, escalation=FixedAdminEscalation(admin="oncall-admin")
    )
    incident_id = make_incident(store)
    rb, step = make_rb("mutating")
    req = gate.submit(incident_id, rb, step).request_id
    assert req is not None

    outcome = gate.on_timeout(req)
    assert outcome.state is ApprovalState.ESCALATED
    assert notifier.sent[-1][0] == "oncall-admin", "無排班時仍通知固定 admin"


def verify_chain_ok(store: Store, incident_id: str) -> bool:
    from oncall_core.incident import verify_chain

    return verify_chain(store, incident_id).ok
