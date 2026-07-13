"""health_common：ToBim 排程與環景 Server 分離評估。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from health_common import (  # noqa: E402
    TOBIM_SERVER_LABEL,
    collect_all_jobs,
    evaluate_job,
    parse_erp_business_window,
    parse_log_window,
    parse_tobim_schedule_window,
    parse_tobim_server_window,
    render_telegram_message,
    should_send_telegram,
    subject_from_items,
)
from health_common import MONITORED_JOBS  # noqa: E402

TZ = ZoneInfo("Asia/Taipei")


class TestTobimServerParsing(unittest.TestCase):
    def setUp(self) -> None:
        self.since = datetime(2026, 7, 10, 15, 0, tzinfo=TZ)
        self.until = datetime(2026, 7, 11, 10, 0, tzinfo=TZ)

    def test_parse_copy_failures_and_examples(self) -> None:
        log = """\
2026-07-11 00:00:01 [START] ToBim (docker py3.12)
掃描完成：略過 350，待處理 80，本輪 COPY 10，延後 70
COPY  105/三民路  X:\\ToBim\\202606\\105\\三民路  jpg=N txt=N csv=Y files=3
FAIL  105/三民路  CSV 檔案中沒有有效的圖片檔名資料
COPY  105/五常街281巷  X:\\path  jpg=N txt=N csv=Y files=3FAIL  105/八德路二段346巷  複製後驗證失敗（jpg=0 txt=N）

結果：成功 0，失敗 10，略過 350
2026-07-11 00:05:00 [OK] ToBim
"""
        path = Path(self._create_temp_log(log))
        stats = parse_tobim_server_window([path], self.since, self.until)
        self.assertEqual(stats.copy_ok, 0)
        self.assertEqual(stats.copy_fail, 10)
        self.assertEqual(stats.copy_skipped, 350)
        self.assertEqual(stats.csv_fail, 1)
        self.assertGreaterEqual(stats.source_missing, 1)
        self.assertEqual(stats.pending, 80)
        self.assertTrue(any("三民路" in ex for ex in stats.examples))

    def test_schedule_ok_when_copy_fails(self) -> None:
        log = """\
2026-07-11 02:00:01 [START] ToBim (docker py3.12)
結果：成功 0，失敗 10，略過 350
2026-07-11 02:05:00 [OK] ToBim
"""
        path = Path(self._create_temp_log(log))
        stats = parse_log_window(path, self.since, self.until)
        self.assertEqual(stats.starts, 1)
        self.assertEqual(stats.oks, 1)
        self.assertEqual(stats.tracebacks, 0)

    def test_collect_all_jobs_splits_tobim_rows(self) -> None:
        log = """\
