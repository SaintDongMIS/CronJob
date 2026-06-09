# CronJob

## 功能

- **ERP**：定時掃描「檢查自動分錄借貸不平衡」列表頁，篩選「沒有在鼎新」的列，並呼叫 `update_balance_status.asp`（每筆預設間隔 5 秒）。
- **ToBim 環景**：於**工作日下班後**（台北 17:30 後至翌日 08:30 前；國定假日與週末不執行）掃描環景檔案瀏覽器上 ToBim 各案號內巷弄；若缺少 `.jpg` 與 `.txt` 則呼叫 `/api/copy-images-and-gps-sse`（等同「複製圖片及產生 Img_GPS」按鈕），兩者皆有則跳過。不依案號 `hasStreetView` 過濾；掃描時以案號層子資料夾的 `hasGpsTxt` 略過已完成巷弄，僅對待處理者查內容。

## 本機執行

```bash
cd /Users/jim/Documents/CronJob
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 編輯 .env：至少填 ERP_LIST_URL；若站台需要登入，填 ERP_COOKIE

python scripts/erp_update_not_in_dingxin.py

# ToBim：本機 .env 設 TOBIM_DRY_RUN=1 即可預覽；正式執行：
TOBIM_DRY_RUN=0 python scripts/tobim_copy_images_gps.py

# ToBim 診斷掃描（列出待 COPY 巷弄，不呼叫 SSE）：
python scripts/scan_tobim_all.py
```

## GitHub Actions

### 排程總覽（台北時間 Asia/Taipei）

| 工作流程 | 腳本 | Cron 觸發（UTC） | 工作日業務頻率（約） |
|----------|------|------------------|----------------------|
| `erp-balance-update.yml` | `erp_update_not_in_dingxin.py` | `0,30 * * * *`（每 30 分，:00/:30） | 每 **30 分鐘** 一次 |
| `tobim-copy-images-gps.yml` | `tobim_copy_images_gps.py` | `0 */2 * * *`（每 2 小時，:00） | 每 **2 小時** 一次 |
| `health-digest-email.yml` | `workflow_health_email.py` | `0 2,7 * * *`（台北 10:00 / 15:00） | 每天都寄信（查 Actions 存活） |

（先前因 GitHub Actions 排程延遲曾刻意避開 **:00 / :30**；目前已改回整點/半點。）

**Gate**（`scripts/should_run.py`，國定假日／週末一律不執行業務）：

| 功能 | `SHOULD_RUN_MODE` | 允許時段（台北） |
|------|-------------------|------------------|
| **ERP** | `day`（預設） | **08:30～17:30**（含） |
| **ToBim** | `offhours` | **17:30 後** 或 **翌日 08:30 前** |

其餘時間 workflow 仍可能被 cron 喚起，但只跑 gate、**不執行**業務腳本。

**通過 Gate 後，業務腳本大約會在（台北）**：

- **ERP**：09:00、09:30、10:00 … 直至 17:30（約 **18 次／工作日**）
- **ToBim**：18:00、20:00、22:00、00:00、02:00、04:00、06:00（每 2 小時一檔，約 **7 次／工作日**；08:00 起屬上班時間會 SKIP）

兩支為 **獨立 workflow**，頻率由各自 cron 決定。

**健康檢查 Email**（與業務執行脫鉤）：

| 班次 | 寄信時間（台北） | 統計區間 |
|------|------------------|----------|
| 早報 | **10:00** | 昨日 15:00 ～ 今日 10:00 |
| 午報 | **15:00** | 今日 10:00 ～ 15:00 |

一封郵件內含 **ERP + ToBim** 兩段，用 GitHub Actions 執行紀錄判斷排程是否正常（非業務結果彙總）。若改在 **NAS** 跑 ERP/ToBim，請改用下方 NAS 的 `run_health_email.sh`，可停用本 workflow。

### Secrets / Variables

**Secrets（建議）**

- ERP：`ERP_COOKIE`、`ERP_DELAY_SEC`（選填）
- ToBim：`TOBIM_DRY_RUN`（選填 Variables，預設 `0` 正式執行；其餘參數用腳本預設）
- Email：`SMTP_HOST`、`SMTP_PORT`、`SMTP_USER`、`SMTP_PASSWORD`、`SMTP_FROM`、`EMAIL_TO`

**Variables**

- `ERP_LIST_URL`：必填

### 健康檢查 Email（本機試寄）

```bash
# 需 GITHUB_TOKEN（repo read）與 .env 內 SMTP_*
export GITHUB_REPOSITORY=owner/CronJob
export GITHUB_TOKEN=ghp_...
DIGEST_SLOT=morning DIGEST_DRY_RUN=1 python scripts/workflow_health_email.py
python scripts/workflow_health_email.py
```

## Synology NAS（SSH）部署與排程（不依賴 GitHub Actions）

若環境位於內網且不便使用 GitHub Actions，可將專案放到 NAS（例如 `192.168.98.48`）並用 Docker 的 Python 執行腳本，再用 `/etc/cron.d` 排程。

### 目標

