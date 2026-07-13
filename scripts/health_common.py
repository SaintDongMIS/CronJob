"""
NAS CronJob 健康檢查共用邏輯：log 解析、分 job 評估、Email / Telegram 訊息。
"""

from __future__ import annotations

import os
import re
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from taiwan_holidays.taiwan_calendar import TaiwanCalendar

TZ = ZoneInfo("Asia/Taipei")
ENV_NAS_LOG_DIR = "NAS_LOG_DIR"
ENV_HEALTH_GRACE_MIN = "HEALTH_GRACE_MIN"
ENV_HEALTH_GRACE_MIN_TOBIM = "HEALTH_GRACE_MIN_TOBIM"
ENV_HEALTH_WINDOW_HOURS = "HEALTH_WINDOW_HOURS"
ENV_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "TELEGRAM_CHAT_ID"
ENV_HEALTH_NOTIFY_OK = "HEALTH_NOTIFY_OK"
ENV_HEALTH_SKIP_HOLIDAY = "HEALTH_SKIP_HOLIDAY"
ENV_SMTP_HOST = "SMTP_HOST"
ENV_SMTP_PORT = "SMTP_PORT"
ENV_SMTP_USER = "SMTP_USER"
ENV_SMTP_PASSWORD = "SMTP_PASSWORD"
ENV_SMTP_FROM = "SMTP_FROM"
ENV_EMAIL_TO = "EMAIL_TO"
ENV_DIGEST_SLOT = "DIGEST_SLOT"
ENV_DIGEST_DRY_RUN = "DIGEST_DRY_RUN"

MODE_DAY = "day"
MODE_WEEKDAY = "weekday"
MODE_OFFHOURS = "offhours"
BUSINESS_START = time(8, 30)
BUSINESS_END = time(17, 30)

_LINE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_START = re.compile(r"\[START\]")
_OK = re.compile(r"\[OK\]")
_LOCKED = re.compile(r"\[SKIP\].*locked", re.IGNORECASE)
_API_SKIP = re.compile(r"SKIP\s+API 無法連線")
_PAUSED_SKIP = re.compile(r"SKIP\s+已暫停")
_EXCEPTION_LINE = re.compile(
    r"^(\w[\w.]*Error|ConnectTimeout|TimeoutError|MaxRetryError):"
)
_TOBIM_RESULT = re.compile(r"結果：成功 (\d+)，失敗 (\d+)，略過 (\d+)")
_TOBIM_SCAN = re.compile(r"掃描完成：略過 (\d+)，待處理 (\d+)")
_TOBIM_FAIL = re.compile(r"FAIL\s+(\S+/\S+)\s+(.+)")
_TOBIM_FAIL_CSV = re.compile(r"CSV|有效的圖片檔名")
_TOBIM_FAIL_SOURCE = re.compile(r"複製後驗證失敗|jpg=0")
_TOBIM_GATE = re.compile(r"decision=(RUN|SKIP)")
_TOBIM_RUN_ACTIVITY = re.compile(r"^(COPY\s|OK\s+\d|FAIL\s|掃描完成|結果：)")
_ERP_FORMS = re.compile(r"符合「.+」且可更新之表單數: (\d+)")
_ERP_UPDATE = re.compile(r"\[\d+/\d+\] .+ -> (OK|FAIL)")
_ERP_DRY_RUN = re.compile(r"DRY_RUN：")

DEFAULT_GRACE_MIN = 5
DEFAULT_GRACE_MIN_TOBIM = 15
TOBIM_MAX_RUNTIME_MIN = 50
TOBIM_SERVER_LABEL = "ToBim 環景 Server"
DEFAULT_ASSETS_BASE_URL = "http://assets.bim-group.com:9880"

SUBJECT_LABELS: dict[str, str] = {
    "ERP 借貸平衡": "ERP",
    "ToBim 排程": "ToBim 排程",
    TOBIM_SERVER_LABEL: "環景 Server",
}


@dataclass(frozen=True, slots=True)
class JobSpec:
    label: str
    log_prefix: str
    schedule_mode: str  # day | weekday | offhours


@dataclass(frozen=True, slots=True)
class LogRunStats:
    starts: int = 0
    oks: int = 0
    locked: int = 0
    tracebacks: int = 0
    api_skips: int = 0
    paused_skips: int = 0
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
    execution_summary: str | None = None
    detail_html: str | None = None
    row_background: str | None = None


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    to_addr: str