2026-07-11 04:00:01 [START] ToBim (docker py3.12)
FAIL  105/三民路  CSV 檔案中沒有有效的圖片檔名資料
結果：成功 0，失敗 1，略過 350
2026-07-11 04:01:00 [OK] ToBim
"""
        log_dir = Path(self._create_temp_dir())
        (log_dir / "tobim_2026-07-11.log").write_text(log, encoding="utf-8")
        now = datetime(2026, 7, 11, 10, 5, tzinfo=TZ)
        items = collect_all_jobs(
            since=self.since,
            until=self.until,
            now=now,
            respect_schedule=False,
            log_dir_path=log_dir,
        )
        labels = [item.label for item in items]
        self.assertEqual(
            labels,
            ["ERP 借貸平衡", "ToBim 排程", TOBIM_SERVER_LABEL],
        )
        schedule = items[1]
        server = items[2]
        self.assertEqual(schedule.status, "ok")
        self.assertIn("跑完 1/1 次", schedule.execution_summary or "")
        self.assertEqual(server.status, "degraded")
        self.assertIsNotNone(server.detail_html)

    def test_erp_and_tobim_execution_summaries(self) -> None:
        since = datetime(2026, 7, 13, 10, 0, tzinfo=TZ)
        until = datetime(2026, 7, 13, 12, 0, tzinfo=TZ)
        log_dir = Path(self._create_temp_dir())
        (log_dir / "erp_2026-07-13.log").write_text(
            "2026-07-13 11:00:01 [START] ERP\n"
            "符合「沒有在鼎新」且可更新之表單數: 2\n"
            "[1/2] A001 -> OK msg\n"
            "[2/2] A002 -> FAIL msg\n"
            "2026-07-13 11:00:30 [OK] ERP\n",
            encoding="utf-8",
        )
        (log_dir / "tobim_2026-07-13.log").write_text(
            "2026-07-13 11:44:02 [START] ToBim (docker py3.12)\n"
            "now=2026-07-13T11:44:33+08:00 mode=day decision=RUN (workday, business_hours)\n"
            "掃描完成：略過 362，待處理 178，本輪 COPY 10，延後 168\n"
            "結果：成功 9，失敗 1，略過 362\n"
            "2026-07-13 11:55:00 [OK] ToBim\n",
            encoding="utf-8",
        )
        erp_path = log_dir / "erp_2026-07-13.log"
        tobim_path = log_dir / "tobim_2026-07-13.log"
        erp_biz = parse_erp_business_window([erp_path], since, until)
        self.assertEqual(erp_biz.forms_count, 2)
        self.assertEqual(erp_biz.update_ok, 1)
        self.assertEqual(erp_biz.update_fail, 1)
        sched = parse_tobim_schedule_window([tobim_path], since, until)
        self.assertEqual(sched.copy_ok, 9)
        self.assertEqual(sched.copy_fail, 1)
        self.assertEqual(sched.pending, 178)

    def test_telegram_always_reports_job_results(self) -> None:
        items = collect_all_jobs(
            since=self.since,
            until=self.until,
            now=datetime(2026, 7, 11, 10, 5, tzinfo=TZ),
            respect_schedule=False,
            log_dir_path=Path(self._create_temp_dir()),
        )
        self.assertTrue(should_send_telegram(items))
        message = render_telegram_message(
            title="健康檢查 10:05",
            period_label="測試區間",
            generated_at="2026-07-11 10:05",
            host_label="NAS",
            items=items,
        )
        self.assertIn("ERP 借貸平衡", message)
        self.assertIn("ToBim 排程", message)

    def test_tobim_grace_covers_long_copy_run(self) -> None:
        """12:00 START、12:05 檢查時仍在複製，不應標異常。"""
        log = """\
2026-07-13 12:00:01 [START] ToBim (docker py3.12)
掃描完成：略過 385，待處理 165，本輪 COPY 10，延後 155
COPY  105/八德路三段74巷  X:\\path  jpg=N txt=N csv=Y files=3
OK    105/八德路三段74巷  images=420 progress=422 verified
"""
        log_dir = Path(self._create_temp_dir())
        path = log_dir / "tobim_2026-07-13.log"
        path.write_text(log, encoding="utf-8")
        since = datetime(2026, 7, 13, 10, 5, tzinfo=TZ)
        until = datetime(2026, 7, 13, 12, 5, tzinfo=TZ)
        now = datetime(2026, 7, 13, 12, 5, tzinfo=TZ)
        stats = parse_log_window(path, since, until)
        spec = MONITORED_JOBS[1]
        health = evaluate_job(
            spec,
            stats,
            [path],
            since=since,
            until=until,
            now=now,
            respect_schedule=False,
        )
        self.assertEqual(health.status, "ok")
        self.assertIn("執行中", health.status_note)

    def test_subject_uses_short_labels(self) -> None:
        log_dir = Path(self._create_temp_dir())
        (log_dir / "tobim_2026-07-11.log").write_text(
            "2026-07-11 04:00:01 [START] ToBim\n"
            "結果：成功 0，失敗 1，略過 1\n"
            "2026-07-11 04:01:00 [OK] ToBim\n",
            encoding="utf-8",
        )
        now = datetime(2026, 7, 11, 10, 5, tzinfo=TZ)
        items = collect_all_jobs(
            since=self.since,
            until=self.until,
            now=now,
            respect_schedule=False,
            log_dir_path=log_dir,
        )
        subject = subject_from_items("早報 10:05", items)
        self.assertIn("ToBim 排程", subject)
        self.assertIn("環景 Server", subject)

    def _create_temp_log(self, content: str) -> str:
        import tempfile

        handle = tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False, encoding="utf-8"
        )
        handle.write(content)
        handle.close()
        self.addCleanup(Path(handle.name).unlink, missing_ok=True)
        return handle.name

    def _create_temp_dir(self) -> str:
        import tempfile

        return tempfile.mkdtemp()


if __name__ == "__main__":
    unittest.main()
