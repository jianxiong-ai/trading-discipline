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

    def test_bottom_breakout_transition_uses_next_valid_resistance(self):
        transition = tech(
            support=53.68, resistance=57.18, vwap=57.52, volume_ratio=1.22,
            close=58.70, previous=58.00, open_=58.10, last_low=57.20,
            next_resistance=86.39, atr14=4.7371,
            rsi14=46.78, previous_rsi14=36.78, rsi_min_5=17.82,
            ma20_slope_5d=-0.1191, range_position_60=0.1657,
            recent_high_60=124.888, recent_low_60=46.0,
        )
        diagnostics = {}
        signals = evaluate_position(
            watchlist_position(symbol="600487.SH", name="亨通光电"),
            quote(58.74, symbol="600487.SH", name="亨通光电"),
            transition, "10:15", 0.01, 0.01, date(2026, 8, 7),
            {},
            {"max_loss_ratio": 0.25, "warning_ratio": 0.20, "max_single_position_ratio": 0.30},
            set(), 100000, 0.0, 190000, None, None,
            {"minimum_strong_confirmations": 2, "minimum_ma20_slope_5d": 0.0},
            0, equity_evidence(1), {}, diagnostics,
            {"watchlist_initial_weight": 0.05, "watchlist_entry_risk_weight": 0.0035},
            {}, True, {},
            {
                "minimum_strong_confirmations": 2,
                "minimum_expected_spread_ratio": 0.05,
                "minimum_reward_risk": 2.0,
                "maximum_target_distance_ratio": 0.50,
                "maximum_target_atr_multiple": 8.0,
            },
        )
        self.assertEqual([signal.code for signal in signals], ["WATCH_ENTRY"])
        self.assertEqual(signals[0].details["entry_setup"], "bottom_breakout_transition")
        self.assertEqual(signals[0].details["target"], 86.39)
        self.assertEqual(signals[0].shares, 100)
        self.assertGreater(signals[0].details["reward_risk"], 4.0)
        self.assertTrue(
            diagnostics["checks"]["watchlist_entry"]["target_scale_sane"]["passed"]
        )

    def test_transition_rejects_implausibly_distant_target(self):
        transition = tech(
            support=53.68, resistance=57.18, vwap=57.52, volume_ratio=1.22,
            close=58.70, previous=58.00, open_=58.10, last_low=57.20,
            next_resistance=100.0, atr14=4.7371,
            rsi14=46.78, previous_rsi14=36.78, rsi_min_5=17.82,
            ma20_slope_5d=-0.1191, range_position_60=0.1657,
            recent_high_60=124.888, recent_low_60=46.0,
        )
        diagnostics = {}
        signals = evaluate_position(
            watchlist_position(), quote(58.74), transition, "10:15", 0.01, 0.01,
            date(2026, 8, 7), {},
            {"max_loss_ratio": 0.25, "warning_ratio": 0.20, "max_single_position_ratio": 0.30},
            set(), 100000, 0.0, 190000, None, None,
            {"minimum_strong_confirmations": 2, "minimum_ma20_slope_5d": 0.0},
            0, equity_evidence(1), {}, diagnostics,
            {"watchlist_initial_weight": 0.05, "watchlist_entry_risk_weight": 0.0035},
            {}, True, {},
            {
                "minimum_strong_confirmations": 2,
                "minimum_expected_spread_ratio": 0.05,
                "minimum_reward_risk": 2.0,
                "maximum_target_distance_ratio": 0.50,
                "maximum_target_atr_multiple": 8.0,
            },
        )
        self.assertFalse(any(signal.code == "WATCH_ENTRY" for signal in signals))
        self.assertFalse(
            diagnostics["checks"]["watchlist_entry"]["target_scale_sane"]["passed"]
        )

    def test_near_entry_reminder_is_non_actionable_and_groups_blockers(self):
        near = tech(
            support=51.03, resistance=55.30, vwap=54.83, volume_ratio=2.22,
            close=56.40, previous=55.50, open_=55.80, last_low=55.20,
            next_resistance=58.18, atr14=4.975,
            rsi14=36.78, previous_rsi14=25.0, rsi_min_5=17.82,
            ma20_slope_5d=-0.1318, range_position_60=0.1376,
            recent_high_60=124.888, recent_low_60=46.0,
        )
        signals = evaluate_position(
            watchlist_position(), quote(56.51), near, "10:15", 0.01, 0.01,
            date(2026, 8, 6), {},
            {"max_loss_ratio": 0.25, "warning_ratio": 0.20, "max_single_position_ratio": 0.30},
            set(), 100000, 0.0, 180000, None, None,
            {"minimum_strong_confirmations": 2, "minimum_ma20_slope_5d": 0.0},
            0, equity_evidence(1), {}, {},
            {"watchlist_initial_weight": 0.05, "watchlist_entry_risk_weight": 0.0035},
            {}, True, {},
            {
                "minimum_strong_confirmations": 2,
                "minimum_expected_spread_ratio": 0.05,
                "minimum_reward_risk": 2.0,
                "maximum_target_distance_ratio": 0.50,
                "maximum_target_atr_multiple": 8.0,
                "notify_near_entry": True,
                "near_entry_max_blocker_groups": 2,
            },
        )
        self.assertEqual([signal.code for signal in signals], ["WATCH_NEAR_ENTRY"])
        self.assertEqual(signals[0].shares, 0)
        self.assertTrue(signals[0].details["informational_only"])
        self.assertIn("暂不建仓", signals[0].action)
        self.assertIn("不足100股", signals[0].reason)

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
        self.assertFalse(any(signal.code == "WATCH_ENTRY" for signal in signals))
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
