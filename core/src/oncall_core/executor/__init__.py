"""★ executor 頂層套件——全系統唯一允許碰生產環境的模組。

邊界鐵律（spec §2.2）：其他模組**禁止** import 本套件；
CI 以測試斷言強制（tests/test_t011_executor.py::test_no_external_imports）。
"""

from oncall_core.executor.redact import (
    SECRET_PATTERNS,
    contains_secret,
    redact_text,
)
from oncall_core.executor.runner import (
    ExecutionOutcome,
    ExecutorRunner,
    StepResult,
)

__all__ = [
    "SECRET_PATTERNS",
    "ExecutionOutcome",
    "ExecutorRunner",
    "StepResult",
    "contains_secret",
    "redact_text",
]
