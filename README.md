# CronJob

## 功能

定時掃描 ERP「檢查自動分錄借貸不平衡」列表頁，篩選「沒有在鼎新」的列，並以與前端相同的方式呼叫 `update_balance_status.asp` 更新狀態（每筆預設間隔 5 秒）。

## 本機執行

```bash
cd /Users/jim/Documents/CronJob
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# 編輯 .env：至少填 ERP_LIST_URL；若站台需要登入，填 ERP_COOKIE

python scripts/erp_update_not_in_dingxin.py
```

## GitHub Actions

工作流程：`.github/workflows/erp-balance-update.yml`

- 觸發：每 30 分鐘一次
- Gate：`scripts/should_run.py`（以 Asia/Taipei，且使用台灣政府行政行事曆；僅於 08:30～17:30 且非放假日執行）
- Variables：
  - `ERP_LIST_URL`：必填（列表頁 URL）
- Secrets：
  - `ERP_COOKIE`：若站台/環境需要登入才可 POST，請填入（否則可不設）
  - `ERP_DELAY_SEC`：選填，預設 5

