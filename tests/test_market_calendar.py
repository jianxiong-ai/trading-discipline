import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from astock_bot.market_calendar import is_market_day, parse_official_closures


class MarketCalendarTests(unittest.TestCase):
    def test_official_closure_ranges_and_extra_weekend_dates_are_expanded(self):
        html = """
        <h2>2026年休市安排</h2>
        <p>元旦：1月1日（星期四）至1月3日（星期六）休市，1月5日（星期一）起照常开市。另外，1月4日（星期日）为周末休市。</p>
        <p>春节：2月15日（星期日）至2月23日（星期一）休市，2月24日（星期二）起照常开市。另外，2月14日（星期六）为周末休市。</p>
        <h2>相关公告</h2>
        """
        dates = parse_official_closures(html)
        self.assertIn("2026-01-01", dates)
        self.assertIn("2026-01-03", dates)
        self.assertIn("2026-01-04", dates)
        self.assertIn("2026-02-23", dates)
        self.assertNotIn("2026-01-05", dates)

    def test_only_top_level_holidays_are_manual_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                """holidays:\n  - 2026-01-01\ncorporate_events:\n  - ex_date: 2026-08-09\n""",
                encoding="utf-8",
            )
            # A date in another config section must not be treated as a
            # holiday by the host scheduler.
            with patch("astock_bot.market_calendar._fetch", return_value="<h2>2026年休市安排</h2>"):
                self.assertTrue(is_market_day(path, date(2026, 8, 10)))


if __name__ == "__main__":
    unittest.main()
