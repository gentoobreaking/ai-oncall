"""evalkit：離線回放評測與 prompt_version 品質閘門（F16、§D.3/D.4）。"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from oncall_core.brain.providers.chain import ProviderChain
from oncall_core.brain.triage import PipelineInput, TriagePipeline
from oncall_core.logging import get_logger
from oncall_core.redact import redact_text
from oncall_core.store import Store

if TYPE_CHECKING:
    from oncall_core.brain.schema_validator import TriageReport

log = get_logger(__name__)

# §D.4/spec §5 標準 11：影子報告上線門檻
MIN_SHADOW_REPORTS = 30


@dataclass(slots=True)
class ReplayCase:
    """歷史已脫敏事故回放件：含當時人工結論作為 ground truth。"""

    case_id: str
    context_summary: dict[str, object]
    ground_truth_cause: str
    expected_action_keyword: str = ""
    degraded_sources: list[str] = field(default_factory=list)
    rag_hits: list[str] = field(default_factory=list)


@dataclass(slots=True)
class CaseResult:
    case_id: str
    status: str  # report | degraded | aborted | budget_exceeded
    cause_hit: bool
    action_usable: bool
    tokens_used: int


@dataclass(slots=True)
class ReplayReport:
    prompt_version: str
    total_cases: int
    reported_cases: int
    cause_hits: int
    action_usable_count: int
    total_tokens: int
    created_at: float = field(default_factory=time.time)

    @property
    def cause_hit_rate(self) -> float:
        return self.cause_hits / self.total_cases if self.total_cases else 0.0

    @property
    def action_usable_rate(self) -> float:
        return self.action_usable_count / self.total_cases if self.total_cases else 0.0

    @property
    def avg_tokens_per_case(self) -> float:
        return self.total_tokens / self.total_cases if self.total_cases else 0.0

    def summary(self) -> dict[str, Any]:
        return {
            "prompt_version": self.prompt_version,
            "total_cases": self.total_cases,
            "reported_cases": self.reported_cases,
            "cause_hit_rate": round(self.cause_hit_rate, 4),
            "action_usable_rate": round(self.action_usable_rate, 4),
            "avg_tokens_per_case": round(self.avg_tokens_per_case, 1),
        }

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")
        return p


class EvalKit:
    def __init__(
        self,
        store: Store,
        *,
        shadow_dir: str | Path = "shadow_reports",
        reports_dir: str | Path = "eval_reports",
    ) -> None:
        self._store = store
        self._shadow_dir = Path(shadow_dir)
        self._reports_dir = Path(reports_dir)

    # ------------------------------------------------------------------
    # §D.3 回放：離線跑 shadow 路徑（不推播不執行）
    # ------------------------------------------------------------------

    def replay(
        self,
        cases: list[ReplayCase],
        chain: ProviderChain,
        *,
        prompt_version: str,
        ledger_factory=None,
    ) -> ReplayReport:
        """對回放集逐件重跑分診管線，產出三項指標報告。

        ledger_factory: () -> BudgetLedger（每件獨立預算；預設新建）。
        """
        if len(cases) < MIN_SHADOW_REPORTS // 2 and len(cases) < 20:
            log.warning("replay set smaller than recommended", cases=len(cases))

        from oncall_core.brain.budget import BudgetLedger

        # 回放件是歷史事故——以 case_id 為 incident id 確保存在（timeline FK）
        for case in cases:
            self._store.ensure_incident(
                case.case_id,
                fingerprint=f"replay-{case.case_id}",
                title=case.ground_truth_cause,
                labels={"case_id": case.case_id},
                status="open",
            )

        report_cases: list[CaseResult] = []
        for case in cases:
            ledger = ledger_factory() if ledger_factory is not None else BudgetLedger()
            pipeline = TriagePipeline(
                self._store,
                chain,
                ledger,
                prompt_version=prompt_version,
                shadow_mode=True,  # §D.3：一律走 shadow 路徑
                shadow_dir=self._shadow_dir,
            )
            outcome = pipeline.run(self._to_pipeline_input(case))
            budget = ledger.budget_for(case.case_id)

            report_cases.append(
                CaseResult(
                    case_id=case.case_id,
                    status=outcome.status,
                    cause_hit=self._cause_hit(outcome.report, case.ground_truth_cause)
                    if outcome.report
                    else False,
                    action_usable=bool(outcome.report and outcome.report.suggested_actions)
                    and self._action_matches(outcome.report, case.expected_action_keyword),
                    tokens_used=budget.tokens_used,
                )
            )

        report = ReplayReport(
            prompt_version=prompt_version,
            total_cases=len(cases),
            reported_cases=sum(1 for c in report_cases if c.status == "report"),
            cause_hits=sum(1 for c in report_cases if c.cause_hit),
            action_usable_count=sum(1 for c in report_cases if c.action_usable),
            total_tokens=sum(c.tokens_used for c in report_cases),
        )
        path = self.save_report(report)
        log.info("replay complete", **report.summary(), path=str(path))
        return report

    # ------------------------------------------------------------------
    # 指標判定
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(text: str) -> str:
        import re

        return re.sub(r"[\s\-_,.:;!?\uff01\uff1f\uff0c\uff1a\uff1b]", "", text.lower())

    def _cause_hit(self, report: TriageReport | None, ground_truth: str) -> bool:
        """ground truth 關鍵詞出現在任一假設即算命中（v1 字串正規化比對）。"""
        if report is None:
            return False
        gt = self._normalize(ground_truth)
        return any(
            gt in self._normalize(h.cause) or self._normalize(h.cause) in gt
            for h in report.hypotheses
        )

    def _action_matches(self, report: TriageReport | None, keyword: str) -> bool:
        if report is None:
            return False
        if not keyword:
            return bool(report.suggested_actions)
        kw = self._normalize(keyword)
        return any(kw in self._normalize(a.action) for a in report.suggested_actions)

    def _to_pipeline_input(self, case: ReplayCase) -> PipelineInput:
        # §D.5：回放集先過遮蔽層再進管線
        safe_context = {
            k: redact_text(v) if isinstance(v, str) else v for k, v in case.context_summary.items()
        }
        return PipelineInput(
            incident_id=case.case_id,
            context_summary=safe_context,
            degraded_sources=case.degraded_sources,
            rag_hits=case.rag_hits,
        )

    # ------------------------------------------------------------------
    # §D.3/標準12：版本對比與上線檢查點
    # ------------------------------------------------------------------

    def compare(self, baseline: ReplayReport, candidate: ReplayReport) -> dict[str, object]:
        """新舊版本三項指標對比；任一項下降即 quality_regression。"""
        regressions: list[str] = []
        if candidate.cause_hit_rate < baseline.cause_hit_rate:
            regressions.append("cause_hit_rate")
        if candidate.action_usable_rate < baseline.action_usable_rate:
            regressions.append("action_usable_rate")
        if candidate.avg_tokens_per_case > baseline.avg_tokens_per_case * 1.5:
            regressions.append("avg_tokens_per_case")
        verdict = "reject" if regressions else "pass"
        log.info(
            "version compare",
            baseline=baseline.prompt_version,
            candidate=candidate.prompt_version,
            verdict=verdict,
            regressions=regressions,
        )
        return {
            "baseline": baseline.summary(),
            "candidate": candidate.summary(),
            "regressions": regressions,
            "verdict": verdict,
        }

    def release_gate(self, baseline: ReplayReport, candidate: ReplayReport) -> bool:
        """spec.md §5 標準 12：品質下降的版本不得上線。"""
        return self.compare(baseline, candidate)["verdict"] == "pass"

    def save_report(self, report: ReplayReport) -> Path:
        name = f"{report.prompt_version.replace('.', '_')}_{int(report.created_at)}.json"
        return report.save(self._reports_dir / name)
