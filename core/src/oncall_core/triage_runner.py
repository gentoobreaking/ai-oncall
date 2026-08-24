"""分診執行緒工廠：把 ReportIncident 接上 TriagePipeline（非同步、可關閉）。

設計：
- 新 Incident 建立後以背景 thread 跑分診，不阻塞 gRPC 回應
- RAG 檢索結果與 context 摘要組成 PipelineInput
- 報告完成後經 gate_notifier（gRPC DeliverNotification）推播；
  SHADOW_MODE=1 時管線自身已跳過外部副作用，此處僅 log
- LLM 未設定（LLM_PROVIDERS 缺）時 pipeline 為 None → 只建檔不分診
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from oncall_core.brain.schema_validator import TriageReport
from oncall_core.brain.triage import PipelineInput, TriagePipeline
from oncall_core.logging import get_logger
from oncall_core.memory import SearchFilters, search_knowledge
from oncall_core.store import Store

log = get_logger(__name__)


class GateNotifier:
    """core → gate 的 DeliverNotification 客戶端封裝。"""

    def __init__(self, channel) -> None:  # type: ignore[no-untyped-def]
        from oncall_core._proto.oncall.v1 import oncall_pb2_grpc

        self._stub = oncall_pb2_grpc.OncallServiceStub(channel)

    def deliver(self, incident_id: str, text: str, chat_id: str = "") -> bool:
        from oncall_core._proto.oncall.v1 import oncall_pb2

        try:
            resp = self._stub.DeliverNotification(
                oncall_pb2.DeliverNotificationRequest(
                    notification=oncall_pb2.Notification(
                        incident_id=incident_id,
                        chat_id=chat_id,
                        text=text,
                    )
                ),
                timeout=15,
            )
            return bool(resp.accepted)
        except Exception as exc:
            log.warning("deliver notification failed", incident_id=incident_id, error=str(exc))
            return False


def make_triage_runner(
    store: Store,
    pipeline: TriagePipeline | None,
    notifier=None,
    *,
    shadow: bool = False,
) -> Callable[[str], None]:
    """回傳 runner(incident_id)；pipeline=None 時 runner 為 no-op。

    runner 以 daemon thread 執行——gRPC 回應不被分診延遲拖住。
    """

    def run_triage_async(incident_id: str) -> None:
        if pipeline is None:
            return

        def _work() -> None:
            incident = store.get_incident(incident_id)
            if incident is None:
                return

            # RAG 歷史比對（§A.1 步驟 H）
            service = incident.labels.get("service", "")
            hits = search_knowledge(
                store,
                f"{incident.title} {service}".strip(),
                SearchFilters(service=service or None),
                top_k=3,
            )
            rag_hits = [f"[{h.chunk.source}] {h.chunk.text[:200]}" for h in hits]

            input_ = PipelineInput(
                incident_id=incident_id,
                context_summary={
                    "title": incident.title,
                    "labels": incident.labels,
                    "severity": incident.severity,
                },
                degraded_sources=[],
                rag_hits=rag_hits,
            )
            assert pipeline is not None  # caller guarantees non-None when wired
            outcome = pipeline.run(input_)

            # 推播語意：shadow 模式下管線已落盤不外送；正式模式才通知
            if (
                outcome.status == "report"
                and outcome.report is not None
                and not shadow
                and notifier
            ):
                _deliver_report(notifier, incident_id, outcome.report)
            elif outcome.status != "report":
                log.warning(
                    "triage did not produce report; pure-context path",
                    incident_id=incident_id,
                    status=outcome.status,
                )

        threading.Thread(target=_work, name=f"triage-{incident_id}", daemon=True).start()

    return run_triage_async


def _deliver_report(notifier, incident_id: str, report: TriageReport) -> None:
    """把分診報告格式化為 Telegram 訊息送出。"""
    lines = [f"🚨 分診報告 {incident_id} - prompt v{report.prompt_version}", ""]
    for i, h in enumerate(report.hypotheses, 1):
        lines.append(f"{i}. {h.cause} (信心度 {h.confidence:.0%})")
    if report.suggested_actions:
        lines.append("")
        for a in report.suggested_actions:
            flag = "⚠️需批准" if a.risk == "mutating" else "✅唯讀"
            lines.append(f"- [{flag}] {a.action}")
    if report.missing_context:
        lines.append("")
        lines.append("⚠️ 缺漏 context: " + "; ".join(report.missing_context))
    notifier.deliver(incident_id, "\n".join(lines))
