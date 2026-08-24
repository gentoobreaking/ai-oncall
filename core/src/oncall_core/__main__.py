"""oncall-core daemon 進入點：gRPC server + readapi + 分診/批准/執行接線。"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
from collections.abc import Callable

import grpc

from oncall_core.approval_flow import ApprovalOrchestrator
from oncall_core.brain.budget import BudgetLedger
from oncall_core.brain.providers import provider_chain_from_env
from oncall_core.brain.schema_validator import validate_report
from oncall_core.brain.triage import TriagePipeline
from oncall_core.executor.runner import ExecutorRunner
from oncall_core.grpc_servicer import serve
from oncall_core.logging import get_logger, setup_logging
from oncall_core.memory import KnowledgeIndexer
from oncall_core.readapi import ReadApiServer
from oncall_core.shadow import ShadowController
from oncall_core.store import Store
from oncall_core.triage_runner import GateNotifier


def _log_only_command_runner(action: str, *, dry_run: bool) -> str:
    """EXECUTOR_MODE=log-only（預設）：記錄命令但不執行。"""
    prefix = "[dry-run] " if dry_run else ""
    return f"{prefix}{action}"


def _shell_command_runner(action: str, *, dry_run: bool) -> str:
    """生產 shell adapter：kubectl 動作支援 --dry-run=server。

    EXECUTOR_MODE=shell 時啟用；實際部署前應以組織的安全政策審視。
    """
    import subprocess

    if dry_run and action.startswith("kubectl"):
        parts = action.split()
        action = " ".join([*parts[:2], "--dry-run=server", *parts[2:]])
    result = subprocess.run(
        action,
        shell=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    output = (result.stdout + result.stderr).strip()
    return output or f"exit={result.returncode}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oncall-core")
    parser.add_argument("--db", default="data/oncall.db", help="SQLite 路徑")
    parser.add_argument("--addr", default="127.0.0.1:50051", help="gRPC 監聽位址")
    parser.add_argument(
        "--readapi-addr",
        default="127.0.0.1:8090",
        help="唯讀 HTTP API 監聽位址 ui 資料源",
    )
    parser.add_argument(
        "--gate-channel",
        default=os.environ.get("GATE_ADDR", ""),
        help="gate gRPC 位址, 設定後分診報告經 DeliverNotification 推播",
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    log = get_logger("main")

    store = Store(args.db)

    # 分診管線接線：LLM_PROVIDERS 未設定時 chain 僅含 FakeProvider
    # -> 視為離線模式(只建檔不分診), 避免意外燒 token
    chain = provider_chain_from_env()
    llm_names = [n for n in chain.provider_states() if not n.startswith("fake")]
    shadow = ShadowController(store)
    run_triage: Callable[[str], None] | None = None
    orchestrator: ApprovalOrchestrator | None = None

    if not llm_names:
        log.warning("LLM_PROVIDERS 未設定 - 僅建檔不分診(離線模式)")
    else:
        notifier = None
        if args.gate_channel:
            gate_channel = grpc.insecure_channel(args.gate_channel)
            notifier = GateNotifier(gate_channel)

        pipeline = TriagePipeline(
            store,
            chain,
            BudgetLedger(),
            prompt_version=os.environ.get("TRIAGE_PROMPT_VERSION", "1.0.0"),
            shadow_mode=shadow.enabled,
            shadow_dir=os.environ.get("SHADOW_DIR", "shadow_reports"),
        )

        executor_mode = os.environ.get("EXECUTOR_MODE", "log-only")
        runner = ExecutorRunner(
            store,
            command_runner=(
                _shell_command_runner if executor_mode == "shell" else _log_only_command_runner
            ),
            audit_dir=os.environ.get("AUDIT_DIR", "audit"),
            retention_days=int(os.environ.get("AUDIT_RETENTION_DAYS", "90")),
        )
        if executor_mode == "shell":
            log.warning("EXECUTOR_MODE=shell - 批准後將實際執行命令")

        approval_timeout = float(os.environ.get("APPROVAL_TIMEOUT", "300"))
        indexer = KnowledgeIndexer(store)
        from oncall_core.runbook.approval import ApprovalGate

        approval_gate = ApprovalGate(
            store,
            indexer,
            notifier=notifier,
        )

        def run_triage(incident_id: str) -> None:
            """背景分診完成後註冊批准請求（mutating 動作）。"""
            pipeline.run(_pipeline_input(store, incident_id))
            pred = store.latest_prediction(incident_id)
            if pred is None:
                return
            report = _report_from_prediction(incident_id, pred)
            if report is None:
                return
            orchestrator.register_from_report(incident_id, report)

        orchestrator = ApprovalOrchestrator(
            store,
            approval_gate,
            runner=runner,
            timeout_seconds=approval_timeout,
            notifier=notifier,
        )
        orchestrator.start_timeout_scheduler(interval=30)
        run_triage = orchestrator_wrapped(store, run_triage, orchestrator)
        log.info(
            "triage wired",
            llm_providers=llm_names,
            shadow=shadow.enabled,
            executor_mode=executor_mode,
        )

    server = serve(store, args.addr, run_triage=run_triage)
    server.start()
    host, _, port = args.readapi_addr.rpartition(":")
    readapi = ReadApiServer(store, host=host or "127.0.0.1", port=int(port))
    readapi.start_background()
    log.info("oncall-core started", addr=args.addr, readapi=readapi.url, db=args.db)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        stop.wait()
    except KeyboardInterrupt:
        pass
    finally:
        readapi.stop()
        server.stop(grace=5).wait()
        store.close()
        log.info("oncall-core stopped")
    return 0


def orchestrator_wrapped(store: Store, base_run_triage, orchestrator):
    """分診完成後註冊批准請求（mutating 動作）。"""

    def wrapped(incident_id: str) -> None:
        base_run_triage(incident_id)
        pred = store.latest_prediction(incident_id)
        if pred is None:
            return
        data = {
            "incident_id": incident_id,
            "hypotheses": json.loads(pred["hypotheses_json"]),
            "suggested_actions": json.loads(pred["actions_json"]),
            "missing_context": json.loads(pred["missing_context_json"]),
            "prompt_version": pred["prompt_version"],
        }
        report = validate_report(data)
        orchestrator.register_from_report(incident_id, report)

    return wrapped


def _pipeline_input(store: Store, incident_id: str):
    from oncall_core.brain.triage import PipelineInput

    inc = store.get_incident(incident_id)
    labels = dict(inc.labels) if inc else {}
    return PipelineInput(
        incident_id=incident_id,
        context_summary={"title": inc.title if inc else "", "labels": labels},
        rag_hits=[],
    )


def _report_from_prediction(incident_id: str, pred):
    try:
        return validate_report(
            {
                "incident_id": incident_id,
                "hypotheses": json.loads(pred["hypotheses_json"]),
                "suggested_actions": json.loads(pred["actions_json"]),
                "missing_context": json.loads(pred["missing_context_json"]),
                "prompt_version": pred["prompt_version"],
            }
        )
    except Exception as exc:
        get_logger(__name__).warning("prediction to report failed", error=str(exc))
        return None


if __name__ == "__main__":
    sys.exit(main())
