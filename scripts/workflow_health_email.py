#!/usr/bin/env python3
"""
彙整 GitHub Actions 工作流執行狀態，寄送早晚健康檢查 Email（ERP + ToBim 各一段）。

用途：確認排程有在跑（存活），非業務結果彙總。
設定：.env / GitHub Secrets（SMTP_*、GITHUB_TOKEN 於 Actions 自動提供）。
"""

from __future__ import annotations

import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv

TZ = ZoneInfo("Asia/Taipei")

ENV_SMTP_HOST = "SMTP_HOST"
ENV_SMTP_PORT = "SMTP_PORT"
ENV_SMTP_USER = "SMTP_USER"
ENV_SMTP_PASSWORD = "SMTP_PASSWORD"
ENV_SMTP_FROM = "SMTP_FROM"
ENV_EMAIL_TO = "EMAIL_TO"
ENV_DIGEST_SLOT = "DIGEST_SLOT"  # morning | afternoon
ENV_DIGEST_DRY_RUN = "DIGEST_DRY_RUN"
ENV_GITHUB_REPOSITORY = "GITHUB_REPOSITORY"
ENV_GITHUB_TOKEN = "GITHUB_TOKEN"

# workflow 檔名 → 顯示名稱
MONITORED_WORKFLOWS: list[tuple[str, str]] = [
    ("erp-balance-update.yml", "ERP 借貸平衡"),
    ("tobim-copy-images-gps.yml", "ToBim 環景"),
]


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    host: str
    port: int
    user: str
    password: str
    from_addr: str
    to_addr: str


@dataclass(frozen=True, slots=True)
class WorkflowHealth:
    label: str
    workflow_file: str
    workflow_id: int | None
    period_total: int
    period_success: int
    period_failure: int
    period_other: int
    last_run_at: str | None
    last_conclusion: str | None
    last_html_url: str | None
    status: str  # ok | warn | fail
    status_note: str


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_dotenv_if_present() -> None:
    path = _project_root() / ".env"
    if path.is_file():
        load_dotenv(path, override=False)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def load_smtp() -> SmtpSettings:
    host = os.environ.get(ENV_SMTP_HOST, "").strip()
    user = os.environ.get(ENV_SMTP_USER, "").strip()
    password = os.environ.get(ENV_SMTP_PASSWORD, "").strip()
    from_addr = os.environ.get(ENV_SMTP_FROM, "").strip() or user
    to_addr = os.environ.get(ENV_EMAIL_TO, "").strip()
    port = _int_env(ENV_SMTP_PORT, 587)
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


def digest_window(slot: str, now: datetime) -> tuple[datetime, datetime, str]:
    """回傳 (開始, 結束, 時段說明)。"""
    if slot == "morning":
        start = (now - timedelta(days=1)).replace(hour=15, minute=0, second=0, microsecond=0)
        label = "昨日 15:00 ～ 今晨 10:00"
    else:
        start = now.replace(hour=10, minute=0, second=0, microsecond=0)
        label = "今日 10:00 ～ 15:00"
    return start, now, label


def _github_session() -> tuple[str, dict[str, str]]:
    repo = os.environ.get(ENV_GITHUB_REPOSITORY, "").strip()
    token = os.environ.get(ENV_GITHUB_TOKEN, "").strip()
    if not repo:
        raise RuntimeError(
            "需要 GITHUB_REPOSITORY（Actions 請在 workflow 傳入 github.repository）"
        )
    if not token:
        raise RuntimeError(
            "需要 GITHUB_TOKEN（Actions 請在 workflow 傳入 secrets.GITHUB_TOKEN）"
        )
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return repo, headers


def _api_get(url: str, headers: dict[str, str]) -> Any:
    response = requests.get(url, headers=headers, timeout=60)
    response.raise_for_status()
    return response.json()


def _resolve_workflow_id(
    repo: str, headers: dict[str, str], workflow_file: str
) -> int | None:
    owner, name = repo.split("/", 1)
    url = f"https://api.github.com/repos/{owner}/{name}/actions/workflows"
    data = _api_get(url, headers)
    for item in data.get("workflows", []):
        if item.get("path", "").endswith(workflow_file):
            return int(item["id"])
    return None


def _fetch_runs_since(
    repo: str,
    headers: dict[str, str],
    workflow_id: int,
    since: datetime,
) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    runs: list[dict[str, Any]] = []
    page = 1
    while page <= 5:
        url = (
            f"https://api.github.com/repos/{owner}/{name}/actions/workflows/"
            f"{workflow_id}/runs?per_page=100&page={page}"
        )
        data = _api_get(url, headers)
        batch = data.get("workflow_runs", [])
        if not batch:
            break
        for run in batch:
            created = run.get("created_at", "")
            if not created:
                continue
            created_dt = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(TZ)
            if created_dt < since.astimezone(TZ):
                return runs
            runs.append(run)
        page += 1
    return runs


