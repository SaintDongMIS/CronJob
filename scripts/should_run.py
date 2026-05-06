#!/usr/bin/env python3
"""
判斷 GitHub Actions 這次是否要執行更新。

條件（以 Asia/Taipei 判定）：
- 非台灣政府行政機關「放假日」（包含週末與調整放假）
- 時間介於 08:30（含）～17:30（含）

輸出：
- 若在 GitHub Actions 環境，會寫入 $GITHUB_OUTPUT：should_run=true/false
- 同時也會印出簡短判斷資訊
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

from taiwan_holidays.taiwan_calendar import TaiwanCalendar


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


def main() -> int:
    tz = ZoneInfo("Asia/Taipei")
    now = datetime.now(tz)
    window = Window(start=time(8, 30), end=time(17, 30))

    cal = TaiwanCalendar()
    is_holiday = bool(cal.is_holiday(now.date()))
    in_window = window.contains(now.time().replace(microsecond=0))

    should_run = (not is_holiday) and in_window
    _write_github_output("should_run", "true" if should_run else "false")

    reason = []
    reason.append("holiday" if is_holiday else "workday")
    reason.append("in_window" if in_window else "out_of_window")
    print(f"now={now.isoformat()} decision={'RUN' if should_run else 'SKIP'} ({', '.join(reason)})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
