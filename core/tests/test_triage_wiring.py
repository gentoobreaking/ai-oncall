"""分診接線整合測試：ReportIncident → 非同步分診 → 推播/shadow。"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pytest

from oncall_core._proto.oncall.v1 import oncall_pb2
from oncall_core.brain.budget import BudgetLedger
from oncall_core.brain.providers import FakeProvider, ProviderChain
from oncall_core.brain.triage import TriagePipeline
from oncall_core.grpc_servicer import OncallCoreServicer
from oncall_core.store import Store
from oncall_core.triage_runner import make_triage_runner


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "wire.db")


def make_report_json(incident_id: str) -> str:
    return json.dumps(
        {
            "incident_id": incident_id,
            "hypotheses": [{"cause": "bad deploy", "confidence": 0.9, "evidence": []}],
            "suggested_actions": [{"action": "rollback", "risk": "mutating"}],
            "missing_context": [],
            "prompt_version": "2.1.0",
        }
    )


class SpyNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def deliver(self, incident_id: str, text: str, chat_id: str = "") -> bool:
        self.sent.append((incident_id, text))
        return True


class EchoReportProvider(FakeProvider):
    """從 prompt 抽出 Incident ID，動態產生合法報告（模擬真實 LLM 行為）。"""

    def complete(self, request):  # type: ignore[no-untyped-def]
        m = re.search(r"Incident: (\S+)", request.prompt)
        if m:
            self._responses = [make_report_json(m.group(1))]
        return super().complete(request)


def build_servicer(store: Store, tmp_path: Path, *, shadow: bool):
    provider = EchoReportProvider("llm")
    pipeline = TriagePipeline(
        store,
        ProviderChain([provider]),
        BudgetLedger(),
        prompt_version="2.1.0",
        shadow_mode=shadow,
        shadow_dir=tmp_path / "shadow_reports",
    )
    notifier = SpyNotifier()
    runner = make_triage_runner(store, pipeline, notifier, shadow=shadow)
    servicer = OncallCoreServicer(store, run_triage=runner)
    return servicer, provider, notifier


def fake_request(fingerprint: str, summary: str) -> oncall_pb2.ReportIncidentRequest:
    return oncall_pb2.ReportIncidentRequest(
        event=oncall_pb2.AlertEvent(
            fingerprint=fingerprint,
            status=oncall_pb2.AlertStatus.ALERT_STATUS_FIRING,
            severity=oncall_pb2.Severity.SEVERITY_CRITICAL,
            labels={"service": "api"},
            summary=summary,
        )
    )


def call(servicer: OncallCoreServicer, req):  # type: ignore[no-untyped-def]
    # context 僅供 gRPC 內部使用；測試以 None 搭配 type ignore
    return servicer.ReportIncident(req, None)  # type: ignore[arg-type]


def _wait_until(cond, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_report_incident_triggers_async_triage_and_notify(store: Store, tmp_path: Path) -> None:
    """正式模式：新 Incident → 背景分診 → 報告推播給 notifier。"""
    servicer, _provider, notifier = build_servicer(store, tmp_path, shadow=False)

    resp = call(servicer, fake_request("fp-wire-1", "latency spike"))
    assert resp.accepted and resp.incident_id

    assert _wait_until(lambda: len(notifier.sent) > 0), "正式模式應推播分診報告"
    inc_id, text = notifier.sent[0]
    assert inc_id == resp.incident_id
    assert "bad deploy" in text and "需批准" in text
    # 分診紀錄入庫（prompt_version 綁定）
    pred = store.latest_prediction(inc_id)
    assert pred is not None and pred["prompt_version"] == "2.1.0"


def test_report_incident_shadow_mode_no_notify_but_file(store: Store, tmp_path: Path) -> None:
    """Shadow 模式：報告落盤、不推播。"""
    servicer, _, notifier = build_servicer(store, tmp_path, shadow=True)
    shadow_dir = tmp_path / "shadow_reports"

    resp = call(servicer, fake_request("fp-wire-2", "db down"))
    assert resp.accepted

    assert _wait_until(lambda: bool(list(shadow_dir.glob("*.md")))), "影子報告必須落盤"
    time.sleep(0.2)
    assert notifier.sent == [], "shadow 模式不得推播"


def test_duplicate_incident_does_not_retriage(store: Store, tmp_path: Path) -> None:
    """冪等命中（非新建）不觸發分診——避免風暴重跑燒 token。"""
    servicer, _provider, _ = build_servicer(store, tmp_path, shadow=False)

    r1 = call(servicer, fake_request("fp-dup", "first"))
    assert r1.accepted
    assert _wait_until(lambda: _provider.call_count >= 1)

    r2 = call(servicer, fake_request("fp-dup", "first"))
    assert r2.deduplicated is True
    calls_after_first = _provider.call_count
    time.sleep(0.3)
    assert _provider.call_count == calls_after_first, "重送不得重跑分診"


def test_no_llm_configured_servicer_still_accepts(store: Store) -> None:
    """LLM_PROVIDERS 未設定（pipeline=None）：只建檔，接受警報不分診。"""
    servicer = OncallCoreServicer(store)  # 無 run_triage
    resp = call(servicer, fake_request("fp-nollm", "x"))
    assert resp.accepted
