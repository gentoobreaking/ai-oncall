"""oncall-core daemon 進入點：gRPC server + readapi + 分診接線。"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading

import grpc

from oncall_core.brain.budget import BudgetLedger
from oncall_core.brain.providers import provider_chain_from_env
from oncall_core.brain.triage import TriagePipeline
from oncall_core.grpc_servicer import serve
from oncall_core.logging import get_logger, setup_logging
from oncall_core.readapi import ReadApiServer
from oncall_core.shadow import ShadowController
from oncall_core.store import Store
from oncall_core.triage_runner import GateNotifier, make_triage_runner


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oncall-core")
    parser.add_argument("--db", default="data/oncall.db", help="SQLite 路徑")
    parser.add_argument("--addr", default="127.0.0.1:50051", help="gRPC 監聽位址")
    parser.add_argument(
        "--readapi-addr",
        default="127.0.0.1:8090",
        help="唯讀 HTTP API 監聽位址 - ui 資料源",
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
    run_triage = None
    notifier = None
    if llm_names:
        ledger = BudgetLedger()
        pipeline = TriagePipeline(
            store,
            chain,
            ledger,
            prompt_version=os.environ.get("TRIAGE_PROMPT_VERSION", "1.0.0"),
            shadow_mode=shadow.enabled,
            shadow_dir=os.environ.get("SHADOW_DIR", "shadow_reports"),
        )
        if args.gate_channel and not shadow.enabled:
            gate_channel = grpc.insecure_channel(args.gate_channel)
            notifier = GateNotifier(gate_channel)
        run_triage = make_triage_runner(store, pipeline, notifier, shadow=shadow.enabled)
        log.info("triage wired", llm_providers=llm_names, shadow=shadow.enabled)
    else:
        log.warning("LLM_PROVIDERS 未設定 - 僅建檔不分診(離線模式)")

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


if __name__ == "__main__":
    sys.exit(main())
