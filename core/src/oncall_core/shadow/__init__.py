"""Shadow Mode 全域旗標與上線門檻（F15、§D.4、spec §5 標準 11）。

- SHADOW_MODE=1：管線照跑（收集/RAG/分診），但推播與執行一律跳過，
  報告寫入 shadow_reports/ 含人工評分欄位
- 上線門檻：≥30 份影子報告完成人工評分，且原因正確率／建議可用率
  達設定門檻，才允許關閉旗標；不足時明確拒絕並說明差距
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from oncall_core.logging import get_logger
from oncall_core.store import Store

log = get_logger(__name__)

DEFAULT_MIN_SCORED_REPORTS = 30
DEFAULT_MIN_CAUSE_ACCURACY = 0.8
DEFAULT_MIN_ACTION_USABILITY = 0.7


class ShadowGateError(PermissionError):
    """評分不足時嘗試關閉 Shadow Mode 的明確拒絕。"""

    def __init__(self, detail: dict[str, object]) -> None:
        self.detail = detail
        parts = [
            f"scored={detail['scored']}/{detail['required_reports']}",
            f"cause_accuracy={detail['cause_accuracy']:.2f}/{detail['required_cause']:.2f}",
            f"action_usability={detail['action_usability']:.2f}/{detail['required_action']:.2f}",
        ]
        super().__init__("shadow release gate not satisfied: " + ", ".join(parts))


@dataclass(slots=True)
class ShadowStats:
    scored: int
    required_reports: int
    cause_accuracy: float
    action_usability: float


class ShadowController:
    """全域旗標持有者＋影子報告評分統計庫。"""

    def __init__(
        self,
        store: Store,
        *,
        enabled: bool | None = None,
        required_reports: int = DEFAULT_MIN_SCORED_REPORTS,
        required_cause_accuracy: float = DEFAULT_MIN_CAUSE_ACCURACY,
        required_action_usability: float = DEFAULT_MIN_ACTION_USABILITY,
    ) -> None:
        self._store = store
        self.required_reports = required_reports
        self.required_cause_accuracy = required_cause_accuracy
        self.required_action_usability = required_action_usability
        # 明示參數優先；否則讀環境旗標（§A.6）
        self.enabled = enabled if enabled is not None else os.environ.get("SHADOW_MODE") == "1"

    # ------------------------------------------------------------------
    # 評分寫回統計庫（§D.4）
    # ------------------------------------------------------------------

    def record_score(
        self, *, incident_id: str, cause_correct: bool, action_usable: bool, reviewer: str
    ) -> None:
        """人工評分寫回統計庫（§D.4）。"""
        self._store.record_shadow_score(
            incident_id=incident_id,
            cause_correct=cause_correct,
            action_usable=action_usable,
            reviewer=reviewer,
        )

    def stats(self) -> ShadowStats:
        rows = self._store.all_shadow_scores()
        n = len(rows)
        cause_acc = sum(r["cause_correct"] for r in rows) / n if n else 0.0
        action_acc = sum(r["action_usable"] for r in rows) / n if n else 0.0
        return ShadowStats(
            scored=n,
            required_reports=self.required_reports,
            cause_accuracy=float(cause_acc),
            action_usability=float(action_acc),
        )

    # ------------------------------------------------------------------
    # 上線門檻檢查（spec §5 標準 11）
    # ------------------------------------------------------------------

    def can_disable(self) -> tuple[bool, dict[str, object]]:
        s = self.stats()
        ok = (
            s.scored >= self.required_reports
            and s.cause_accuracy >= self.required_cause_accuracy
            and s.action_usability >= self.required_action_usability
        )
        detail: dict[str, object] = {
            "scored": s.scored,
            "required_reports": self.required_reports,
            "cause_accuracy": s.cause_accuracy,
            "required_cause": self.required_cause_accuracy,
            "action_usability": s.action_usability,
            "required_action": self.required_action_usability,
        }
        return ok, detail

    def assert_can_disable(self) -> None:
        ok, detail = self.can_disable()
        if not ok:
            log.warning("shadow disable refused", **detail)
            raise ShadowGateError(detail)

    def disable(self) -> None:
        """通過門檻才允許關閉旗標。"""
        self.assert_can_disable()
        self.enabled = False
        log.info("shadow mode disabled——正式推播與執行已啟用")
