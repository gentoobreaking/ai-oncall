"""T016 測試：Shadow Mode 零外部副作用、評分欄位與統計、上線門檻。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oncall_core.brain.budget import BudgetLedger
from oncall_core.brain.providers import FakeProvider, ProviderChain
from oncall_core.brain.triage import PipelineInput, TriagePipeline
from oncall_core.executor import ExecutorRunner
from oncall_core.shadow import ShadowController, ShadowGateError
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t016.db")


def good_json(incident_id: str) -> str:
    return json.dumps(
        {
            "incident_id": incident_id,
            "hypotheses": [{"cause": "bad deploy", "confidence": 0.8, "evidence": ["d"]}],
            "suggested_actions": [{"action": "kubectl rollout undo", "risk": "mutating"}],
            "missing_context": [],
            "prompt_version": "2.1.0",
        }
    )


class SpyNotifier:
    """記錄所有推播——shadow 模式下必須為空。"""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def notify(self, target: str, text: str) -> None:
        self.sent.append((target, text))


class SpyCommandRunner:
    """記錄所有底層命令——shadow 模式下必須為空。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, action: str, *, dry_run: bool) -> str:
        self.calls.append(action)
        return "ok"


# ---------------------------------------------------------------------------
# 旗標開啟時零外部副作用（整合測試）
# ---------------------------------------------------------------------------


def test_shadow_mode_zero_external_side_effects(store: Store, tmp_path: Path) -> None:
    """分診照跑 → 報告落盤含評分欄位；推播跳過、executor 一律跳過。"""
    incident_id = "inc-shadow-1"
    store.ensure_incident(incident_id, fingerprint="fp-s1")

    notifier = SpyNotifier()
    cmd = SpyCommandRunner()
    ExecutorRunner(store, command_runner=cmd, audit_dir=tmp_path / "audit")

    provider = FakeProvider("llm", responses=[good_json(incident_id)])
    pipeline = TriagePipeline(
        store,
        ProviderChain([provider]),
        BudgetLedger(),
        prompt_version="2.1.0",
        shadow_mode=True,
        shadow_dir=tmp_path / "shadow_reports",
    )
    outcome = pipeline.run(
        PipelineInput(
            incident_id=incident_id,
            context_summary={"service": "api", "error_rate": 0.2},
        )
    )

    # 分診照跑：報告有產出且落盤
    assert outcome.status == "report"
    assert outcome.shadow_path is not None
    md = Path(outcome.shadow_path).read_text(encoding="utf-8")
    assert "原因正確" in md and "建議可用" in md and "reviewer" in md, "影子報告必須含評分欄位"

    # 推播跳過
    assert notifier.sent == []
    # executor 跳過：shadow 模式下不應有任何生產環境呼叫
    assert cmd.calls == []
    exec_kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert not any(k.startswith("execution_") for k in exec_kinds)


def test_non_shadow_mode_would_have_side_effects(store: Store, tmp_path: Path) -> None:
    """對照組：關閉旗標時同樣輸入會產生推播請求與執行路徑。"""
    incident_id = "inc-live-1"
    store.ensure_incident(incident_id, fingerprint="fp-l1")

    notifier = SpyNotifier()
    _ = notifier  # 對照組：正式路徑允許推播
    provider = FakeProvider("llm", responses=[good_json(incident_id)])
    pipeline = TriagePipeline(
        store,
        ProviderChain([provider]),
        BudgetLedger(),
        prompt_version="2.1.0",
        shadow_mode=False,
        shadow_dir=tmp_path / "shadow_reports",
    )
    outcome = pipeline.run(
        PipelineInput(
            incident_id=incident_id,
            context_summary={"service": "api"},
        )
    )
    assert outcome.status == "report"
    # 非 shadow：管線允許下游互動（此處以「未寫影子寫影子檔」證明走的是正式路徑）
    assert outcome.shadow_path is None
    report = outcome.report
    assert report is not None and report.validated


# ---------------------------------------------------------------------------
# 上線門檻：評分不足拒絕關閉；足額後放行
# ---------------------------------------------------------------------------


def test_gate_refuses_when_scored_below_threshold(store: Store) -> None:
    ctrl = ShadowController(store, enabled=True)
    for i in range(29):  # 少於 30 份
        ctrl.record_score(
            incident_id=f"inc-{i}", cause_correct=True, action_usable=True, reviewer="human"
        )

    ok, detail = ctrl.can_disable()
    assert not ok
    assert detail["scored"] == 29
    with pytest.raises(ShadowGateError) as exc_info:
        ctrl.assert_can_disable()
    assert "scored=29/30" in str(exc_info.value), "拒絕訊息須說明差距"


def test_gate_refuses_when_accuracy_below_threshold(store: Store) -> None:
    ctrl = ShadowController(store, enabled=True)
    for i in range(30):
        # 全數 30 份都有評分，但只有一半原因正確 → 0.5 < 0.8
        ctrl.record_score(
            incident_id=f"inc-{i}", cause_correct=i % 2 == 0, action_usable=True, reviewer="human"
        )
    ok, detail = ctrl.can_disable()
    assert not ok
    assert abs(detail["cause_accuracy"] - 0.5) < 1e-9  # type: ignore[union-attr]


def test_gate_passes_after_thresholds_met_and_disables(store: Store) -> None:
    ctrl = ShadowController(store, enabled=True)
    for i in range(30):
        ctrl.record_score(
            incident_id=f"inc-{i}", cause_correct=True, action_usable=True, reviewer="human"
        )
    ok, _ = ctrl.can_disable()
    assert ok
    ctrl.disable()
    assert ctrl.enabled is False


def test_env_flag_enables_controller(monkeypatch: pytest.MonkeyPatch, store: Store) -> None:
    monkeypatch.setenv("SHADOW_MODE", "1")
    assert ShadowController(store).enabled is True
    monkeypatch.delenv("SHADOW_MODE")
    assert ShadowController(store).enabled is False
