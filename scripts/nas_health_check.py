#!/usr/bin/env python3
"""
NAS 排程健康檢查（Telegram 輪詢）：讀取 logs/erp_*.log、logs/tobim_*.log。

設定：.env 內 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID；
選填 HEALTH_WINDOW_HOURS（預設 2）、HEALTH_GRACE_MIN（ERP 預設 5）、
HEALTH_GRACE_MIN_TOBIM（ToBim 預設 15）、
HEALTH_SKIP_HOLIDAY（1=放假日不發）、DIGEST_DRY_RUN。
Telegram 每次輪詢皆回報 ERP / ToBim 執行結果（含全綠）。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from dotenv import load_dotenv

from health_common import (
    ENV_HEALTH_SKIP_HOLIDAY,
    TZ,
    collect_all_jobs,
    health_window,
    is_holiday,
    project_root,
    render_telegram_message,
    send_telegram,
    should_send_telegram,
    truthy_env,
)

ENV_DIGEST_DRY_RUN = "DIGEST_DRY_RUN"


def _load_dotenv_if_present() -> None:
    path = project_root() / ".env"
    if path.is_file():
        load_dotenv(path, override=False)


def main() -> int:
    _load_dotenv_if_present()
    now = datetime.now(TZ)
    since, until, period_label = health_window("rolling", now)
    generated_at = now.strftime("%Y-%m-%d %H:%M")
    host_label = os.environ.get("NAS_HOSTNAME", "").strip() or "NAS"
    title = f"健康檢查 {now.strftime('%H:%M')}"

    if truthy_env(ENV_HEALTH_SKIP_HOLIDAY) and is_holiday(now.date()):
        message = render_telegram_message(
            title=title,
            period_label=period_label,
            generated_at=generated_at,
            host_label=host_label,
            items=[],
            holiday_skipped=True,
        )
        if truthy_env(ENV_DIGEST_DRY_RUN):
            print(message)
            print("(dry-run: holiday skip, not sent)")
            return 0
        send_telegram(message)
        print("Sent: holiday skip notice -> Telegram")
        return 0

    items = collect_all_jobs(
        since=since,
        until=until,
        now=now,
        respect_schedule=True,
    )
    message = render_telegram_message(
        title=title,
        period_label=period_label,
        generated_at=generated_at,
        host_label=host_label,
        items=items,
    )

    if truthy_env(ENV_DIGEST_DRY_RUN):
        print(message)
        print(f"(dry-run: would_send={should_send_telegram(items)})")
        return 0

    if not should_send_telegram(items):
        print(f"Skipped: all ok/skip ({generated_at})")
        return 0

    try:
        send_telegram(message)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"Sent: Telegram health check ({generated_at})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
