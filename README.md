# CronJob

## 功能

- **ERP**：定時掃描「檢查自動分錄借貸不平衡」列表頁，篩選「沒有在鼎新」的列，並呼叫 `update_balance_status.asp`（每筆預設間隔 5 秒）。
- **ToBim 環景**：掃描 `assets.bim-group.com` 上 ToBim「未完成」案號內各巷弄；若缺少 `.jpg` 與 `.txt` 則呼叫 `/api/copy-images-and-gps-sse`（等同「複製圖片及產生 Img_GPS」按鈕），兩者皆有則跳過。

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
| `erp-balance-update.yml` | `erp_update_not_in_dingxin.py` | `7,37 * * * *`（每 30 分，:07/:37） | 每 **30 分鐘** 一次 |
| `tobim-copy-images-gps.yml` | `tobim_copy_images_gps.py` | `7 */2 * * *`（每 2 小時，:07） | 每 **2 小時** 一次 |
| `health-digest-email.yml` | `workflow_health_email.py` | `0 2,7 * * *`（台北 10:00 / 15:00） | 每天都寄信（查 Actions 存活） |

Cron 刻意避開 **:00 / :30** 以降低 GitHub Actions 排程延遲；實際開始時間仍可能漂移數分鐘（平台 best-effort）。

**ERP / ToBim 共同 Gate**（`scripts/should_run.py`）：

- **工作日**：非台灣政府行政機關放假日（含週末、補假）
- **時段**：**08:30～17:30**（含）
- 其餘時間 workflow 仍可能被 cron 喚起，但只跑 gate、**不執行**更新／複製腳本

**通過 Gate 後，業務腳本大約會在（台北）**：

- **ERP**：08:37、09:07、09:37 … 直至 17:37（約 **19～20 次／工作日**）
- **ToBim**：08:07、10:07、12:07、14:07、16:07（約 **5 次／工作日**；18:07 已超出 17:30 窗）

兩支為 **獨立 workflow**，頻率由各自 cron 決定，假日／下班由 gate 擋住。

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

