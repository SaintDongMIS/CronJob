#!/usr/bin/env python3
"""
判斷 GitHub Actions 這次是否要執行更新。

條件（以 Asia/Taipei 判定）：
- 非台灣政府行政機關「放假日」（包含週末與調整放假）
- 時段依 SHOULD_RUN_MODE：
  - day（預設，ERP）：08:30（含）～17:30（含）上班時間
  - offhours（ToBim）：下班後 — 17:30 之後或翌日 08:30 之前

觸發頻率由各 workflow 的 cron 決定，本腳本不負責間隔。

輸出：
- 若在 GitHub Actions 環境，會寫入 $GITHUB_OUTPUT：should_run=true/false
- 同時也會印出簡短判斷資訊
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
try:
    from zoneinfo import ZoneInfo  # py>=3.9
except ModuleNotFoundError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo  # py<3.9

from taiwan_holidays.taiwan_calendar import TaiwanCalendar

ENV_SHOULD_RUN_MODE = "SHOULD_RUN_MODE"
MODE_DAY = "day"
MODE_OFFHOURS = "offhours"

BUSINESS_START = time(8, 30)
BUSINESS_END = time(17, 30)


@dataclass(frozen=True, slots=True)
class Window:
    start: time
    end: time

    def contains(self, t: time) -> bool:
        return self.start <= t <= self.end


def _write_github_output(key: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT", "").strip()
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{key}={value}\n")


def _resolve_mode() -> str:
    raw = os.environ.get(ENV_SHOULD_RUN_MODE, MODE_DAY).strip().lower()
    if raw in (MODE_OFFHOURS, "off_hours", "night"):
        return MODE_OFFHOURS
    return MODE_DAY


def _in_business_hours(now: datetime) -> bool:
    return Window(start=BUSINESS_START, end=BUSINESS_END).contains(
        now.time().replace(microsecond=0)
    )


def _time_allowed(mode: str, now: datetime) -> tuple[bool, str]:
    in_business = _in_business_hours(now)
    if mode == MODE_OFFHOURS:
        allowed = not in_business
        slot = "offhours" if allowed else "business_hours"
        return allowed, slot
    allowed = in_business
    slot = "business_hours" if allowed else "offhours"
    return allowed, slot


def main() -> int:
    tz = ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    mode = _resolve_mode()

    cal = TaiwanCalendar()
    is_holiday = bool(cal.is_holiday(now.date()))
    time_ok, time_label = _time_allowed(mode, now)

    should_run = (not is_holiday) and time_ok
    _write_github_output("should_run", "true" if should_run else "false")

    day_type = "holiday" if is_holiday else "workday"
    print(
        f"now={now.isoformat()} mode={mode} decision={'RUN' if should_run else 'SKIP'} "
        f"({day_type}, {time_label})"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
