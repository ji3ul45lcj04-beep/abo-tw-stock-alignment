from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from should_run import is_fresh, parse_generated, target_hour_for


TAIPEI = ZoneInfo("Asia/Taipei")


class TargetHourTests(unittest.TestCase):
    def test_before_morning_window_skips(self) -> None:
        self.assertIsNone(target_hour_for(datetime(2026, 11, 19, 5, 59, tzinfo=TAIPEI)))

    def test_morning_window(self) -> None:
        self.assertEqual(target_hour_for(datetime(2026, 11, 19, 6, 0, tzinfo=TAIPEI)), 6)
        self.assertEqual(target_hour_for(datetime(2026, 11, 19, 13, 59, tzinfo=TAIPEI)), 6)

    def test_afternoon_window(self) -> None:
        self.assertEqual(target_hour_for(datetime(2026, 11, 19, 14, 0, tzinfo=TAIPEI)), 14)
        self.assertEqual(target_hour_for(datetime(2026, 11, 19, 23, 59, tzinfo=TAIPEI)), 14)

    def test_parse_generated(self) -> None:
        generated = parse_generated("Generated: 2026-11-24 14:48:00 | Rows: 12")
        self.assertEqual(generated, datetime(2026, 11, 24, 14, 48, tzinfo=TAIPEI))

    def test_freshness_separates_morning_and_afternoon(self) -> None:
        now = datetime(2026, 11, 24, 15, 0, tzinfo=TAIPEI)
        morning = datetime(2026, 11, 24, 7, 15, tzinfo=TAIPEI)
        afternoon = datetime(2026, 11, 24, 14, 45, tzinfo=TAIPEI)
        self.assertTrue(is_fresh(morning, now, 6))
        self.assertFalse(is_fresh(morning, now, 14))
        self.assertTrue(is_fresh(afternoon, now, 14))

    def test_previous_day_is_stale(self) -> None:
        now = datetime(2026, 11, 24, 7, 0, tzinfo=TAIPEI)
        previous = datetime(2026, 11, 23, 14, 45, tzinfo=TAIPEI)
        self.assertFalse(is_fresh(previous, now, 6))


if __name__ == "__main__":
    unittest.main()
