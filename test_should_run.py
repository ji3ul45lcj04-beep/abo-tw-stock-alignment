from datetime import datetime
import unittest
from zoneinfo import ZoneInfo

from should_run import target_hour_for


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


if __name__ == "__main__":
    unittest.main()
