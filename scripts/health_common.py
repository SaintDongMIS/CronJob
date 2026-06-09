"""
NAS CronJob 健康檢查共用邏輯：log 解析、分 job 評估、Telegram / HTML 訊息。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from taiwan_holidays.taiwan_calendar import TaiwanCalendar

TZ = ZoneInfo("Asia/Taipei")
ENV_NAS_LOG_DIR = "NAS_LOG_DIR"
ENV_HEALTH_GRACE_MIN = "HEALTH_GRACE_MIN"
ENV_HEALTH_WINDOW_HOURS = "HEALTH_WINDOW_HOURS"
ENV_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_HEALTH_NOTIFY_OK = "HEALTH_NOTIFY_OK"
ENV_HEALTH_SKIP_HOLIDAY = "HEALTH_SKIP_HOLIDAY"

MODE_DAY = "day"
MODE_OFFHOURS = "offhours"
BUSINESS_START = time(8, 30)
BUSINESS_END = time(17, 30)

_LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_START = re.compile(r"\[START\]")
_OK = re.compile(r"\[OK\]")
_LOCKED = re.compile(r"\[SKIP\].*locked", re.IGNORECASE)
_EXCEPTION_LINE = re.compile(
    r"^(\w[\w.]*Error|ConnectTimeout|TimeoutError|MaxRetryError):"
)


@dataclass(frozen=True, slots=True)
class JobSpec:
    label: str
    log_prefix: str
    schedule_mode: str  # day | offhours


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
    status: str  # ok | warn | fail | skip
    status_note: str
    log_files: str
    failure_snippet: str | None = None
    suggestion: str | None = None


MONITORED_JOBS: tuple[JobSpec, ...] = (
    JobSpec(label="ERP 借貸平衡", log_prefix="erp", schedule_mode=MODE_DAY),
    JobSpec(label="ToBim 環景", log_prefix="tobim", schedule_mode=MODE_OFFHOURS),
)


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def log_dir() -> Path:
    raw = os.environ.get(ENV_NAS_LOG_DIR, "").strip()
    if raw:
        return Path(raw)
    return project_root() / "logs"


def grace_minutes() -> int:
    return max(1, int_env(ENV_HEALTH_GRACE_MIN, 5))


def is_holiday(day: date) -> bool:
    cal = TaiwanCalendar()
    try:
        return bool(cal.is_holiday(day))
    except ValueError:
        return day.weekday() >= 5


def _in_business_hours(now: datetime) -> bool:
    current = now.time().replace(microsecond=0)
    return BUSINESS_START <= current <= BUSINESS_END


def job_expected_in_window(
    spec: JobSpec,
    since: datetime,
    until: datetime,
) -> bool:
    """統計區間與該 job 預期執行窗是否有重疊。"""
    if is_holiday(since.date()) and is_holiday(until.date()):
        if since.date() == until.date():
            return False
    probe = since
    while probe <= until:
        if not is_holiday(probe.date()):
            in_business = _in_business_hours(probe)
            if spec.schedule_mode == MODE_DAY and in_business:
                return True
            if spec.schedule_mode == MODE_OFFHOURS and not in_business:
                return True
        probe += timedelta(minutes=30)
    return False


def health_window(
    window_kind: str,
    now: datetime,
) -> tuple[datetime, datetime, str]:
    if window_kind == "rolling":
        hours = max(1, int_env(ENV_HEALTH_WINDOW_HOURS, 2))
        since = now - timedelta(hours=hours)
        label = f"{since.strftime('%H:%M')}–{now.strftime('%H:%M')}（過去 {hours} 小時）"
        return since, now, label
    if window_kind == "morning":
        since = (now - timedelta(days=1)).replace(
            hour=15, minute=0, second=0, microsecond=0
        )
        return since, now, "昨日 15:00 ～ 今晨 10:00"
    if window_kind == "afternoon":
        since = now.replace(hour=10, minute=0, second=0, microsecond=0)
        return since, now, "今日 10:00 ～ 15:00"
    raise ValueError(f"unknown window kind: {window_kind}")


def _parse_line_time(line: str) -> datetime | None:
    match = _LINE_TS.match(line)
    if not match:
        return None
    return datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)


def log_paths_for_window(
    log_dir_path: Path, prefix: str, since: datetime, until: datetime
) -> list[Path]:
    dates: set[date] = set()
    current = since.date()
    end = until.date()
    while current <= end:
        dates.add(current)
        current += timedelta(days=1)
    paths = [log_dir_path / f"{prefix}_{d.isoformat()}.log" for d in sorted(dates)]
    return [path for path in paths if path.is_file()]


def parse_log_window(path: Path, since: datetime, until: datetime) -> LogRunStats:
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


def merge_stats(items: list[LogRunStats]) -> LogRunStats:
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


def _hint_for_line(line: str) -> str | None:
    lower = line.lower()
    if "connecttimeout" in lower or "timed out" in lower:
        return "Docker 可能連不到 API，請確認 .env 的 ASSETS_BASE_URL 是否為內網 IP"
    if "dry_run=true" in lower or "dry_run：未呼叫" in lower:
        return "仍在試跑模式（TOBIM_DRY_RUN=1），不會真的執行"
    if "locked" in lower:
        return "前次執行尚未結束，這次被 lock 略過（通常可再觀察）"
    return None


def extract_failure_snippet(
    paths: list[Path],
    since: datetime,
    until: datetime,
) -> tuple[str | None, str | None]:
    """從 log 擷取最後一則錯誤摘要與建議。"""
    last_exception: str | None = None
    last_hint: str | None = None
    in_traceback = False

    for path in paths:
        if not path.is_file():
            continue
        context_in_window = False
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            ts = _parse_line_time(line)
            if ts is not None:
                context_in_window = since <= ts <= until
                if not context_in_window:
                    in_traceback = False
                    continue
            elif not context_in_window:
                continue

            hint = _hint_for_line(line)
            if hint:
                last_hint = hint

            if line.startswith("Traceback (most recent call last):"):
                in_traceback = True
                continue

            if in_traceback:
                if _EXCEPTION_LINE.match(line) or line.startswith(
                    "requests.exceptions."
                ):
                    last_exception = line[:300]
                    in_traceback = False
                continue

            if "ConnectTimeout" in line or "MaxRetryError" in line:
                last_exception = line[:300]

    return last_exception, last_hint


def evaluate_job(
    spec: JobSpec,
    stats: LogRunStats,
    log_files: list[Path],
    *,
    since: datetime,
    until: datetime,
    now: datetime,
    respect_schedule: bool,
) -> JobHealth:
    log_names = ", ".join(path.name for path in log_files) or "（無 log 檔）"
    last_run_at = (
        stats.last_event_at.strftime("%Y-%m-%d %H:%M") if stats.last_event_at else None
    )
    last_status = stats.last_event_label

    if respect_schedule and not job_expected_in_window(spec, since, until):
        note = (
            "工作日 08:30–17:30"
            if spec.schedule_mode == MODE_DAY
            else "工作日 17:30 後～翌日 08:30 前"
        )
        return JobHealth(
            label=spec.label,
            log_label=spec.log_prefix,
            period_starts=stats.starts,
            period_ok=stats.oks,
            period_locked=stats.locked,
            period_fail=0,
            last_run_at=last_run_at,
            last_status=last_status,
            status="skip",
            status_note=f"非執行窗（{note}）",
            log_files=log_names,
        )

    unfinished = max(0, stats.starts - stats.oks - stats.locked)
    period_fail = max(unfinished, stats.tracebacks)
    failure_snippet, suggestion = extract_failure_snippet(log_files, since, until)

    if (
        unfinished == 1
        and stats.last_event_label == "START"
        and stats.last_event_at is not None
        and (now - stats.last_event_at) < timedelta(minutes=grace_minutes())
    ):
        period_fail = stats.tracebacks
        status, note = "ok", f"執行中（{grace_minutes()} 分鐘內暫不計異常）"
    elif not log_files:
        status, note = "warn", "時段內找不到 log 檔"
    elif stats.starts == 0:
        status, note = "warn", "時段內無 [START]"
    elif period_fail > 0:
        status, note = "fail", f"異常 {period_fail} 次"
    elif stats.locked > 0 and stats.oks == 0:
        status, note = "warn", f"僅見 locked {stats.locked} 次"
    else:
        status, note = "ok", "排程正常"

    return JobHealth(
        label=spec.label,
        log_label=spec.log_prefix,
        period_starts=stats.starts,
        period_ok=stats.oks,
        period_locked=stats.locked,
        period_fail=period_fail,
        last_run_at=last_run_at,
        last_status=last_status,
        status=status,
        status_note=note,
        log_files=log_names,
        failure_snippet=failure_snippet,
        suggestion=suggestion,
    )


def collect_job_health(
    log_dir_path: Path,
    spec: JobSpec,
    since: datetime,
    until: datetime,
    now: datetime,
    *,
    respect_schedule: bool,
) -> JobHealth:
    paths = log_paths_for_window(log_dir_path, spec.log_prefix, since, until)
    stats = merge_stats(parse_log_window(path, since, until) for path in paths)
    return evaluate_job(
        spec,
        stats,
        paths,
        since=since,
        until=until,
        now=now,
        respect_schedule=respect_schedule,
    )


def collect_all_jobs(
    *,
    since: datetime,
    until: datetime,
    now: datetime,
    respect_schedule: bool,
    log_dir_path: Path | None = None,
) -> list[JobHealth]:
    root = log_dir_path or log_dir()
    return [
        collect_job_health(
            root, spec, since, until, now, respect_schedule=respect_schedule
        )
        for spec in MONITORED_JOBS
    ]


def status_badge(status: str) -> tuple[str, str]:
    if status == "ok":
        return "🟢", "正常"
    if status == "warn":
        return "🟡", "注意"
    if status == "skip":
        return "⏭", "略過"
    return "🔴", "異常"


def should_send_telegram(items: list[JobHealth]) -> bool:
    if truthy_env(ENV_HEALTH_NOTIFY_OK):
        return True
    return any(item.status in ("warn", "fail") for item in items)


def render_telegram_message(
    *,
    title: str,
    period_label: str,
    generated_at: str,
    host_label: str,
    items: list[JobHealth],
    holiday_skipped: bool = False,
) -> str:
    lines = [
        f"[NAS CronJob] {title}",
        f"主機：{host_label}｜{generated_at}（台北）",
        f"區間：{period_label}",
        "",
    ]
    if holiday_skipped:
        lines.append("今日為放假日，監控略過。")
        return "\n".join(lines)

    for item in items:
        icon, _text = status_badge(item.status)
        lines.append(f"{item.label} {icon}")
        if item.status == "skip":
            lines.append(f"  {item.status_note}")
            continue
        lines.append(
            f"  {item.period_starts} 次啟動 / {item.period_ok} OK / "
            f"{item.period_fail} 異常"
        )
        if item.last_run_at and item.last_status:
            lines.append(f"  最近：{item.last_run_at} {item.last_status}")
        if item.status_note:
            lines.append(f"  {item.status_note}")
        if item.status in ("fail", "warn") and item.failure_snippet:
            lines.append(f"  錯誤：{item.failure_snippet}")
        if item.suggestion:
            lines.append(f"  建議：{item.suggestion}")

    return "\n".join(lines)


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
        icon, text = status_badge(item.status)
        extra = ""
        if item.failure_snippet:
            extra = (
                f'<br><span style="color:#888;font-size:12px;">'
                f"{item.failure_snippet}</span>"
            )
        rows.append(
            f"""
            <tr>
              <td><strong>{item.label}</strong><br>
                  <span style="color:#666;font-size:12px;">{item.log_label}_*.log</span></td>
              <td>{icon} {text}<br>
                  <span style="color:#666;font-size:12px;">{item.status_note}</span>{extra}</td>
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
      執行窗參考：ERP 工作日 08:30–17:30；ToBim 工作日 17:30 後～翌日 08:30 前。
    </p>
  </div>
</body>
</html>"""


def load_telegram() -> tuple[str, str]:
    token = os.environ.get(ENV_TELEGRAM_BOT_TOKEN, "").strip()
    chat_id = os.environ.get(ENV_TELEGRAM_CHAT_ID, "").strip()
    missing = [
        name
        for name, value in [
            ("TELEGRAM_BOT_TOKEN", token),
            ("TELEGRAM_CHAT_ID", chat_id),
        ]
        if not value
    ]
    if missing:
        raise RuntimeError(f"缺少 Telegram 設定：{', '.join(missing)}")
    return token, chat_id


def send_telegram(text: str) -> None:
    token, chat_id = load_telegram()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = requests.post(
        url,
        json={"chat_id": chat_id, "text": text},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(payload.get("description") or "Telegram API 回傳失敗")


def subject_from_items(slot_title: str, items: list[JobHealth]) -> str:
    return f"[NAS CronJob] {slot_title} — " + " / ".join(
        f"{item.label}{status_badge(item.status)[0]}" for item in items
    )
