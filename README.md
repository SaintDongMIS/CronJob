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

| 工作流程 | 腳本 | Cron 觸發 | 實際執行業務的時間 |
|----------|------|-----------|-------------------|
| `erp-balance-update.yml` | `erp_update_not_in_dingxin.py` | 每 30 分鐘 `*/30 * * * *`（UTC） | 見下方 Gate |
| `tobim-copy-images-gps.yml` | `tobim_copy_images_gps.py` | 同上 | 見下方 Gate |
| `health-digest-email.yml` | `workflow_health_email.py` | **每天 10:00、15:00**（UTC 02:00 / 07:00） | 每次都寄信（查 Actions 存活） |

**ERP / ToBim 共同 Gate**（`scripts/should_run.py`）：

- **工作日**：非台灣政府行政機關放假日（含週末、補假）
- **時段**：**08:30～17:30**（含）
- 其餘時間 workflow 仍會被 cron 喚起，但只跑 gate、**不執行**更新／複製腳本

在上述時段內，兩支 job 約每 **30 分鐘** 執行一次業務邏輯（例如 08:30、09:00、09:30 … 17:30，每個工作日各約 **19 次**）。ERP 與 ToBim **排程相同、各自獨立 workflow**。

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

