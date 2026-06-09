#!/usr/bin/env python3
"""
NAS 排程健康檢查 Email：讀取 logs/erp_*.log、logs/tobim_*.log，彙整 cron 執行狀態後寄信。

用途：確認 NAS 上 run_erp.sh / run_tobim.sh 是否有被 cron 喚起並正常結束（非業務明細）。
設定：.env 內 SMTP_*、EMAIL_TO；選填 DIGEST_SLOT（morning|afternoon）、DIGEST_DRY_RUN、NAS_LOG_DIR。
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

from dotenv import load_dotenv

from health_common import (
    TZ,
    collect_all_jobs,
    health_window,
    project_root,
    render_html,
    subject_from_items,
    truthy_env,
)
from workflow_health_email import (
    ENV_DIGEST_DRY_RUN,
    ENV_DIGEST_SLOT,
    load_smtp,
    send_email,
)


def _load_dotenv_if_present() -> None:
    path = project_root() / ".env"
    if path.is_file():
        load_dotenv(path, override=False)


def main() -> int:
    _load_dotenv_if_present()
    slot = os.environ.get(ENV_DIGEST_SLOT, "morning").strip().lower()
    if slot not in ("morning", "afternoon"):
        slot = "morning"
    slot_title = "早報 10:05" if slot == "morning" else "午報 15:05"

    now = datetime.now(TZ)
    since, until, period_label = health_window(slot, now)
    generated_at = now.strftime("%Y-%m-%d %H:%M")
    host_label = os.environ.get("NAS_HOSTNAME", "").strip() or "NAS"

    items = collect_all_jobs(
        since=since,
        until=until,
        now=now,
        respect_schedule=False,
    )

    html = render_html(
        slot_title=slot_title,
        period_label=period_label,
        generated_at=generated_at,
        host_label=host_label,
        items=items,
    )
    subject = subject_from_items(slot_title, items)

    if truthy_env(ENV_DIGEST_DRY_RUN):
        print(subject)
        for item in items:
            print(
                f"  {item.label}: {item.status} starts={item.period_starts} "
                f"ok={item.period_ok} fail={item.period_fail} logs={item.log_files}"
            )
            if item.failure_snippet:
                print(f"    錯誤：{item.failure_snippet}")
        print(html[:500], "...")
        return 0

    try:
        smtp = load_smtp()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    send_email(smtp, subject, html)
    print(f"Sent: {subject} -> {smtp.to_addr}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
