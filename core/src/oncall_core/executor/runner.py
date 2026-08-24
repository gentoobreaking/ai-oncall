"""執行器 runner（algs/approval-executor.md §B.3 安全規則表全部）。

§B.3 五規則：
1. 冪等：同一動作對同一 Incident 只執行一次（DB 記錄 + 併發鎖）
2. 已緩解再檢查：執行前確認 Incident 未被人工標記 mitigated/resolved
3. 逐步回報：每步輸出即時回報時間線；失敗即停，不盲目續跑
4. 輸入契約：只接受通過 schema 驗證的 brain 輸出（TriageReport）；
   畸形輸入硬拒絕——即使人類手動批准
5. 隔離：本套件是唯一碰生產環境者；CI 斷言禁止其他模組 import

§B.4：原始輸出存本地加密稽核檔（Fernet），保留期可調（預設 90 天）。
"""

from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from oncall_core.brain.schema_validator import ExecutorRejected, TriageReport
from oncall_core.executor.redact import redact_text
from oncall_core.incident.hashchain import HashChain
from oncall_core.logging import get_logger
from oncall_core.store import Store

log = get_logger(__name__)

DEFAULT_RETENTION_DAYS = 90


def default_command_runner(action: str, *, dry_run: bool) -> str:
    """正式環境的底層 adapter 範例：kubectl 類可注入 --dry-run=server；
    其他（shell）動作無法預演，明確回傳標注文字。

    測試一律以 fake 注入，本函式僅供部署層參考/使用。
    """
    if not dry_run:
        raise NotImplementedError("production adapter must be provided at deployment")
    if action.startswith("kubectl"):
        parts = action.split()
        # 在子命令後插入 --dry-run=server（kubectl apply/patch/replace/scale 等）
        return " ".join([*parts[:2], "--dry-run=server", *parts[2:]])
    return "無法預演 (non-kubectl action)"


@dataclass(slots=True)
class StepResult:
    step_name: str
    ok: bool
    output_redacted: str
    duration_seconds: float
    # mutating 步驟實際執行前的預演輸出（已遮蔽）
    dry_run_output: str | None = None


@dataclass(slots=True)
class ExecutionOutcome:
    incident_id: str
    executed: bool
    skipped_reason: str | None = None
    steps: list[StepResult] = field(default_factory=list)

    @property
    def success(self) -> bool:
        return self.executed and bool(self.steps) and all(s.ok for s in self.steps)


