"""oncall-ui 進入點：uvicorn 啟動（預設只聽 127.0.0.1）。"""

from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(prog="oncall-ui")
    parser.add_argument(
        "--host", default="127.0.0.1", help="僅綁 127.0.0.1；對外一律經反向代理認證"
    )
    parser.add_argument("--port", type=int, default=8091)
    parser.add_argument("--readapi-url", default="http://127.0.0.1:8090")
    args = parser.parse_args()

    if args.host not in ("127.0.0.1", "localhost"):
        print("WARNING: oncall-ui 綁定非 loopback 位址——對外必須經反向代理認證")

    import os

    from oncall_ui.client import default_readapi_url  # noqa: F401

    os.environ.setdefault("READAPI_URL", args.readapi_url)
    uvicorn.run(
        "oncall_ui.app:create_app", factory=True, host=args.host, port=args.port, log_level="info"
    )


if __name__ == "__main__":
    main()
