#!/bin/bash
set -euo pipefail

BASE="/volume1/docker/CronJob"
SLOT="${1:-morning}"
LOG="$BASE/logs/health_email_$(date +%F).log"

echo "$(date '+%F %T') [START] health-email slot=$SLOT" >> "$LOG"
sudo -n /usr/local/bin/docker run --rm \
  --env-file "$BASE/.env" \
  -e DIGEST_SLOT="$SLOT" \
  -e NAS_HOSTNAME="$(hostname)" \
  -e NAS_LOG_DIR=/app/logs \
  -v "$BASE":/app \
  -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/nas_health_email.py" >> "$LOG" 2>&1
echo "$(date '+%F %T') [OK] health-email" >> "$LOG"
