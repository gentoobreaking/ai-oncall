"""oncall-core daemon 進入點：gRPC server（gate → core 介面）。"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from oncall_core.grpc_servicer import serve
from oncall_core.logging import get_logger, setup_logging
from oncall_core.readapi import ReadApiServer
from oncall_core.store import Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="oncall-core")
    parser.add_argument("--db", default="data/oncall.db", help="SQLite 路徑")
    parser.add_argument("--addr", default="127.0.0.1:50051", help="gRPC 監聽位址")
    parser.add_argument("--readapi-addr", default="127.0.0.1:8090",
                        help="唯讀 HTTP API 監聽位址（ui 資料源）")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    setup_logging(args.log_level)
    log = get_logger("main")

    store = Store(args.db)
    server = serve(store, args.addr)
    server.start()
    host, _, port = args.readapi_addr.rpartition(":")
    readapi = ReadApiServer(store, host=host or "127.0.0.1", port=int(port))
    readapi.start_background()
    log.info("oncall-core started", addr=args.addr,
             readapi=readapi.url, db=args.db)

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
