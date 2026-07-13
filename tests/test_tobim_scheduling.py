"""should_run strict gate、weekday 模式與 ToBim 每輪上限。"""

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
from tobim_copy_images_gps import _budget_exhausted, _int_env  # noqa: E402


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


    def test_strict_weekday_mode_runs_at_night_on_workday(self) -> None:
        fixed = __import__("datetime").datetime(
            2026, 7, 14, 22, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Taipei")
        )
        with (
            mock.patch.dict(
                os.environ, {"SHOULD_RUN_MODE": "weekday", "SHOULD_RUN_STRICT": "1"}
            ),
            mock.patch("should_run.datetime") as dt_mod,
            mock.patch("should_run.TaiwanCalendar") as cal_cls,
        ):
            dt_mod.now.return_value = fixed
            cal_cls.return_value.is_holiday.return_value = False
            self.assertEqual(should_run_main(), 0)

    def test_strict_weekday_mode_skips_on_holiday(self) -> None:
        fixed = __import__("datetime").datetime(
            2026, 7, 14, 22, 0, tzinfo=__import__("zoneinfo").ZoneInfo("Asia/Taipei")
        )
        with (
            mock.patch.dict(
                os.environ, {"SHOULD_RUN_MODE": "weekday", "SHOULD_RUN_STRICT": "1"}
            ),
            mock.patch("should_run.datetime") as dt_mod,
            mock.patch("should_run.TaiwanCalendar") as cal_cls,
        ):
            dt_mod.now.return_value = fixed
            cal_cls.return_value.is_holiday.return_value = True
            self.assertEqual(should_run_main(), 1)


class TestIntEnv(unittest.TestCase):
    def test_max_copy_zero_is_default_unlimited(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_int_env("TOBIM_MAX_COPY_PER_RUN", default=0), 0)
        with mock.patch.dict(os.environ, {"TOBIM_MAX_COPY_PER_RUN": "10"}):
            self.assertEqual(_int_env("TOBIM_MAX_COPY_PER_RUN", default=0), 10)


class TestRunBudget(unittest.TestCase):
    def test_budget_exhausted_after_elapsed(self) -> None:
        started = 1000.0
        with mock.patch("tobim_copy_images_gps.time") as time_mod:
            time_mod.monotonic.return_value = 1000.0
            self.assertFalse(_budget_exhausted(started, 60.0))
            time_mod.monotonic.return_value = 1059.9
            self.assertFalse(_budget_exhausted(started, 60.0))
            time_mod.monotonic.return_value = 1060.0
            self.assertTrue(_budget_exhausted(started, 60.0))

    def test_budget_zero_never_exhausted(self) -> None:
        with mock.patch("tobim_copy_images_gps.time") as time_mod:
            time_mod.monotonic.return_value = 999999.0
            self.assertFalse(_budget_exhausted(0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
