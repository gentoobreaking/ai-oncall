"""T019 — spec.md §5 十五條上線標準的端到端自動化驗證。

對照表(標準 → 測試)：
  標準 1  端到端演練            test_std01_e2e_alert_to_report
  標準 2  批准閘門實測          test_std02_approval_gate_flow
  標準 3  知識飛輪              test_std03_knowledge_flywheel_reuse
  標準 4  韌性(core 掛掉)     test_std04_core_down_retry_recovery
  標準 5  資源水位              (部署層驗證, 見 docs/deploy.md)
  標準 6  UI 安全               ui/tests/test_t017_ui.py::test_get_only_routes_whitelist
  標準 7  風暴聚合              test_std07_storm_aggregation
  標準 8  取消檢查點            test_std08_cancellation_checkpoint_zero_token
  標準 9  傳輸安全              test_std09_transport_security_binding
                                ＋ docs/deploy.md 的 nmap 步驟
  標準 10 容量暴漲情境          test_std10_capacity_scenario_hpa_quota
  標準 11 Shadow 門檻           test_std11_shadow_release_gate
  標準 12 Prompt 迭代           test_std12_prompt_version_gate
  標準 13 認證冪等              test_std13_auth_and_idempotency_e2e
  標準 14 遮蔽                  test_std14_redaction_timeline_audit
  標準 15 雜湊鏈                test_std15_hashchain_tamper_detection
  (跨 process 契約測試)        test_cross_process_grpc_contract

全套離線可跑(LLM 以 fake 注入)；跨 process 測試需先建置 gate binary。
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from oncall_core.brain.budget import BudgetLedger
from oncall_core.brain.providers import FakeProvider, ProviderChain
from oncall_core.brain.schema_validator import validate_report
from oncall_core.brain.triage import PipelineInput, TriagePipeline
from oncall_core.executor import ExecutorRunner
from oncall_core.incident import verify_chain
from oncall_core.memory import KnowledgeIndexer, SearchFilters, search_knowledge
from oncall_core.postmortem import PostmortemWriter
from oncall_core.readapi import ReadApiServer
from oncall_core.shadow import ShadowController
from oncall_core.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t019.db")


@pytest.fixture()
def indexer(store: Store) -> KnowledgeIndexer:
    return KnowledgeIndexer(store)


def good_json(incident_id: str, cause: str = "bad deploy") -> str:
    return json.dumps(
        {
            "incident_id": incident_id,
            "hypotheses": [{"cause": cause, "confidence": 0.9, "evidence": ["ctx"]}],
            "suggested_actions": [{"action": "kubectl rollout undo", "risk": "mutating"}],
            "missing_context": [],
            "prompt_version": "2.1.0",
        }
    )


def make_pipeline(
    store: Store,
    provider: FakeProvider,
    tmp_path: Path,
    *,
    shadow: bool = False,
    ledger: BudgetLedger | None = None,
) -> TriagePipeline:
    return TriagePipeline(
        store,
        ProviderChain([provider]),
        ledger or BudgetLedger(),
        prompt_version="2.1.0",
        shadow_mode=shadow,
        shadow_dir=tmp_path / "shadow_reports",
    )


# ---------------------------------------------------------------------------
# 標準 1：端到端——警報進來到分診報告到手
# ---------------------------------------------------------------------------


def test_std01_e2e_alert_to_report(store: Store, tmp_path: Path) -> None:
    incident_id = "inc-e2e-01"
    store.ensure_incident(incident_id, fingerprint="fp-e2e-01")
    provider = FakeProvider("llm", responses=[good_json(incident_id)])
    pipeline = make_pipeline(store, provider, tmp_path)

    outcome = pipeline.run(
        PipelineInput(
            incident_id=incident_id,
            context_summary={"service": "api", "error_rate": 0.2},
            rag_hits=["postmortem inc-99: 同症狀"],
        )
    )

    assert outcome.status == "report"
    report = outcome.report
    assert report is not None
    assert report.prompt_version == "2.1.0"
    # 分診紀錄入庫(prompt_version 綁定, 標準 12 前提)
    pred = store.latest_prediction(incident_id)
    assert pred is not None and pred["prompt_version"] == "2.1.0"
    # 時間線有完整軌跡且鏈完整
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "triage_completed" in kinds
    assert verify_chain(store, incident_id).ok


# ---------------------------------------------------------------------------
# 標準 2：批准閘門——mutating 未批准零副作用；拒絕與逾時皆有時間線紀錄
# ---------------------------------------------------------------------------


def test_std02_approval_gate_flow(store: Store, indexer: KnowledgeIndexer, tmp_path: Path) -> None:
    from oncall_core.runbook.approval import ApprovalGate, ApprovalState
    from oncall_core.runbook.parse import Runbook, RunbookStep

    incident_id = "inc-e2e-02"
    store.ensure_incident(incident_id, fingerprint="fp-e2e-02")

    sent: list[tuple[str, str]] = []

    class Notifier:
        def notify(self, target: str, text: str) -> None:
            sent.append((target, text))

    gate = ApprovalGate(store, indexer, notifier=Notifier())
    rb = Runbook(name="rb", service="api", description="")
    mutating = RunbookStep(name="undo", action="kubectl rollout undo", risk="mutating")

    submitted = gate.submit(incident_id, rb, mutating)
    assert submitted.request_id is not None
    # 未批准前：executor 硬拒絕(即使人類「想」執行)
    runner = ExecutorRunner(
        store, command_runner=lambda a, *, dry_run: "ok", audit_dir=tmp_path / "audit"
    )
    report = validate_report(json.loads(good_json(incident_id)))
    with pytest.raises(Exception, match="approved request"):
        runner.execute(incident_id, report)

    # 拒絕路徑：原因入時間線
    rejected = gate.on_reject(submitted.request_id, rejected_by="david", reason="誤報")
    assert rejected.state is ApprovalState.REJECTED

    # 逾時路徑：升級→棄單皆記錄
    submitted2 = gate.submit(incident_id, rb, mutating)
    assert submitted2.request_id is not None
    gate.on_timeout(submitted2.request_id)
    gate.on_timeout(submitted2.request_id)
    trail = [r["kind"] for r in store.timeline(incident_id)]
    assert "approval_escalated" in trail and "approval_abandoned" in trail
    # Incident 仍 open, 軌跡未消失(§B.2)
    assert store.get_incident(incident_id).status == "open"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# 標準 3：知識飛輪——同類事故第二次發生, 檢索必須引用第一次結論
# ---------------------------------------------------------------------------


def test_std03_knowledge_flywheel_reuse(store: Store, indexer: KnowledgeIndexer) -> None:
    writer = PostmortemWriter(store, indexer)
    first, _ = store.create_incident(fingerprint="fp-fly-1")
    writer.finalize(first.id, conclusion="root cause: quota 觸頂導致限流", service="api")

    # 三個月後同類事故：分診前的 RAG 檢索必須撈回第一次結論
    hits = search_knowledge(
        store,
        "quota 觸頂 限流",
        SearchFilters(service="api"),
        top_k=3,
        provider=indexer._provider,
    )
    assert hits, "歷史結論應被檢索"
    assert any("quota" in h.chunk.text for h in hits)


# ---------------------------------------------------------------------------
# 標準 4：韌性——core 掛掉期間警報不遺失(gate 重試), 恢復後自動補分診
# ---------------------------------------------------------------------------


def test_std04_core_down_retry_recovery(tmp_path: Path) -> None:
    """core 掛掉：gate 回 502、警報不寫冪等快取；恢復後 AM 重試即成功。"""
    gate_bin = REPO_ROOT / "gate" / "bin" / "gate"
    if not gate_bin.exists():
        pytest.skip("gate binary 未建置(make gate-build 後可用)")
    _run_gate_retry_scenario(tmp_path, gate_bin)


# ---------------------------------------------------------------------------
# 標準 7：風暴聚合——同一根因 10 條警報 → 1 個 Incident、只跑 1 次分診
# ---------------------------------------------------------------------------


def test_std07_storm_aggregation(store: Store, tmp_path: Path) -> None:
    from oncall_core.incident import CorrelateAction, Correlator

    correlator = Correlator(store)

    results = []
    for i in range(10):
        r = correlator.ingest_alert(
            fingerprint=f"fp-storm-{i}",
            labels={"cluster": "prod", "service": "api", "severity": "critical"},
            summary=f"alert {i}",
        )
        results.append(r)

    incidents = {r.incident.id for r in results}
    assert len(incidents) == 1, f"10 條同根因警報應聚為 1 個 Incident, 得到 {len(incidents)}"
    created = sum(1 for r in results if r.action is CorrelateAction.CREATED)
    merged = sum(1 for r in results if r.action in (CorrelateAction.MERGED,))
    assert created == 1 and merged == 9, "只有第一條觸發分診"


# ---------------------------------------------------------------------------
# 標準 8：取消檢查點——分診中自我緩解 → 中止不產報告
# ---------------------------------------------------------------------------


def test_std08_cancellation_checkpoint_zero_token(store: Store, tmp_path: Path) -> None:
    incident_id = "inc-cancel"
    store.ensure_incident(incident_id, fingerprint="fp-cancel")
    ledger = BudgetLedger()

    # 模擬「context 收集期間警報自我緩解」：先標 resolved 再跑管線
    from oncall_core.incident import transition

    transition(store, incident_id, "investigating")
    transition(store, incident_id, "mitigated")
    transition(store, incident_id, "resolved")

    provider = FakeProvider("llm")
    pipeline = make_pipeline(store, provider, tmp_path, ledger=ledger)
    outcome = pipeline.run(
        PipelineInput(incident_id=incident_id, context_summary={"service": "api"})
    )

    assert outcome.status == "aborted"
    assert outcome.report is None
    assert provider.call_count == 0, "中止後不得打 LLM, token 消耗為零"
    totals = ledger.totals()
    assert totals["llm_tokens_total"] == 0


# ---------------------------------------------------------------------------
# 標準 9：傳輸安全——readapi/ui 只綁 loopback(自動化部分)
# ---------------------------------------------------------------------------


def test_std09_transport_security_binding(store: Store) -> None:
    srv = ReadApiServer(store, port=0)
    try:
        assert str(srv.host).startswith("127."), "readapi 必須只聽 loopback"
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# 標準 10：容量暴漲情境——HPA 軌跡與 quota 快照進入分診輸入
# ---------------------------------------------------------------------------


def test_std10_capacity_scenario_hpa_quota(store: Store, tmp_path: Path) -> None:
    incident_id = "inc-capacity"
    store.ensure_incident(incident_id, fingerprint="fp-capacity")
    provider = FakeProvider("llm", responses=[good_json(incident_id)])
    pipeline = make_pipeline(store, provider, tmp_path)

    input_ = PipelineInput(
        incident_id=incident_id,
        context_summary={
            "scaling_events": [{"replicas_from": 4, "replicas_to": 12}],
            "quota_snapshot": {"cpu_used_pct": 98},
        },
        degraded_sources=[],
    )
    outcome = pipeline.run(input_)
    assert outcome.status == "report"

    # HPA 軌跡與 quota 快照必須出現在給 LLM 的 prompt(分診可見)
    assert provider.last_prompt is not None
    assert "replicas_from" in provider.last_prompt
    assert "quota_snapshot" in provider.last_prompt


# ---------------------------------------------------------------------------
# 標準 11/12：Shadow 門檻與 prompt 迭代閘門(流程級整合)
# ---------------------------------------------------------------------------


def test_std11_shadow_release_gate(store: Store) -> None:
    ctrl = ShadowController(store, enabled=True)
    with pytest.raises(Exception, match="scored=0/30"):
        ctrl.assert_can_disable()
    for i in range(30):
        ctrl.record_score(
            incident_id=f"s-{i}", cause_correct=True, action_usable=True, reviewer="human"
        )
    ctrl.disable()
    assert ctrl.enabled is False


def test_std12_prompt_version_gate(store: Store, indexer: KnowledgeIndexer, tmp_path: Path) -> None:
    """品質下降版本不得上線：v2 命中率低於 v1 → release gate 擋下。"""
    cases = []
    for i in range(20):
        cases.append(
            __import__("oncall_core.evalkit", fromlist=["ReplayCase"]).ReplayCase(
                case_id=f"g-{i:02d}",
                context_summary={"service": "api"},
                ground_truth_cause="config regression",
            )
        )
    from oncall_core.evalkit import EvalKit

    kit = EvalKit(store, shadow_dir=tmp_path / "shadow", reports_dir=tmp_path / "reports")
    responses_v1 = [
        good_json(c.case_id).replace('"bad deploy"', '"config regression"') for c in cases
    ]
    responses_v2 = [
        json.dumps(
            {
                "incident_id": c.case_id,
                "hypotheses": [{"cause": "unrelated noise", "confidence": 0.9, "evidence": []}],
                "suggested_actions": [],
                "missing_context": [],
                "prompt_version": "2.0.0",
            }
        )
        for c in cases
    ]

    v1 = kit.replay(
        cases, ProviderChain([FakeProvider("llm", responses=responses_v1)]), prompt_version="1.0.0"
    )
    v2 = kit.replay(
        cases, ProviderChain([FakeProvider("llm", responses=responses_v2)]), prompt_version="2.0.0"
    )

    comparison = kit.compare(v1, v2)
    assert comparison["verdict"] == "reject", "命中率下降的 v2 必須被擋"
    assert not kit.release_gate(v1, v2)


# ---------------------------------------------------------------------------
# 標準 13：認證冪等(HTTP 層, 3 次重送僅 1 Incident)
# ---------------------------------------------------------------------------


def test_std13_auth_and_idempotency_e2e(tmp_path: Path) -> None:
    """跨 process：無 secret 401；同 fingerprint 經 HTTP 重送 3 次 →
    gate 冪等回同結果, core 只收到 1 次(spec §5 標準 13 全鏈路)。"""
    gate_bin = REPO_ROOT / "gate" / "bin" / "gate"
    if not gate_bin.exists():
        pytest.skip("gate binary 未建置(make gate-build 後可用)")

    core_port = _free_port()
    gate_port = _free_port()
    db_path = tmp_path / "std13.db"
    core_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "oncall_core",
            "--db",
            str(db_path),
            "--addr",
            f"127.0.0.1:{core_port}",
        ],
        cwd=REPO_ROOT / "core",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    env = dict(
        os.environ,
        SHARED_SECRET="s13",
        CORE_ADDR=f"127.0.0.1:{core_port}",
        LISTEN_ADDR=f"127.0.0.1:{gate_port}",
    )
    gate_proc = subprocess.Popen(
        [str(gate_bin)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_port(core_port)
        _wait_port(gate_port)

        body = json.dumps(
            {
                "alerts": [
                    {
                        "fingerprint": "fp-std13",
                        "status": "firing",
                        "labels": {"service": "api", "cluster": "prod", "severity": "critical"},
                    }
                ]
            }
        )

        # 無 secret → 401

        req401 = urllib.request.Request(
            f"http://127.0.0.1:{gate_port}/alerts", data=body.encode(), method="POST"
        )
        try:
            urllib.request.urlopen(req401, timeout=10)
            raise AssertionError("無 secret 應 401")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401

        results = []
        for _ in range(3):
            req = urllib.request.Request(
                f"http://127.0.0.1:{gate_port}/alerts",
                data=body.encode(),
                method="POST",
                headers={"Authorization": "Bearer s13"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                results.append(json.loads(resp.read()))
        assert all(r["alerts"][0]["accepted"] for r in results)
        assert results[1]["alerts"][0]["deduplicated"], "第 2 次重送應標記冪等"
        assert results[2]["alerts"][0]["incident_id"] == results[0]["alerts"][0]["incident_id"]

        time.sleep(0.3)
        conn = sqlite3.connect(db_path)
        count = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        conn.close()
        assert count == 1, f"重送 3 次僅能產生 1 個 Incident, 得到 {count}"
    finally:
        gate_proc.terminate()
        core_proc.terminate()
        gate_proc.wait(timeout=10)
        core_proc.wait(timeout=10)


def _run_gate_retry_scenario(tmp_path: Path, gate_bin: Path) -> None:
    """標準 4：core down → 502；core 恢復 → 同 fingerprint 重試補分診。"""
    core_port = _free_port()
    gate_port = _free_port()

    # 階段一：只有 gate, core 不存在
    env = dict(
        os.environ,
        SHARED_SECRET="s4",
        CORE_ADDR=f"127.0.0.1:{core_port}",
        LISTEN_ADDR=f"127.0.0.1:{gate_port}",
    )
    gate_proc = subprocess.Popen(
        [str(gate_bin)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    body = json.dumps(
        {"alerts": [{"fingerprint": "fp-std4", "status": "firing", "labels": {"service": "api"}}]}
    )

    try:
        _wait_port(gate_port)
        req = urllib.request.Request(
            f"http://127.0.0.1:{gate_port}/alerts",
            data=body.encode(),
            method="POST",
            headers={"Authorization": "Bearer s4"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req, timeout=15)
        assert exc_info.value.code == 502, "core 掛掉時 gate 必須回 502 讓 AM 重試"
        time.sleep(1.0)  # 等過 gRPC 連線退避窗

        # 階段二：core 起來, 同一警報重試 → 成功入庫
        db_path = tmp_path / "std4.db"
        core_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "oncall_core",
                "--db",
                str(db_path),
                "--addr",
                f"127.0.0.1:{core_port}",
            ],
            cwd=REPO_ROOT / "core",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_port(core_port)
            # AM 重試語意：持續重送直到成功(gRPC 退避窗內可能先吃 502)
            payload = None
            for _attempt in range(10):
                try:
                    req = urllib.request.Request(
                        f"http://127.0.0.1:{gate_port}/alerts",
                        data=body.encode(),
                        method="POST",
                        headers={"Authorization": "Bearer s4"},
                    )
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        payload = json.loads(resp.read())
                    break
                except urllib.error.HTTPError:
                    time.sleep(0.5)
            assert payload is not None and payload["alerts"][0]["accepted"], "恢復後重試應成功"
        finally:
            core_proc.terminate()
            core_proc.wait(timeout=10)
    finally:
        gate_proc.terminate()
        gate_proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# 標準 14：遮蔽——注入假 token 樣式 → 時間線打碼、原始值只在加密稽核檔
# ---------------------------------------------------------------------------


def test_std14_redaction_timeline_audit(store: Store, tmp_path: Path) -> None:
    incident_id = "inc-redact"
    store.ensure_incident(incident_id, fingerprint="fp-redact")
    store.append_chained_event(incident_id, "approval_granted", {})

    secret_line = "rollout output: bearer abcdefghijklmnopqrst1234 done"
    runner = ExecutorRunner(
        store,
        command_runner=lambda a, *, dry_run: secret_line,
        audit_dir=tmp_path / "audit",
    )
    report = validate_report_for_std14(incident_id)
    outcome = runner.execute(incident_id, report)
    assert outcome.success

    timeline_text = json.dumps([dict(r) for r in store.timeline(incident_id)], ensure_ascii=False)
    assert "<REDACTED:bearer>" in timeline_text
    assert "abcdefghijklmnopqrst1234" not in timeline_text.replace("<REDACTED:bearer>", "")

    enc_files = list((tmp_path / "audit").glob("*.enc"))
    assert enc_files, "原始輸出必須存加密稽核檔"
    assert b"abcdefghijklmnopqrst1234" not in enc_files[0].read_bytes()


def validate_report_for_std14(incident_id: str):
    from oncall_core.brain.schema_validator import validate_report

    return validate_report(
        {
            "incident_id": incident_id,
            "hypotheses": [{"cause": "x", "confidence": 0.9, "evidence": []}],
            "suggested_actions": [{"action": "cmd", "risk": "read-only"}],
            "missing_context": [],
            "prompt_version": "2.1.0",
        }
    )


# ---------------------------------------------------------------------------
# 標準 15：雜湊鏈竄改偵測
# ---------------------------------------------------------------------------


def test_std15_hashchain_tamper_detection(store: Store) -> None:
    incident_id = "inc-chain"
    store.ensure_incident(incident_id, fingerprint="fp-chain")
    from oncall_core.incident import HashChain

    hc = HashChain(store)
    chain_ids = [
        hc.append(incident_id, "event_a", {"n": 1}),
        hc.append(incident_id, "event_b", {"n": 2}),
        hc.append(incident_id, "event_c", {"n": 3}),
    ]
    assert verify_chain(store, incident_id).ok

    # 竄改中間事件 payload
    store.tamper_timeline_payload(chain_ids[1], {"n": "tampered"})
    verdict = verify_chain(store, incident_id)
    assert not verdict.ok
    assert verdict.corrupt_id == chain_ids[1], "應標記損毀位置"


# ---------------------------------------------------------------------------
# 跨 process 契約測試：Go gate ↔ Python core 各自獨立行程
# ---------------------------------------------------------------------------


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_cross_process_grpc_contract(tmp_path: Path) -> None:
    """真實跨行程：subprocess 啟動 Python core daemon + Go gate binary。"""
    gate_bin = REPO_ROOT / "gate" / "bin" / "gate"
    if not gate_bin.exists():
        pytest.skip("gate binary 未建置(make gate-build 後可用)")

    core_port = _free_port()
    gate_port = _free_port()
    db_path = tmp_path / "e2e.db"

    # 接線驗證：LLM_PROVIDERS 指向假 LLM 端點，
    # 若 servicer→pipeline 接線斷裂，此 server 不會收到任何請求
    from test_openai_compat import FakeOpenAIServer

    llm = FakeOpenAIServer(dynamic_report=True)

    core_env = dict(os.environ)
    core_env["LLM_PROVIDERS"] = f"e2e-llm|{llm.url}|test-model|key"

    core_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "oncall_core",
            "--db",
            str(db_path),
            "--addr",
            f"127.0.0.1:{core_port}",
        ],
        cwd=REPO_ROOT / "core",
        env=core_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    env = dict(
        os.environ,
        SHARED_SECRET="e2e-secret",
        CORE_ADDR=f"127.0.0.1:{core_port}",
        LISTEN_ADDR=f"127.0.0.1:{gate_port}",
    )
    gate_proc = subprocess.Popen(
        [str(gate_bin)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_port(core_port)
        _wait_port(gate_port)

        body = json.dumps(
            {
                "alerts": [
                    {
                        "fingerprint": "e2e-cross-001",
                        "status": "firing",
                        "labels": {
                            "alertname": "HighLatency",
                            "service": "api",
                            "severity": "critical",
                        },
                        "annotations": {"summary": "e2e"},
                    }
                ]
            }
        )

        code: int | None = None
        payload: dict | None = None
        for attempt in range(10):  # gate 對 core 連線可能需要重試視窗
            req = urllib.request.Request(
                f"http://127.0.0.1:{gate_port}/alerts",
                data=body.encode(),
                method="POST",
                headers={"Authorization": "Bearer e2e-secret"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    code, payload = resp.status, json.loads(resp.read())
                break
            except Exception:
                if attempt == 9:
                    raise
                time.sleep(0.5)

        assert code == 200 and payload is not None
        assert payload["alerts"][0]["accepted"]
        incident_id = payload["alerts"][0]["incident_id"]

        # 驗證資料真的寫進 core 的 SQLite
        time.sleep(0.3)
        # 接線斷言：分診必須真的被觸發（predictions 入庫＋LLM 端點收到請求）
        deadline = time.time() + 10
        pred_row = None
        while time.time() < deadline:
            conn = sqlite3.connect(db_path)
            pred_row = conn.execute(
                "SELECT prompt_version FROM predictions WHERE incident_id = ?",
                (incident_id,),
            ).fetchone()
            conn.close()
            if pred_row is not None:
                break
            time.sleep(0.2)
        assert pred_row is not None, "ReportIncident 未觸發分診管線——servicer→pipeline 接線斷裂"
        assert llm.requests, "LLM 假端點未收到任何請求——接線或 provider 設定斷裂"

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT fingerprint FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        conn.close()
        assert row is not None and row[0] == "e2e-cross-001"
    finally:
        gate_proc.terminate()
        core_proc.terminate()
        gate_proc.wait(timeout=10)
        core_proc.wait(timeout=10)
        out = core_proc.stdout.read().decode() if core_proc.stdout else ""
        print("=== CORE LOG ===\n", out[-2000:])


def _wait_port(port: int, timeout: float = 20.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"port {port} not ready")
