"""T015 測試：§D.3 三項指標回放、版本對比與上線檢查點（標準 12）、§D.5 遮蔽。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oncall_core.brain.providers import FakeProvider, ProviderChain
from oncall_core.evalkit import EvalKit, ReplayCase
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t015.db")


def make_cases(n: int = 20) -> list[ReplayCase]:
    """合成 ≥20 件回放集；ground truth 交替兩類根因。"""
    cases = []
    for i in range(n):
        if i % 2 == 0:
            gt, kw, ctx = "memory exhaustion on cache node", "restart", {"service": "cache"}
        else:
            gt, kw, ctx = "bad deployment rollback needed", "rollback", {"service": "api"}
        cases.append(
            ReplayCase(
                case_id=f"case-{i:03d}",
                context_summary=dict(ctx),
                ground_truth_cause=gt,
                expected_action_keyword=kw,
            )
        )
    return cases


def scripted_provider(cases: list[ReplayCase], *, quality: str) -> FakeProvider:
    """依品質等級腳本化回應：good 全命中；poor 一半給不相干假設。"""
    responses = []
    for case in cases:
        inc_id = case.case_id
        if quality == "good":
            cause = case.ground_truth_cause
            action_kw = case.expected_action_keyword
        else:
            cause = "network flapping unrelated"
            action_kw = "ping"
        responses.append(
            json.dumps(
                {
                    "incident_id": inc_id,
                    "hypotheses": [{"cause": cause, "confidence": 0.8, "evidence": ["ctx"]}],
                    "suggested_actions": [
                        {"action": f"{action_kw} the service", "risk": "read-only"}
                    ],
                    "missing_context": [],
                    "prompt_version": "9.9.9",
                }
            )
        )
    return FakeProvider("llm", responses=responses)


# ---------------------------------------------------------------------------
# §D.3 回放三項報告
# ---------------------------------------------------------------------------


def test_replay_good_quality_metrics(store: Store, tmp_path: Path) -> None:
    cases = make_cases(20)
    provider = scripted_provider(cases, quality="good")
    kit = EvalKit(store, shadow_dir=tmp_path / "shadow", reports_dir=tmp_path / "reports")

    report = kit.replay(
        cases,
        ProviderChain([provider]),
        prompt_version="1.0.0",
        ledger_factory=lambda: __import__(
            "oncall_core.brain.budget", fromlist=["BudgetLedger"]
        ).BudgetLedger(),
    )
    summary = report.summary()
    assert summary["total_cases"] == 20
    assert summary["cause_hit_rate"] == pytest.approx(1.0)
    assert summary["action_usable_rate"] == pytest.approx(1.0)
    assert float(summary["avg_tokens_per_case"]) > 0, "平均 token 成本必須呈現"


def test_replay_poor_quality_lower_hit_rate(store: Store, tmp_path: Path) -> None:
    cases = make_cases(20)
    provider = scripted_provider(cases, quality="poor")
    kit = EvalKit(store, shadow_dir=tmp_path / "shadow", reports_dir=tmp_path / "reports")

    report = kit.replay(cases, ProviderChain([provider]), prompt_version="0.9.0")
    assert report.cause_hit_rate == pytest.approx(0.0), "不相干假設不應計入命中"


def test_replay_report_persisted(store: Store, tmp_path: Path) -> None:
    cases = make_cases(20)
    provider = scripted_provider(cases, quality="good")
    kit = EvalKit(store, shadow_dir=tmp_path / "shadow", reports_dir=tmp_path / "reports")
    kit.replay(cases, ProviderChain([provider]), prompt_version="3.2.1")

    saved = list((tmp_path / "reports").glob("*.json"))
    assert len(saved) == 1
    data = json.loads(saved[0].read_text())
    assert data["prompt_version"] == "3.2.1"


def test_replay_uses_shadow_path_no_side_effects(store: Store, tmp_path: Path) -> None:
    """§D.3：回放走 shadow 路徑——報告寫 shadow_reports/ 而非觸發執行。"""
    cases = make_cases(4)
    provider = scripted_provider(cases, quality="good")
    shadow_dir = tmp_path / "shadow"
    kit = EvalKit(store, shadow_dir=shadow_dir, reports_dir=tmp_path / "reports")
    kit.replay(cases, ProviderChain([provider]), prompt_version="1.0.0")

    assert list(shadow_dir.glob("*.md")), "影子報告必須落盤"
    for c in cases:
        kinds = [r["kind"] for r in store.timeline(c.case_id)]
        assert not any(k.startswith("execution") for k in kinds), "回放不得觸發執行"


# ---------------------------------------------------------------------------
# 版本對比與上線檢查點（spec §5 標準 12）
# ---------------------------------------------------------------------------


def test_release_gate_blocks_regression(store: Store, tmp_path: Path) -> None:
    cases = make_cases(20)
    kit = EvalKit(store, shadow_dir=tmp_path / "shadow", reports_dir=tmp_path / "reports")

    good = scripted_provider(cases, quality="good")
    v1 = kit.replay(cases, ProviderChain([good]), prompt_version="1.0.0")
    poor = scripted_provider(cases, quality="poor")
    v2 = kit.replay(cases, ProviderChain([poor]), prompt_version="2.0.0")

    comparison = kit.compare(v1, v2)
    assert comparison["verdict"] == "reject"
    regressions: list[str] = comparison["regressions"]  # type: ignore[assignment]
    assert "cause_hit_rate" in regressions
    assert kit.release_gate(v1, v2) is False, "品質下降的版本不得上線"


def test_release_gate_passes_equal_or_better(store: Store, tmp_path: Path) -> None:
    cases = make_cases(20)
    kit = EvalKit(store, shadow_dir=tmp_path / "shadow", reports_dir=tmp_path / "reports")

    good1 = scripted_provider(cases, quality="good")
    v1 = kit.replay(cases, ProviderChain([good1]), prompt_version="1.0.0")
    good2 = scripted_provider(cases, quality="good")
    v2 = kit.replay(cases, ProviderChain([good2]), prompt_version="2.0.0")

    assert kit.release_gate(v1, v2) is True


# ---------------------------------------------------------------------------
# §D.5 回放集先過遮蔽層
# ---------------------------------------------------------------------------


def test_replay_redacts_case_context_before_llm(store: Store, tmp_path: Path) -> None:
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    cases = [
        ReplayCase(
            case_id=f"sec-{i}",
            context_summary={"note": f"leaked {secret}", "service": "api"},
            ground_truth_cause="config error",
        )
        for i in range(20)
    ]
    provider = scripted_provider(cases, quality="good")
    kit = EvalKit(store, shadow_dir=tmp_path / "shadow", reports_dir=tmp_path / "reports")
    kit.replay(cases, ProviderChain([provider]), prompt_version="1.0.0")

    assert provider.last_prompt is not None
    assert secret not in provider.last_prompt, "金鑰不得進入 prompt/管線"
