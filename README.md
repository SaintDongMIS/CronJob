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

一封郵件內含 **ERP + ToBim** 兩段，用 GitHub Actions 執行紀錄判斷排程是否正常（非業務結果彙總）。

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
# 編輯 .env：至少填 ERP_LIST_URL；需要登入就填 ERP_COOKIE；ASSETS_BASE_URL 建議用內網版
```

> 注意：`.env` 可能含敏感資訊（如 `ERP_COOKIE`），**不要提交到 Git**。

若 NAS 未開 SFTP subsystem（導致 `scp` 失敗），可在 Mac 端用舊協定強制上傳：

```bash
scp -O /Users/jim/Documents/CronJob/.env jimWu01@192.168.98.48:/volume1/docker/CronJob/.env
```

3) **確認 Docker 可用**

```bash
sudo -n /usr/local/bin/docker --version
```

### 執行腳本（以 Docker 跑 Python 3.12）

#### 建立兩個可重複執行腳本

建立 `/volume1/docker/CronJob/run_erp.sh`：

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

建立 `/volume1/docker/CronJob/run_tobim.sh`（上線前可先 `TOBIM_DRY_RUN=1` 觀察）：

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
  -e TOBIM_DRY_RUN=1 \
  -v "$BASE":/app \
  -w /app \
  python:3.12-slim \
  bash -lc "pip -q install -r requirements.txt && python scripts/should_run.py && python scripts/tobim_copy_images_gps.py" >> "$LOG" 2>&1
echo "$(date '+%F %T') [OK] ToBim" >> "$LOG"
```

給執行權限：

```bash
chmod +x /volume1/docker/CronJob/run_erp.sh /volume1/docker/CronJob/run_tobim.sh
```

手動驗證（會產生 log）：

```bash
/volume1/docker/CronJob/run_erp.sh
/volume1/docker/CronJob/run_tobim.sh
tail -n 120 /volume1/docker/CronJob/logs/erp_$(date +%F).log
tail -n 120 /volume1/docker/CronJob/logs/tobim_$(date +%F).log
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

