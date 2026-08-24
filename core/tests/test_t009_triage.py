"""T009 測試：§C.1 schema 契約、§C.2 修復修復迴圈、壞輸出語料集、
取消檢查點（§A.3）、降級模式（§A.5）、Shadow Mode（§A.6）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oncall_core.brain.budget import BudgetLedger
from oncall_core.brain.providers import FakeProvider, ProviderChain
from oncall_core.brain.schema_validator import (
    ExecutorRejected,
    TriageReport,
    ensure_executor_input,
    extract_json_object,
    validate_report,
)
from oncall_core.brain.triage import PipelineInput, TriagePipeline
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t009.db")


def make_incident(store: Store, fingerprint: str = "fp-tri") -> str:
    inc, _ = store.create_incident(fingerprint=fingerprint)
    return inc.id


def make_pipeline(store: Store, provider: FakeProvider, **kwargs) -> TriagePipeline:
    chain = ProviderChain([provider])
    ledger = kwargs.pop("ledger", None) or BudgetLedger()
    shadow = kwargs.pop("shadow_mode", False)
    return TriagePipeline(
        store,
        chain,
        ledger,
        prompt_version="2.1.0",
        shadow_mode=shadow,
        shadow_dir=kwargs.pop("shadow_dir", "shadow_reports_test"),
        **kwargs,
    )


def good_json(incident_id: str) -> str:
    return json.dumps(
        {
            "incident_id": incident_id,
            "hypotheses": [
                {"cause": "bad deploy", "confidence": 0.8, "evidence": ["deploy 5m ago"]},
                {"cause": "cache stampede", "confidence": 0.3, "evidence": []},
            ],
            "suggested_actions": [
                {"action": "rollback deployment", "risk": "mutating", "runbook_ref": "rollback"},
                {"action": "check error rate", "risk": "read-only", "runbook_ref": None},
            ],
            "missing_context": [],
            "prompt_version": "2.1.0",
        }
    )


def pipeline_input(incident_id: str) -> PipelineInput:
    return PipelineInput(
        incident_id=incident_id,
        context_summary={"service": "api", "error_rate": 0.15},
        degraded_sources=["logs: unavailable (loki down)"],
        rag_hits=["postmortem inc-42: same symptom, root cause was quota"],
    )


# ---------------------------------------------------------------------------
# §C.1 schema 驗證單元
# ---------------------------------------------------------------------------


def test_validate_report_accepts_good() -> None:
    report = validate_report(json.loads(good_json("inc-1")), expected_incident_id="inc-1")
    assert isinstance(report, TriageReport)
    assert report.validated is True
    assert len(report.hypotheses) == 2
    assert report.suggested_actions[0].risk == "mutating"


def test_extract_json_strips_markdown_fence() -> None:
    raw = '```json\n{"a": 1}\n```'
    assert extract_json_object(raw) == {"a": 1}


def test_extract_json_rejects_truncation() -> None:
    with pytest.raises(ValueError, match=r"invalid JSON|no JSON"):
        extract_json_object('{"incident_id": "x", "hypotheses": [{"cause": "y"')


def test_executor_hard_rejects_unvalidated_input() -> None:
    """§C.2：executor 對未通過驗證的輸入硬拒絕——即使人類已批准。"""
    with pytest.raises(ExecutorRejected):
        ensure_executor_input({"action": "rollback", "risk": "mutating"})
    with pytest.raises(ExecutorRejected):
        ensure_executor_input(None)
    # validated TriageReport 才放行
    report = validate_report(json.loads(good_json("inc-1")))
    assert ensure_executor_input(report) is report


# ---------------------------------------------------------------------------
# §C.2/§C.3 壞輸出語料集（≥8 案例）——repair 次數與降級路徑
# ---------------------------------------------------------------------------

BAD_CORPUS: dict[str, str] = {
    # 1. 截斷 JSON
    "truncated": '{"incident_id": "INC", "hypotheses": [{"cause": "dep',
    # 2. markdown 包裹 + 內文合法（fence 剝除後應成功）
    "markdown_wrapped": '```json\n{"h": 0}\n```',
    # 3. 幻覺 enum
    "bad_enum": json.dumps(
        {
            "incident_id": "INC",
            "prompt_version": "2.1.0",
            "missing_context": [],
            "hypotheses": [{"cause": "x", "confidence": 0.9, "evidence": []}],
            "suggested_actions": [{"action": "nuke", "risk": "delete-everything"}],
        }
    ),
    # 4. 缺欄位（無 prompt_version）
    "missing_field": json.dumps(
        {
            "incident_id": "INC",
            "hypotheses": [{"cause": "x", "confidence": 0.9, "evidence": []}],
            "suggested_actions": [],
            "missing_context": [],
        }
    ),
    # 5. 型別錯誤（confidence 是字串）
    "wrong_type": json.dumps(
        {
            "incident_id": "INC",
            "prompt_version": "2.1.0",
            "missing_context": [],
            "hypotheses": [{"cause": "x", "confidence": "high", "evidence": []}],
            "suggested_actions": [],
        }
    ),
    # 6. 空陣列 hypotheses
    "empty_hypotheses": json.dumps(
        {
            "incident_id": "INC",
            "prompt_version": "2.1.0",
            "missing_context": [],
            "hypotheses": [],
            "suggested_actions": [],
        }
    ),
    # 7. 超量 hypotheses（6 項 > 上限 5）
    "too_many_hypotheses": json.dumps(
        {
            "incident_id": "INC",
            "prompt_version": "2.1.0",
            "missing_context": [],
            "hypotheses": [{"cause": f"c{i}", "confidence": 0.5, "evidence": []} for i in range(6)],
            "suggested_actions": [],
        }
    ),
    # 8. confidence 超界
    "confidence_out_of_range": json.dumps(
        {
            "incident_id": "INC",
            "prompt_version": "2.1.0",
            "missing_context": [],
            "hypotheses": [{"cause": "x", "confidence": 1.5, "evidence": []}],
            "suggested_actions": [],
        }
    ),
    # 9. 非 JSON 純文字（幻覺敘述）
    "plain_text": "I think the problem is the database connection pool.",
    # 10. incident_id 不符
    "wrong_incident_id": good_json("inc-other"),
}


def substitute(corpus_case: str, incident_id: str) -> str:
    return corpus_case.replace("INC", incident_id)


def test_corpus_all_cases_repair_once_then_degrade_or_succeed(store: Store, tmp_path: Path) -> None:
    """每案例：第一次輸出必失敗；repair 一次後仍壞 → 降級路徑。"""
    incident_id = make_incident(store)

    for case_name in (
        "truncated",
        "bad_enum",
        "missing_field",
        "wrong_type",
        "empty_hypotheses",
        "too_many_hypotheses",
        "confidence_out_of_range",
        "plain_text",
    ):
        provider = FakeProvider(
            "llm",
            responses=[substitute(BAD_CORPUS[case_name], incident_id)],
            default_reply=substitute(BAD_CORPUS[case_name], incident_id),
        )
        p = make_pipeline(store, provider, shadow_dir=tmp_path / "sr")
        outcome = p.run(pipeline_input(incident_id))

        assert outcome.status == "degraded", f"{case_name} 應走降級"
        assert outcome.repair_attempts == 1, f"{case_name} repair 至多一次"
        assert outcome.report is None, f"{case_name} 不得產出報告"
        assert provider.call_count == 2, f"{case_name} 應恰好呼叫 LLM 兩次"
        # token 預算照扣（§C.2）
        assert outcome.tokens_used > 0
        # 時間線記錄 schema_failure
        kinds = [r["kind"] for r in store.timeline(incident_id)]
        assert "schema_failure" in kinds, f"{case_name} 時間線缺 schema_failure"


def test_corpus_markdown_wrapper_repairs_to_success(store: Store, tmp_path: Path) -> None:
    """markdown 包裹案例：repair 後給正確 JSON → 成功出報告。"""
    incident_id = make_incident(store)
    bad = f"```json\n{json.dumps({'oops': True})}\n```"
    provider = FakeProvider("llm", responses=[bad], default_reply=good_json(incident_id))
    p = make_pipeline(store, provider, shadow_dir=tmp_path / "sr")
    outcome = p.run(pipeline_input(incident_id))

    assert outcome.status == "report"
    assert outcome.repair_attempts == 1
    assert outcome.report is not None and outcome.has_validated_report
    assert provider.call_count == 2


def test_first_try_success_no_repair(store: Store, tmp_path: Path) -> None:
    incident_id = make_incident(store)
    provider = FakeProvider("llm", responses=[good_json(incident_id)])
    p = make_pipeline(store, provider, shadow_dir=tmp_path / "sr")
    outcome = p.run(pipeline_input(incident_id))

    assert outcome.status == "report"
    assert outcome.repair_attempts == 0
    assert provider.call_count == 1
    # 每筆輸出帶 prompt_version
    assert outcome.report.prompt_version == "2.1.0"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 取消檢查點 §A.3：中止不產報告、token 照計入成本統計
# ---------------------------------------------------------------------------


def test_checkpoint_aborts_when_resolved_before_llm(store: Store, tmp_path: Path) -> None:
    from oncall_core.incident import transition

    incident_id = make_incident(store)
    for status in ("investigating", "mitigated", "resolved"):
        transition(store, incident_id, status)

    provider = FakeProvider("llm")
    p = make_pipeline(store, provider, shadow_dir=tmp_path / "sr")
    outcome = p.run(pipeline_input(incident_id))

    assert outcome.status == "aborted"
    assert outcome.report is None
    assert provider.call_count == 0, "中止後不得打 LLM"
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "triage_aborted" in kinds


def test_tokens_counted_even_when_degraded(store: Store, tmp_path: Path) -> None:
    """§A.3：取消/失敗時已耗 token 仍計入成本統計。"""
    incident_id = make_incident(store)
    bad = BAD_CORPUS["truncated"].replace("INC", incident_id)
    provider = FakeProvider("llm", responses=[bad], default_reply=bad)
    p = make_pipeline(store, provider, shadow_dir=tmp_path / "sr")
    outcome = p.run(pipeline_input(incident_id))

    assert outcome.status == "degraded"
    totals = p._ledger.totals()
    assert totals["llm_tokens_total"] > 0, "失敗路徑 token 也必須入帳"


# ---------------------------------------------------------------------------
# 降級模式 §A.5：missing_context 必列、禁止幻覺補完
# ---------------------------------------------------------------------------


def test_missing_context_forced_when_degraded_sources(store: Store, tmp_path: Path) -> None:
    """degraded_sources 非空時，報告 missing_context 必須明列缺漏。"""
    incident_id = make_incident(store)
    # LLM 回了空的 missing_context——管線必須補上降級來源
    forced = json.loads(good_json(incident_id))
    forced["missing_context"] = []
    provider = FakeProvider("llm", responses=[json.dumps(forced)])
    p = make_pipeline(store, provider, shadow_dir=tmp_path / "sr")
    outcome = p.run(pipeline_input(incident_id))

    assert outcome.status == "report"
    assert outcome.report is not None
    assert any("loki down" in m for m in outcome.report.missing_context), (
        "缺漏 context 必須被明列-禁止幻覺補完"
    )


def test_prompt_includes_do_not_invent_warning(store: Store, tmp_path: Path) -> None:
    incident_id = make_incident(store)
    provider = FakeProvider("llm", responses=[good_json(incident_id)])
    p = make_pipeline(store, provider, shadow_dir=tmp_path / "sr")
    p.run(pipeline_input(incident_id))
    assert provider.last_prompt is not None
    assert "do NOT invent" in provider.last_prompt


# ---------------------------------------------------------------------------
# Shadow Mode §A.6：報告寫檔不推播不執行
# ---------------------------------------------------------------------------


def test_shadow_mode_writes_file(store: Store, tmp_path: Path) -> None:
    incident_id = make_incident(store)
    shadow_dir = tmp_path / "shadow_reports"
    provider = FakeProvider("llm", responses=[good_json(incident_id)])
    p = make_pipeline(store, provider, shadow_mode=True, shadow_dir=shadow_dir)
    outcome = p.run(pipeline_input(incident_id))

    assert outcome.status == "report"
    assert outcome.shadow_path is not None
    written = Path(outcome.shadow_path)
    assert written.exists(), "Shadow 報告必須落盤"
    content = written.read_text(encoding="utf-8")
    assert "Shadow triage report" in content
    assert "2.1.0" in content  # prompt_version 綁定


def test_shadow_mode_env_var(monkeypatch: pytest.MonkeyPatch, store: Store, tmp_path: Path) -> None:
    monkeypatch.setenv("SHADOW_MODE", "1")
    incident_id = make_incident(store)
    provider = FakeProvider("llm", responses=[good_json(incident_id)])
    chain = ProviderChain([provider])
    p = TriagePipeline(
        store,
        chain,
        BudgetLedger(),
        prompt_version="2.1.0",
        shadow_dir=tmp_path / "sr_env",
    )
    outcome = p.run(pipeline_input(incident_id))
    assert outcome.shadow_path is not None, "SHADOW_MODE=1 應自動啟用影子模式"


# ---------------------------------------------------------------------------
# budget_exceeded → 降級
# ---------------------------------------------------------------------------


def test_budget_exceeded_degrades_to_pure_context(store: Store, tmp_path: Path) -> None:
    incident_id = make_incident(store)
    provider = FakeProvider("llm", tokens_per_reply=10_000)  # 一發就爆 token 上限
    ledger = BudgetLedger(max_calls=10, max_tokens=5_000)
    p = make_pipeline(store, provider, ledger=ledger, shadow_dir=tmp_path / "sr")

    # 第一次呼叫後 budget 已超限 → 第二次進管線應直接降級
    r1 = p.run(pipeline_input(incident_id))
    assert r1.status == "degraded", "token 超上限應在 repair 前被擋下"

    r2 = p.run(pipeline_input(incident_id))
    assert r2.status == "degraded"
    assert "budget_exceeded" in r2.reason
