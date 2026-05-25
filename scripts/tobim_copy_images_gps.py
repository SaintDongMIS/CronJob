#!/usr/bin/env python3
"""
掃描環景檔案瀏覽器 ToBim「未完成」案號下的各巷弄資料夾：
若同時缺少 *.jpg 與 *.txt，則呼叫與前端相同的 SSE API 執行「複製圖片及產生 Img_GPS」；
若兩者皆有則跳過。

不需模擬瀏覽器；對應前端按鈕：/api/copy-images-and-gps-sse

設定來源：腳本內建預設；選填覆寫見 .env.example（本機建議僅設 TOBIM_DRY_RUN=1）。
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote, urljoin

import requests
from dotenv import load_dotenv

ENV_BASE_URL = "ASSETS_BASE_URL"
ENV_DELAY_SEC = "TOBIM_DELAY_SEC"
ENV_DRY_RUN = "TOBIM_DRY_RUN"
ENV_REQUIRE_CSV = "TOBIM_REQUIRE_CSV"
ENV_REQUEST_TIMEOUT_SEC = "TOBIM_REQUEST_TIMEOUT_SEC"
ENV_SSE_TIMEOUT_SEC = "TOBIM_SSE_TIMEOUT_SEC"
ENV_USER_AGENT = "TOBIM_USER_AGENT"

_DEFAULT_BASE_URL = "http://assets.bim-group.com:9880"
_DEFAULT_DELAY_SEC = 2.0
_DEFAULT_REQUEST_TIMEOUT_SEC = 120.0
_DEFAULT_SSE_TIMEOUT_SEC = 3600.0
_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; ToBim-Copy-Gps/1.0)"


@dataclass(frozen=True, slots=True)
class Settings:
    base_url: str
    delay_seconds: float
    dry_run: bool
    require_csv: bool
    request_timeout_seconds: float
    sse_timeout_seconds: float
    user_agent: str


@dataclass(frozen=True, slots=True)
class SubfolderTask:
    case_id: str
    subfolder_name: str
    full_path: str
    has_jpg: bool
    has_txt: bool
    has_csv: bool
    file_count: int


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


def _require_csv_from_env() -> bool:
    raw = os.environ.get(ENV_REQUIRE_CSV, "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _dry_run_from_env() -> bool:
    """未設定 TOBIM_DRY_RUN 時：本機預設 dry-run，GitHub Actions 預設正式執行。"""
    if ENV_DRY_RUN in os.environ:
        return _truthy_env(ENV_DRY_RUN)
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() != "true"


def load_settings() -> Settings:
    _load_dotenv_if_present()
    base = os.environ.get(ENV_BASE_URL, "").strip() or _DEFAULT_BASE_URL
    return Settings(
        base_url=base.rstrip("/"),
        delay_seconds=_float_env(ENV_DELAY_SEC, default=_DEFAULT_DELAY_SEC),
        dry_run=_dry_run_from_env(),
        require_csv=_require_csv_from_env(),
        request_timeout_seconds=_float_env(
            ENV_REQUEST_TIMEOUT_SEC, default=_DEFAULT_REQUEST_TIMEOUT_SEC
        ),
        sse_timeout_seconds=_float_env(
            ENV_SSE_TIMEOUT_SEC, default=_DEFAULT_SSE_TIMEOUT_SEC
        ),
        user_agent=_str_env(ENV_USER_AGENT, default=_DEFAULT_USER_AGENT),
    )


def _file_flags(files: list[dict[str, Any]]) -> tuple[bool, bool, bool]:
    jpg = False
    txt = False
    csv = False
    for item in files:
        if item.get("isDirectory"):
            continue
        name = item.get("name", "")
        lower = name.lower()
        if lower.endswith(".jpg"):
            jpg = True
        elif lower.endswith(".txt"):
            txt = True
        elif lower.endswith(".csv"):
            csv = True
    return jpg, txt, csv


def _api_get_json(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    timeout: float,
) -> dict[str, Any]:
    url = urljoin(f"{base_url}/", path.lstrip("/"))
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(payload.get("error") or "API 回傳 success=false")
    return payload


def iter_subfolder_tasks(
    session: requests.Session,
    base_url: str,
    *,
    timeout: float,
) -> Iterator[SubfolderTask]:
    folders_payload = _api_get_json(
        session, base_url, "/api/folders", timeout=timeout
    )
    for top in folders_payload.get("data", []):
        if top.get("mainFolder") != "ToBim":
            continue
        if top.get("hasStreetView"):
            continue
        case_id = top.get("name", "")
        top_path = top.get("fullPath", "")
        for entry in top.get("files", []):
            if not entry.get("isDirectory"):
                continue
            sub_name = entry.get("name", "")
            sub_path = f"{top_path}\\{sub_name}"
            content_payload = _api_get_json(
                session,
                base_url,
                f"/api/folder?path={quote(sub_path, safe='')}",
                timeout=timeout,
            )
            files = content_payload.get("data", [])
            has_jpg, has_txt, has_csv = _file_flags(files)
            yield SubfolderTask(
                case_id=case_id,
                subfolder_name=sub_name,
                full_path=sub_path,
                has_jpg=has_jpg,
                has_txt=has_txt,
                has_csv=has_csv,
                file_count=len(files),
            )


def should_skip(task: SubfolderTask, *, require_csv: bool) -> tuple[bool, str]:
    if require_csv and not task.has_csv:
        return True, "無 .csv（前端不顯示按鈕）"
    if task.has_jpg and task.has_txt:
        return True, "已有 .jpg 與 .txt"
    return False, ""


def _parse_sse_payload(payload: str) -> dict[str, Any] | None:
    """解析 SSE data 行；partial 事件若 JSON 過大解析失敗則略過。"""
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        if '"type":"complete"' in payload or '"type": "complete"' in payload:
            return {"type": "complete"}
        if '"type":"error"' in payload or '"type": "error"' in payload:
            return {"type": "error", "message": "SSE 回傳錯誤（JSON 無法完整解析）"}
        return None


def run_copy_sse(
    session: requests.Session,
    base_url: str,
    folder_path: str,
    *,
    sse_timeout: float,
) -> dict[str, Any]:
    url = urljoin(
        f"{base_url}/",
        f"/api/copy-images-and-gps-sse?path={quote(folder_path, safe='')}",
    )
    complete: dict[str, Any] | None = None
    with session.get(url, stream=True, timeout=(30.0, sse_timeout)) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data: "):
                continue
            data = _parse_sse_payload(raw_line[6:])
            if not data:
                continue
            event_type = data.get("type")
            if event_type == "error":
                raise RuntimeError(data.get("message") or data.get("error") or "SSE 錯誤")
            if event_type == "complete":
                complete = data
                break
    if complete is None:
        raise RuntimeError("SSE 連線結束但未收到 complete 事件")
    return complete


def main() -> int:
    settings = load_settings()
    session = requests.Session()
    session.headers.update({"User-Agent": settings.user_agent})

    print(f"BASE_URL {settings.base_url}")
    print(
        f"DRY_RUN={settings.dry_run}  REQUIRE_CSV={settings.require_csv}  "
        f"DELAY={settings.delay_seconds}s"
    )

    skipped = 0
    copied = 0
    failed = 0
    pending_copy: list[SubfolderTask] = []

    for task in iter_subfolder_tasks(
        session,
        settings.base_url,
        timeout=settings.request_timeout_seconds,
    ):
        skip, reason = should_skip(task, require_csv=settings.require_csv)
        status = (
            f"jpg={'Y' if task.has_jpg else 'N'} "
            f"txt={'Y' if task.has_txt else 'N'} "
            f"csv={'Y' if task.has_csv else 'N'} "
            f"files={task.file_count}"
        )
        label = f"{task.case_id}/{task.subfolder_name}"
        if skip:
            skipped += 1
            print(f"SKIP  {label}  ({reason})  {status}")
            continue

        pending_copy.append(task)
        print(f"COPY  {label}  {task.full_path}  {status}")

    print(
        f"\n掃描完成：略過 {skipped}，待處理 {len(pending_copy)}"
    )

    if settings.dry_run:
        print("DRY_RUN：未呼叫 copy-images-and-gps-sse")
        return 0

    for index, task in enumerate(pending_copy):
        if index > 0 and settings.delay_seconds > 0:
            time.sleep(settings.delay_seconds)
        try:
            result = run_copy_sse(
                session,
                settings.base_url,
                task.full_path,
                sse_timeout=settings.sse_timeout_seconds,
            )
            copied += 1
            copied_images = result.get("copiedImages") or []
            gps_entries = result.get("gpsEntries") or []
            print(
                f"OK    {task.case_id}/{task.subfolder_name}  "
                f"images={len(copied_images)} gps={len(gps_entries)}"
            )
        except Exception as exc:  # noqa: BLE001 — 記錄後繼續下一筆
            failed += 1
            print(
                f"FAIL  {task.case_id}/{task.subfolder_name}  {exc}",
                file=sys.stderr,
            )

    print(f"\n結果：成功 {copied}，失敗 {failed}，略過 {skipped}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