def _evaluate_health(
    label: str,
    workflow_file: str,
    workflow_id: int | None,
    runs: list[dict[str, Any]],
) -> WorkflowHealth:
    success = sum(1 for r in runs if r.get("conclusion") == "success")
    failure = sum(1 for r in runs if r.get("conclusion") == "failure")
    other = len(runs) - success - failure
    last = runs[0] if runs else None
    last_at = None
    last_conclusion = None
    last_url = None
    if last:
        created = last.get("created_at", "")
        if created:
            last_at = (
                datetime.fromisoformat(created.replace("Z", "+00:00"))
                .astimezone(TZ)
                .strftime("%Y-%m-%d %H:%M")
            )
        last_conclusion = last.get("conclusion") or last.get("status")
        last_url = last.get("html_url")

    if workflow_id is None:
        status, note = "fail", "找不到 workflow（尚未 push 或檔名不符）"
    elif not runs:
        status, note = "warn", "時段內無執行紀錄（可能放假日或尚未到執行窗）"
    elif failure > 0:
        status, note = "fail", f"時段內 {failure} 次失敗"
    elif last_conclusion not in (None, "success"):
        status, note = "warn", f"最近一次為 {last_conclusion}"
    else:
        status, note = "ok", "排程正常"

    return WorkflowHealth(
        label=label,
        workflow_file=workflow_file,
        workflow_id=workflow_id,
        period_total=len(runs),
        period_success=success,
        period_failure=failure,
        period_other=other,
        last_run_at=last_at,
        last_conclusion=last_conclusion,
        last_html_url=last_url,
        status=status,
        status_note=note,
    )


def _status_badge(status: str) -> tuple[str, str]:
    if status == "ok":
        return "🟢", "正常"
    if status == "warn":
        return "🟡", "注意"
    return "🔴", "異常"


def render_html(
    *,
    slot_title: str,
    period_label: str,
    generated_at: str,
    repo: str,
    items: list[WorkflowHealth],
) -> str:
    rows = []
    for item in items:
        icon, text = _status_badge(item.status)
        link = (
            f'<a href="{item.last_html_url}">查看 Actions</a>'
            if item.last_html_url
            else "—"
        )
        rows.append(
            f"""
            <tr>
              <td><strong>{item.label}</strong><br><span style="color:#666;font-size:12px;">{item.workflow_file}</span></td>
              <td>{icon} {text}<br><span style="color:#666;font-size:12px;">{item.status_note}</span></td>
              <td>{item.period_total} 次<br>
                  <span style="color:#2e7d32;">成功 {item.period_success}</span> /
                  <span style="color:#c62828;">失敗 {item.period_failure}</span>
              </td>
              <td>{item.last_run_at or "—"}<br>{item.last_conclusion or "—"}</td>
              <td>{link}</td>
            </tr>
            """
        )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <title>CronJob {slot_title}</title>
</head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#222;line-height:1.5;margin:0;padding:16px;background:#f5f5f5;">
  <div style="max-width:640px;margin:0 auto;background:#fff;border-radius:8px;padding:20px;border:1px solid #e0e0e0;">
    <h2 style="margin:0 0 8px;">CronJob 排程健康檢查 — {slot_title}</h2>
    <p style="margin:0 0 16px;color:#666;font-size:14px;">
      產生時間：{generated_at}（台北）<br>
      統計區間：{period_label}<br>
      Repository：<code>{repo}</code>
    </p>
    <p style="margin:0 0 12px;font-size:14px;">確認 ERP / ToBim 定時工作流是否有執行（非業務明細彙總）。</p>
    <table style="width:100%;border-collapse:collapse;font-size:14px;">
      <thead>
        <tr style="background:#f0f4f8;">
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">功能</th>
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">狀態</th>
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">時段內執行</th>
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">最近一次</th>
          <th style="text-align:left;padding:8px;border-bottom:1px solid #ddd;">連結</th>
        </tr>
      </thead>
      <tbody>
        {''.join(rows)}
      </tbody>
    </table>
    <p style="margin:16px 0 0;font-size:12px;color:#888;">
      執行窗參考：ERP 工作日 08:30–17:30；ToBim 工作日 17:30 後～翌日 08:30 前（should_run.py）。詳細 log 請至 GitHub Actions。
    </p>
  </div>
</body>
</html>"""


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


def collect_health(
    repo: str,
    headers: dict[str, str],
    since: datetime,
) -> list[WorkflowHealth]:
    results: list[WorkflowHealth] = []
    for workflow_file, label in MONITORED_WORKFLOWS:
        workflow_id = _resolve_workflow_id(repo, headers, workflow_file)
        runs: list[dict[str, Any]] = []
        if workflow_id is not None:
            runs = _fetch_runs_since(repo, headers, workflow_id, since)
        results.append(_evaluate_health(label, workflow_file, workflow_id, runs))
    return results


def main() -> int:
    _load_dotenv_if_present()
    slot = os.environ.get(ENV_DIGEST_SLOT, "morning").strip().lower()
    if slot not in ("morning", "afternoon"):
        slot = "morning"
    slot_title = "早報 10:00" if slot == "morning" else "午報 15:00"

    now = datetime.now(TZ)
    since, _until, period_label = digest_window(slot, now)
    generated_at = now.strftime("%Y-%m-%d %H:%M")

    try:
        repo, headers = _github_session()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    items = collect_health(repo, headers, since)
    html = render_html(
        slot_title=slot_title,
        period_label=period_label,
        generated_at=generated_at,
        repo=repo,
        items=items,
    )

    subject = f"[CronJob] {slot_title} — " + " / ".join(
        f"{i.label}{_status_badge(i.status)[0]}" for i in items
    )

    if _truthy_env(ENV_DIGEST_DRY_RUN):
        print(subject)
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
