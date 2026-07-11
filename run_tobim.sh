#!/bin/bash
set -euo pipefail

BASE="/volume1/docker/CronJob"
mkdir -p "$BASE/logs" "$BASE/.lock"
LOCK="$BASE/.lock/tobim.lock"
LOG="$BASE/logs/tobim_$(date +%F).log"

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') [SKIP] ToBim locked" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

echo "$(date '+%F %T') [START] ToBim (docker py3.12)" >> "$LOG"
set +e
sudo -n /usr/local/bin/docker run --rm \
  --env-file "$BASE/.env" \
  -e SHOULD_RUN_MODE=offhours \
  -e SHOULD_RUN_STRICT=1 \
  -v "$BASE":/app \
  -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/should_run.py && python scripts/tobim_copy_images_gps.py" >> "$LOG" 2>&1
set -e
# 排程健康：COPY 失敗或 should_run SKIP 仍視為跑完；Traceback 由健康檢查另判
echo "$(date '+%F %T') [OK] ToBim" >> "$LOG"
