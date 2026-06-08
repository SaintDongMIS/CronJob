#!/usr/bin/env python3
"""
診斷用：掃描所有 ToBim 巷弄待處理狀態（與 tobim_copy_images_gps.py 相同掃描邏輯）。

用法：
  python scripts/scan_tobim_all.py
  python scripts/scan_tobim_all.py --folders /tmp/folders.json
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from tobim_copy_images_gps import (  # noqa: E402
    ENV_BASE_URL,
    _DEFAULT_BASE_URL,
    _require_csv_from_env,
    iter_subfolder_tasks,
    should_skip,
)


def _project_root() -> Path:
    return _SCRIPTS_DIR.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="掃描 ToBim 巷弄待處理狀態（診斷用）")
    parser.add_argument(
        "--folders",
        type=Path,
        help="保留參數（相容舊用法）；掃描一律走案號層 API",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()

    if args.folders is not None:
        print(f"（忽略 --folders {args.folders}，改為即時 API 掃描）")

    load_dotenv(_project_root() / ".env", override=False)
    base_url = (os.environ.get(ENV_BASE_URL, "").strip() or _DEFAULT_BASE_URL).rstrip(
        "/"
    )
    require_csv = _require_csv_from_env()

    session = requests.Session()
    need_copy: list[tuple[str, str, bool, bool]] = []
    skipped = 0

    for task in iter_subfolder_tasks(
        session,
        base_url,
        timeout=args.timeout,
        require_csv=require_csv,
    ):
        skip, _reason = should_skip(task, require_csv=require_csv)
        if skip:
            skipped += 1
            continue
        need_copy.append(
            (task.case_id, task.subfolder_name, task.has_jpg, task.has_txt)
        )

    print(f"BASE_URL {base_url}")
    print(f"REQUIRE_CSV={require_csv}")
    print(f"\n=== 掃描結果 ===")
    print(f"略過（已完成 hasGpsTxt）: {skipped}")
    print(f"待 COPY: {len(need_copy)}")
    for case_id, sub_name, has_jpg, has_txt in need_copy:
        print(f"  {case_id}/{sub_name}  jpg={has_jpg} txt={has_txt}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
