from datetime import date
import unittest

from astock_bot.config import _validate_position
from astock_bot.models import Position, SatellitePosition
from astock_bot.strategy import evaluate_position

from test_strategy import equity_evidence, quote, tech


def watchlist_position(**overrides):
    values = {
        "symbol": "600362.SH",
        "name": "江西铜业观察",
        "main_shares": 0,
        "economic_basis": 0,
        "sector": "copper",
        "satellite_limit": 300,
        "main_adjustment_shares": 500,
        "peers": (),
        "satellite": SatellitePosition(),
        "role": "watchlist",
    }
    values.update(overrides)
    return Position(**values)


class WatchlistTests(unittest.TestCase):
    def test_watchlist_bottom_confirmation_generates_starter_entry(self):
        bottom = tech(
            support=39.8, resistance=43, vwap=39.9, volume_ratio=1.2,
            close=40.05, previous=39.7, open_=39.8, last_low=39.7,
            atr14=0.5, rsi14=40, previous_rsi14=35, rsi_min_5=30,
            rsi_max_5=45, ma20_slope_5d=-0.005, range_position_60=0.32,
            recent_high_60=45, recent_low_60=39,
        )
        diagnostics = {}
        signals = evaluate_position(
            watchlist_position(), quote(40), bottom, "10:15", 0.01, 0.01,
            date(2026, 7, 30), {},
            {"max_loss_ratio": 0.25, "warning_ratio": 0.20, "max_single_position_ratio": 0.30},
            set(), 100000, 0.0, 180000, None, None,
            {"minimum_strong_confirmations": 2}, 0, equity_evidence(1), {}, diagnostics,
            {"watchlist_initial_weight": 0.05, "watchlist_entry_risk_weight": 0.0035},
            {}, True, {},
            {"minimum_expected_spread_ratio": 0.05, "minimum_reward_risk": 2.0},
        )
        self.assertEqual(signals[0].code, "WATCH_ENTRY")
        self.assertEqual(signals[0].details["entry_setup"], "bottom_confirmed")
        self.assertEqual(diagnostics["stage"]["label"], "BOTTOM_CONFIRMED")

    def test_watchlist_entry_is_also_evaluated_at_1315(self):
        bottom = tech(
            support=39.8, resistance=43, vwap=39.9, volume_ratio=1.2,
            close=40.05, previous=39.7, open_=39.8, last_low=39.7,
            atr14=0.5, rsi14=40, previous_rsi14=35, rsi_min_5=30,
            rsi_max_5=45, ma20_slope_5d=-0.005, range_position_60=0.32,
            recent_high_60=45, recent_low_60=39,
        )
        signals = evaluate_position(
            watchlist_position(), quote(40), bottom, "13:15", 0.01, 0.01,
            date(2026, 7, 30), {},
            {"max_loss_ratio": 0.25, "warning_ratio": 0.20, "max_single_position_ratio": 0.30},
            set(), 100000, 0.0, 180000, None, None,
            {"minimum_strong_confirmations": 2}, 0, equity_evidence(1), {}, {},
            {"watchlist_initial_weight": 0.05, "watchlist_entry_risk_weight": 0.0035},
            {}, True, {},
            {
                "allowed_nodes": ["10:15", "13:15", "14:15"],
                "minimum_strong_confirmations": 2,
                "minimum_expected_spread_ratio": 0.05,
                "minimum_reward_risk": 2.0,
            },
        )
        self.assertEqual([signal.code for signal in signals], ["WATCH_ENTRY"])

    def test_watchlist_breakout_generates_distinct_starter_entry(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=1.5,
            close=41.2, previous=40.8, open_=40.9, last_low=40.95,
            next_resistance=44,
        )
        diagnostics = {}
        signals = evaluate_position(
            watchlist_position(), quote(41.2), breakout, "14:15", 0.01, 0.01,
            date(2026, 7, 30), {},
            {"max_loss_ratio": 0.25, "warning_ratio": 0.20, "max_single_position_ratio": 0.30},
            set(), 100000, 0.0, 180000, None, None,
            {"minimum_strong_confirmations": 2}, 0, equity_evidence(1), {}, diagnostics,
            {
                "watchlist_initial_weight": 0.05,
                "watchlist_entry_risk_weight": 0.0035,
                "max_single_position_weight": 0.30,
            },
            {}, True, {},
            {
                "enabled": True,
                "allowed_nodes": ["10:15", "14:15"],
                "minimum_strong_confirmations": 2,
                "minimum_expected_spread_ratio": 0.05,
                "minimum_reward_risk": 2.0,
            },
        )
        self.assertEqual([signal.code for signal in signals], ["WATCH_ENTRY"])
        self.assertEqual(signals[0].action, "首次建立主仓起始档")
        self.assertEqual(signals[0].shares, 200)
        self.assertEqual(signals[0].details["entry_setup"], "breakout_confirmed")
        self.assertAlmostEqual(signals[0].details["planned_nav_ratio"], 0.0458, places=4)

    def test_watchlist_uses_stricter_spread_than_existing_main_add(self):
        narrow = tech(
            support=39, resistance=41, vwap=40, volume_ratio=1.5,
            close=41.2, previous=40.8, open_=40.9, last_low=40.95,
            next_resistance=43,
        )
        diagnostics = {}
        signals = evaluate_position(
            watchlist_position(), quote(41.2), narrow, "14:15", 0.01, 0.01,
            date(2026, 7, 30), {},
            {"max_loss_ratio": 0.25, "warning_ratio": 0.20, "max_single_position_ratio": 0.30},
            set(), 100000, 0.0, 180000, None, None,
            {"minimum_strong_confirmations": 2}, 0, equity_evidence(1), {}, diagnostics,
            {"watchlist_initial_weight": 0.05, "watchlist_entry_risk_weight": 0.0035},
            {}, True, {},
            {"minimum_expected_spread_ratio": 0.05, "minimum_reward_risk": 2.0},
        )
        self.assertEqual(signals, [])
        self.assertFalse(
            diagnostics["checks"]["watchlist_entry"]["minimum_expected_spread"]["passed"]
        )

    def test_watchlist_never_falls_through_to_satellite_entry(self):
        signals = evaluate_position(
            watchlist_position(), quote(40), tech(), "10:15", 0.01, 0.005,
            date(2026, 7, 30), {"max_active_positions": 1},
            {"max_loss_ratio": 0.25, "warning_ratio": 0.20}, set(),
            100000, 0.0, 180000, None, None, {}, 0, equity_evidence(1), {}, {},
            {"watchlist_initial_weight": 0.05}, {}, True, {}, {},
        )
        self.assertEqual(signals, [])

    def test_watchlist_must_be_empty_and_cannot_enable_migration(self):
        with self.assertRaisesRegex(ValueError, "main_shares=0"):
            _validate_position(watchlist_position(main_shares=100, economic_basis=4000))
        with self.assertRaisesRegex(ValueError, "不得启用存量仓迁移"):
            _validate_position(watchlist_position(migration={
                "enabled": True,
                "initial_ceiling_weight": 0.1,
                "risk_principal_ceiling": 10000,
            }))


if __name__ == "__main__":
    unittest.main()