- **不依賴 NAS 內建 Python 版本**（常見只有 Python 3.8，會遇到 `zoneinfo`、`dataclass slots` 等相容問題）
- 每次執行以 `python:3.12-slim` container 跑腳本（跑完即 `--rm`）
- 排程採用 `/etc/cron.d/cronjob`（可全程 SSH 操作，不開 DSM UI）

### 一次性初始化

1) **Clone 專案到 NAS**

```bash
cd /volume1/docker
git clone https://github.com/SaintDongMIS/CronJob.git CronJob
cd /volume1/docker/CronJob
```

2) **準備 `.env`**

```bash
cp .env.example .env
# 編輯 .env，至少：
#   ERP_LIST_URL=...
#   ASSETS_BASE_URL=http://192.168.98.61:9880   # 環景 API；Docker 內請用內網 IP，勿用 hostname
#   TOBIM_DRY_RUN=0                               # 正式執行；試跑改 1
#   SMTP_*、EMAIL_TO=...                          # NAS 健康檢查 Email
#   TELEGRAM_BOT_TOKEN=...、TELEGRAM_CHAT_ID=...  # NAS Telegram 輪詢
```

> 注意：`.env` 可能含敏感資訊（如 `ERP_COOKIE`），**不要提交到 Git**。  
> `assets.bim-group.com` 在 NAS 主機上或許能連，但 **Docker container 內常 timeout**；請改內網 IP（例如 `192.168.98.61:9880`）。

若 NAS 未開 SFTP subsystem（導致 `scp` 失敗），可在 Mac 端用舊協定強制上傳：

```bash
scp -O /Users/jim/Documents/CronJob/.env jimWu01@192.168.98.48:/volume1/docker/CronJob/.env
```

3) **確認 Docker 可用**

```bash
sudo -n /usr/local/bin/docker --version
```

### 執行腳本（以 Docker 跑 Python 3.12）

- **`run_health_email.sh`、`run_health_check.sh`**：隨 repo 提供（`git clone` / `git pull` 後即有）。
- **`run_erp.sh`、`run_tobim.sh`**：請依下方範本在 NAS 建立（或對照更新舊版）。

```bash
cd /volume1/docker/CronJob
chmod +x run_erp.sh run_tobim.sh run_health_email.sh run_health_check.sh
```

#### 建立 `run_erp.sh`

```bash
#!/bin/bash
set -euo pipefail

BASE="/volume1/docker/CronJob"
mkdir -p "$BASE/logs" "$BASE/.lock"
LOCK="$BASE/.lock/erp.lock"
LOG="$BASE/logs/erp_$(date +%F).log"

if ! mkdir "$LOCK" 2>/dev/null; then
  echo "$(date '+%F %T') [SKIP] ERP locked" >> "$LOG"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

echo "$(date '+%F %T') [START] ERP (docker py3.12)" >> "$LOG"
sudo -n /usr/local/bin/docker run --rm \
  --env-file "$BASE/.env" \
  -v "$BASE":/app \
  -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/should_run.py && python scripts/erp_update_not_in_dingxin.py" >> "$LOG" 2>&1
echo "$(date '+%F %T') [OK] ERP" >> "$LOG"
```

#### 建立 `run_tobim.sh`

`TOBIM_DRY_RUN` 由 **`.env` 控制**（正式 `0`、試跑 `1`）；**不要在 shell 裡再加 `-e TOBIM_DRY_RUN=1`**，否則會覆蓋 `.env`。

```bash
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
sudo -n /usr/local/bin/docker run --rm \
  --env-file "$BASE/.env" \
  -e SHOULD_RUN_MODE=offhours \
  -v "$BASE":/app \
  -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/should_run.py && python scripts/tobim_copy_images_gps.py" >> "$LOG" 2>&1
echo "$(date '+%F %T') [OK] ToBim" >> "$LOG"
```

> **NAS 與 GitHub Actions 的 Gate 差異**：Actions 在 `should_run=false` 時**不執行**業務腳本；NAS 的 `run_*.sh` 使用 `should_run.py && 業務腳本`，而 `should_run.py` **永遠 exit 0**，故 ToBim 在上班時段仍會被 cron 喚起（`should_run` 只會印 `SKIP` 到 log）。若要在 NAS 也嚴格擋住時段，需另改 shell 邏輯。

手動驗證（會產生 log）：

```bash
/volume1/docker/CronJob/run_erp.sh
/volume1/docker/CronJob/run_tobim.sh
tail -n 120 /volume1/docker/CronJob/logs/erp_$(date +%F).log
tail -n 120 /volume1/docker/CronJob/logs/tobim_$(date +%F).log

# ToBim 診斷掃描（待 COPY 巷弄列表）：
sudo -n /usr/local/bin/docker run --rm \
  --env-file /volume1/docker/CronJob/.env \
  -e NAS_LOG_DIR=/app/logs -v /volume1/docker/CronJob:/app -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/scan_tobim_all.py"
```

#### 健康檢查（NAS cron，不依賴 GitHub Actions）

讀 `logs/erp_*.log`、`logs/tobim_*.log` 的 `[START]`/`[OK]`（非 GitHub API、非業務明細）。

