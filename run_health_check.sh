#!/bin/bash
set -euo pipefail

BASE="/volume1/docker/CronJob"
LOG="$BASE/logs/health_check_$(date +%F).log"

echo "$(date '+%F %T') [START] health-check telegram" >> "$LOG"
sudo -n /usr/local/bin/docker run --rm \
  --env-file "$BASE/.env" \
  -e NAS_HOSTNAME="$(hostname)" \
  -e NAS_LOG_DIR=/app/logs \
  -v "$BASE":/app \
  -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/nas_health_check.py" >> "$LOG" 2>&1
echo "$(date '+%F %T') [OK] health-check" >> "$LOG"
