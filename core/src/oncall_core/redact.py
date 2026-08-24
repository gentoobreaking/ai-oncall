"""金鑰樣式遮蔽（algs/knowledge-flywheel.md §D.5 / approval-executor.md §B.4）。

入庫（memory）與執行輸出外流（executor/T011）共用同一組樣式掃描。
"""

from __future__ import annotations

import re

# 常見金鑰樣式；遮蔽為 <REDACTED:類別>
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer", re.compile(r"(?i)bearer\s+[a-z0-9._\-]{16,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(r"(?i)aws.{0,20}?['\"][0-9a-zA-Z/+]{40}['\"]")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*\b")),
    (
        "conn_string",
        re.compile(r"(?i)\b(postgres|mysql|mongodb(\+srv)?|redis|amqp)://[^\s:/@]+:[^\s/@]+@"),
    ),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "generic_api_key",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)['\"]?\s*[:=]\s*['\"][^\s'\"]{8,}['\"]"
        ),
    ),
)


def redact_text(text: str) -> str:
    """掃描金鑰樣式並打碼。回傳可安全入库/外流的文字。"""
    out = text
    for name, pattern in SECRET_PATTERNS:
        out = pattern.sub(f"<REDACTED:{name}>", out)
    return out


def contains_secret(text: str) -> str | None:
    """回傳第一個命中的金鑰類別；無則 None。"""
    for name, pattern in SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None
