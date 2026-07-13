#!/usr/bin/env python3
"""
掃描環景檔案瀏覽器 ToBim 各案號下的巷弄資料夾：
若同時缺少 *.jpg 與 *.txt，則呼叫與前端相同的 SSE API 執行「複製圖片及產生 Img_GPS」；
若兩者皆有則跳過。不依案號 hasStreetView 過濾（案號可能已標完成，但巷弄仍待處理）。

掃描策略：每案號只呼叫一次 /api/folder，以子資料夾的 hasGpsTxt 判斷是否已完成；
僅對 hasGpsTxt=false 的巷弄再查內容（驗證 .csv 等），避免對已完成的大資料夾做重型列舉。

不需模擬瀏覽器；對應前端按鈕：/api/copy-images-and-gps-sse

設定來源：腳本內建預設；選填覆寫見 .env.example（本機建議僅設 TOBIM_DRY_RUN=1）。
"""

from __future__ import annotations

import json
import os
import re
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
ENV_PAUSED = "TOBIM_PAUSED"
ENV_PROBE_TIMEOUT_SEC = "TOBIM_PROBE_TIMEOUT_SEC"
ENV_REQUIRE_CSV = "TOBIM_REQUIRE_CSV"
ENV_REQUEST_TIMEOUT_SEC = "TOBIM_REQUEST_TIMEOUT_SEC"
ENV_SSE_TIMEOUT_SEC = "TOBIM_SSE_TIMEOUT_SEC"
ENV_MAX_COPY_PER_RUN = "TOBIM_MAX_COPY_PER_RUN"
ENV_RUN_BUDGET_SEC = "TOBIM_RUN_BUDGET_SEC"
ENV_USER_AGENT = "TOBIM_USER_AGENT"

_DEFAULT_BASE_URL = "http://assets.bim-group.com:9880"
_DEFAULT_DELAY_SEC = 5.0
_DEFAULT_MAX_COPY_PER_RUN = 0
_DEFAULT_RUN_BUDGET_SEC = 2700.0
_DEFAULT_PROBE_TIMEOUT_SEC = 5.0
_DEFAULT_REQUEST_TIMEOUT_SEC = 120.0
_DEFAULT_SSE_TIMEOUT_SEC = 3600.0
_DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; ToBim-Copy-Gps/1.0)"

# 健康檢查會辨識這兩行（勿改格式）
LOG_API_SKIP_PREFIX = "SKIP  API 無法連線"
LOG_PAUSED_PREFIX = "SKIP  已暫停"


@dataclass(frozen=True, slots=True)
class Settings:
    base_url: str
    delay_seconds: float
    dry_run: bool
    require_csv: bool
    request_timeout_seconds: float
    sse_timeout_seconds: float
    max_copy_per_run: int
    run_budget_seconds: float
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


@dataclass(frozen=True, slots=True)
class CopySseResult:
    """SSE 複製結果；images/gps 可能來自 complete JSON 或 progress 估算。"""

    saw_progress: bool
    progress_steps: int
    images: int | None
    gps: int | None


@dataclass(frozen=True, slots=True)
class VerifiedCopy:
    jpg_count: int
    has_gps_txt: bool


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_dotenv_if_present() -> None:
    dotenv_path = _project_root() / ".env"
    if dotenv_path.is_file():
        load_dotenv(dotenv_path, override=False)


def _truthy_env(name: str) -> bool:
    value = os.environ.get(name, "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _int_env(name: str, *, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return max(0, default)
    try:
        return max(0, int(raw))
    except ValueError:
        return max(0, default)


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
    """未設定 TOBIM_DRY_RUN 時預設 dry-run（本機安全）。"""
    if ENV_DRY_RUN in os.environ:
        return _truthy_env(ENV_DRY_RUN)
    return True


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
        max_copy_per_run=_int_env(
            ENV_MAX_COPY_PER_RUN, default=_DEFAULT_MAX_COPY_PER_RUN
        ),
        run_budget_seconds=_float_env(
            ENV_RUN_BUDGET_SEC, default=_DEFAULT_RUN_BUDGET_SEC
        ),
        user_agent=_str_env(ENV_USER_AGENT, default=_DEFAULT_USER_AGENT),
    )


