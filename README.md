# CronJob

## 功能

- **ERP**：定時掃描「檢查自動分錄借貸不平衡」列表頁，篩選「沒有在鼎新」的列，並呼叫 `update_balance_status.asp`（每筆預設間隔 5 秒）。
- **ToBim 環景**：於**工作日全天**（台北時間；國定假日與週末不執行）掃描環景檔案瀏覽器上 ToBim 各案號內巷弄；若缺少 `.jpg` 與 `.txt` 則呼叫 `/api/copy-images-and-gps-sse`（等同「複製圖片及產生 Img_GPS」按鈕），兩者皆有則跳過。不依案號 `hasStreetView` 過濾；掃描時以案號層子資料夾的 `hasGpsTxt` 略過已完成巷弄，僅對待處理者查內容。

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

## Synology NAS（SSH）部署與排程

專案在 NAS（例如 `192.168.98.48`）以 Docker 的 Python 3.12 執行腳本，並用 `/etc/cron.d` 排程。

### 排程總覽（台北時間）

| Job | 腳本 | Cron | 工作日頻率（約） |
|-----|------|------|------------------|
| ERP | `erp_update_not_in_dingxin.py` | 每 30 分（`:00` / `:30`） | **18 次／日** |
| ToBim | `tobim_copy_images_gps.py` | 週一～五每小時（`0 * * * 1-5`） | **24 次／日** |
| Email 早報 | `nas_health_email.py` | 每天 10:05 | — |
| Email 午報 | `nas_health_email.py` | 每天 15:00 | — |
| Telegram | `nas_health_check.py` | 週一～五每 2 小時（含夜間） | **12 次／日** |

**Gate**（`scripts/should_run.py`，國定假日／週末一律不執行業務）：

| 功能 | `SHOULD_RUN_MODE` | 允許時段 |
|------|-------------------|----------|
| **ERP** | `day`（預設） | **08:30～17:30** |
| **ToBim** | `weekday` | **工作日全天** |

ERP：cron 在非執行窗仍可能觸發，但 `should_run.py` 會 SKIP。ToBim：週末不觸發 cron；工作日任何時段皆可 RUN（假日 gate 仍擋）。

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

- **`run_health_email.sh`、`run_health_check.sh`、`run_tobim.sh`**：隨 repo 提供（`git clone` / `git pull` 後即有）。
- **`run_erp.sh`**：請依下方範本在 NAS 建立（或對照更新舊版）。

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

#### `run_tobim.sh`（隨 repo 提供）

`TOBIM_DRY_RUN` 由 **`.env` 控制**（正式 `0`、試跑 `1`）。預設每輪 **時間預算 45 分鐘**（`TOBIM_RUN_BUDGET_SEC=2700`），時間內盡量 COPY；`TOBIM_MAX_COPY_PER_RUN=0` 表示不限制巷數（可設正整數作硬上限）。巷間隔 **5** 秒（`TOBIM_DELAY_SEC`）。

NAS 版已設 `SHOULD_RUN_STRICT=1`：`SHOULD_RUN_MODE=weekday` 時僅擋國定假日／週末；ERP 仍為 `day`（08:30 前／17:30 後 SKIP）。

若 NAS 上仍是舊版手寫腳本，請 `git pull` 後覆蓋或補上 `-e SHOULD_RUN_STRICT=1`。

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

#### 健康檢查（NAS cron）

讀 `logs/erp_*.log`、`logs/tobim_*.log`：

| 列 | 判斷依據 |
|----|---------|
| **ERP 借貸平衡** | `[START]` / `[OK]` — cron 是否存活 |
| **ToBim 排程** | `[START]` / `[OK]` — cron 是否存活（COPY 失敗仍算 OK） |
| **ToBim 環景 Server** | `結果：成功/失敗`、FAIL 分類 — 環景主機業務是否正常 |

| 通道 | 腳本 | 節奏 | 說明 |
|------|------|------|------|
| **Email** | `run_health_email.sh` | 10:05 / 15:05 | 早晚摘要（需 `SMTP_*`、`EMAIL_TO`） |
| **Telegram** | `run_health_check.sh` | 平日每 2h | 即時通知（需 `TELEGRAM_BOT_TOKEN`、`TELEGRAM_CHAT_ID`） |

`.env` 範例（Telegram 相關）：

```bash
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=你的chat_id
HEALTH_NOTIFY_OK=0        # 已廢止；Telegram 固定每次回報 job 結果
HEALTH_SKIP_HOLIDAY=1     # 放假日不發 Telegram
HEALTH_GRACE_MIN=5          # ERP：START 後 N 分鐘內不計異常
HEALTH_GRACE_MIN_TOBIM=15   # ToBim：預設 15（複製常需 10–15 分）；log 有 COPY 活動時最長寬限 50 分
HEALTH_WINDOW_HOURS=2     # Telegram 統計過去 N 小時
```

Telegram 會依 job 預期執行窗判斷：ERP 為工作日 08:30–17:30；ToBim 為工作日全天。週末／放假日顯示 ⏭ 略過。異常時會附 log 錯誤摘要，不必 SSH 查檔。

Email 主旨範例：`ERP🟢 / ToBim 排程🟢 / 環景 Server🟠`。環景 Server 異常時，信內會附 CSV 格式、來源圖片缺失等明細（非 NAS 排程問題）。

ToBim 執行前會以短逾時（預設 5 秒）探測 `ASSETS_BASE_URL`：連不上則優雅略過（log 印 `SKIP  API 無法連線`），環景 Server 列顯示 🔴。長期暫停可設 `TOBIM_PAUSED=1`（排程列顯示 ⏭ 手動暫停）。

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
  -e DIGEST_DRY_RUN=1 \
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

# ToBim: weekdays hourly 24h (Taipei)
0 * * * 1-5 jimWu01 /volume1/docker/CronJob/run_tobim.sh

# Health email: Taipei 10:05 / 15:00
5 10 * * * jimWu01 /volume1/docker/CronJob/run_health_email.sh morning
0 15 * * * jimWu01 /volume1/docker/CronJob/run_health_email.sh afternoon

# Telegram health: weekdays every 2h (incl. night)
5 0,2,4,6,8,10,12,14,16,18,20,22 * * 1-5 jimWu01 /volume1/docker/CronJob/run_health_check.sh
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

