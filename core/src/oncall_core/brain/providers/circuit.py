"""熔斷器：連續失敗 N 次後跳閘，冷卻期內直接拒絕不浪費逾時等待。"""

from __future__ import annotations

import time
from collections.abc import Callable


class CircuitBreaker:
    CLOSED = "closed"  # 正常放行
    OPEN = "open"  # 跳閘：直接拒絕
    HALF_OPEN = "half_open"  # 冷卻結束，允許一次試探

    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_seconds: float = 30.0,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._threshold = threshold
        self._cooldown = cooldown_seconds
        self._now = now or time.monotonic
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        if self._opened_at is None:
            return self.CLOSED
        if self._now() - self._opened_at >= self._cooldown:
            return self.HALF_OPEN
        return self.OPEN

    def allow_request(self) -> bool:
        return self.state in (self.CLOSED, self.HALF_OPEN)

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._threshold:
            self._opened_at = self._now()
