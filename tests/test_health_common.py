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
    parse_log_window,
    parse_tobim_server_window,
    subject_from_items,
)

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
        self.assertEqual(server.status, "degraded")
        self.assertIsNotNone(server.detail_html)

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
