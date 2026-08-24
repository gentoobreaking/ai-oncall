"""T011 測試：§B.3 五規則、§B.4 遮蔽與加密稽核、dry-run 先行。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from oncall_core.brain.schema_validator import (
    ExecutorRejected,
    TriageReport,
    validate_report,
)
from oncall_core.executor import ExecutorRunner, redact_text
from oncall_core.executor.runner import ExecutionOutcome
from oncall_core.incident import transition
from oncall_core.store import Store


@pytest.fixture()
def store(tmp_path) -> Store:
    return Store(tmp_path / "t011.db")


@pytest.fixture()
def audit_dir(tmp_path) -> Path:
    return tmp_path / "audit"


def make_incident(store: Store, fingerprint: str = "fp-exec") -> str:
    inc, _ = store.create_incident(fingerprint=fingerprint)
    return inc.id


def good_report(incident_id: str, risk: str = "read-only") -> TriageReport:
    return validate_report(
        {
            "incident_id": incident_id,
            "hypotheses": [{"cause": "x", "confidence": 0.9, "evidence": []}],
            "suggested_actions": [
                {"action": "kubectl rollout undo deployment/api", "risk": risk, "runbook_ref": None}
            ],
            "missing_context": [],
            "prompt_version": "2.1.0",
        }
    )


class FakeRunner:
    """可控成敗的底層執行函式；記錄每次呼叫與 dry_run 旗標。"""

    def __init__(self, outputs: list[str] | str = "ok", fail_on_call: int | None = None):
        self.outputs = [outputs] if isinstance(outputs, str) else outputs
        self.fail_on_call = fail_on_call
        self.calls: list[tuple[str, bool]] = []
        self.lock = threading.Lock()

    def __call__(self, action: str, *, dry_run: bool) -> str:
        with self.lock:
            n = len(self.calls)
            self.calls.append((action, dry_run))
        if self.fail_on_call is not None and n == self.fail_on_call:
            raise RuntimeError("boom: token ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        return self.outputs[n] if n < len(self.outputs) else "ok"


def approved_runner(store: Store, incident_id: str, **kwargs) -> ExecutorRunner:
    """已含 approval_granted 紀錄的 runner（mutating 前提）。"""
    store.append_chained_event(incident_id, "approval_granted", {"by": "test"})
    return ExecutorRunner(
        store, command_runner=FakeRunner(), audit_dir=kwargs.pop("audit_dir"), **kwargs
    )


# ---------------------------------------------------------------------------
# §B.3-4 輸入契約：硬拒絕
# ---------------------------------------------------------------------------


def test_hard_rejects_non_validated_input(store: Store, audit_dir: Path) -> None:
    runner = ExecutorRunner(store, command_runner=FakeRunner(), audit_dir=audit_dir)
    incident_id = make_incident(store)

    with pytest.raises(ExecutorRejected):
        runner.execute(incident_id, {"action": "rollback"})  # type: ignore[arg-type]
    with pytest.raises(ExecutorRejected):
        report = good_report(incident_id)
        object.__setattr__(report, "validated", False)
        runner.execute(incident_id, report)


def test_hard_rejects_unknown_incident(store: Store, audit_dir: Path) -> None:
    runner = ExecutorRunner(store, command_runner=FakeRunner(), audit_dir=audit_dir)
    report = good_report("inc-ghost")
    with pytest.raises(ExecutorRejected, match="unknown incident"):
        runner.execute("inc-ghost", report)


# ---------------------------------------------------------------------------
# §B.3-1 冪等 + 併發鎖
# ---------------------------------------------------------------------------


def test_idempotent_same_action_executed_once(store: Store, audit_dir: Path) -> None:
    incident_id = make_incident(store)
    fake = FakeRunner()
    store.append_chained_event(incident_id, "approval_granted", {})
    runner = ExecutorRunner(store, command_runner=fake, audit_dir=audit_dir)
    report = good_report(incident_id)

    r1 = runner.execute(incident_id, report)
    r2 = runner.execute(incident_id, report)

    assert r1.executed and r1.success
    assert not r2.executed and r2.skipped_reason == "idempotent"
    assert len(fake.calls) == 1, "第二次不得真的執行"


def test_concurrent_approvals_only_execute_once(store: Store, audit_dir: Path) -> None:
    """併發鎖防止重複批准競態：兩執行緒同時執行，底層只跑一次。"""
    incident_id = make_incident(store)
    fake = FakeRunner()
    store.append_chained_event(incident_id, "approval_granted", {})
    runner = ExecutorRunner(store, command_runner=fake, audit_dir=audit_dir)
    report = good_report(incident_id)

    outcomes: list[ExecutionOutcome] = []

    def worker() -> None:
        outcomes.append(runner.execute(incident_id, report))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    executed_count = sum(1 for o in outcomes if o.executed)
    assert executed_count == 1, f"併發下應只有一個真正執行-得到 {executed_count}"
    assert isinstance(outcomes[0], ExecutionOutcome)


# ---------------------------------------------------------------------------
# §B.3-2 已緩解檢查
# ---------------------------------------------------------------------------


def test_skips_when_already_mitigated(store: Store, audit_dir: Path) -> None:
    incident_id = make_incident(store)
    transition(store, incident_id, "investigating")
    transition(store, incident_id, "mitigated")

    fake = FakeRunner()
    runner = ExecutorRunner(store, command_runner=fake, audit_dir=audit_dir)
    outcome = runner.execute(incident_id, good_report(incident_id))

    assert not outcome.executed and outcome.skipped_reason == "already mitigated"
    assert fake.calls == [], "已緩解不得觸碰生產環境"
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "execution_skipped" in kinds


# ---------------------------------------------------------------------------
# §B.3-3 逐步回報 + 失敗即停
# ---------------------------------------------------------------------------


def test_fail_fast_stops_subsequent_steps(store: Store, audit_dir: Path) -> None:
    incident_id = make_incident(store)
    store.append_chained_event(incident_id, "approval_granted", {})

    class TwoStepReport(TriageReport):
        pass

    report = validate_report(
        {
            "incident_id": incident_id,
            "hypotheses": [{"cause": "x", "confidence": 0.9, "evidence": []}],
            "suggested_actions": [
                {"action": "cmd-a", "risk": "read-only"},
                {"action": "cmd-b", "risk": "read-only"},
                {"action": "cmd-c", "risk": "read-only"},
            ],
            "missing_context": [],
            "prompt_version": "2.1.0",
        }
    )
    fake = FakeRunner(outputs=["a ok", "b ok", "c ok"], fail_on_call=1)  # 第 2 通呼叫失敗
    runner = ExecutorRunner(store, command_runner=fake, audit_dir=audit_dir)
    outcome = runner.execute(incident_id, report)

    assert not outcome.success
    assert len(fake.calls) == 2, "失敗即停-第三步不得執行"
    assert outcome.steps[1].ok is False
    kinds = [r["kind"] for r in store.timeline(incident_id)]
    assert "step_failed" in kinds


def test_step_outputs_redacted_into_timeline(store: Store, audit_dir: Path) -> None:
    incident_id = make_incident(store)
    secret_output = "connected with bearer abcdefghijklmnopqrst1234 - done"
    fake = FakeRunner(outputs=secret_output)
    runner = ExecutorRunner(store, command_runner=fake, audit_dir=audit_dir)
    outcome = runner.execute(incident_id, good_report(incident_id))

    assert outcome.success
    assert all("<REDACTED:" in s.output_redacted for s in outcome.steps)
    timeline_text = json.dumps([dict(r) for r in store.timeline(incident_id)], ensure_ascii=False)
    assert "abcdefghijklmnopqrst" not in timeline_text, "金鑰不得進入時間線"


# ---------------------------------------------------------------------------
# mutating 三段式：dry-run 先行 + 未批准拒絕
# ---------------------------------------------------------------------------


def test_mutating_without_approval_rejected(store: Store, audit_dir: Path) -> None:
    incident_id = make_incident(store)
    fake = FakeRunner()
    runner = ExecutorRunner(store, command_runner=fake, audit_dir=audit_dir)

    with pytest.raises(ExecutorRejected, match="approved request"):
        runner.execute(incident_id, good_report(incident_id, risk="mutating"))
    assert fake.calls == []


def test_mutating_runs_dry_run_before_real(store: Store, audit_dir: Path) -> None:
    incident_id = make_incident(store)
    store.append_chained_event(incident_id, "approval_granted", {})
    fake = FakeRunner(outputs=["dry-run ok", "applied"])
    runner = ExecutorRunner(store, command_runner=fake, audit_dir=audit_dir)

    outcome = runner.execute(incident_id, good_report(incident_id, risk="mutating"))
    assert outcome.success
    # dry-run 先行：第一通 dry_run=True 且注入 --dry-run=server 的責任在 adapter；
    # 此處驗證旗標順序
    assert fake.calls[0] == (fake.calls[0][0], True)
    assert fake.calls[1] == (fake.calls[1][0], False)
    assert outcome.steps[0].dry_run_output == "dry-run ok"


def test_default_adapter_injects_dry_run_server_for_kubectl() -> None:
    from oncall_core.executor.runner import default_command_runner

    out = default_command_runner("kubectl scale deployment api --replicas=6", dry_run=True)
    assert "--dry-run=server" in out

    shell_out = default_command_runner("systemctl restart api", dry_run=True)
    assert "無法預演" in shell_out


# ---------------------------------------------------------------------------
# §B.4 遮蔽 ≥8 案例 + 加密稽核檔
# ---------------------------------------------------------------------------

REDACT_CASES = [
    ("bearer", "auth bearer abcdefghijklmnopqrst1234"),
    ("aws_key", "key AKIAIOSFODNN7EXAMPLE in use"),
    ("github", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"),
    ("slack", "xoxb-123456789-abcdef"),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"),
    ("aliyun", "access key LTAI5tABcDefGhiJklMnop"),
    ("gcp_key_id", '{"private_key_id": "abcdef0123456789abcdef0123456789abcdef01"}'),
    ("conn_str", "postgres://admin:s3cret@db:5432/app"),
    ("private_key", "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...\n-----END RSA PRIVATE KEY-----"),
]


def test_redact_covers_at_least_8_secret_patterns() -> None:
    assert len(REDACT_CASES) >= 8
    for name, sample in REDACT_CASES:
        masked = redact_text(sample)
        assert "<REDACTED:" in masked, f"{name} 未被打碼: {masked}"


def test_raw_output_encrypted_audit_file(store: Store, audit_dir: Path) -> None:
    incident_id = make_incident(store)
    raw = "secret output: ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    fake = FakeRunner(outputs=raw)
    runner = ExecutorRunner(store, command_runner=fake, audit_dir=audit_dir)
    outcome = runner.execute(incident_id, good_report(incident_id))
    assert outcome.success

    enc_files = list(audit_dir.glob("*.enc"))
    assert len(enc_files) == 1, "原始輸出必須存加密稽核檔"
    encrypted_bytes = enc_files[0].read_bytes()
    assert b"ghp_" not in encrypted_bytes, "稽核檔必須加密"

    # 可用同一把金鑰解密驗證原始內容存在
    from cryptography.fernet import Fernet

    key = (audit_dir / ".key").read_bytes()
    plain = Fernet(key).decrypt(encrypted_bytes).decode()
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" in plain


def test_purge_expired_audits(store: Store, audit_dir: Path) -> None:
    incident_id = make_incident(store)
    fake = FakeRunner(outputs="some output")
    runner = ExecutorRunner(
        store,
        command_runner=fake,
        audit_dir=audit_dir,
        retention_days=90,
    )
    outcome = runner.execute(incident_id, good_report(incident_id))
    assert outcome.success
    assert len(list(audit_dir.glob("*.enc"))) == 1
    assert runner.purge_expired_audits() == 0, "未逾期不應清除"


# ---------------------------------------------------------------------------
# §B.3-5 隔離：CI 斷言——其他模組禁止 import executor
# ---------------------------------------------------------------------------


def test_no_external_imports_of_executor() -> None:
    """§B.3-5 隔離鐵律的 CI 斷言。"""
    src_root = Path(__file__).resolve().parents[1] / "src" / "oncall_core"
    # 組合根（daemon 進入點）是唯一被允許 import executor 的位置——
    # 它負責把 ExecutorRunner 注入業務流程；其餘模組一律禁止。
    allowed_importers = {"__main__.py"}
    violations: list[str] = []
    for py in src_root.rglob("*.py"):
        rel = py.relative_to(src_root)
        if rel.parts[0] == "executor" or rel.name in allowed_importers:
            continue
        content = py.read_text(encoding="utf-8")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith(("import oncall_core.executor", "from oncall_core.executor")):
                violations.append(f"{rel}: {stripped}")
    assert violations == [], f"executor 只能被頂層進入點使用-違規:\n{violations}"
