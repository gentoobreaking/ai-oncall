"""Telegram 決策層互動：callback → 批准/拒絕/忽略語意（F5）。

本模組把 tgtransport 收到的 callback 語意化：
  - approve → ApprovalGate.on_approve（RBAC：僅 admin 可批准 mutating）
  - reject + 原因 → ApprovalGate.on_reject（§B.5 即時入 RAG）
  - ignore/snooze → 只記時間線
並以 Roster 驅動 §B.2 升級鏈。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from oncall_core.logging import get_logger
from oncall_core.runbook.approval import ApprovalGate, ApprovalState, GateOutcome
from oncall_core.schedule import Roster

log = get_logger(__name__)


class RBACError(PermissionError):
    """無權限執行該決策（沿數位分身三級模式：admin 可批准 mutating）。"""


@dataclass(slots=True)
class CallbackEvent:
    request_id: str
    kind: str  # approve | reject | snooze
    user: str
    reason: str = ""


class NotifierProtocol(Protocol):
    def notify(self, target: str, text: str) -> None: ...


class InteractionRouter:
    """把 callback 事件路由到批准閘門；RBAC 檢查在 mutating 批准前。"""

    def __init__(
        self,
        gate: ApprovalGate,
        roster: Roster,
        *,
        notifier: NotifierProtocol | None = None,
        admins: set[str] | None = None,
    ) -> None:
        self._gate = gate
        self._roster = roster
        self._notifier = notifier
        # 三級 RBAC：admin＝roster 的 manager＋primary＋明列 admins
        self._admins = admins or set()

    @property
    def admin_ids(self) -> set[str]:
        return {self._roster.manager, self._roster.primary, *self._admins}

    def handle(self, event: CallbackEvent) -> GateOutcome:
        if event.kind == "approve":
            if event.user not in self.admin_ids:
                log.warning("rbac denied", user=event.user, action=event.kind)
                raise RBACError(f"user {event.user!r} is not allowed to approve mutating actions")
            return self._gate.on_approve(event.request_id, approved_by=event.user)

        if event.kind == "reject":
            if not event.reason.strip():
                return GateOutcome(state=ApprovalState.REJECTED, detail="reason required (F9)")
            return self._gate.on_reject(
                event.request_id, rejected_by=event.user, reason=event.reason
            )

        # snooze / ignore：只記錄，不改變閘門狀態
        log.info("callback snoozed", request_id=event.request_id, user=event.user)
        return GateOutcome(
            state=ApprovalState.PENDING, request_id=event.request_id, detail="snoozed"
        )