@dataclass(frozen=True, slots=True)
class TobimServerStats:
    copy_ok: int = 0
    copy_fail: int = 0
    copy_skipped: int = 0
    csv_fail: int = 0
    source_missing: int = 0
    api_offline: int = 0
    pending: int | None = None
    examples: tuple[str, ...] = ()
    last_event_at: datetime | None = None
    last_result: str | None = None


@dataclass(frozen=True, slots=True)
class ErpBusinessStats:
    forms_count: int = 0
    update_ok: int = 0
    update_fail: int = 0
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class TobimScheduleStats:
    gate_runs: int = 0
    gate_skips: int = 0
    copy_ok: int = 0
    copy_fail: int = 0
    copy_skipped: int = 0
    pending: int | None = None


MONITORED_JOBS: tuple[JobSpec, ...] = (
    JobSpec(label="ERP 借貸平衡", log_prefix="erp", schedule_mode=MODE_DAY),
    JobSpec(label="ToBim 排程", log_prefix="tobim", schedule_mode=MODE_WEEKDAY),
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


def grace_minutes_for(spec: JobSpec) -> int:
    """ERP 約 1 分鐘內跑完；ToBim 複製 10 巷常需 10–15 分鐘。"""
    if spec.log_prefix == "tobim":
        fallback = int_env(ENV_HEALTH_GRACE_MIN, DEFAULT_GRACE_MIN_TOBIM)
        return max(1, int_env(ENV_HEALTH_GRACE_MIN_TOBIM, fallback))
    return max(1, int_env(ENV_HEALTH_GRACE_MIN, DEFAULT_GRACE_MIN))


def _tobim_run_in_progress(log_files: list[Path], started_at: datetime) -> bool:
    """START 之後若 log 已有複製活動、且尚未見 [OK] ToBim，視為仍在執行。"""
    for path in log_files:
        if not path.is_file():
            continue
        in_run = False
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            ts = _parse_line_time(line)
            if ts is not None:
                in_run = ts >= started_at
                if in_run and _OK.search(line) and "ToBim" in line:
                    return False
                continue
            if not in_run:
                continue
            if _TOBIM_RUN_ACTIVITY.match(line):
                return True
    return False


def is_holiday(day: date) -> bool:
    cal = TaiwanCalendar()
    try:
        return bool(cal.is_holiday(day))
    except ValueError:
        return day.weekday() >= 5


def _in_business_hours(now: datetime) -> bool:
    current = now.time().replace(microsecond=0)
    return BUSINESS_START <= current <= BUSINESS_END


def _schedule_window_note(spec: JobSpec) -> str:
    if spec.schedule_mode == MODE_DAY:
        return "工作日 08:30–17:30"
    if spec.schedule_mode == MODE_WEEKDAY:
        return "工作日全天"
    return "工作日 17:30 後～翌日 08:30 前"


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
            if spec.schedule_mode == MODE_WEEKDAY:
                return True
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

    active_run_in_window = False
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        ts = _parse_line_time(line)
        if ts is not None and since <= ts <= until:
            if _START.search(line):
                active_run_in_window = True
                stats = LogRunStats(
                    starts=stats.starts + 1,
                    oks=stats.oks,
                    locked=stats.locked,
                    tracebacks=stats.tracebacks,
                    api_skips=stats.api_skips,
                    paused_skips=stats.paused_skips,
                    last_event_at=ts,
                    last_event_label="START",
                )
            elif _OK.search(line):
                active_run_in_window = False
                stats = LogRunStats(
                    starts=stats.starts,
                    oks=stats.oks + 1,
                    locked=stats.locked,
                    tracebacks=stats.tracebacks,
                    api_skips=stats.api_skips,
                    paused_skips=stats.paused_skips,
                    last_event_at=ts,
                    last_event_label="OK",
                )
            elif _LOCKED.search(line):
                active_run_in_window = False
                stats = LogRunStats(
                    starts=stats.starts,
                    oks=stats.oks,
                    locked=stats.locked + 1,
                    tracebacks=stats.tracebacks,
                    api_skips=stats.api_skips,
                    paused_skips=stats.paused_skips,
                    last_event_at=ts,
                    last_event_label="LOCKED",
                )
            continue

        if not active_run_in_window:
            continue

        if line.startswith("Traceback (most recent call last):"):
            stats = LogRunStats(
                starts=stats.starts,
                oks=stats.oks,
                locked=stats.locked,
                tracebacks=stats.tracebacks + 1,
                api_skips=stats.api_skips,
                paused_skips=stats.paused_skips,
                last_event_at=stats.last_event_at,
                last_event_label="FAIL",
            )
        elif _API_SKIP.search(line):
            stats = LogRunStats(
                starts=stats.starts,
                oks=stats.oks,
                locked=stats.locked,
                tracebacks=stats.tracebacks,
                api_skips=stats.api_skips + 1,
                paused_skips=stats.paused_skips,
                last_event_at=stats.last_event_at,
                last_event_label=stats.last_event_label,
            )
        elif _PAUSED_SKIP.search(line):
            stats = LogRunStats(
                starts=stats.starts,
                oks=stats.oks,
                locked=stats.locked,
                tracebacks=stats.tracebacks,
                api_skips=stats.api_skips,
                paused_skips=stats.paused_skips + 1,
                last_event_at=stats.last_event_at,
                last_event_label=stats.last_event_label,
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
            api_skips=merged.api_skips + item.api_skips,
            paused_skips=merged.paused_skips + item.paused_skips,
            last_event_at=last_at,
            last_event_label=last_label,
        )
    return merged


def _hint_for_line(line: str) -> str | None:
    lower = line.lower()
    if "skip  api 無法連線" in lower:
        return "環景 API 離線，腳本已略過；請確認主機與 9880 服務是否啟動"
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


def _classify_tobim_fail(message: str) -> str:
    if _TOBIM_FAIL_CSV.search(message):
        return "csv"
    if _TOBIM_FAIL_SOURCE.search(message):
        return "source"
    return "source"


def parse_tobim_server_window(
    paths: list[Path],
    since: datetime,
    until: datetime,
) -> TobimServerStats:
    copy_ok = 0
    copy_fail = 0
    copy_skipped = 0
    csv_fail = 0
    source_missing = 0
    api_offline = 0
    pending: int | None = None
    examples: list[str] = []
    last_event_at: datetime | None = None
    last_result: str | None = None
    in_window = False

    for path in paths:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            ts = _parse_line_time(line)
            if ts is not None:
                in_window = since <= ts <= until
                if not in_window:
                    continue
                if ts and (last_event_at is None or ts >= last_event_at):
                    last_event_at = ts

            if not in_window:
                continue

            if _API_SKIP.search(line):
                api_offline += 1
                continue

            result_match = _TOBIM_RESULT.search(line)
            if result_match:
                ok_count = int(result_match.group(1))
                fail_count = int(result_match.group(2))
                skip_count = int(result_match.group(3))
                copy_ok += ok_count
                copy_fail += fail_count
                copy_skipped = max(copy_skipped, skip_count)
                last_result = f"{ok_count} 成功"
                if ts:
                    last_event_at = ts
                continue

            scan_match = _TOBIM_SCAN.search(line)
            if scan_match:
                pending = int(scan_match.group(2))
                continue

            fail_matches = list(_TOBIM_FAIL.finditer(line))
            for fail_match in fail_matches:
                label = fail_match.group(1)
                message = fail_match.group(2).strip()
                kind = _classify_tobim_fail(message)
                if kind == "csv":
                    csv_fail += 1
                else:
                    source_missing += 1
                example = f"{label} — {message}"
                if example not in examples:
                    examples.append(example)
                if len(examples) > 5:
                    examples = examples[:5]

    return TobimServerStats(
        copy_ok=copy_ok,
        copy_fail=copy_fail,
        copy_skipped=copy_skipped,
        csv_fail=csv_fail,
        source_missing=source_missing,
        api_offline=api_offline,
        pending=pending,
        examples=tuple(examples),
        last_event_at=last_event_at,
        last_result=last_result,
    )


def parse_erp_business_window(
    paths: list[Path],
    since: datetime,
    until: datetime,
) -> ErpBusinessStats:
    forms_count = 0
    update_ok = 0
    update_fail = 0
    dry_run = False
    in_window = False
    in_run = False

    for path in paths:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            ts = _parse_line_time(line)
            if ts is not None:
                in_window = since <= ts <= until
                if not in_window:
                    in_run = False
                    continue
            if not in_window:
                continue

            if _START.search(line):
                in_run = True
                continue
            if _OK.search(line) or _LOCKED.search(line):
                in_run = False
                continue
            if not in_run:
                continue

            forms_match = _ERP_FORMS.search(line)
            if forms_match:
                forms_count = int(forms_match.group(1))
                continue
            update_match = _ERP_UPDATE.search(line)
            if update_match:
                if update_match.group(1) == "OK":
                    update_ok += 1
                else:
                    update_fail += 1
                continue
            if _ERP_DRY_RUN.search(line):
                dry_run = True

    return ErpBusinessStats(
        forms_count=forms_count,
        update_ok=update_ok,
        update_fail=update_fail,
        dry_run=dry_run,
    )


def parse_tobim_schedule_window(
    paths: list[Path],
    since: datetime,
    until: datetime,
) -> TobimScheduleStats:
    gate_runs = 0
    gate_skips = 0
    copy_ok = 0
    copy_fail = 0
    copy_skipped = 0
    pending: int | None = None
    in_window = False

    for path in paths:
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            ts = _parse_line_time(line)
            if ts is not None:
                in_window = since <= ts <= until
            if not in_window:
                continue

            gate_match = _TOBIM_GATE.search(line)
            if gate_match:
                if gate_match.group(1) == "RUN":
                    gate_runs += 1
                else:
                    gate_skips += 1
                continue

            result_match = _TOBIM_RESULT.search(line)
            if result_match:
                copy_ok += int(result_match.group(1))
                copy_fail += int(result_match.group(2))
                copy_skipped = max(copy_skipped, int(result_match.group(3)))
                continue

            scan_match = _TOBIM_SCAN.search(line)
            if scan_match:
                pending = int(scan_match.group(2))

    return TobimScheduleStats(
        gate_runs=gate_runs,
        gate_skips=gate_skips,
        copy_ok=copy_ok,
        copy_fail=copy_fail,
        copy_skipped=copy_skipped,
        pending=pending,
    )


def _format_erp_execution_summary(
    stats: LogRunStats,
    biz: ErpBusinessStats,
) -> str:
    if stats.starts == 0:
        return "時段內無執行"
    parts = [f"跑完 {stats.oks}/{stats.starts} 次"]
    if biz.dry_run:
        parts.append(f"試跑｜待更新 {biz.forms_count} 筆（未 POST）")
    elif biz.forms_count == 0:
        parts.append("待更新 0 筆")
    else:
        parts.append(f"待更新 {biz.forms_count} 筆")
        if biz.update_ok or biz.update_fail:
            parts.append(f"成功 {biz.update_ok} / 失敗 {biz.update_fail}")
    return "｜".join(parts)


def _format_tobim_schedule_summary(
    stats: LogRunStats,
    sched: TobimScheduleStats,
) -> str:
    if stats.starts == 0:
        return "時段內無執行"
    parts = [f"跑完 {stats.oks}/{stats.starts} 次"]
    if sched.gate_skips and sched.gate_runs == 0:
        parts.append(f"SKIP {sched.gate_skips} 次（非執行窗）")
    elif sched.copy_ok or sched.copy_fail:
        parts.append(f"COPY 成功 {sched.copy_ok} / 失敗 {sched.copy_fail}")
    if sched.pending is not None:
        parts.append(f"待處理 {sched.pending} 巷")
    return "｜".join(parts)


def _assets_base_url() -> str:
    return os.environ.get("ASSETS_BASE_URL", "").strip() or DEFAULT_ASSETS_BASE_URL


def _tobim_server_detail_html(stats: TobimServerStats) -> str:
    pending_line = (
        f"<p style=\"margin:0 0 10px;\">待處理巷弄：<strong>{stats.pending}</strong></p>"
        if stats.pending is not None
        else ""
    )
    example_items = "".join(
        f"<li>{example}</li>" for example in stats.examples[:3]
    )
    examples_block = (
        f"<p style=\"margin:0 0 6px;font-size:13px;color:#444;\"><strong>失敗範例</strong></p>"
        f"<ul style=\"margin:0 0 10px;padding-left:20px;font-size:13px;color:#444;\">"
        f"{example_items}</ul>"
        if example_items
        else ""
    )
    assets_url = _assets_base_url()
    return f"""
    <div style="margin:16px 0 0;padding:14px;background:#fafafa;border:1px solid #e8e8e8;border-radius:8px;font-size:14px;">
      <strong style="display:block;margin-bottom:8px;">{TOBIM_SERVER_LABEL} — 明細</strong>
      <p style="margin:0 0 10px;color:#555;">
        環景 Server 異常：圖片從環景主機複製不出來（<strong>非 NAS 排程問題</strong>）
      </p>
      {pending_line}
      <table style="width:100%;border-collapse:collapse;font-size:13px;margin-bottom:10px;">
        <tr style="background:#f0f4f8;">
          <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #ddd;">類型</th>
          <th style="padding:6px 8px;border-bottom:1px solid #ddd;">筆數</th>
          <th style="text-align:left;padding:6px 8px;border-bottom:1px solid #ddd;">說明</th>
        </tr>
        <tr>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;">📄 CSV 格式問題</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:center;">{stats.csv_fail}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;">CSV 無有效圖片檔名</td>
        </tr>
        <tr>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;">📷 來源圖片缺失</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;text-align:center;">{stats.source_missing}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #eee;">API 跑完但複製 0 張</td>
        </tr>
        <tr>
          <td style="padding:6px 8px;">🔌 API 離線</td>
          <td style="padding:6px 8px;text-align:center;">{stats.api_offline}</td>
          <td style="padding:6px 8px;">—</td>
        </tr>
      </table>
      {examples_block}
      <p style="margin:0;font-size:13px;color:#666;">
        建議：請查環景 Server（<a href="{assets_url}">{assets_url}</a>）的 SV 來源與 copy API
      </p>
    </div>
    """


def evaluate_tobim_server(
    stats: TobimServerStats,
    log_files: list[Path],
    *,
    since: datetime,
    until: datetime,
    respect_schedule: bool,
) -> JobHealth:
    log_names = ", ".join(path.name for path in log_files) or "（無 log 檔）"
    last_run_at = (
        stats.last_event_at.strftime("%Y-%m-%d %H:%M")
        if stats.last_event_at
        else None
    )
    day_window = JobSpec(
        label=TOBIM_SERVER_LABEL,
        log_prefix="tobim",
        schedule_mode=MODE_WEEKDAY,
    )
    if respect_schedule and not job_expected_in_window(day_window, since, until):
        return JobHealth(
            label=TOBIM_SERVER_LABEL,
            log_label="tobim（業務）",
            period_starts=0,
            period_ok=0,
            period_locked=0,
            period_fail=0,
            last_run_at=last_run_at,
            last_status=stats.last_result,
            status="skip",
            status_note=f"非執行窗（{_schedule_window_note(day_window)}）",
            log_files=log_names,
        )

    total_copy = stats.copy_ok + stats.copy_fail
    execution_summary = (
        f"COPY 成功 {stats.copy_ok}<br>"
        f'<span style="color:#c62828;">失敗 {stats.copy_fail}</span> / '
        f'<span style="color:#888;">略過 {stats.copy_skipped}</span>'
    )
    detail_html: str | None = None
    failure_snippet: str | None = None
    suggestion: str | None = None

    if stats.api_offline > 0 and total_copy == 0:
        status, note = "fail", f"API 離線 {stats.api_offline} 次"
        suggestion = "環景 API 離線；請確認主機與 9880 服務是否啟動"
    elif total_copy == 0:
        if stats.api_offline > 0:
            status, note = "fail", f"API 離線 {stats.api_offline} 次"
            suggestion = "環景 API 離線；請確認主機與 9880 服務是否啟動"
        else:
            status, note = "skip", "時段內無 COPY 執行"
    elif stats.copy_fail > 0 and stats.copy_ok == 0:
        status, note = "degraded", "圖片複製失敗"
        detail_html = _tobim_server_detail_html(stats)
        if stats.examples:
            failure_snippet = stats.examples[0][:120]
        suggestion = f"請查環景 Server（{_assets_base_url()}）"
    elif stats.copy_fail > 0:
        status, note = "warn", f"部分失敗（成功 {stats.copy_ok} / 失敗 {stats.copy_fail}）"
        detail_html = _tobim_server_detail_html(stats)
        if stats.examples:
            failure_snippet = stats.examples[0][:120]
    else:
        status, note = "ok", "複製正常"

    return JobHealth(
        label=TOBIM_SERVER_LABEL,
        log_label="tobim（業務）",
        period_starts=total_copy,
        period_ok=stats.copy_ok,
        period_locked=0,
        period_fail=stats.copy_fail,
        last_run_at=last_run_at,
        last_status=stats.last_result,
        status=status,
        status_note=note,
        log_files=log_names,
        failure_snippet=failure_snippet,
        suggestion=suggestion,
        execution_summary=execution_summary,
        detail_html=detail_html,
        row_background="#fffaf0" if status in ("degraded", "fail", "warn") else None,
    )


def collect_tobim_server_health(
    log_dir_path: Path,
    since: datetime,
    until: datetime,
    *,
    respect_schedule: bool,
) -> JobHealth:
    paths = log_paths_for_window(log_dir_path, "tobim", since, until)
    stats = parse_tobim_server_window(paths, since, until)
    return evaluate_tobim_server(
        stats,
        paths,
        since=since,
        until=until,
        respect_schedule=respect_schedule,
    )


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
        note = _schedule_window_note(spec)
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
            execution_summary=_job_execution_summary(
                spec, stats, log_files, since=since, until=until
            ),
        )

    unfinished = max(0, stats.starts - stats.oks - stats.locked)
    period_fail = max(unfinished, stats.tracebacks)
    failure_snippet, suggestion = extract_failure_snippet(log_files, since, until)

    grace = grace_minutes_for(spec)
    started_at = stats.last_event_at
    elapsed = (now - started_at) if started_at is not None else timedelta.max
    in_grace = elapsed < timedelta(minutes=grace)
    still_running = (
        spec.log_prefix == "tobim"
        and started_at is not None
        and elapsed < timedelta(minutes=TOBIM_MAX_RUNTIME_MIN)
        and _tobim_run_in_progress(log_files, started_at)
    )

    if (
        unfinished >= 1
        and stats.last_event_label == "START"
        and started_at is not None
        and (in_grace or still_running)
    ):
        period_fail = stats.tracebacks
        if still_running and not in_grace:
            status, note = "ok", "執行中（複製進行中，暫不計異常）"
        else:
            status, note = "ok", f"執行中（{grace} 分鐘內暫不計異常）"
    elif not log_files:
        status, note = "warn", "時段內找不到 log 檔"
    elif stats.starts == 0:
        status, note = "warn", "時段內無 [START]"
    elif period_fail > 0:
        status, note = "fail", f"異常 {period_fail} 次"
    elif (
        stats.paused_skips > 0
        and unfinished == 0
        and stats.tracebacks == 0
    ):
        status, note = "skip", "手動暫停（TOBIM_PAUSED=1）"
        failure_snippet = None
        suggestion = None
    elif (
        stats.api_skips > 0
        and unfinished == 0
        and stats.tracebacks == 0
    ):
        status, note = "warn", "API 無法連線，已略過"
        failure_snippet = None
        suggestion = "環景 API 離線，腳本已略過；請確認主機與 9880 服務是否啟動"
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
        execution_summary=_job_execution_summary(
            spec, stats, log_files, since=since, until=until
        ),
    )


