"""批准→執行編排（T021）：把 ApprovalGate、ExecutorRunner 接進 daemon 生產路徑。

流程：
  分診報告含 mutating 動作 → ApprovalGate.submit 註冊請求＋對映射入 store
  → 按鈕訊息推播（approve:{rid} / reject:{rid}:{reason}）
  → ActionCallback 回來 → handle_action 路由：
      approve → gate.on_approve → ExecutorRunner.execute(report) → 結果推播
      reject  → gate.on_reject（原因即時入 RAG）
  → 逾時：daemon 排程驅動 tick_timeouts()，鏈上無下一位才棄單
"""

from __future__ import annotations

import json
import threading
import time

from oncall_core.brain.schema_validator import TriageReport, validate_report
from oncall_core.logging import get_logger
from oncall_core.runbook.approval import ApprovalGate, ApprovalState, GateOutcome
from oncall_core.runbook.parse import Runbook, RunbookStep
from oncall_core.store import Store

log = get_logger(__name__)

DEFAULT_APPROVAL_TIMEOUT = 300.0


class UnknownRequestError(KeyError):
    """callback 帶來的 request_id 找不到對應的 pending 請求。"""


class ApprovalOrchestrator:
    """分診報告與批准閘門／執行器之間的編排者。

    runner 允許為 None（未設定生產執行 adapter）：批准仍會記錄，
    但跳過實際執行並記 log——誠實標注而非假裝執行。
    """

    def __init__(
        self,
        store: Store,
        gate: ApprovalGate,
        *,
        # executor 以 duck-typing 注入：需有 execute(incident_id, report)
        # （回傳含 success/executed/skipped_reason 屬性的結果）。
        # 刻意不 import executor——維持「僅頂層進入點可 import」的隔離鐵律。
        runner=None,
        timeout_seconds: float = DEFAULT_APPROVAL_TIMEOUT,
        notifier=None,
        chat_id: str = "",
    ) -> None:
        self._store = store
        self._gate = gate
        self._runner = runner
        self.timeout_seconds = timeout_seconds
        self.chat_id = chat_id
        self.notifier = notifier
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 分診報告產出後：註冊 pending approval + 推播按鈕訊息
    # ------------------------------------------------------------------

    def register_from_report(self, incident_id: str, report: TriageReport) -> list[str]:
        """mutating 動作建立批准請求；回傳建立的 request_id 清單。"""
        mutating = [a for a in report.suggested_actions if a.risk == "mutating"]
        if not mutating:
            return []

        rb = Runbook(
            name=f"report-{incident_id}",
            service=incident_id,
            description=f"triage report actions for {incident_id}",
        )
        step = RunbookStep(
            name="triage-mutating-actions",
            action="; ".join(a.action for a in mutating),
            risk="mutating",
            dry_run_capable=False,  # 報告動作組合無法整體預演，提高門檻
        )
        outcome = self._gate.submit(incident_id, rb, step)
        if outcome.request_id is None or outcome.state is not ApprovalState.PENDING:
            log.warning("approval submit failed", incident_id=incident_id, state=outcome.state)
            return []

        rid = outcome.request_id
        self._store.record_pending_approval(
            request_id=rid,
            incident_id=incident_id,
            report_json=json.dumps(_report_to_dict(report), ensure_ascii=False),
        )

        buttons = [
            ("approve:" + rid, "✅批准"),
            ("reject:" + rid + ":已另行處理", "❌拒絕"),
        ]
        if self.notifier is not None:
            self.notifier.deliver_buttons(incident_id, _approval_text(rid, mutating), buttons)
        return [rid]

    # ------------------------------------------------------------------
    # callback 路由（gate ActionCallback → 閘門 → 執行器）
    # ------------------------------------------------------------------

    def handle_action(
        self, *, kind: str, request_id: str, user: str, reason: str = ""
    ) -> GateOutcome:
        """處理 Telegram callback 轉發來的決策。"""
        if kind == "approve":
            outcome = self._gate.on_approve(request_id, approved_by=user)
            if outcome.state is ApprovalState.APPROVED:
                self._execute_approved(request_id)
            return outcome

        if kind == "reject":
            outcome = self._gate.on_reject(request_id, rejected_by=user, reason=reason)
            if outcome.state is ApprovalState.REJECTED:
                self._store.mark_approval_done(request_id, "rejected")
                log.info("request rejected", request_id=request_id, by=user)
            return outcome

        return GateOutcome(state=ApprovalState.PENDING, detail="unknown kind")

    def _execute_approved(self, request_id: str) -> None:
        report_json = self._store.get_pending_report(request_id)
        if report_json is None:
            log.error("approved request missing report", request_id=request_id)
            return
        if self._runner is None:
            log.warning("executor not configured; skipping execution", request_id=request_id)
            self._store.mark_approval_done(request_id, "approved_no_executor")
            return

        report = validate_report(json.loads(report_json))
        outcome = self._runner.execute(report.incident_id, report)
        summary = (
            "executed"
            if outcome.success and outcome.executed
            else f"skipped ({outcome.skipped_reason})"
            if not outcome.executed
            else "failed"
        )
        log.info("execution finished", request_id=request_id, result=summary)

    # ------------------------------------------------------------------
    # 逾時排程（§B.2：由 daemon 驅動，非僅測試手動）
    # ------------------------------------------------------------------

    def tick_timeouts(self) -> int:
        """掃描逾時的 pending 請求並推進閘門狀態機一輪。

        每次呼叫推進一步：第一次逾時觸發升級提醒，
        鏈上無下一位且再逾時才棄單。回傳本輪處理的筆數。
        """
        now = time.time()
        handled = 0
        for row in self._store.list_pending_approvals():
            if now - row["created_at"] < self.timeout_seconds:
                continue
            outcome = self._gate.on_timeout(row["request_id"])
            if outcome.state is ApprovalState.ABANDONED:
                # 棄單後關閉 store 紀錄（時間線軌跡已保留）
                self._store.mark_approval_done(row["request_id"], "abandoned")
            handled += 1
        return handled

    def start_timeout_scheduler(
        self,
        interval: float = 30.0,
    ) -> tuple[threading.Thread, threading.Event]:
        """背景排程：每 interval 秒推進一次逾時狀態機。

        回傳 (thread, stop_event)；呼叫端 set stop_event 以停止。
        """
        stop_event = threading.Event()

        def _loop() -> None:
            while not stop_event.is_set():
                if stop_event.wait(timeout=interval):
                    return
                try:
                    self.tick_timeouts()
                except Exception as exc:
                    log.error("timeout scheduler error", error=str(exc))

        t = threading.Thread(target=_loop, name="approval-timeout-scheduler", daemon=True)
        t.start()
        return t, stop_event


def _approval_text(request_id: str, mutating_actions) -> str:  # type: ignore[no-untyped-def]
    names = "; ".join(a.action for a in mutating_actions)
    return f"⚠️ 批准請求 {request_id}\n{names}"


def _report_to_dict(report: TriageReport) -> dict[str, object]:
    return {
        "incident_id": report.incident_id,
        "hypotheses": [
            {"cause": h.cause, "confidence": h.confidence, "evidence": h.evidence}
            for h in report.hypotheses
        ],
        "suggested_actions": [
            {"action": a.action, "risk": a.risk, "runbook_ref": a.runbook_ref}
            for a in report.suggested_actions
        ],
        "missing_context": list(report.missing_context),
        "prompt_version": report.prompt_version,
    }
