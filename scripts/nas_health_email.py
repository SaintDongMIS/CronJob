#!/usr/bin/env python3
"""
NAS 排程健康檢查 Email：讀取 logs/erp_*.log、logs/tobim_*.log，彙整 cron 執行狀態後寄信。

用途：確認 NAS 上 run_erp.sh / run_tobim.sh 是否有被 cron 喚起並正常結束（非業務明細）。
設定：.env 內 SMTP_*、EMAIL_TO；選填 DIGEST_SLOT（morning|afternoon）、DIGEST_DRY_RUN、NAS_LOG_DIR。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from workflow_health_email import (
    ENV_DIGEST_DRY_RUN,
    ENV_DIGEST_SLOT,
    digest_window,
    load_smtp,
    send_email,
)

TZ = ZoneInfo("Asia/Taipei")
ENV_NAS_LOG_DIR = "NAS_LOG_DIR"

_LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_START = re.compile(r"\[START\]")
_OK = re.compile(r"\[OK\]")
_LOCKED = re.compile(r"\[SKIP\].*locked", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class LogRunStats:
    starts: int = 0
    oks: int = 0
    locked: int = 0
    tracebacks: int = 0
    last_event_at: datetime | None = None
    last_event_label: str | None = None


@dataclass(frozen=True, slots=True)
class JobHealth:
    label: str
    log_label: str
    period_starts: int
    period_ok: int
    period_locked: int
    period_fail: int
    last_run_at: str | None
    last_status: str | None
    status: str
    status_note: str
    log_files: str


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_dotenv_if_present() -> None:
    path = _project_root() / ".env"
    if path.is_file():
        load_dotenv(path, override=False)


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _log_dir() -> Path:
    raw = os.environ.get(ENV_NAS_LOG_DIR, "").strip()
    if raw:
        return Path(raw)
    return _project_root() / "logs"


def _parse_line_time(line: str) -> datetime | None:
    match = _LINE_TS.match(line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)


def _log_paths_for_window(log_dir: Path, prefix: str, since: datetime, until: datetime) -> list[Path]:
    dates: set[date] = set()
    current = since.date()
    end = until.date()
    while current <= end:
        dates.add(current)
        current += timedelta(days=1)
    paths = [log_dir / f"{prefix}_{d.isoformat()}.log" for d in sorted(dates)]
    return [path for path in paths if path.is_file()]


def _parse_log_window(path: Path, since: datetime, until: datetime) -> LogRunStats:
    stats = LogRunStats()
    if not path.is_file():
        return stats

    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ts = _parse_line_time(line)
        if ts is None or ts < since or ts > until:
            continue

        if _START.search(line):
            stats = LogRunStats(
                starts=stats.starts + 1,
                oks=stats.oks,
                locked=stats.locked,
                tracebacks=stats.tracebacks,
                last_event_at=ts,
                last_event_label="START",
            )
        elif _OK.search(line):
            stats = LogRunStats(
                starts=stats.starts,
                oks=stats.oks + 1,
                locked=stats.locked,
                tracebacks=stats.tracebacks,
                last_event_at=ts,
                last_event_label="OK",
            )
        elif _LOCKED.search(line):
            stats = LogRunStats(
                starts=stats.starts,
                oks=stats.oks,
                locked=stats.locked + 1,
                tracebacks=stats.tracebacks,
                last_event_at=ts,
                last_event_label="LOCKED",
            )
        elif line.startswith("Traceback (most recent call last):"):
            stats = LogRunStats(
                starts=stats.starts,
                oks=stats.oks,
                locked=stats.locked,
                tracebacks=stats.tracebacks + 1,
                last_event_at=ts,
                last_event_label="FAIL",
            )

    return stats


def _merge_stats(items: list[LogRunStats]) -> LogRunStats:
    merged = LogRunStats()
    for item in items:
        last_at = merged.last_event_at
        last_label = merged.last_event_label
        if item.last_event_at and (last_at is None or item.last_event_at >= last_at):
            last_at = item.last_event_at
            last_label = item.last_event_label
        merged = LogRunStats(
            starts=merged.starts + item.starts,
            oks=merged.oks + item.oks,
            locked=merged.locked + item.locked,
            tracebacks=merged.tracebacks + item.tracebacks,
            last_event_at=last_at,
            last_event_label=last_label,
        )
    return merged


def _evaluate_job(label: str, log_prefix: str, stats: LogRunStats, log_files: list[Path]) -> JobHealth:
    unfinished = max(0, stats.starts - stats.oks - stats.locked)
    period_fail = max(unfinished, stats.tracebacks)
    last_run_at = (
        stats.last_event_at.strftime("%Y-%m-%d %H:%M") if stats.last_event_at else None
    )
    last_status = stats.last_event_label
    log_names = ", ".join(path.name for path in log_files) or "（無 log 檔）"

    if not log_files:
        status, note = "warn", "時段內找不到 log 檔"
    elif stats.starts == 0:
        status, note = "warn", "時段內無 [START]（可能放假日或尚未執行）"
    elif period_fail > 0:
        status, note = "fail", f"異常 {period_fail} 次（未完成或 Traceback）"
    elif stats.locked > 0 and stats.oks == 0:
        status, note = "warn", f"僅見 locked {stats.locked} 次"
    else:
        status, note = "ok", "排程正常"

    return JobHealth(
        label=label,
        log_label=log_prefix,
        period_starts=stats.starts,
        period_ok=stats.oks,
        period_locked=stats.locked,
        period_fail=period_fail,
        last_run_at=last_run_at,
        last_status=last_status,
        status=status,
        status_note=note,
        log_files=log_names,
    )


def _status_badge(status: str) -> tuple[str, str]:
    if status == "ok":
        return "🟢", "正常"
    if status == "warn":
        return "🟡", "注意"
    return "🔴", "異常"


def _collect_job_health(
    log_dir: Path,
    *,
    label: str,
    log_prefix: str,
    since: datetime,
    until: datetime,
) -> JobHealth:
    paths = _log_paths_for_window(log_dir, log_prefix, since, until)
    stats = _merge_stats(_parse_log_window(path, since, until) for path in paths)
    return _evaluate_job(label, log_prefix, stats, paths)


def render_html(
    *,
    slot_title: str,
    period_label: str,
    generated_at: str,
    host_label: str,
    items: list[JobHealth],
) -> str:
    rows = []
    for item in items:
        icon, text = _status_badge(item.status)
        rows.append(
            f"""
            <tr>
              <td><strong>{item.label}</strong><br>
                  <span style="color:#666;font-size:12px;">{item.log_label}_*.log</span></td>
              <td>{icon} {text}<br>
                  <span style="color:#666;font-size:12px;">{item.status_note}</span></td>
              <td>{item.period_starts} 次啟動<br>
                  <span style="color:#2e7d32;">OK {item.period_ok}</span> /
                  <span style="color:#888;">locked {item.period_locked}</span> /
                  <span style="color:#c62828;">異常 {item.period_fail}</span></td>
              <td>{item.last_run_at or "—"}<br>{item.last_status or "—"}</td>
              <td style="font-size:12px;color:#666;">{item.log_files}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <title>NAS CronJob {slot_title}</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;line-height:1.5;margin:0;padding:16px;background:#f5f5f5;">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:8px;padding:20px;border:1px solid #e0e0e0;">
    <h2 style="margin:0 0 8px;">NAS CronJob 排程健康檢查 — {slot_title}</h2>
    <p style="margin:0 0 16px;color:#666;font-size:14px;">
      產生時間：{generated_at}（台北）<br>
      統計區間：{period_label}<br>
      主機：<code>{host_label}</code>
    </p>
    <p style="margin:0 0 12px;font-size:14px;">依 <code>logs/erp_*.log</code>、<code>logs/tobim_*.log</code> 的 [START]/[OK] 判斷 cron 是否存活（非業務明細）。</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <thead>
        <tr style="background:#f0f4f8;">
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">功能</th>
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">狀態</th>
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">時段內執行</th>
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">最近一次</th>
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">Log 檔</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    <p style="margin:16px 0 0;font-size:12px;color:#888;">
      執行窗參考：ERP 工作日 08:30–17:30；ToBim 工作日 17:30 後～翌日 08:30 前。詳細內容請 SSH 查看 NAS log。
    </p>
  </div>