def _job_execution_summary(
    spec: JobSpec,
    stats: LogRunStats,
    log_files: list[Path],
    *,
    since: datetime,
    until: datetime,
) -> str | None:
    if spec.log_prefix == "erp":
        biz = parse_erp_business_window(log_files, since, until)
        return _format_erp_execution_summary(stats, biz)
    if spec.log_prefix == "tobim":
        sched = parse_tobim_schedule_window(log_files, since, until)
        return _format_tobim_schedule_summary(stats, sched)
    return None


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
    items = [
        collect_job_health(
            root, spec, since, until, now, respect_schedule=respect_schedule
        )
        for spec in MONITORED_JOBS
    ]
    items.append(
        collect_tobim_server_health(
            root, since, until, respect_schedule=respect_schedule
        )
    )
    return items


def status_badge(status: str) -> tuple[str, str]:
    if status == "ok":
        return "🟢", "正常"
    if status == "degraded":
        return "🟠", "異常"
    if status == "warn":
        return "🟡", "注意"
    if status == "skip":
        return "⏭", "略過"
    return "🔴", "異常"


def should_send_telegram(items: list[JobHealth]) -> bool:
    """Telegram 固定回報各 job 執行結果（含全綠）。"""
    return True


def _plain_telegram_text(text: str) -> str:
    plain = re.sub(r"<br\s*/?>", " / ", text, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", plain).strip()


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
        if item.execution_summary:
            lines.append(f"  {_plain_telegram_text(item.execution_summary)}")
        elif item.status == "skip":
            lines.append(f"  {item.status_note}")
            continue
        else:
            lines.append(
                f"  {item.period_starts} 次啟動 / {item.period_ok} OK / "
                f"{item.period_fail} 異常"
            )
        if item.status == "skip" and not item.execution_summary:
            lines.append(f"  {item.status_note}")
            continue
        if item.last_run_at and item.last_status:
            lines.append(f"  最近：{item.last_run_at} {item.last_status}")
        if item.status_note and item.status != "skip":
            lines.append(f"  {item.status_note}")
        if item.status in ("fail", "warn", "degraded") and item.failure_snippet:
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
    detail_blocks: list[str] = []
    for item in items:
        icon, text = status_badge(item.status)
        extra = ""
        if item.failure_snippet:
            extra = (
                f'<br><span style="color:#888;font-size:12px;">'
                f"{item.failure_snippet}</span>"
            )
        if item.execution_summary:
            execution_cell = item.execution_summary
        else:
            execution_cell = (
                f"{item.period_starts} 次啟動<br>"
                f'<span style="color:#2e7d32;">OK {item.period_ok}</span> / '
                f'<span style="color:#888;">locked {item.period_locked}</span> / '
                f'<span style="color:#c62828;">異常 {item.period_fail}</span>'
            )
        row_style = (
            f' style="background:{item.row_background};"'
            if item.row_background
            else ""
        )
        rows.append(
            f"""
            <tr{row_style}>
              <td><strong>{item.label}</strong><br>
                  <span style="color:#666;font-size:12px;">{item.log_label}_*.log</span></td>
              <td>{icon} {text}<br>
                  <span style="color:#666;font-size:12px;">{item.status_note}</span>{extra}</td>
              <td>{execution_cell}</td>
              <td>{item.last_run_at or "—"}<br>{item.last_status or "—"}</td>
              <td style="font-size:12px;color:#666;">{item.log_files}</td>
            </tr>
            """
        )
        if item.detail_html:
            detail_blocks.append(item.detail_html)

    details_section = "".join(detail_blocks)

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
    <p style="margin:0 0 12px;font-size:14px;color:#444;">
      <strong>排程列</strong> → NAS cron / Docker 是否存活<br>
      <strong>環景 Server 列</strong> → 環景主機資料與服務是否正常（非 NAS 排程問題）
    </p>
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
    {details_section}
    <p style="margin:16px 0 0;font-size:12px;color:#888;">
      執行窗參考：ERP 為工作日 08:30–17:30；ToBim 為工作日全天。
    </p>
  </div>
</body>
</html>"""


def load_smtp() -> SmtpSettings:
    host = os.environ.get(ENV_SMTP_HOST, "").strip()
    user = os.environ.get(ENV_SMTP_USER, "").strip()
    password = os.environ.get(ENV_SMTP_PASSWORD, "").strip()
    from_addr = os.environ.get(ENV_SMTP_FROM, "").strip() or user
    to_addr = os.environ.get(ENV_EMAIL_TO, "").strip()
    port = int_env(ENV_SMTP_PORT, 587)
    missing = [
        k
        for k, v in [
            ("SMTP_HOST", host),
            ("SMTP_USER", user),
            ("SMTP_PASSWORD", password),
            ("EMAIL_TO", to_addr),
        ]
        if not v
    ]
    if missing:
        raise RuntimeError(f"缺少郵件設定：{', '.join(missing)}")
    return SmtpSettings(
        host=host,
        port=port,
        user=user,
        password=password,
        from_addr=from_addr,
        to_addr=to_addr,
    )


def send_email(smtp: SmtpSettings, subject: str, html_body: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp.from_addr
    msg["To"] = smtp.to_addr
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP(smtp.host, smtp.port, timeout=60) as server:
        server.starttls()
        server.login(smtp.user, smtp.password)
        server.sendmail(smtp.from_addr, [smtp.to_addr], msg.as_string())


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
        f"{SUBJECT_LABELS.get(item.label, item.label)}{status_badge(item.status)[0]}"
        for item in items
    )
