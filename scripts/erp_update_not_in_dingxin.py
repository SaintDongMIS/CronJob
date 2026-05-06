#!/usr/bin/env python3
"""
掃描「檢查自動分錄借貸不平衡」列表頁，找出指定檢查結果之列，
依序對每筆呼叫 update_balance_status.asp（間隔可設定，預設 5 秒）。

網頁上的「篩選狀態」僅在前端隱藏列；此腳本依儲存格文字篩選，效果等同選擇該篩選值。

設定來源：環境變數，以及專案根目錄的 .env（見 .env.example）。
"""

from __future__ import annotations

import json
import os
import hashlib
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 環境變數名稱（避免字串散落）
# ---------------------------------------------------------------------------
ENV_LIST_URL = "ERP_LIST_URL"
ENV_COOKIE = "ERP_COOKIE"
ENV_DELAY_SEC = "ERP_DELAY_SEC"
ENV_DRY_RUN = "ERP_DRY_RUN"
ENV_UPDATE_REL_PATH = "ERP_UPDATE_REL_PATH"
ENV_FILTER_STATUS = "ERP_FILTER_STATUS"
ENV_REQUEST_TIMEOUT_SEC = "ERP_REQUEST_TIMEOUT_SEC"
ENV_USER_AGENT = "ERP_USER_AGENT"

# 僅在對應環境變數未設定時使用之預設（URL 必須由 .env 或環境提供，不在此列預設）
_DEFAULT_UPDATE_REL_PATH = "update_balance_status.asp"
_DEFAULT_FILTER_STATUS = "沒有在鼎新"
_DEFAULT_REQUEST_TIMEOUT_SEC = 120.0
_DEFAULT_DELAY_SEC = 5.0
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (compatible; ERP-Balance-Updater/1.0; +https://github.com/SaintDongMIS)"
)


@dataclass(frozen=True, slots=True)
class Settings:
    list_url: str
    cookie: str
    delay_seconds: float
    dry_run: bool
    update_rel_path: str
    filter_status: str
    request_timeout_seconds: float
    user_agent: str


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_dotenv_if_present() -> None:
    dotenv_path = _project_root() / ".env"
    if dotenv_path.is_file():
        load_dotenv(dotenv_path, override=False)


def _truthy_env(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _float_env(name: str, *, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(0.0, default)
    try:
        return max(0.0, float(raw))
    except ValueError:
        return max(0.0, default)


def _str_env(name: str, *, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value if value else default


def load_settings() -> Settings:
    """自環境變數組裝設定；並於存在時載入專案根目錄 .env。"""
    _load_dotenv_if_present()

    list_url = os.environ.get(ENV_LIST_URL, "").strip()
    cookie = os.environ.get(ENV_COOKIE, "").strip()

    return Settings(
        list_url=list_url,
        cookie=cookie,
        delay_seconds=_float_env(ENV_DELAY_SEC, default=_DEFAULT_DELAY_SEC),
        dry_run=_truthy_env(ENV_DRY_RUN),
        update_rel_path=_str_env(
            ENV_UPDATE_REL_PATH,
            default=_DEFAULT_UPDATE_REL_PATH,
        ),
        filter_status=_str_env(ENV_FILTER_STATUS, default=_DEFAULT_FILTER_STATUS),
        request_timeout_seconds=_float_env(
            ENV_REQUEST_TIMEOUT_SEC,
            default=_DEFAULT_REQUEST_TIMEOUT_SEC,
        ),
        user_agent=_str_env(ENV_USER_AGENT, default=_DEFAULT_USER_AGENT),
    )


def collect_form_numbers(html: str, filter_status: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    form_numbers: list[str] = []
    seen: set[str] = set()
    for row in soup.select("tbody tr"):
        status_cell = row.select_one(".status-cell")
        if status_cell is None:
            continue
        if filter_status not in status_cell.get_text(strip=True):
            continue
        button = row.select_one("button.update-status-btn")
        if button is None:
            continue
        form_no = (button.get("data-formno") or "").strip()
        if not form_no or form_no in seen:
            continue
        seen.add(form_no)
        form_numbers.append(form_no)
    return form_numbers


def fetch_list(session: requests.Session, *, list_url: str, user_agent: str, timeout: float) -> str:
    response = session.get(
        list_url,
        headers={"User-Agent": user_agent},
        timeout=timeout,
    )
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding or "utf-8"
    return response.text


def post_update(
    session: requests.Session,
    *,
    update_url: str,
    form_no: str,
    user_agent: str,
    timeout: float,
) -> tuple[bool, str]:
    response = session.post(
        update_url,
        data={"formNo": form_no, "action": "update"},
        headers={
            "User-Agent": user_agent,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
        timeout=timeout,
    )
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        return False, f"HTTP {response.status_code}: {exc}"

    body = (response.text or "").strip()
    if not body:
        return False, "empty response"

    try:
        payload = response.json()
    except json.JSONDecodeError:
        return False, f"non-JSON response (first 200 chars): {body[:200]!r}"

    if payload.get("success"):
        return True, "ok"
    return False, str(payload.get("message") or payload)


def _mask_endpoint(url: str) -> str:
    """
    避免在 logs 揭露完整 URL。
    回傳格式：<host_hash8>/<basename>
    """
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").encode("utf-8")
        host_hash = hashlib.sha256(host).hexdigest()[:8] if host else "nohost"
        basename = (parsed.path.rsplit("/", 1)[-1] or "").strip() or "path"
        return f"{host_hash}/{basename}"
    except Exception:
        return "masked"


def main() -> int:
    settings = load_settings()

    if not settings.list_url:
        print(
            f"ERROR: 請設定 {ENV_LIST_URL}（參考 .env.example，或複製為 .env 後填入）",
            file=sys.stderr,
        )
        return 2

    if not settings.cookie:
        print(
            f"WARNING: 未設定 {ENV_COOKIE}，將以無 Cookie 方式嘗試（若站台有登入/權限驗證，POST 可能失敗）",
            file=sys.stderr,
        )

    update_url = urljoin(settings.list_url, settings.update_rel_path)
    session = requests.Session()
    if settings.cookie:
        session.headers.update({"Cookie": settings.cookie})

    print(f"LIST_URL set={bool(settings.list_url)} id={_mask_endpoint(settings.list_url)}")
    print(f"POST_URL set={bool(update_url)} id={_mask_endpoint(update_url)}")
    print(f"FILTER  {settings.filter_status}")
    print(f"DELAY   {settings.delay_seconds}s  DRY_RUN={settings.dry_run}")

    html = fetch_list(
        session,
        list_url=settings.list_url,
        user_agent=settings.user_agent,
        timeout=settings.request_timeout_seconds,
    )
    form_numbers = collect_form_numbers(html, settings.filter_status)
    print(f"符合「{settings.filter_status}」且可更新之表單數: {len(form_numbers)}")
    for index, form_no in enumerate(form_numbers, start=1):
        print(f"  {index}. {form_no}")

    if settings.dry_run:
        print("DRY_RUN：未送出任何 POST")
        return 0

    failure_count = 0
    for index, form_no in enumerate(form_numbers):
        if index > 0 and settings.delay_seconds > 0:
            time.sleep(settings.delay_seconds)
        success, message = post_update(
            session,
            update_url=update_url,
            form_no=form_no,
            user_agent=settings.user_agent,
            timeout=settings.request_timeout_seconds,
        )
        suffix = "OK" if success else "FAIL"
        print(f"[{index + 1}/{len(form_numbers)}] {form_no} -> {suffix} {message}")
        if not success:
            failure_count += 1

    return 1 if failure_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