| 通道 | 腳本 | 節奏 | 說明 |
|------|------|------|------|
| **Email** | `run_health_email.sh` | 10:05 / 15:05 | 早晚摘要（需 `SMTP_*`、`EMAIL_TO`） |
| **Telegram** | `run_health_check.sh` | 平日每 2h | 即時通知（需 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`） |

`.env` 範例（Telegram 相關）：

```bash
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=你的chat_id
HEALTH_NOTIFY_OK=0        # 0=僅 warn/fail 發送；1=全綠也發
HEALTH_SKIP_HOLIDAY=1     # 放假日不發 Telegram
HEALTH_GRACE_MIN=5        # START 後 N 分鐘內不計異常（避免 10:00 誤報）
HEALTH_WINDOW_HOURS=2     # Telegram 統計過去 N 小時
```

Telegram 會依 job 預期執行窗判斷：ERP 工作日 08:30–17:30；ToBim 工作日 17:30 後～翌日 08:30 前。非執行窗顯示 ⏭ 略過。異常時會附 log 錯誤摘要，不必 SSH 查檔。

```bash
chmod +x run_health_email.sh run_health_check.sh

# Email 試寄（不真的發信）
sudo -n /usr/local/bin/docker run --rm \
  --env-file /volume1/docker/CronJob/.env \
  -e DIGEST_SLOT=morning -e DIGEST_DRY_RUN=1 \
  -e NAS_LOG_DIR=/app/logs -v /volume1/docker/CronJob:/app -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/nas_health_email.py"

# Telegram 試跑（不真的發送）
sudo -n /usr/local/bin/docker run --rm \
  --env-file /volume1/docker/CronJob/.env \
  -e DIGEST_DRY_RUN=1 -e HEALTH_NOTIFY_OK=1 \
  -e NAS_HOSTNAME="$(hostname)" -e NAS_LOG_DIR=/app/logs \
  -v /volume1/docker/CronJob:/app -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/nas_health_check.py"

# 正式寄 Email / 發 Telegram
/volume1/docker/CronJob/run_health_email.sh morning
/volume1/docker/CronJob/run_health_check.sh
tail -n 20 /volume1/docker/CronJob/logs/health_email_$(date +%F).log
tail -n 10 /volume1/docker/CronJob/logs/health_check_$(date +%F).log
```

### 設定排程（全程 SSH）

建立 `/etc/cron.d/cronjob`：

```bash
sudo -n sh -lc 'cat > /etc/cron.d/cronjob <<'"'"'EOF'"'"'
# CronJob (ERP/ToBim) - run via docker python
# format: min hour dom mon dow user command

# ERP: every 30 minutes (:00 / :30)
0,30 * * * * jimWu01 /volume1/docker/CronJob/run_erp.sh

# ToBim: every 2 hours at :00
0 */2 * * * jimWu01 /volume1/docker/CronJob/run_tobim.sh

# Health email: Taipei 10:05 / 15:05（:05 避開與業務同秒啟動誤報）
5 10 * * * jimWu01 /volume1/docker/CronJob/run_health_email.sh morning
5 15 * * * jimWu01 /volume1/docker/CronJob/run_health_email.sh afternoon

# Telegram health: weekdays 08–17 every 2h（ERP 監控）
5 8,10,12,14,16 * * 1-5 jimWu01 /volume1/docker/CronJob/run_health_check.sh
# Telegram health: weekdays 18–06 every 2h（ToBim 監控）
5 18,20,22,0,2,4,6 * * 1-5 jimWu01 /volume1/docker/CronJob/run_health_check.sh
EOF
chmod 644 /etc/cron.d/cronjob
synoschedtask --sync
'
```

> 修改 `/etc/cron.d/cronjob` 後，建議執行 `sudo -n synoschedtask --sync` 讓排程器同步。

### 日後更新代碼（重點）

1) **更新程式碼**

```bash
cd /volume1/docker/CronJob
git pull --ff-only
```

2) **不需要重設排程**

排程會自動用最新程式碼執行（因為 container 會 mount 專案資料夾）。

3) **若 `requirements.txt` 有更新**

因為每次執行都會在 container 內 `pip install -r requirements.txt`，不需額外手動安裝；但若外網不穩導致安裝變慢，可考慮改成自建 image（把依賴預先烤進 image）以提高穩定性。

4) **log 位置**

- ERP：`/volume1/docker/CronJob/logs/erp_YYYY-MM-DD.log`
- ToBim：`/volume1/docker/CronJob/logs/tobim_YYYY-MM-DD.log`
- 健康檢查 Email：`/volume1/docker/CronJob/logs/health_email_YYYY-MM-DD.log`
- 健康檢查 Telegram：`/volume1/docker/CronJob/logs/health_check_YYYY-MM-DD.log`

> 若 ERP/ToBim 改在 NAS 執行，可停用 GitHub 的 `health-digest-email.yml`；改由 NAS 的 `run_health_email.sh`（早晚摘要）與 `run_health_check.sh`（Telegram 輪詢）監控。

