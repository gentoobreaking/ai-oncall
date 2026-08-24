"""批准閘門語意（algs/approval-executor.md §B.1/B.2/B.5）。

- read-only：自動執行，無需批准
- mutating 三段式鐵律：dry-run → 批准 → 執行；本模組負責「批准」段的狀態機，
  實際執行屬 executor（T011）
- 逾期升級（§B.2）：逾時 → 再提醒＋排班換渠道（v1 無排班＝固定 admin）
  → 再逾時才棄單；完整嘗試軌跡入時間線，Incident 未結不得默默消失
- 拒絕捕獲（§B.5）：一句話原因即時入 RAG（不等 postmortem）
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from oncall_core.incident.hashchain import HashChain
from oncall_core.logging import get_logger
from oncall_core.memory.indexer import KnowledgeIndexer
from oncall_core.runbook.parse import Runbook, RunbookStep
from oncall_core.store import Store

log = get_logger(__name__)

DEFAULT_APPROVE_TIMEOUT_SECONDS = 300  # §B.2 預設 5 分鐘


class ApprovalState(StrEnum):
    AUTO_APPROVED = "auto_approved"  # read-only 自動執行
    PENDING = "pending"  # mutating 待批准
    APPROVED = "approved"  # 人類批准，可交 executor
    REJECTED = "rejected"  # 拒絕（含一句話原因）
    ESCALATED = "escalated"  # 第一次逾時：已再提醒＋換渠道
    ABANDONED = "abandoned"  # 第二次逾時棄單（軌跡留存）


class Notifier(Protocol):
    """推播出口抽象（Telegram 由 interact/T012 接線；測試以 fake 注入）。"""

    def notify(self, target: str, text: str) -> None: ...


class EscalationChain(Protocol):
    """§B.2 升級鏈：回傳下一個通知對象；None = 無更多層級。"""

    def next_target(self, previous_target: str) -> str | None: ...


class FixedAdminEscalation:
    """v1：無排班表時固定 admin，升級鏈為空操作。"""

    def __init__(self, admin: str = "admin") -> None:
        self.admin = admin

    def next_target(self, previous_target: str) -> str | None:
        return None


@dataclass(slots=True)
class PendingApproval:
    request_id: str
    incident_id: str
    step: RunbookStep
    runbook_name: str
    state: ApprovalState
    notified_targets: list[str] = field(default_factory=list)
    timeout_count: int = 0


@dataclass(slots=True)
class GateOutcome:
    state: ApprovalState
    request_id: str | None = None
    detail: str = ""


class ApprovalGate:
    """mutating 動作的批准狀態機 + read-only 直通判定。"""

    def __init__(
        self,
        store: Store,
        indexer: KnowledgeIndexer,
        *,
        notifier: Notifier | None = None,
        escalation: EscalationChain | None = None,
        initial_target: str | None = None,
        approve_timeout_seconds: int = DEFAULT_APPROVE_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._chain = HashChain(store)
        self._indexer = indexer
        self._notifier = notifier
        self._escalation = escalation or FixedAdminEscalation()
        # v1 無排班：初始對象取 escalation 的固定 admin
        self._initial_target = initial_target or getattr(self._escalation, "admin", "admin")
        self.timeout_seconds = approve_timeout_seconds
        self._pending: dict[str, PendingApproval] = {}

    # ------------------------------------------------------------------
    # 提交（建議動作進閘門）
    # ------------------------------------------------------------------

    def submit(self, incident_id: str, runbook: Runbook, step: RunbookStep) -> GateOutcome:
        if step.risk == "read-only":
            # §B.1：read-only 自動執行，無需批准
            self._chain.append(
                incident_id,
                "approval_auto_approved",
                {"step": step.name, "risk": step.risk},
            )
            return GateOutcome(ApprovalState.AUTO_APPROVED, detail="read-only auto-execute")

        # §B.1 三段式：dry-run 先行——不可預演者提高門檻（記錄警示）
        if not step.dry_run_capable:
            self._chain.append(
                incident_id,
                "approval_dry_run_unavailable",
                {"step": step.name, "note": "shell action cannot be previewed; stricter gate"},
            )

        request_id = f"appr-{uuid.uuid4().hex[:10]}"
        pending = PendingApproval(
            request_id=request_id,
            incident_id=incident_id,
            step=step,
            runbook_name=runbook.name,
            state=ApprovalState.PENDING,
        )
        self._pending[request_id] = pending

        self._notify(
            pending, self._initial_target, f"[{runbook.name}] 批准請求 {step.name} (mutating)"
        )
        self._chain.append(
            incident_id,
            "approval_requested",
            {
                "request_id": request_id,
                "step": step.name,
                "target": self._initial_target,
                "timeout_seconds": self.timeout_seconds,
                "dry_run_capable": step.dry_run_capable,
            },
        )
        return GateOutcome(ApprovalState.PENDING, request_id=request_id)

    # ------------------------------------------------------------------
    # 人類決策
    # ------------------------------------------------------------------

    def on_approve(self, request_id: str, approved_by: str) -> GateOutcome:
        pending = self._take(request_id, ApprovalState.PENDING)
        if pending is None:
            return GateOutcome(ApprovalState.REJECTED, detail="unknown or non-pending request")
        pending.state = ApprovalState.APPROVED
        self._chain.append(
            pending.incident_id,
            "approval_granted",
            {"request_id": request_id, "step": pending.step.name, "by": approved_by},
        )
        log.info("approval granted", request_id=request_id, by=approved_by)
        return GateOutcome(ApprovalState.APPROVED, request_id=request_id)

    def on_reject(self, request_id: str, rejected_by: str, reason: str) -> GateOutcome:
        pending = self._take(request_id, ApprovalState.PENDING)
        if pending is None:
            return GateOutcome(ApprovalState.REJECTED, detail="unknown or non-pending request")

        self._chain.append(
            pending.incident_id,
            "approval_rejected",
            {
                "request_id": request_id,
                "step": pending.step.name,
                "by": rejected_by,
                "reason": reason,
            },
        )
        # §B.5：一句話原因即時入 RAG——飛輪最貴的養分，不等 postmortem
        self._indexer.index_override(
            incident_id=pending.incident_id,
            actual_action=reason,
        )
        self._chain.append(
            pending.incident_id,
            "override_indexed_to_rag",
            {"request_id": request_id, "reason": reason},
        )
        log.info("approval rejected and override indexed", request_id=request_id)
        return GateOutcome(ApprovalState.REJECTED, request_id=request_id)

    # ------------------------------------------------------------------
    # 逾時與升級鏈（§B.2）
    # ------------------------------------------------------------------

    def on_timeout(self, request_id: str) -> GateOutcome:
        """批准逾時：第一次升級（再提醒＋換渠道），第二次才棄單。"""
        pending = self._pending.get(request_id)
        if pending is None or pending.state not in (ApprovalState.PENDING, ApprovalState.ESCALATED):
            return GateOutcome(ApprovalState.REJECTED, detail="unknown or non-pending request")

        pending.timeout_count += 1
        last_target = (
            pending.notified_targets[-1] if pending.notified_targets else self._initial_target
        )

        if pending.timeout_count == 1:
            # 升級而非棄單：再提醒一次 + 排班表換渠道
            next_target = self._escalation.next_target(last_target) or last_target
            pending.state = ApprovalState.ESCALATED
            pending.notified_targets.append(next_target)
            self._notify(
                pending,
                next_target,
                f"[再次提醒] 批准請求 {pending.step.name} 已逾時, 升級至 {next_target}",
            )
            self._chain.append(
                pending.incident_id,
                "approval_escalated",
                {"request_id": request_id, "from": last_target, "to": next_target},
            )
            return GateOutcome(
                ApprovalState.ESCALATED, request_id=request_id, detail=f"escalated to {next_target}"
            )

        # 再逾時才棄單；時間線保留完整嘗試軌跡——Incident 未結不默默消失
        pending.state = ApprovalState.ABANDONED
        del self._pending[request_id]
        self._chain.append(
            pending.incident_id,
            "approval_abandoned",
            {
                "request_id": request_id,
                "step": pending.step.name,
                "attempts": pending.notified_targets,
                "timeouts": pending.timeout_count,
                "note": "incident remains open; trail preserved",
            },
        )
        return GateOutcome(ApprovalState.ABANDONED, request_id=request_id)

    # ------------------------------------------------------------------

    def _notify(self, pending: PendingApproval, target: str, text: str) -> None:
        pending.notified_targets.append(target)
        if self._notifier is not None:
            self._notifier.notify(target, text)

    def _take(self, request_id: str, expected: ApprovalState) -> PendingApproval | None:
        pending = self._pending.get(request_id)
        if pending is None or pending.state is not expected:
            return None
        del self._pending[request_id]
        return pending