def probe_timeout_seconds() -> float:
    return _float_env(ENV_PROBE_TIMEOUT_SEC, default=_DEFAULT_PROBE_TIMEOUT_SEC)


def probe_api_reachable(
    session: requests.Session,
    base_url: str,
    *,
    timeout: float,
) -> bool:
    """短逾時探測 /api/folders；連不上時不進入 120s 掃描。"""
    try:
        url = urljoin(f"{base_url}/", "/api/folders")
        response = session.get(url, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        return bool(payload.get("success"))
    except (requests.RequestException, ValueError, TypeError):
        return False


def preflight_or_exit(settings: Settings, session: requests.Session) -> int | None:
    """若應提早結束回傳 exit code，否則回傳 None 繼續掃描。"""
    if _truthy_env(ENV_PAUSED):
        print(f"{LOG_PAUSED_PREFIX}（TOBIM_PAUSED=1）")
        return 0
    if not probe_api_reachable(
        session,
        settings.base_url,
        timeout=probe_timeout_seconds(),
    ):
        print(f"{LOG_API_SKIP_PREFIX} ({settings.base_url})")
        return 0
    return None


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
    require_csv: bool = True,
) -> Iterator[SubfolderTask]:
    folders_payload = _api_get_json(
        session, base_url, "/api/folders", timeout=timeout
    )
    for top in folders_payload.get("data", []):
        if top.get("mainFolder") != "ToBim":
            continue
        case_id = top.get("name", "")
        top_path = top.get("fullPath", "")
        case_payload = _api_get_json(
            session,
            base_url,
            f"/api/folder?path={quote(top_path, safe='')}",
            timeout=timeout,
        )
        for entry in case_payload.get("data", []):
            if not entry.get("isDirectory"):
                continue
            sub_name = entry.get("name", "")
            sub_path = f"{top_path}\\{sub_name}"
            if entry.get("hasGpsTxt"):
                yield SubfolderTask(
                    case_id=case_id,
                    subfolder_name=sub_name,
                    full_path=sub_path,
                    has_jpg=True,
                    has_txt=True,
                    has_csv=True,
                    file_count=0,
                )
                continue

            has_jpg = False
            has_txt = False
            has_csv = not require_csv
            file_count = 0
            if require_csv:
                content_payload = _api_get_json(
                    session,
                    base_url,
                    f"/api/folder?path={quote(sub_path, safe='')}",
                    timeout=timeout,
                )
                files = content_payload.get("data", [])
                has_jpg, has_txt, has_csv = _file_flags(files)
                file_count = len(files)

            yield SubfolderTask(
                case_id=case_id,
                subfolder_name=sub_name,
                full_path=sub_path,
                has_jpg=has_jpg,
                has_txt=has_txt,
                has_csv=has_csv,
                file_count=file_count,
            )


def should_skip(task: SubfolderTask, *, require_csv: bool) -> tuple[bool, str]:
    if require_csv and not task.has_csv:
        return True, "無 .csv（前端不顯示按鈕）"
    if task.has_jpg and task.has_txt:
        return True, "已有 .jpg 與 .txt"
    return False, ""


def _parse_sse_payload(payload: str) -> dict[str, Any] | None:
    """解析 SSE data 行；complete 若 JSON 過大解析失敗仍標記為 complete。"""
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        if '"type":"complete"' in payload or '"type": "complete"' in payload:
            return {"type": "complete", "_raw": payload}
        if '"type":"error"' in payload or '"type": "error"' in payload:
            return {"type": "error", "message": "SSE 回傳錯誤（JSON 無法完整解析）"}
        return None