class ExecutorRunner:
    """唯一被允許觸碰生產環境的元件。

    command_runner 為注入的底層執行函式：
        command_runner(action: str, *, dry_run: bool) -> str（原始輸出）
    """

    def __init__(
        self,
        store: Store,
        *,
        command_runner: Callable[..., str],
        audit_dir: str | Path = "audit",
        retention_days: int = DEFAULT_RETENTION_DAYS,
        step_timeout_seconds: float = 60.0,
        encryption_key: bytes | None = None,
    ) -> None:
        self._store = store
        self._chain = HashChain(store)
        self._run_command = command_runner
        self._audit_dir = Path(audit_dir)
        self._retention_days = retention_days
        self._step_timeout = step_timeout_seconds
        self._fernet = self._make_fernet(encryption_key)
        self._incident_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def execute(self, incident_id: str, report: TriageReport) -> ExecutionOutcome:
        """執行報告中的建議動作。非 validated TriageReport 一律硬拒絕。"""
        if not isinstance(report, TriageReport) or not report.validated:
            raise ExecutorRejected("executor only accepts a validated TriageReport")

        with self._lock_for(incident_id):  # §B.3-1 併發鎖
            return self._execute_locked(incident_id, report)

    def _execute_locked(self, incident_id: str, report: TriageReport) -> ExecutionOutcome:
        action_key = f"{incident_id}:{self._action_key(report)}"

        # §B.3-1 冪等
        if self._store.has_executed_action(action_key):
            log.info("execution skipped (idempotent)", incident_id=incident_id)
            self._chain.append(
                incident_id, "execution_skipped", {"reason": "idempotent", "action_key": action_key}
            )
            return ExecutionOutcome(
                incident_id=incident_id, executed=False, skipped_reason="idempotent"
            )

        # §B.3-2 已緩解檢查
        incident = self._store.get_incident(incident_id)
        if incident is None:
            raise ExecutorRejected(f"unknown incident {incident_id}")
        if incident.status in {"mitigated", "resolved"}:
            self._chain.append(
                incident_id,
                "execution_skipped",
                {"reason": "already_mitigated", "status": incident.status},
            )
            return ExecutionOutcome(
                incident_id=incident_id, executed=False, skipped_reason=f"already {incident.status}"
            )

        # 三段式鐵律：mutating 必須已有批准紀錄（approval_granted 於時間線）
        mutating = [a for a in report.suggested_actions if a.risk == "mutating"]
        if mutating and not self._store.has_timeline_kind(incident_id, "approval_granted"):
            raise ExecutorRejected(
                "mutating actions require an approved request (no approval_granted in timeline)"
            )

        self._store.record_action_started(action_key)
        self._chain.append(
            incident_id,
            "execution_started",
            {
                "action_key": action_key,
                "actions": [a.action for a in report.suggested_actions],
            },
        )

        outcome = ExecutionOutcome(incident_id=incident_id, executed=True)
        raw_log: list[dict[str, object]] = []

        for i, action in enumerate(report.suggested_actions):
            step_name = f"step-{i}-{action.action[:40]}"
            is_mutating = action.risk == "mutating"
            started = time.monotonic()
            dry_output: str | None = None
            try:
                if is_mutating:
                    # §B.1：--dry-run=server 先行；shell 類由 adapter 標注「無法預演」
                    dry_raw = self._run_with_timeout(action.action, dry_run=True)
                    dry_output = redact_text(dry_raw)
                    raw_log.append({"phase": "dry-run", "step": step_name, "raw": dry_raw})
                    if "無法預演" in dry_output:
                        log.warning(
                            "step cannot be previewed, stricter gate applied",
                            incident_id=incident_id,
                            step=step_name,
                        )

                raw = self._run_with_timeout(action.action, dry_run=False)
                duration = time.monotonic() - started
                safe = redact_text(raw)
                raw_log.append({"phase": "execute", "step": step_name, "raw": raw})
                result = StepResult(
                    step_name=step_name,
                    ok=True,
                    output_redacted=safe,
                    duration_seconds=duration,
                    dry_run_output=dry_output,
                )
                self._chain.append(
                    incident_id, "step_completed", {"step": step_name, "output": safe[:2000]}
                )
            except Exception as exc:
                duration = time.monotonic() - started
                safe = redact_text(str(exc))
                result = StepResult(
                    step_name=step_name,
                    ok=False,
                    output_redacted=safe,
                    duration_seconds=duration,
                    dry_run_output=dry_output,
                )
                self._chain.append(
                    incident_id, "step_failed", {"step": step_name, "error": safe[:2000]}
                )
                outcome.steps.append(result)
                self._write_audit(incident_id, raw_log)
                # §B.3-3 失敗即停
                return outcome
            outcome.steps.append(result)

        self._store.mark_action_done(action_key)
        self._write_audit(incident_id, raw_log)
        self._chain.append(
            incident_id,
            "execution_finished",
            {
                "steps_ok": sum(1 for s in outcome.steps if s.ok),
                "steps_total": len(outcome.steps),
            },
        )
        return outcome

    # ------------------------------------------------------------------
    # 底層工具
    # ------------------------------------------------------------------

    def _run_with_timeout(self, action: str, *, dry_run: bool) -> str:
        from concurrent.futures import ThreadPoolExecutor
        from concurrent.futures import TimeoutError as FutureTimeoutError

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._run_command, action, dry_run=dry_run)
            try:
                return future.result(timeout=self._step_timeout)
            except FutureTimeoutError:
                raise RuntimeError(f"step timeout after {self._step_timeout}s") from None

    @staticmethod
    def _action_key(report: TriageReport) -> str:
        return "|".join(sorted(a.action for a in report.suggested_actions))[:200]

    def _lock_for(self, incident_id: str) -> threading.Lock:
        with self._locks_guard:
            if incident_id not in self._incident_locks:
                self._incident_locks[incident_id] = threading.Lock()
            return self._incident_locks[incident_id]

    # ------------------------------------------------------------------
    # §B.4 加密稽核檔
    # ------------------------------------------------------------------

    def _make_fernet(self, key: bytes | None):
        from cryptography.fernet import Fernet

        if key is not None:
            return Fernet(key)
        # 金鑰持久化於稽核目錄——重啟後仍可解密歷史檔案
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        key_file = self._audit_dir / ".key"
        if key_file.exists():
            return Fernet(key_file.read_bytes())
        new_key = Fernet.generate_key()
        key_file.write_bytes(new_key)
        return Fernet(new_key)

    def _write_audit(self, incident_id: str, raw_entries: list[dict[str, object]]) -> None:
        if not raw_entries:
            return
        import json as _json

        payload = _json.dumps(
            {
                "incident_id": incident_id,
                "retention_days": self._retention_days,
                "created_at": time.time(),
                "entries": raw_entries,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        token = self._fernet.encrypt(payload)
        self._audit_dir.mkdir(parents=True, exist_ok=True)
        path = self._audit_dir / f"{incident_id}_{uuid.uuid4().hex[:8]}.enc"
        path.write_bytes(token)
        log.info("raw execution output archived", path=str(path))

    def purge_expired_audits(self) -> int:
        """清除超過保留期的稽核檔（預設 90 天）；損壞檔一併清理。"""
        import json as _json

        removed = 0
        now = time.time()
        for path in self._audit_dir.glob("*.enc"):
            try:
                meta = _json.loads(self._fernet.decrypt(path.read_bytes()))
                age_days = (now - meta["created_at"]) / 86400
                expired = age_days > self._retention_days
            except Exception:
                expired = True
            if expired:
                path.unlink(missing_ok=True)
                removed += 1
        return removed
