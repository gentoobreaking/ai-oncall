"""輸出遮蔽（algs/approval-executor.md §B.4 / F18）。

遮蔽層是 executor 的出口過濾器：任何離開 executor 的文字
（Telegram／時間線／UI API）一律先過本模組。
原始未遮蔽輸出僅存本地加密稽核檔（runner 責任），保留期可調（預設 90 天）。
"""

from __future__ import annotations

import re

# 涵蓋 oncall_core.redact 的通用樣式，另補雲端憑證樣式：
#   - GCP service account JSON 內的 private_key 區塊（由私鑰樣式涵蓋）
#   - 阿里雲 AccessKey Id：LTAI 開頭
SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("bearer", re.compile(r"(?i)bearer\s+[a-z0-9._\-]{16,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("aws_secret", re.compile(r"(?i)aws.{0,20}?['\"][0-9a-zA-Z/+]{40}['\"]")),
    ("github_pat", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*\b")),
    ("aliyun_access_key", re.compile(r"\bLTAI[0-9A-Za-z]{12,20}\b")),
    ("gcp_service_account_key_id", re.compile(r'"private_key_id"\s*:\s*"[0-9a-f]{32,}"')),
    (
        "conn_string",
        re.compile(r"(?i)\b(postgres|mysql|mongodb(\+srv)?|redis|amqp)://[^\s:/@]+:[^\s/@]+@"),
    ),
    (
        "private_key_block",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^-]*-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
        ),
    ),
    (
        "generic_api_key",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|token|password)['\"]?\s*[:=]\s*['\"][^\s'\"]{8,}['\"]"
        ),
    ),
)


def redact_text(text: str) -> str:
    """掃描金鑰樣式並以 <REDACTED:類別> 打碼。任何離開 executor 的文字必經此層。"""
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
