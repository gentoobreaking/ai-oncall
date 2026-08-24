"""分診管線編排（algs/triage-pipeline.md §A.1/A.3–A.6 + schema-validation §C.2）。

流程：取消檢查點① → 預算檢查 → LLM 分診 → schema 驗證
→（失敗）repair prompt 一次 → 再失敗降級純 context。
Shadow Mode（§A.6）：SHADOW_MODE=1 時報告寫 shadow_reports/ 不推播不執行。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from oncall_core.brain.budget import BudgetExceeded, BudgetLedger
from oncall_core.brain.providers.base import CompletionRequest
from oncall_core.brain.providers.chain import ProviderChain
from oncall_core.brain.schema_validator import TriageReport, extract_json_object, validate_report
from oncall_core.logging import get_logger
from oncall_core.store import Store

log = get_logger(__name__)

# 取消檢查點允許的狀態（§A.3）
ACTIVE_STATUSES = {"open", "investigating"}

PROMPT_SYSTEM = (
    "You are an SRE triage assistant. Respond with ONLY a JSON object matching: "
    '{"incident_id": str, "hypotheses": [{"cause": str, "confidence": number 0..1, '
    '"evidence": [str]}] (1-5 items, descending confidence), '
    '"suggested_actions": [{"action": str, "risk": "read-only"|"mutating", '
    '"runbook_ref": str|null}], "missing_context": [str], '
    '"prompt_version": semver string}. No markdown fences, no extra text.'
)


@dataclass(slots=True)
class PipelineInput:
    incident_id: str
    context_summary: dict[str, object]
    degraded_sources: list[str] = field(default_factory=list)
    rag_hits: list[str] = field(default_factory=list)  # RAG 相似事故/runbook 摘要


@dataclass(slots=True)
class PipelineOutcome:
    """status：report（已驗證報告）/ degraded（schema 失敗，改推純 context）
    / aborted（取消檢查點）/ budget_exceeded（預算用罄降級）。"""

    status: str
    report: TriageReport | None = None
    repair_attempts: int = 0
    tokens_used: int = 0
    reason: str = ""
    shadow_path: str | None = None

    @property
    def has_validated_report(self) -> bool:
        return self.report is not None and self.report.validated


class TriagePipeline:
    def __init__(
        self,
        store: Store,
        chain: ProviderChain,
        ledger: BudgetLedger,
        *,
        prompt_version: str = "1.0.0",
        shadow_mode: bool | None = None,
        shadow_dir: str | Path = "shadow_reports",
    ) -> None:
        self._store = store
        self._chain = chain
        self._ledger = ledger
        self._prompt_version = prompt_version
        self._shadow = (
            shadow_mode if shadow_mode is not None else os.environ.get("SHADOW_MODE") == "1"
        )
        self._shadow_dir = Path(shadow_dir)

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def run(self, input_: PipelineInput) -> PipelineOutcome:
        incident_id = input_.incident_id

        # 取消檢查點①：context 收集完成後、進入 RAG/LLM 前
        aborted = self._checkpoint(input_, "before_rag")
        if aborted is not None:
            return aborted

        budget = self._ledger.budget_for(incident_id)
        prompt = self._build_prompt(input_)

        try:
            # ---- 第一次 LLM 呼叫 ----
            try:
                budget.assert_can_spend()
                first = self._chain.complete(
                    CompletionRequest(prompt=prompt, system=PROMPT_SYSTEM, max_tokens=1024)
                )
                budget.record(first.tokens_used)
            except BudgetExceeded as exc:
                return self._degraded(input_, f"budget_exceeded: {exc}")

            # 取消檢查點②：RAG/首次 LLM 後、決定是否繼續花費前（§A.3 含預算檢查）
            aborted = self._checkpoint(input_, "after_first_llm")
            if aborted is not None:
                return aborted

            # ---- §C.2 驗證 → repair 一次 → 降級 ----
            try:
                report = validate_report(
                    extract_json_object(first.text), expected_incident_id=incident_id
                )
                return self._finish_report(input_, report, repair_attempts=0)
            except ValueError as exc:
                first_error = str(exc)
                log.warning(
                    "schema validation failed, repairing once",
                    incident_id=incident_id,
                    error=first_error,
                )

            # repair prompt：帶驗證錯誤重問一次（§C.2 至多一次）
            try:
                budget.assert_can_spend()
                repair_prompt = (
                    f"{prompt}\n\nYour previous output was INVALID: {first_error!s}\n"
                    "Return the corrected JSON object only."
                )
                second = self._chain.complete(
                    CompletionRequest(prompt=repair_prompt, system=PROMPT_SYSTEM, max_tokens=1024)
                )
                budget.record(second.tokens_used)
            except BudgetExceeded as exc:
                return self._degraded(
                    input_, f"budget_exceeded_during_repair: {exc}", repair_attempts=1
                )

            try:
                report = validate_report(
                    extract_json_object(second.text), expected_incident_id=incident_id
                )
                return self._finish_report(input_, report, repair_attempts=1)
            except ValueError as repair_error:
                # 再失敗 → 降級：不產分診報告，改推純 context 摘要＋RAG 相似事故連結
                return self._degraded(
                    input_,
                    f"schema failure after repair: {repair_error}",
                    repair_attempts=1,
                    rag_links=[h for h in input_.rag_hits],
                )

        except Exception as exc:
            log.error("triage pipeline failed", incident_id=incident_id, error=str(exc))
            return self._degraded(input_, f"provider_failure: {exc}")

    # ------------------------------------------------------------------

    def _checkpoint(self, input_: PipelineInput, note: str) -> PipelineOutcome | None:
        """取消檢查點（§A.3）：非 open/investigating 即中止，不產報告。"""
        incident = self._store.get_incident(input_.incident_id)
        if incident is not None and incident.status not in ACTIVE_STATUSES:
            log.info(
                "triage checkpoint abort", incident_id=input_.incident_id, status=incident.status
            )
            self._store.append_chained_event(
                input_.incident_id,
                "triage_aborted",
                {"reason": "inactive_status", "status": incident.status, "note": note},
            )
            # token 照計——已耗成本不因中止消失（budget ledger 已記錄）
            return PipelineOutcome(
                status="aborted",
                reason=f"incident status is {incident.status}, not active",
                tokens_used=self._ledger.budget_for(input_.incident_id).tokens_used,
            )
        return None

    def _build_prompt(self, input_: PipelineInput) -> str:
        import json as _json

        parts = [
            f"Incident: {input_.incident_id}",
            "Context (JSON): " + _json.dumps(input_.context_summary, ensure_ascii=False),
        ]
        if input_.degraded_sources:
            # §A.5：明列缺漏來源，禁止 LLM 幻覺補完
            parts.append(
                "DEGRADED context sources unavailable (do NOT invent their contents): "
                + ", ".join(input_.degraded_sources)
            )
        if input_.rag_hits:
            parts.append("Similar historical knowledge:\n- " + "\n- ".join(input_.rag_hits))
        parts.append(f'Return "prompt_version": "{self._prompt_version}".')
        return "\n".join(parts)

    def _finish_report(
        self, input_: PipelineInput, report: TriageReport, *, repair_attempts: int
    ) -> PipelineOutcome:
        """報告定案；Shadow Mode 落盤不推播不執行（§A.6）。"""
        # 降級模式鐵律（§A.5）：degraded_sources 非空時 missing_context 必須非空
        if input_.degraded_sources and not report.missing_context:
            missing = [f"context unavailable: {s}" for s in input_.degraded_sources]
            report.missing_context.extend(missing)

        self._store.save_prediction(
            incident_id=report.incident_id,
            prompt_version=report.prompt_version,
            hypotheses=[
                {"cause": h.cause, "confidence": h.confidence, "evidence": h.evidence}
                for h in report.hypotheses
            ],
            actions=[
                {"action": a.action, "risk": a.risk, "runbook_ref": a.runbook_ref}
                for a in report.suggested_actions
            ],
            missing_context=list(report.missing_context),
        )
        self._store.append_chained_event(
            input_.incident_id,
            "triage_completed",
            {"prompt_version": report.prompt_version, "repair_attempts": repair_attempts},
        )

        shadow_path: str | None = None
        if self._shadow:
            shadow_path = str(self._write_shadow_report(input_, report))

        log.info(
            "triage report produced",
            incident_id=report.incident_id,
            shadow=self._shadow,
            repair_attempts=repair_attempts,
        )
        return PipelineOutcome(
            status="report",
            report=report,
            repair_attempts=repair_attempts,
            shadow_path=shadow_path,
        )

    def _write_shadow_report(self, input_: PipelineInput, report: TriageReport) -> Path:
        self._shadow_dir.mkdir(parents=True, exist_ok=True)
        path = self._shadow_dir / f"{report.incident_id}_{int(time.time())}.md"
        lines = [
            f"# Shadow triage report — {report.incident_id}",
            f"- prompt_version: `{report.prompt_version}`",
            "",
            "## Hypotheses",
        ]
        for i, h in enumerate(report.hypotheses, 1):
            lines.append(f"{i}. **{h.cause}** (confidence={h.confidence:.2f})")
            lines.extend(f"   - evidence: {e}" for e in h.evidence)
        lines += ["", "## Suggested actions"]
        lines.extend(
            f"- [{a.risk}] {a.action}" + (f" (runbook: {a.runbook_ref})" if a.runbook_ref else "")
            for a in report.suggested_actions
        )
        if report.missing_context:
            lines += ["", "## Missing context"]
            lines.extend(f"- {m}" for m in report.missing_context)
        # §D.4 人工評分欄位——評分寫回統計庫是上線門檻依據
        lines += [
            "",
            "## Review (human)",
            "- 原因正確: [ ] yes [ ] no",
            "- 建議可用: [ ] yes [ ] no",
            "- reviewer: ____",
            "- scored_at: ____",
        ]
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def _degraded(
        self,
        input_: PipelineInput,
        reason: str,
        *,
        repair_attempts: int = 0,
        rag_links: list[str] | None = None,
        timeline_kind: str = "schema_failure",
    ) -> PipelineOutcome:
        """降級路徑：純 context 摘要＋RAG 相似事故連結，token 預算照扣。"""
        self._store.append_chained_event(
            input_.incident_id,
            timeline_kind,
            {"reason": reason, "repair_attempts": repair_attempts},
        )
        log.warning(
            "triage degraded to pure-context", incident_id=input_.incident_id, reason=reason
        )
        return PipelineOutcome(
            status="degraded",
            reason=reason,
            repair_attempts=repair_attempts,
            tokens_used=self._ledger.budget_for(input_.incident_id).tokens_used,
        )