def _copied_counts_from_raw(payload: str) -> tuple[int | None, int | None]:
    """從過大的 complete 行估算 copiedImages / gpsEntries 數量。"""
    images: int | None = None
    gps: int | None = None
    for key, target in (("copiedImages", "images"), ("gpsEntries", "gps")):
        pattern = rf'"{key}"\s*:\s*\['
        match = re.search(pattern, payload)
        if not match:
            continue
        start = match.end()
        depth = 1
        end = start
        while end < len(payload) and depth > 0:
            char = payload[end]
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
            end += 1
        chunk = payload[start : end - 1] if depth == 0 else payload[start:]
        count = len(re.findall(r'"(?:[^"\\]|\\.)*"', chunk))
        if target == "images":
            images = count
        else:
            gps = count
    return images, gps


def _counts_from_complete(data: dict[str, Any]) -> tuple[int | None, int | None]:
    copied = data.get("copiedImages")
    gps_entries = data.get("gpsEntries")
    images = len(copied) if isinstance(copied, list) else None
    gps = len(gps_entries) if isinstance(gps_entries, list) else None
    if images is None or gps is None:
        raw = data.get("_raw")
        if isinstance(raw, str):
            raw_images, raw_gps = _copied_counts_from_raw(raw)
            if images is None:
                images = raw_images
            if gps is None:
                gps = raw_gps
    return images, gps


def verify_alley_copied(
    session: requests.Session,
    base_url: str,
    folder_path: str,
    *,
    timeout: float,
) -> VerifiedCopy:
    """COPY 後以 /api/folder 驗證是否出現 .jpg 與 GPS txt。"""
    content_payload = _api_get_json(
        session,
        base_url,
        f"/api/folder?path={quote(folder_path, safe='')}",
        timeout=timeout,
    )
    files = content_payload.get("data", [])
    has_jpg, has_txt, _ = _file_flags(files)
    jpg_count = sum(
        1
        for item in files
        if not item.get("isDirectory")
        and item.get("name", "").lower().endswith(".jpg")
    )
    if not has_jpg or not has_txt:
        raise RuntimeError(
            f"複製後驗證失敗（jpg={jpg_count} txt={'Y' if has_txt else 'N'}）"
        )
    return VerifiedCopy(jpg_count=jpg_count, has_gps_txt=has_txt)


def run_copy_sse(
    session: requests.Session,
    base_url: str,
    folder_path: str,
    *,
    sse_timeout: float,
) -> CopySseResult:
    url = urljoin(
        f"{base_url}/",
        f"/api/copy-images-and-gps-sse?path={quote(folder_path, safe='')}",
    )
    complete: dict[str, Any] | None = None
    saw_progress = False
    progress_steps = 0
    with session.get(url, stream=True, timeout=(30.0, sse_timeout)) as response:
        response.raise_for_status()
        for raw_line in response.iter_lines(decode_unicode=True):
            if not raw_line or not raw_line.startswith("data: "):
                continue
            raw_payload = raw_line[6:]
            data = _parse_sse_payload(raw_payload)
            if not data:
                continue
            event_type = data.get("type")
            if event_type == "progress":
                saw_progress = True
                progress_steps += 1
                continue
            if event_type == "error":
                raise RuntimeError(data.get("message") or data.get("error") or "SSE 錯誤")
            if event_type == "complete":
                if not saw_progress:
                    raise RuntimeError(
                        "SSE complete 但未見 progress 事件（可能未實際複製）"
                    )
                if "_raw" not in data:
                    data = {**data, "_raw": raw_payload}
                complete = data
                break
    if complete is None:
        raise RuntimeError("SSE 連線結束但未收到 complete 事件")
    images, gps = _counts_from_complete(complete)
    return CopySseResult(
        saw_progress=saw_progress,
        progress_steps=progress_steps,
        images=images,
        gps=gps,
    )


def _budget_exhausted(run_started: float, budget_seconds: float) -> bool:
    if budget_seconds <= 0:
        return False
    return (time.monotonic() - run_started) >= budget_seconds


def _format_budget_label(budget_seconds: float) -> str:
    if budget_seconds <= 0:
        return "off"
    return f"{int(budget_seconds)}s"


