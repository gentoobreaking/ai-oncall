"""structlog 設定：全 core 統一 JSON 結構化日誌。"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: str = "INFO") -> None:
    """設定 structlog 輸出 JSON 到 stdout，與 gate 的 slog 對齊。"""
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name=name)  # type: ignore[no-any-return]
