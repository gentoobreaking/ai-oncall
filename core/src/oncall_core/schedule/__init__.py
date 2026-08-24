"""排班整合（F20）：升級鏈 primary → secondary → manager。

v1 支援兩種來源：
  - 靜態名單（config 直接給三級人選）
  - ICS 檔匯入（解析 VEVENT 找當值者；無 ICS 套件依賴，最小解析）
排班未設定時降級為固定 admin（§B.2）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from oncall_core.logging import get_logger

log = get_logger(__name__)

ROLES: tuple[str, ...] = ("primary", "secondary", "manager")


@dataclass(slots=True)
class Roster:
    """當值排班表。任何一級缺位時以 admin 補位（v1 降級語意）。"""

    primary: str = "admin"
    secondary: str = "admin"
    manager: str = "admin"
    source: str = "default"

    def chain(self) -> list[str]:
        """升級鏈：去重、保持 primary→secondary→manager 順序。"""
        seen: list[str] = []
        for role in ROLES:
            who = getattr(self, role)
            if who and who not in seen:
                seen.append(who)
        return seen


def roster_from_static(primary: str, secondary: str, manager: str) -> Roster:
    return Roster(primary=primary, secondary=secondary, manager=manager, source="static")


def roster_from_ics(ics_text: str) -> Roster:
    """最小 ICS 解析：取每個 VEVENT 的 SUMMARY（人名）與 DTSTART，
    以「現在時間正在值班」者為 primary，其後依開始時間排序補 secondary/manager。"""
    now_candidates: list[tuple[str, float]] = []
    for match in re.finditer(r"BEGIN:VEVENT(.*?)END:VEVENT", ics_text, re.DOTALL):
        block = match.group(1)
        summary_m = re.search(r"SUMMARY[^:]*:(\S+)", block)
        dtstart_m = re.search(r"DTSTART[^:]*:(\d{8}(?:T\d{6}Z?)?)", block)
        if not summary_m or not dtstart_m:
            continue
        person = summary_m.group(1).strip()
        ts_raw = dtstart_m.group(1)
        try:
            from datetime import datetime

            fmt = "%Y%m%dT%H%M%SZ" if ts_raw.endswith("Z") else "%Y%m%d"
            ts = datetime.strptime(ts_raw, fmt).timestamp()
            now_candidates.append((person, ts))
        except ValueError:
            continue

    if not now_candidates:
        log.warning("ICS parse yielded no events; falling back to default admin")
        return Roster(source="ics-empty")

    import time as _time

    now = _time.time()
    # 正在值班的（開始時間 <= 現在）取最近一個為 primary
    past = sorted([c for c in now_candidates if c[1] <= now], key=lambda x: x[1])
    future = sorted([c for c in now_candidates if c[1] > now], key=lambda x: x[1])

    ordered: list[str] = []
    if past:
        ordered.append(past[-1][0])
    ordered.extend(p for p, _ in future)
    # 去重保序
    uniq: list[str] = []
    for p in ordered:
        if p not in uniq:
            uniq.append(p)

    roles = dict(zip(ROLES, (uniq + ["admin"] * 3)[:3], strict=False))
    return Roster(**roles, source="ics")


def load_roster(path: str | Path) -> Roster:
    """從 .ics 檔載入排班；檔案不存在回傳預設 admin。"""
    p = Path(path)
    if not p.is_file():
        return Roster()
    return roster_from_ics(p.read_text(encoding="utf-8"))
