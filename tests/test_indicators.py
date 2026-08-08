from datetime import datetime, timedelta
import unittest
from zoneinfo import ZoneInfo

from astock_bot.indicators import compute_technicals
from astock_bot.models import Bar


TZ = ZoneInfo("Asia/Shanghai")


def bar(stamp, close, volume=100):
    return Bar(stamp, close - 0.1, close + 0.1, close - 0.2, close, volume, close * volume * 100)


class IndicatorTests(unittest.TestCase):
    def test_uses_complete_15m_and_same_time_historical_volume(self):
        daily = []
        start = datetime(2026, 6, 1, tzinfo=TZ)
        for index in range(45):
            daily.append(bar(start + timedelta(days=index), 40 + index * 0.01, 1000))
        intraday = []
        for day in (20, 21, 22, 23):
            for minute in (5, 10, 15):
                intraday.append(bar(datetime(2026, 7, day, 10, minute, tzinfo=TZ), 40, 100))
        for minute in (5, 10, 15):
            intraday.append(bar(datetime(2026, 7, 24, 10, minute, tzinfo=TZ), 40.1, 200))
        result = compute_technicals(daily, intraday, 40.1, datetime(2026, 7, 24, 10, 15, 30, tzinfo=TZ))
        self.assertTrue(result.complete_15m)
        self.assertEqual(result.last_15m_close, 40.1)
        self.assertEqual(result.volume_baseline_samples, 4)
        self.assertAlmostEqual(result.volume_ratio, 2.0)
        self.assertEqual(result.vwap_quality, "approximate_bar_close")

    def test_does_not_use_future_partial_bar(self):
        daily = []
        start = datetime(2026, 6, 1, tzinfo=TZ)
        for index in range(45):
            daily.append(bar(start + timedelta(days=index), 40, 1000))
        intraday = [
            bar(datetime(2026, 7, 24, 10, minute, tzinfo=TZ), 40, 100)
            for minute in (5, 10, 15, 20)
        ]
        result = compute_technicals(daily, intraday, 40, datetime(2026, 7, 24, 10, 17, tzinfo=TZ))
        self.assertEqual(result.last_15m_close, 40)

    def test_stage_and_risk_reward_indicators_are_computed_from_completed_daily_bars(self):
        daily = []
        start = datetime(2026, 2, 1, tzinfo=TZ)
        for index in range(90):
            close = 35 + index * 0.08 + (0.4 if index % 4 == 0 else 0)
            daily.append(bar(start + timedelta(days=index), close, 1000))
        intraday = []
        for day in (20, 21, 22, 23):
            for minute in (5, 10, 15):
                intraday.append(bar(datetime(2026, 5, day, 10, minute, tzinfo=TZ), 41, 100))
        for minute in (5, 10, 15):
            intraday.append(bar(datetime(2026, 5, 24, 10, minute, tzinfo=TZ), 41.2, 120))
        result = compute_technicals(
            daily,
            intraday,
            41.2,
            datetime(2026, 5, 24, 10, 15, 30, tzinfo=TZ),
        )
        self.assertIsNotNone(result.atr14)
        self.assertIsNotNone(result.rsi14)
        self.assertIsNotNone(result.previous_rsi14)
        self.assertIsNotNone(result.ma20_slope_5d)
        self.assertIsNotNone(result.range_position_60)
        self.assertIsNotNone(result.recent_high_60)
        self.assertIsNotNone(result.recent_low_60)
        self.assertGreater(result.recent_high_60, result.recent_low_60)
        self.assertAlmostEqual(result.last_15m_high, 41.3)
        self.assertAlmostEqual(result.last_15m_low, 41.0)


if __name__ == "__main__":
    unittest.main()