</body>
</html>"""


def main() -> int:
    _load_dotenv_if_present()
    slot = os.environ.get(ENV_DIGEST_SLOT, "morning").strip().lower()
    if slot not in ("morning", "afternoon"):
        slot = "morning"
    slot_title = "早報 10:00" if slot == "morning" else "午報 15:00"

    now = datetime.now(TZ)
    since, until, period_label = digest_window(slot, now)
    generated_at = now.strftime("%Y-%m-%d %H:%M")
    host_label = os.environ.get("NAS_HOSTNAME", "").strip() or "NAS"

    log_dir = _log_dir()
    items = [
        _collect_job_health(log_dir, label="ERP 借貸平衡", log_prefix="erp", since=since, until=until),
        _collect_job_health(log_dir, label="ToBim 環景", log_prefix="tobim", since=since, until=until),
    ]

    html = render_html(
        slot_title=slot_title,
        period_label=period_label,
        generated_at=generated_at,
        host_label=host_label,
        items=items,
    )
    subject = f"[NAS CronJob] {slot_title} — " + " / ".join(
        f"{item.label}{_status_badge(item.status)[0]}" for item in items
    )

    if _truthy_env(ENV_DIGEST_DRY_RUN):
        print(subject)
        for item in items:
            print(
                f"  {item.label}: {item.status} starts={item.period_starts} "
                f"ok={item.period_ok} fail={item.period_fail} logs={item.log_files}"
            )
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