def main() -> int:
    settings = load_settings()
    session = requests.Session()
    session.headers.update({"User-Agent": settings.user_agent})

    print(f"BASE_URL {settings.base_url}")
    max_copy_label = (
        str(settings.max_copy_per_run)
        if settings.max_copy_per_run > 0
        else "unlimited"
    )
    budget_label = _format_budget_label(settings.run_budget_seconds)
    print(
        f"DRY_RUN={settings.dry_run}  REQUIRE_CSV={settings.require_csv}  "
        f"DELAY={settings.delay_seconds}s  MAX_COPY={max_copy_label}  "
        f"RUN_BUDGET={budget_label}"
    )

    early_exit = preflight_or_exit(settings, session)
    if early_exit is not None:
        return early_exit

    skipped = 0
    copied = 0
    failed = 0
    pending_copy: list[SubfolderTask] = []

    for task in iter_subfolder_tasks(
        session,
        settings.base_url,
        timeout=settings.request_timeout_seconds,
        require_csv=settings.require_csv,
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

    total_pending = len(pending_copy)
    deferred = 0
    if settings.max_copy_per_run > 0 and total_pending > settings.max_copy_per_run:
        deferred = total_pending - settings.max_copy_per_run
        for task in pending_copy[settings.max_copy_per_run :]:
            print(
                f"DEFER {task.case_id}/{task.subfolder_name}  "
                f"(本輪上限 {settings.max_copy_per_run})"
            )
        pending_copy = pending_copy[: settings.max_copy_per_run]

    print(
        f"\n掃描完成：略過 {skipped}，待處理 {total_pending}"
        + (f"，本輪 COPY {len(pending_copy)}，延後 {deferred}" if deferred else "")
        + (
            f"，時間預算 {budget_label}"
            if settings.run_budget_seconds > 0
            else ""
        )
    )

    if settings.dry_run:
        print("DRY_RUN：未呼叫 copy-images-and-gps-sse")
        return 0

    run_started = time.monotonic()
    budget_deferred = 0

    for index, task in enumerate(pending_copy):
        if settings.max_copy_per_run > 0 and index >= settings.max_copy_per_run:
            budget_deferred = len(pending_copy) - index
            for deferred_task in pending_copy[index:]:
                print(
                    f"DEFER {deferred_task.case_id}/{deferred_task.subfolder_name}  "
                    f"(本輪上限 {settings.max_copy_per_run})"
                )
            break
        if _budget_exhausted(run_started, settings.run_budget_seconds):
            budget_deferred = len(pending_copy) - index
            for deferred_task in pending_copy[index:]:
                print(
                    f"DEFER {deferred_task.case_id}/{deferred_task.subfolder_name}  "
                    f"(時間預算 {int(settings.run_budget_seconds)}s)"
                )
            break
        label = f"{task.case_id}/{task.subfolder_name}"
        status = (
            f"jpg={'Y' if task.has_jpg else 'N'} "
            f"txt={'Y' if task.has_txt else 'N'} "
            f"csv={'Y' if task.has_csv else 'N'} "
            f"files={task.file_count}"
        )
        print(f"COPY  {label}  {task.full_path}  {status}")
        if index > 0 and settings.delay_seconds > 0:
            time.sleep(settings.delay_seconds)
        try:
            sse_result = run_copy_sse(
                session,
                settings.base_url,
                task.full_path,
                sse_timeout=settings.sse_timeout_seconds,
            )
            verified = verify_alley_copied(
                session,
                settings.base_url,
                task.full_path,
                timeout=settings.request_timeout_seconds,
            )
            copied += 1
            print(
                f"OK    {task.case_id}/{task.subfolder_name}  "
                f"images={verified.jpg_count} "
                f"progress={sse_result.progress_steps} verified"
            )
        except Exception as exc:  # noqa: BLE001 — 記錄後繼續下一筆
            failed += 1
            print(
                f"FAIL  {task.case_id}/{task.subfolder_name}  {exc}",
                file=sys.stderr,
            )

    print(
        f"\n結果：成功 {copied}，失敗 {failed}，略過 {skipped}"
        + (f"，時間預算延後 {budget_deferred}" if budget_deferred else "")
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
