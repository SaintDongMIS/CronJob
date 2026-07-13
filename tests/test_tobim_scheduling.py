"""should_run strict gate 與 ToBim 每輪上限。"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from should_run import main as should_run_main  # noqa: E402
from tobim_copy_images_gps import _int_env  # noqa: E402


class TestShouldRunStrict(unittest.TestCase):
    def test_strict_skip_exits_nonzero_offhours_during_day(self) -> None:
        fixed = __import__("datetime").datetime(
            2026, 6, 22, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Taipei")
        )
        with (
            mock.patch.dict(os.environ, {"SHOULD_RUN_MODE": "offhours", "SHOULD_RUN_STRICT": "1"}),
            mock.patch("should_run.datetime") as dt_mod,
            mock.patch("should_run.TaiwanCalendar") as cal_cls,
        ):
            dt_mod.now.return_value = fixed
            cal_cls.return_value.is_holiday.return_value = False
            self.assertEqual(should_run_main(), 1)

    def test_strict_day_mode_runs_during_business_hours(self) -> None:
        fixed = __import__("datetime").datetime(
            2026, 6, 22, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Taipei")
        )
        with (
            mock.patch.dict(os.environ, {"SHOULD_RUN_MODE": "day", "SHOULD_RUN_STRICT": "1"}),
            mock.patch("should_run.datetime") as dt_mod,
            mock.patch("should_run.TaiwanCalendar") as cal_cls,
        ):
            dt_mod.now.return_value = fixed
            cal_cls.return_value.is_holiday.return_value = False
            self.assertEqual(should_run_main(), 0)

    def test_strict_not_set_always_exits_zero(self) -> None:
        fixed = __import__("datetime").datetime(
            2026, 6, 22, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Taipei")
        )
        with (
            mock.patch.dict(os.environ, {"SHOULD_RUN_MODE": "offhours"}, clear=False),
            mock.patch("should_run.datetime") as dt_mod,
            mock.patch("should_run.TaiwanCalendar") as cal_cls,
        ):
            os.environ.pop("SHOULD_RUN_STRICT", None)
            dt_mod.now.return_value = fixed
            cal_cls.return_value.is_holiday.return_value = False
            self.assertEqual(should_run_main(), 0)


class TestIntEnv(unittest.TestCase):
    def test_max_copy_zero_means_unlimited_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_int_env("TOBIM_MAX_COPY_PER_RUN", default=10), 10)
        with mock.patch.dict(os.environ, {"TOBIM_MAX_COPY_PER_RUN": "0"}):
            self.assertEqual(_int_env("TOBIM_MAX_COPY_PER_RUN", default=10), 0)


if __name__ == "__main__":
    unittest.main()
