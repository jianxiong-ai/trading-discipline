from datetime import date, datetime
import unittest
from zoneinfo import ZoneInfo

from astock_bot.models import EquityEvidence, Position, Quote, SatellitePosition, Technicals
from astock_bot.strategy import (
    _entry_risk_budget,
    _planned_main_entry_shares,
    _planned_satellite_entry_shares,
    _strong_confirmation_count,
    evaluate_position,
    trading_days_held,
)


TZ = ZoneInfo("Asia/Shanghai")


def tech(
    support=39.8,
    resistance=42.0,
    vwap=39.9,
    volume_ratio=1.4,
    close=40.05,
    previous=39.9,
    open_=39.9,
    last_low=None,
    previous_low=None,
    next_resistance=44.0,
    atr14=0.5,
    rsi14=50.0,
    previous_rsi14=49.0,
    rsi_min_5=40.0,
    rsi_max_5=60.0,
    ma20_slope_5d=0.01,
    range_position_60=0.50,
    recent_high_60=45.0,
    recent_low_60=35.0,
):
    return Technicals(
        ma5=40,
        ma10=40,
        ma20=40,
        support=support,
        resistance=resistance,
        vwap=vwap,
        volume_ratio=volume_ratio,
        last_15m_close=close,
        previous_15m_close=previous,
        last_15m_open=open_,
        complete_15m=True,
        vwap_quality="approximate_bar_close",
        volume_baseline_samples=5,
        last_15m_high=max(close, open_) + 0.1,
        last_15m_low=last_low if last_low is not None else support - 0.05,
        previous_15m_open=previous - 0.05,
        previous_15m_high=previous + 0.1,
        previous_15m_low=previous_low if previous_low is not None else support - 0.10,
        atr14=atr14,
        rsi14=rsi14,
        previous_rsi14=previous_rsi14,
        rsi_min_5=rsi_min_5,
        rsi_max_5=rsi_max_5,
        ma20_slope_5d=ma20_slope_5d,
        range_position_60=range_position_60,
        recent_high_60=recent_high_60,
        recent_low_60=recent_low_60,
        next_resistance=next_resistance,
    )


def quote(price=40.0, symbol="600362.SH", name="江西铜业"):
    return Quote(symbol, name, datetime(2026, 7, 28, 10, 15, tzinfo=TZ), price, 40, 39.8, price, 39.5, 1, 1)


def position(satellite=None, main_shares=1800):
    return Position(
        "600362.SH",
        "江西铜业",
        main_shares,
        87720,
        "copper",
        300,
        300,
        (),
        satellite or SatellitePosition(),
    )


def equity_evidence(
    industry_direction=0,
    announcement_risk="none",
    company_direction=None,
    margin_status="missing",
    margin_signal="missing",
    corporate_action_direction=None,
    corporate_action_strength=0,
    corporate_action_body_status="missing",
):
    return EquityEvidence(
        symbol="600362.SH",
        industry_status="fresh",
        industry_direction=industry_direction,
        announcement_status="fresh",
        announcement_risk=announcement_risk,
        summary="官方产业证据；公告门控通过",
        industry_strength=2 if industry_direction > 0 else 0,
        company_status="fresh" if company_direction is not None else "missing",
        company_direction=company_direction,
        margin_status=margin_status,
        margin_signal=margin_signal,
        corporate_action_status=(
            "fresh" if corporate_action_direction is not None else "none"
        ),
        corporate_action_direction=corporate_action_direction,
        corporate_action_strength=corporate_action_strength,
        corporate_action_stage="plan" if corporate_action_direction is not None else None,
        corporate_action_body_status=corporate_action_body_status,
    )


class StrategyTests(unittest.TestCase):
    def test_verified_corporate_action_counts_once_but_title_candidate_does_not(self):
        verified = equity_evidence(
            industry_direction=1,
            corporate_action_direction=1,
            corporate_action_strength=2,
            corporate_action_body_status="verified",
        )
        self.assertTrue(verified.corporate_action_confirmation)
        self.assertEqual(
            _strong_confirmation_count(-0.01, -0.01, verified, {}),
            2,
        )
        title_only = equity_evidence(
            industry_direction=1,
            corporate_action_direction=0,
            corporate_action_strength=0,
            corporate_action_body_status="unavailable",
        )
        self.assertFalse(title_only.corporate_action_confirmation)
        self.assertEqual(
            _strong_confirmation_count(-0.01, -0.01, title_only, {}),
            1,
        )

    def test_trading_days_held_counts_entry_day(self):
        self.assertEqual(trading_days_held(date(2026, 7, 27), date(2026, 7, 31), set()), 5)

    def test_satellite_entry_is_sized_below_configured_cap(self):
        signals = evaluate_position(
            position(), quote(), tech(), "10:15", 0.01, 0.005, date(2026, 7, 28),
            {"max_cash_fraction_per_trade": 0.50}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 12753.41, 0.20, 180000, None, None, {}, 0, equity_evidence(),
        )
        self.assertEqual([x.code for x in signals], ["SAT_BUY"])
        self.assertEqual(signals[0].shares, 100)
        self.assertEqual(signals[0].details["target"], 42.0)

    def test_satellite_entry_is_evaluated_at_1315(self):
        signals = evaluate_position(
            position(), quote(), tech(), "13:15", 0.01, 0.005, date(2026, 7, 28),
            {"max_cash_fraction_per_trade": 0.50},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 12753.41, 0.20, 180000, None, None, {}, 0, equity_evidence(),
        )
        self.assertEqual([item.code for item in signals], ["SAT_BUY"])

    def test_satellite_entry_respects_post_trade_concentration(self):
        signals = evaluate_position(
            position(), quote(), tech(), "10:15", 0.01, 0.005, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90, "max_single_position_ratio": 0.30},
            set(), 100000, 0.30, 180000, None, None, {}, 0, equity_evidence(),
        )
        self.assertEqual(signals, [])

    def test_migration_satellite_uses_separate_temporary_overlay(self):
        migration = {
            "enabled": True,
            "position_ceiling": 0.40,
            "satellite_overlay_max_weight": 0.035,
            "risk_principal_ceiling": 87720,
        }
        signals = evaluate_position(
            position(), quote(), tech(), "10:15", 0.01, 0.005,
            date(2026, 7, 28),
            {"entry_risk_weight": 0.0025},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.40, 180000, None, None, {}, 0,
            equity_evidence(), {}, {}, {}, migration,
        )
        self.assertEqual([signal.code for signal in signals], ["SAT_BUY"])
        self.assertEqual(signals[0].shares, 100)

        diagnostics = {}
        blocked = evaluate_position(
            position(), quote(), tech(), "10:15", 0.01, 0.005,
            date(2026, 7, 28),
            {"entry_risk_weight": 0.0025},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.43, 180000, None, None, {}, 0,
            equity_evidence(), {}, diagnostics, {}, migration,
        )
        self.assertEqual(blocked, [])
        self.assertAlmostEqual(
            diagnostics["metrics"]["satellite_entry"]["position_cap"], 0.435
        )
        self.assertEqual(diagnostics["metrics"]["satellite_entry"]["sized_shares"], 0)

    def test_migration_satellite_is_blocked_by_pending_reduction_guard(self):
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(), tech(), "10:15", 0.01, 0.005,
            date(2026, 7, 28), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.40, 180000, None, None, {}, 0,
            equity_evidence(), {}, diagnostics, {},
            {
                "enabled": True,
                "position_ceiling": 0.40,
                "satellite_overlay_max_weight": 0.035,
                "risk_principal_ceiling": 87720,
                "satellite_entry_block_reason": "存在待处理减仓信号",
            },
        )
        self.assertEqual(signals, [])
        self.assertFalse(
            diagnostics["checks"]["satellite_entry"]
            ["no_pending_reduction_or_cooldown"]["passed"]
        )

    def test_satellite_entry_requires_complete_15m_and_historical_volume(self):
        incomplete = tech()
        incomplete.complete_15m = False
        self.assertEqual(
            evaluate_position(
                position(), quote(), incomplete, "10:15", 0.01, 0.005, date(2026, 7, 28),
                {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
            ),
            [],
        )
        low_samples = tech()
        low_samples.volume_baseline_samples = 2
        self.assertEqual(
            evaluate_position(
                position(), quote(), low_samples, "10:15", 0.01, 0.005, date(2026, 7, 28),
                {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
            ),
            [],
        )

    def test_main_down_break_separates_satellite_and_main_actions(self):
        sat = SatellitePosition(True, 300, 40, date(2026, 7, 27), 39.8, 42)
        weak = tech(support=39.5, resistance=42, vwap=39.4, volume_ratio=1.5, close=39.0, previous=39.8, open_=39.6)
        signals = evaluate_position(
            position(sat), quote(39.0), weak, "14:15", -0.01, -0.005, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
        )
        self.assertEqual([signal.code for signal in signals], ["DOWN_BREAK"])
        self.assertEqual(signals[0].shares, 400)
        self.assertEqual(signals[0].details["satellite_exit_shares"], 300)

    def test_shallow_single_15m_break_is_filtered_even_with_high_volume(self):
        shallow = tech(
            support=62.41, resistance=63.32, vwap=62.79, volume_ratio=2.79,
            close=62.25, previous=62.50, open_=62.92, atr14=2.11,
        )
        current = Quote(
            "600362.SH", "江西铜业", datetime(2026, 7, 30, 13, 15, tzinfo=TZ),
            62.25, 62.98, 62.96, 63.00, 62.13, 1, 1,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(main_shares=1000), current, shallow, "13:15",
            0.00203, -0.02788, date(2026, 7, 30), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.20, 180000, None, None, {}, 0,
            equity_evidence(0), {}, diagnostics,
        )
        self.assertEqual(signals, [])
        self.assertFalse(
            diagnostics["checks"]["main_reduce"]["break_depth_or_persistence"]["passed"]
        )

    def test_persistent_break_with_one_external_confirmation_stays_medium(self):
        persistent = tech(
            support=62.41, resistance=63.32, vwap=62.79, volume_ratio=2.79,
            close=62.25, previous=62.30, open_=62.50, atr14=2.11,
        )
        current = Quote(
            "600362.SH", "江西铜业", datetime(2026, 7, 30, 13, 30, tzinfo=TZ),
            62.25, 62.98, 62.96, 63.00, 62.13, 1, 1,
        )
        signals = evaluate_position(
            position(main_shares=1000), current, persistent, "13:15",
            0.00203, -0.02788, date(2026, 7, 30), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.20, 180000, None, None,
            {"high_break_minimum_weak_confirmations": 2}, 0, equity_evidence(0),
        )
        self.assertEqual([signal.code for signal in signals], ["DOWN_BREAK"])
        self.assertEqual(signals[0].confidence, "中")
        self.assertEqual(signals[0].shares, 100)
        self.assertIn("市场均值", signals[0].reason)
        self.assertIn("同行未同步走弱", signals[0].reason)

    def test_shallow_persistent_break_cannot_be_high_confidence(self):
        persistent = tech(
            support=62.41, resistance=63.32, vwap=62.79, volume_ratio=2.79,
            close=62.25, previous=62.30, open_=62.50, atr14=2.11,
        )
        current = Quote(
            "600362.SH", "江西铜业", datetime(2026, 7, 30, 13, 30, tzinfo=TZ),
            62.25, 62.98, 62.96, 63.00, 62.13, 1, 1,
        )
        signals = evaluate_position(
            position(main_shares=1000), current, persistent, "13:15",
            -0.01, -0.02788, date(2026, 7, 30), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.20, 180000, None, None,
            {"high_break_minimum_weak_confirmations": 2}, 0, equity_evidence(0),
        )
        self.assertEqual(signals[0].confidence, "中")
        self.assertEqual(signals[0].shares, 100)

    def test_deep_break_with_two_external_confirmations_is_high_confidence(self):
        deep = tech(
            support=62.41, resistance=63.32, vwap=62.79, volume_ratio=2.79,
            close=62.00, previous=62.20, open_=62.50, atr14=2.11,
        )
        current = Quote(
            "600362.SH", "江西铜业", datetime(2026, 7, 30, 13, 30, tzinfo=TZ),
            62.00, 62.98, 62.96, 63.00, 61.90, 1, 1,
        )
        signals = evaluate_position(
            position(main_shares=1000), current, deep, "13:15",
            -0.01, -0.02788, date(2026, 7, 30), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.20, 180000, None, None,
            {"high_break_minimum_weak_confirmations": 2}, 0, equity_evidence(0),
        )
        self.assertEqual(signals[0].confidence, "高")
        self.assertEqual(signals[0].shares, 200)

    def test_shallow_break_with_material_peer_outperformance_is_observation_only(self):
        shallow = tech(
            support=62.35, resistance=62.90, vwap=62.41, volume_ratio=1.56,
            close=62.32, previous=62.33, open_=62.40, atr14=1.8843,
        )
        current = Quote(
            "601336.SH", "新华保险", datetime(2026, 8, 4, 14, 15, tzinfo=TZ),
            62.31, 62.87, 62.50, 62.60, 62.20, 1, 1,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(main_shares=1000), current, shallow, "14:15",
            -0.0234, 0.0273, date(2026, 8, 4), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.20, 180000, None, None,
            {
                "peer_relative_strength_buffer_ratio": 0.01,
                "shallow_relative_strength_observation_only": True,
                "high_break_requires_depth": True,
            },
            0, equity_evidence(-1), {}, diagnostics,
        )
        self.assertEqual(signals, [])
        gate = diagnostics["checks"]["main_reduce"]["shallow_relative_strength_gate"]
        self.assertFalse(gate["passed"])
        self.assertTrue(gate["observed"]["relative_resilient"])

    def test_satellite_stop_does_not_wait_for_peers(self):
        sat = SatellitePosition(True, 100, 40, date(2026, 7, 27), 40, 42)
        broken = tech(support=39, resistance=42, vwap=39.5, volume_ratio=1.0, close=39.3, previous=39.8, open_=39.7)
        signals = evaluate_position(
            position(sat), quote(39.3), broken, "13:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
        )
        self.assertEqual(signals[0].code, "SAT_EXIT")

    def test_satellite_uses_stored_target_not_dynamic_resistance(self):
        sat = SatellitePosition(True, 100, 40, date(2026, 7, 27), 39, 42)
        moved_resistance = tech(support=39, resistance=46, vwap=41, volume_ratio=1.0, close=42, previous=41.8, open_=41.8)
        signals = evaluate_position(
            position(sat), quote(42), moved_resistance, "13:15", 0, 0, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
        )
        self.assertEqual(signals[0].code, "SAT_SELL")
        self.assertEqual(signals[0].key_level, 42)

    def test_expired_satellite_exits_on_tenth_inclusive_day(self):
        sat = SatellitePosition(True, 100, 50, date(2026, 7, 20), 49, 52)
        signals = evaluate_position(
            position(sat), quote(50), tech(support=49, resistance=52, vwap=50, volume_ratio=1.0, close=50),
            "10:15", 0, 0, date(2026, 7, 31), {"max_holding_trading_days": 10},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
        )
        self.assertEqual(signals[0].code, "SAT_EXIT")
        self.assertEqual(signals[0].details["holding_days"], 10)

    def test_main_add_is_disabled_by_default(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=1.5,
            close=41.2, previous=40.8, open_=40.9, last_low=40.95,
        )
        signals = evaluate_position(
            position(), quote(41.2), breakout, "14:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
        )
        self.assertEqual(signals, [])

    def test_main_add_requires_positive_industry_and_clear_announcements(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=1.5,
            close=41.2, previous=40.8, open_=40.9, last_low=40.95,
        )
        common = (
            position(), quote(41.2), breakout, "14:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.10,
            180000, None, None, {"main_add_enabled": True}, 0,
        )
        self.assertEqual(evaluate_position(*common, equity_evidence(0)), [])
        self.assertEqual(evaluate_position(*common, equity_evidence(1, "caution")), [])
        signals = evaluate_position(*common, equity_evidence(1))
        self.assertEqual([signal.code for signal in signals], ["UP_BREAK"])

    def test_main_add_accepts_relaxed_volume_when_breakout_persists(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=0.75,
            close=41.2, previous=41.1, open_=41.0, last_low=41.05,
        )
        signals = evaluate_position(
            position(), quote(41.2), breakout, "14:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.10,
            180000, None, None,
            {
                "main_add_enabled": True,
                "volume_confirmation_ratio": 1.10,
                "breakout_relaxed_volume_ratio": 0.72,
            },
            0, equity_evidence(1),
        )
        self.assertEqual([signal.code for signal in signals], ["UP_BREAK"])

    def test_main_add_rejects_relaxed_volume_without_two_bar_persistence(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=0.75,
            close=41.2, previous=40.8, open_=40.9, last_low=41.15,
        )
        signals = evaluate_position(
            position(), quote(41.2), breakout, "14:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.10,
            180000, None, None,
            {
                "main_add_enabled": True,
                "volume_confirmation_ratio": 1.10,
                "breakout_relaxed_volume_ratio": 0.72,
            },
            0, equity_evidence(1),
        )
        self.assertEqual(signals, [])

    def test_main_add_rejects_volume_below_relaxed_floor_even_with_persistence(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=0.67,
            close=41.2, previous=41.1, open_=41.0, last_low=41.05,
        )
        signals = evaluate_position(
            position(), quote(41.2), breakout, "14:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.10,
            180000, None, None,
            {
                "main_add_enabled": True,
                "volume_confirmation_ratio": 1.10,
                "breakout_relaxed_volume_ratio": 0.72,
            },
            0, equity_evidence(1),
        )
        self.assertEqual(signals, [])

    def test_main_add_is_evaluated_at_1315(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=1.5,
            close=41.2, previous=40.8, open_=40.9, last_low=40.95,
        )
        signals = evaluate_position(
            position(), quote(41.2), breakout, "13:15", 0.01, 0.01,
            date(2026, 7, 28), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.10, 180000, None, None,
            {"main_add_enabled": True}, 0, equity_evidence(1),
        )
        self.assertEqual([signal.code for signal in signals], ["UP_BREAK"])

    def test_satellite_entry_blocks_missing_or_adverse_evidence(self):
        base = (
            position(), quote(), tech(), "10:15", 0.01, 0.005, date(2026, 7, 28),
            {"max_cash_fraction_per_trade": 0.50}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 12753.41, 0.30, 180000, None, None, {}, 0,
        )
        self.assertEqual(evaluate_position(*base, None), [])
        self.assertEqual(evaluate_position(*base, equity_evidence(-1)), [])
        self.assertEqual(evaluate_position(*base, equity_evidence(0, "caution")), [])

    def test_main_down_break_is_checked_at_1315(self):
        weak = tech(
            support=39.5, resistance=42, vwap=39.4, volume_ratio=1.5,
            close=39.0, previous=39.8, open_=39.6,
        )
        signals = evaluate_position(
            position(), quote(39.0), weak, "13:15", -0.01, -0.005, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
        )
        self.assertEqual([signal.code for signal in signals], ["DOWN_BREAK"])

    def test_gap_down_exception_does_not_require_13x_volume(self):
        weak = tech(
            support=39.5, resistance=42, vwap=39.0, volume_ratio=0.8,
            close=38.5, previous=38.8, open_=38.7,
        )
        gap_quote = Quote(
            "600362.SH", "江西铜业", datetime(2026, 7, 28, 10, 15, tzinfo=TZ),
            38.5, 40.0, 38.7, 38.8, 38.2, 1, 1,
        )
        signals = evaluate_position(
            position(), gap_quote, weak, "10:15", -0.01, 0.0, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
            None, None, {"emergency_gap_down_ratio": 0.02}, 0, equity_evidence(0),
        )
        self.assertEqual([signal.code for signal in signals], ["DOWN_BREAK"])
        self.assertTrue(signals[0].details["gap_exception"])

    def test_main_add_requires_room_to_next_resistance_and_reward_risk(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=1.5,
            close=41.2, previous=40.8, open_=40.9, last_low=40.95,
            next_resistance=41.5,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(41.2), breakout, "14:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
            None, None, {"main_add_enabled": True}, 0, equity_evidence(1), {}, diagnostics,
        )
        self.assertEqual(signals, [])
        self.assertFalse(diagnostics["checks"]["main_add"]["minimum_expected_spread"]["passed"])

    def test_main_add_position_size_is_capped_by_risk_budget(self):
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=1.5,
            close=41.2, previous=40.8, open_=40.9, last_low=40.95,
        )
        signals = evaluate_position(
            position(), quote(41.2), breakout, "14:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.10, 180000,
            None, None,
            {"main_add_enabled": True},
            0, equity_evidence(1), {}, {}, {"entry_risk_weight": 0.0004},
        )
        self.assertEqual(signals[0].code, "UP_BREAK")
        self.assertEqual(signals[0].shares, 100)

    def test_flat_holding_must_not_bypass_watchlist_lifecycle(self):
        candidate = Position(
            "600362.SH", "江西铜业", 0, 0, "copper", 300, 500, (),
            SatellitePosition(),
        )
        breakout = tech(
            support=39, resistance=41, vwap=40, volume_ratio=1.5,
            close=41.2, previous=40.8, open_=40.9, last_low=40.95,
        )
        signals = evaluate_position(
            candidate, quote(41.2), breakout, "14:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.25, "warning_ratio": 0.20, "max_single_position_ratio": 0.30},
            set(), 100000, 0.0, 180000, None, None,
            {"main_add_enabled": True}, 0, equity_evidence(1), {}, {},
            {"trend_add_weight": 0.06, "target_main_weight": 0.20, "entry_risk_weight": 0.005},
        )
        self.assertEqual(signals, [])

    def test_satellite_requires_reversal_structure(self):
        no_reversal = tech(
            close=39.9, previous=39.95, open_=39.95,
            last_low=39.85, previous_low=39.75, volume_ratio=1.4,
        )
        signals = evaluate_position(
            position(), quote(40), no_reversal, "10:15", 0.01, 0.005, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 12753.41, 0.20, 180000,
            None, None, {}, 0, equity_evidence(),
        )
        self.assertEqual(signals, [])

    def test_satellite_allows_controlled_contraction_hold(self):
        contraction = tech(
            close=40.05, previous=39.95, open_=39.95,
            last_low=39.82, previous_low=39.75, volume_ratio=0.9,
        )
        signals = evaluate_position(
            position(), quote(40), contraction, "10:15", 0.01, 0.005, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 12753.41, 0.20, 180000,
            None, None, {}, 0, equity_evidence(),
        )
        self.assertEqual(signals[0].code, "SAT_BUY")
        self.assertEqual(signals[0].details["volume_mode"], "contraction_hold")

    def test_bottom_confirmation_can_generate_small_main_reentry(self):
        bottom = tech(
            support=39.8, resistance=43, vwap=39.9, volume_ratio=1.2,
            close=40.05, previous=39.7, open_=39.8, last_low=39.7,
            atr14=0.5, rsi14=40, previous_rsi14=35, rsi_min_5=30,
            rsi_max_5=45, ma20_slope_5d=-0.005, range_position_60=0.32,
            recent_high_60=45, recent_low_60=39,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(40), bottom, "10:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.05, 180000,
            None, None, {"main_add_enabled": True}, 0, equity_evidence(1), {}, diagnostics,
        )
        self.assertEqual(signals[0].code, "STAGE_REENTRY")
        self.assertEqual(diagnostics["stage"]["label"], "BOTTOM_CONFIRMED")

    def test_main_entry_plan_uses_nav_weight_and_target_room(self):
        shares, weight = _planned_main_entry_shares(
            position(), 40, 180000, 0.05,
            {"max_single_position_ratio": 0.30},
            {"initial_main_weight": 0.08, "target_main_weight": 0.20},
            "initial_main_weight",
        )
        self.assertEqual(shares, 300)
        self.assertAlmostEqual(weight, 300 * 40 / 180000)
        at_target, _ = _planned_main_entry_shares(
            position(), 40, 180000, 0.20,
            {"max_single_position_ratio": 0.30}, {}, "trend_add_weight",
        )
        self.assertEqual(at_target, 0)

    def test_per_position_sizing_override_and_share_cap(self):
        overridden = Position(
            "600362.SH", "江西铜业", 0, 87720, "copper", 300, 1000, (),
            SatellitePosition(), {"initial_main_weight": 0.04},
        )
        shares, _ = _planned_main_entry_shares(
            overridden, 40, 180000, 0.0,
            {"max_single_position_ratio": 0.30}, {}, "initial_main_weight",
        )
        self.assertEqual(shares, 100)
        satellite_shares, _ = _planned_satellite_entry_shares(
            overridden, 10, 180000, {"satellite_weight": 0.03},
        )
        self.assertEqual(satellite_shares, 300)

    def test_satellite_one_lot_tolerance_avoids_rounding_valid_setup_to_zero(self):
        one_lot, weight = _planned_satellite_entry_shares(
            position(), 58.5, 180000, {"satellite_weight": 0.03}, {},
            {"one_lot_tolerance_max_weight": 0.035},
        )
        too_large, _ = _planned_satellite_entry_shares(
            position(), 70, 180000, {"satellite_weight": 0.03}, {},
            {"one_lot_tolerance_max_weight": 0.035},
        )
        self.assertEqual(one_lot, 100)
        self.assertAlmostEqual(weight, 100 * 58.5 / 180000)
        self.assertEqual(too_large, 0)

    def test_satellite_uses_its_own_smaller_stop_risk_budget(self):
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(), tech(), "10:15", 0.01, 0.005,
            date(2026, 7, 28),
            {"entry_risk_weight": 0.0025},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0,
            equity_evidence(), {}, diagnostics,
        )
        self.assertEqual([signal.code for signal in signals], ["SAT_BUY"])
        self.assertAlmostEqual(
            diagnostics["metrics"]["satellite_entry"]["risk_budget"], 450
        )
        self.assertAlmostEqual(signals[0].details["planned_nav_ratio"], 4000 / 180000, 4)

    def test_migration_mode_can_reuse_only_released_weight_room(self):
        standard, _ = _planned_main_entry_shares(
            position(), 40, 180000, 0.35,
            {"max_single_position_ratio": 0.30}, {}, "trend_add_weight",
        )
        migrated, weight = _planned_main_entry_shares(
            position(), 40, 180000, 0.35,
            {"max_single_position_ratio": 0.30}, {}, "trend_add_weight",
            {"enabled": True, "position_ceiling": 0.4285, "main_add_weight": 0.03},
        )
        at_ceiling, _ = _planned_main_entry_shares(
            position(), 40, 180000, 0.4285,
            {"max_single_position_ratio": 0.30}, {}, "trend_add_weight",
            {"enabled": True, "position_ceiling": 0.4285, "main_add_weight": 0.03},
        )
        self.assertEqual(standard, 0)
        self.assertEqual(migrated, 100)
        self.assertAlmostEqual(weight, 100 * 40 / 180000)
        self.assertEqual(at_ceiling, 0)

    def test_migration_rebound_rejection_generates_trim(self):
        rejected = tech(
            support=39, resistance=42, vwap=42.0, volume_ratio=1.2,
            close=41.7, previous=42.1, open_=42.05,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(41.8), rejected, "14:15", -0.01, 0.001,
            date(2026, 7, 28), {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.35, 180000, None, None, {}, 0,
            equity_evidence(0), {}, diagnostics, {},
            {
                "enabled": True,
                "position_ceiling": 0.4285,
                "long_term_target_weight": 0.20,
                "rebound_trim_weight": 0.03,
                "rebound_resistance_distance_ratio": 0.01,
                "rebound_trim_volume_ratio": 1.0,
            },
        )
        self.assertEqual(signals[0].code, "MIGRATION_TRIM")
        self.assertEqual(signals[0].shares, 100)
        self.assertTrue(diagnostics["checks"]["migration_trim"]["rebound_rejection"]["passed"])

    def test_overweight_recovered_migration_position_trims_to_target_buffer(self):
        august_seventh = tech(
            support=45.71,
            resistance=47.85,
            vwap=48.39,
            volume_ratio=1.57,
            close=49.50,
            previous=49.30,
            open_=49.20,
            atr14=2.95,
            rsi14=70.96,
            previous_rsi14=69.0,
            rsi_max_5=70.96,
            range_position_60=0.704,
            recent_high_60=54.57,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(49.50), august_seventh, "14:15", 0.01, 0.005,
            date(2026, 8, 7), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.471, 189000, None, None, {}, 0,
            equity_evidence(0), {}, diagnostics, {},
            {
                "enabled": True,
                "position_ceiling": 0.4285,
                "long_term_target_weight": 0.20,
                "recovery_anchor_price": 87720 / 1800,
                "recovery_trim_enabled": True,
                "recovery_trim_cost_buffer_ratio": 0.005,
                "recovery_trim_target_buffer_weight": 0.03,
                "recovery_trim_rsi_min": 70,
                "recovery_trim_atr_extension_min": 3.0,
                "recovery_trim_breakout_volume_ratio": 1.30,
                "minimum_volume_baseline_samples": 3,
            },
        )
        self.assertEqual([signal.code for signal in signals], ["MIGRATION_RECOVERY_TRIM"])
        self.assertEqual(signals[0].shares, 1000)
        self.assertAlmostEqual(signals[0].details["retained_target_weight"], 0.23)
        self.assertTrue(
            diagnostics["checks"]["migration_recovery_trim"]
            ["above_recovery_price"]["passed"]
        )
        self.assertTrue(
            diagnostics["metrics"]["migration_recovery_trim"]["atr_overheated"]
        )

    def test_recovery_cost_cross_alone_does_not_trigger_migration_trim(self):
        not_overheated = tech(
            support=45.71,
            resistance=51.0,
            vwap=49.0,
            volume_ratio=1.0,
            close=49.50,
            previous=49.40,
            open_=49.30,
            atr14=10.0,
            rsi14=55.0,
            previous_rsi14=54.0,
            range_position_60=0.60,
            recent_high_60=60.0,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(49.50), not_overheated, "14:15", 0.01, 0.005,
            date(2026, 8, 7), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.471, 189000, None, None, {}, 0,
            equity_evidence(0), {}, diagnostics, {},
            {
                "enabled": True,
                "long_term_target_weight": 0.20,
                "recovery_anchor_price": 87720 / 1800,
                "recovery_trim_enabled": True,
                "recovery_trim_cost_buffer_ratio": 0.005,
                "recovery_trim_target_buffer_weight": 0.03,
                "recovery_trim_rsi_min": 70,
                "recovery_trim_atr_extension_min": 3.0,
                "minimum_volume_baseline_samples": 3,
            },
        )
        self.assertEqual(signals, [])
        self.assertFalse(
            diagnostics["checks"]["migration_recovery_trim"]
            ["overheat_or_rejection"]["passed"]
        )

    def test_verified_strong_breakout_blocks_recovery_trim(self):
        breakout = tech(
            support=45.71,
            resistance=47.85,
            vwap=48.39,
            volume_ratio=1.57,
            close=49.50,
            previous=49.30,
            open_=49.20,
            atr14=2.95,
            rsi14=70.96,
            previous_rsi14=69.0,
            range_position_60=0.704,
            recent_high_60=54.57,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(49.50), breakout, "14:15", 0.01, 0.005,
            date(2026, 8, 7), {},
            {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.471, 189000, None, None, {}, 0,
            equity_evidence(1, company_direction=1), {}, diagnostics, {},
            {
                "enabled": True,
                "long_term_target_weight": 0.20,
                "recovery_anchor_price": 87720 / 1800,
                "recovery_trim_enabled": True,
                "recovery_trim_cost_buffer_ratio": 0.005,
                "recovery_trim_target_buffer_weight": 0.03,
                "recovery_trim_breakout_volume_ratio": 1.30,
                "minimum_volume_baseline_samples": 3,
            },
        )
        self.assertEqual(signals, [])
        self.assertFalse(
            diagnostics["checks"]["migration_recovery_trim"]
            ["strong_breakout_not_confirmed"]["passed"]
        )

    def test_migration_trim_does_not_treat_missing_industry_as_weak(self):
        rejected = tech(
            support=39, resistance=42, vwap=42.0, volume_ratio=1.2,
            close=41.7, previous=42.1, open_=42.05,
        )
        unavailable = EquityEvidence(
            symbol="600362.SH",
            industry_status="missing",
            industry_direction=None,
            announcement_status="missing",
            announcement_risk="unknown",
            summary="产业证据不可用",
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(41.8), rejected, "14:15", 0.01, 0.001,
            date(2026, 7, 28), {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.35, 180000, None, None, {}, 0,
            unavailable, {}, diagnostics, {},
            {
                "enabled": True,
                "position_ceiling": 0.4285,
                "long_term_target_weight": 0.20,
                "rebound_trim_weight": 0.03,
                "rebound_resistance_distance_ratio": 0.01,
                "rebound_trim_volume_ratio": 1.0,
            },
        )
        self.assertEqual(signals, [])

    def test_record_date_still_blocks_routine_migration_trim(self):
        rejected = tech(
            support=39, resistance=42, vwap=42.0, volume_ratio=1.2,
            close=41.7, previous=42.1, open_=42.05,
        )
        holder = Position(
            "600362.SH", "江西铜业", 1800, 87720, "copper", 300, 300, (),
            SatellitePosition(), corporate_events=({
                "type": "cash_dividend", "record_date": "2026-07-28",
                "ex_date": "2026-07-29", "cash_per_share": 0.50,
            },),
        )
        diagnostics = {}
        signals = evaluate_position(
            holder, quote(41.8), rejected, "14:15", -0.01, 0.001,
            date(2026, 7, 28), {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.35, 180000, None, None, {}, 0,
            equity_evidence(0), {}, diagnostics, {},
            {
                "enabled": True,
                "position_ceiling": 0.4285,
                "long_term_target_weight": 0.20,
                "rebound_trim_weight": 0.03,
                "rebound_resistance_distance_ratio": 0.01,
                "rebound_trim_volume_ratio": 1.0,
            },
        )
        self.assertEqual(signals, [])

    def test_satellite_cannot_bypass_loss_warning_in_migration_mode(self):
        recovering = tech(
            support=37.8, resistance=40, vwap=37.9, volume_ratio=1.4,
            close=38.05, previous=37.8, open_=37.9, last_low=37.75,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(38), recovering, "10:15", 0.01, 0.005,
            date(2026, 7, 28), {},
            {"max_loss_ratio": 0.25, "near_limit_ratio": 0.225, "warning_ratio": 0.20},
            set(), 100000, 0.20, 180000, None, None, {}, 0,
            equity_evidence(0), {}, diagnostics, {},
            {"enabled": True, "position_ceiling": 0.30, "risk_principal_ceiling": 87720},
        )
        self.assertEqual(signals, [])
        self.assertFalse(diagnostics["checks"]["satellite_entry"]["below_risk_warning"]["passed"])

    def test_migration_risk_principal_ceiling_does_not_expand(self):
        legacy = Position(
            "600362.SH", "江西铜业", 1800, 100000, "copper", 300, 300, (),
            SatellitePosition(),
        )
        uncapped = _entry_risk_budget(
            legacy, 43, {"max_loss_ratio": 0.25}, 180000, 0.005, 0.10,
        )
        capped = _entry_risk_budget(
            legacy, 43, {"max_loss_ratio": 0.25}, 180000, 0.005, 0.10, 80000,
        )
        self.assertGreater(uncapped, 0)
        self.assertEqual(capped, 0)

    def test_migration_risk_principal_does_not_shrink_after_reduction(self):
        reduced = Position(
            "600362.SH", "江西铜业", 1600, 77820, "copper", 300, 300, (),
            SatellitePosition(), risk_principal=87720,
        )
        from astock_bot.strategy import _loss_ratio

        self.assertAlmostEqual(
            _loss_ratio(reduced, 43, 87720),
            (77820 - 1600 * 43) / 87720,
        )

    def test_low_zone_without_right_side_confirmation_is_only_bottoming(self):
        bottom = tech(
            support=39.8, resistance=43, vwap=40.2, volume_ratio=1.2,
            close=39.85, previous=40.0, open_=40.0, last_low=39.7,
            rsi14=34, previous_rsi14=35, rsi_min_5=30,
            ma20_slope_5d=-0.005, range_position_60=0.15,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(40), bottom, "13:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
            None, None, {}, 0, equity_evidence(1), {}, diagnostics,
        )
        self.assertEqual(signals, [])
        self.assertEqual(diagnostics["stage"]["label"], "BOTTOMING")

    def test_near_stage_top_does_not_trigger_exit_without_reversal(self):
        near_top = tech(
            support=41, resistance=45, vwap=43, volume_ratio=1.2,
            close=44.1, previous=44.0, open_=44.0,
            atr14=1, rsi14=70, previous_rsi14=69, rsi_max_5=72,
            range_position_60=0.90, recent_high_60=45, recent_low_60=35,
        )
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(44), near_top, "13:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
            None, None, {}, 0, equity_evidence(1), {}, diagnostics,
        )
        self.assertEqual(signals, [])
        self.assertEqual(diagnostics["stage"]["label"], "NEAR_STAGE_TOP")

    def test_confirmed_stage_top_trims_instead_of_guessing_full_exit(self):
        top = tech(
            support=40, resistance=43, vwap=42.1, volume_ratio=1.2,
            close=41.8, previous=42.2, open_=42.2,
            atr14=1, rsi14=60, previous_rsi14=65, rsi_max_5=72,
            range_position_60=0.85, recent_high_60=43, recent_low_60=35,
        )
        top.ma5 = 43
        top.ma10 = 41
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(42), top, "13:15", -0.01, 0.001, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
            None, None, {}, 0, equity_evidence(0), {"full_exit_enabled": True}, diagnostics,
        )
        self.assertEqual(signals[0].code, "STAGE_TOP_EXIT")
        self.assertEqual(signals[0].shares, 900)
        self.assertFalse(diagnostics["stage"]["full_exit_ready"])

    def test_confirmed_top_full_exit_requires_drawdown_ma10_and_external_weakness(self):
        top = tech(
            support=39, resistance=42, vwap=40.5, volume_ratio=1.2,
            close=39.8, previous=40.5, open_=40.5,
            atr14=0.5, rsi14=60, previous_rsi14=65, rsi_max_5=72,
            range_position_60=0.60, recent_high_60=42, recent_low_60=32,
        )
        top.ma5 = 41
        top.ma10 = 40.5
        top.ma20 = 39
        diagnostics = {}
        signals = evaluate_position(
            position(), quote(40), top, "14:15", -0.01, -0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(), 100000, 0.20, 180000,
            None, None, {}, 0, equity_evidence(-1, company_direction=-1),
            {"full_exit_enabled": True, "full_exit_requires_company_thesis_break": True}, diagnostics,
        )
        self.assertEqual(signals[0].code, "STAGE_TOP_EXIT")
        self.assertEqual(signals[0].shares, 1800)
        self.assertTrue(diagnostics["stage"]["full_exit_ready"])

    def test_hard_risk_limit_exits_all_main_shares(self):
        signals = evaluate_position(
            position(), quote(30), tech(), "10:15", None, None, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.25, "near_limit_ratio": 0.225, "warning_ratio": 0.20},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(),
            technical_data_fresh=False,
        )
        self.assertEqual(signals[0].code, "EMERGENCY_RISK")
        self.assertEqual(signals[0].shares, 1800)
        self.assertEqual(signals[0].details["event_rank"], 3)

    def test_stale_technicals_still_honor_satellite_fixed_target(self):
        satellite = SatellitePosition(
            active=True, shares=300, entry_price=38, entry_date=date(2026, 7, 27),
            entry_support=37.5, target_price=42, stop_price=36.8,
        )
        signals = evaluate_position(
            position(satellite=satellite), quote(42.2), tech(), "14:15", None, None,
            date(2026, 7, 28), {},
            {"max_loss_ratio": 0.99, "near_limit_ratio": 0.98, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(),
            technical_data_fresh=False,
        )
        self.assertEqual(signals[0].code, "SAT_SELL")
        self.assertFalse(signals[0].details["technical_data_fresh"])

    def test_risk_warning_reduction_uses_risk_ratio_not_main_add_cap(self):
        weak = tech(
            support=39.5, resistance=42, vwap=39.4, volume_ratio=1.5,
            close=38.9, previous=39.8, open_=39.6,
        )
        signals = evaluate_position(
            position(), quote(38.9), weak, "10:15", -0.01, -0.005,
            date(2026, 7, 28), {},
            {"max_loss_ratio": 0.25, "near_limit_ratio": 0.225, "warning_ratio": 0.20},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(-1),
        )
        self.assertEqual(signals[0].code, "EMERGENCY_RISK")
        self.assertEqual(signals[0].shares, 100)

    def test_stale_technical_data_blocks_non_hard_limit_signal(self):
        weak = tech(
            support=39.5, resistance=42, vwap=39.4, volume_ratio=1.5,
            close=39.0, previous=39.8, open_=39.6,
        )
        signals = evaluate_position(
            position(), quote(39.0), weak, "10:15", -0.01, -0.005,
            date(2026, 7, 28), {},
            {"max_loss_ratio": 0.99, "near_limit_ratio": 0.98, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(-1),
            {}, {}, {}, {}, False,
        )
        self.assertEqual(signals, [])

    def test_top_memory_carries_overbought_context_into_slow_distribution(self):
        first = tech(
            support=41, resistance=45, vwap=43.8, volume_ratio=1.0,
            close=44.1, previous=44.0, open_=44.0,
            atr14=1, rsi14=70, previous_rsi14=69, rsi_max_5=72,
            range_position_60=0.90, recent_high_60=45, recent_low_60=35,
        )
        first_diagnostics = {}
        evaluate_position(
            position(), quote(44), first, "13:15", 0.01, 0.01, date(2026, 7, 28),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.20, 180000, None, None, {}, 0, equity_evidence(1),
            {"top_state_max_calendar_days": 45}, first_diagnostics,
        )
        memory = first_diagnostics["stage"]["memory_update"]
        second_quote = Quote(
            "600362.SH", "江西铜业", datetime(2026, 8, 5, 14, 15, tzinfo=TZ),
            42.0, 42.5, 42.4, 42.5, 41.8, 1, 1,
        )
        second = tech(
            support=40, resistance=44, vwap=42.2, volume_ratio=1.2,
            close=41.9, previous=42.3, open_=42.3,
            atr14=1, rsi14=57, previous_rsi14=61, rsi_max_5=62,
            range_position_60=0.70, recent_high_60=45, recent_low_60=35,
        )
        second.ma5 = 43
        second.ma10 = 41
        diagnostics = {}
        signals = evaluate_position(
            position(), second_quote, second, "14:15", -0.01, -0.001,
            date(2026, 8, 5), {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(0),
            {"top_state_max_calendar_days": 45}, diagnostics, {}, {}, True, memory,
        )
        self.assertTrue(diagnostics["stage"]["remembered_top"])
        self.assertFalse(diagnostics["stage"]["recent_overbought_seen"])
        self.assertEqual(signals[0].code, "STAGE_TOP_EXIT")
        self.assertEqual(diagnostics["stage"]["memory_update"]["started_at"], "2026-07-28")
        self.assertGreater(diagnostics["stage"]["memory_update"]["peak_price"], 0)

    def test_neutral_node_does_not_wipe_active_top_memory(self):
        memory = {
            "state": "NEAR_STAGE_TOP",
            "started_at": "2026-07-28",
            "peak_price": 45.0,
            "last_updated": "2026-07-28T14:15:00+08:00",
        }
        calm = tech(
            support=40, resistance=44, vwap=41.5, volume_ratio=0.9,
            close=41.2, previous=41.0, open_=41.0,
            atr14=1, rsi14=55, previous_rsi14=54, rsi_max_5=56,
            range_position_60=0.55, recent_high_60=45, recent_low_60=35,
        )
        diagnostics = {}
        evaluate_position(
            position(), quote(41.2), calm, "10:15", 0.01, 0.01, date(2026, 7, 30),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.20, 180000, None, None, {}, 0, equity_evidence(1),
            {"top_state_max_calendar_days": 45}, diagnostics, {}, {}, True, memory,
        )
        self.assertTrue(diagnostics["stage"]["remembered_top"])
        self.assertEqual(diagnostics["stage"]["label"], "NEAR_STAGE_TOP")
        self.assertEqual(diagnostics["stage"]["memory_update"]["started_at"], "2026-07-28")
        self.assertAlmostEqual(diagnostics["stage"]["memory_update"]["peak_price"], 45.0)

    def test_record_date_allows_effective_down_break_but_keeps_hard_risk_priority(self):
        weak = tech(
            support=39.5, resistance=42, vwap=39.4, volume_ratio=1.5,
            close=38.9, previous=39.8, open_=39.6,
        )
        holder = Position(
            "601336.SH",
            "新华保险",
            900,
            69557.93,
            "insurance",
            100,
            300,
            (),
            SatellitePosition(),
            corporate_events=(
                {
                    "type": "cash_dividend",
                    "record_date": "2026-08-06",
                    "ex_date": "2026-08-07",
                    "cash_per_share": 2.06,
                    "basis_adjusted": False,
                },
            ),
        )
        signals = evaluate_position(
            holder, Quote("601336.SH", "新华保险", datetime(2026, 8, 6, 10, 15, tzinfo=TZ), 39.0, 62.0, 61, 61, 38.9, 1, 1),
            weak, "10:15", -0.01, -0.005, date(2026, 8, 6),
            {}, {"max_loss_ratio": 0.99, "near_limit_ratio": 0.98, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(-1),
        )
        self.assertEqual(signals[0].code, "DOWN_BREAK")

        hard = evaluate_position(
            holder, Quote("601336.SH", "新华保险", datetime(2026, 8, 6, 10, 15, tzinfo=TZ), 40.0, 62.0, 50, 50, 40, 1, 1),
            weak, "10:15", -0.01, -0.005, date(2026, 8, 6),
            {}, {"max_loss_ratio": 0.25, "near_limit_ratio": 0.225, "warning_ratio": 0.20},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(-1),
        )
        self.assertEqual(hard[0].code, "EMERGENCY_RISK")

    def test_unapplied_dividend_lowers_effective_loss_ratio(self):
        holder = Position(
            "601336.SH",
            "新华保险",
            900,
            69557.93,
            "insurance",
            100,
            300,
            (),
            SatellitePosition(),
            corporate_events=(
                {
                    "type": "cash_dividend",
                    "record_date": "2026-08-06",
                    "ex_date": "2026-08-07",
                    "cash_per_share": 2.06,
                    "basis_adjusted": False,
                },
            ),
        )
        # 除息后价格约 60.69；未调账时账面亏损虚高，有效成本扣分红后应离开预警区。
        price = 60.69
        raw_loss = (69557.93 - 900 * price) / 69557.93
        self.assertGreater(raw_loss, 0.20)
        from astock_bot.strategy import _loss_ratio

        adjusted = _loss_ratio(holder, price, today=date(2026, 8, 7))
        self.assertLess(adjusted, 0.20)

    def test_stage_top_trim_rounds_down_to_lot(self):
        top = tech(
            support=40, resistance=43, vwap=42.1, volume_ratio=1.2,
            close=41.8, previous=42.2, open_=42.2,
            atr14=1, rsi14=60, previous_rsi14=65, rsi_max_5=72,
            range_position_60=0.65, recent_high_60=43, recent_low_60=35,
        )
        top.ma5 = 43
        top.ma10 = 41
        diagnostics = {}
        signals = evaluate_position(
            position(main_shares=500), quote(42), top, "13:15", -0.01, 0.001,
            date(2026, 7, 28), {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(0),
            {"full_exit_enabled": True}, diagnostics,
        )
        self.assertEqual(signals[0].code, "STAGE_TOP_EXIT")
        # 500 * 50% = 250 → 向下取整 200，而不是向上 300。
        self.assertEqual(signals[0].shares, 200)
        self.assertIn("区间位置", signals[0].reason)

    def test_executed_top_trim_requires_new_high_to_rearm(self):
        top = tech(
            support=40, resistance=43, vwap=42.1, volume_ratio=1.2,
            close=41.8, previous=42.2, open_=42.2,
            atr14=1, rsi14=60, previous_rsi14=65, rsi_max_5=72,
            range_position_60=0.65, recent_high_60=43, recent_low_60=35,
        )
        top.ma5 = 43
        top.ma10 = 41
        memory = {
            "state": "STAGE_TOP_CONFIRMED",
            "started_at": "2026-07-28",
            "peak_price": 43.0,
            "top_trim_stage": 1,
            "top_execution_peak": 43.0,
        }
        diagnostics = {}
        signals = evaluate_position(
            position(main_shares=500), quote(42), top, "13:15", -0.01, 0.001,
            date(2026, 7, 28), {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(0),
            {"full_exit_enabled": True, "top_rearm_new_high_ratio": 0.03}, diagnostics,
            {}, {}, True, memory,
        )
        self.assertTrue(diagnostics["stage"]["top_confirmed"])
        self.assertFalse(diagnostics["stage"]["top_trim_rearmed"])
        self.assertEqual(signals, [])

        rearmed = tech(
            support=40, resistance=45, vwap=42.1, volume_ratio=1.2,
            close=41.8, previous=42.2, open_=42.2,
            atr14=1, rsi14=60, previous_rsi14=65, rsi_max_5=72,
            range_position_60=0.65, recent_high_60=45, recent_low_60=35,
        )
        rearmed.ma5 = 43
        rearmed.ma10 = 41
        diagnostics = {}
        signals = evaluate_position(
            position(main_shares=500), quote(42), rearmed, "13:15", -0.01, 0.001,
            date(2026, 7, 28), {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0, equity_evidence(0),
            {"full_exit_enabled": True, "top_rearm_new_high_ratio": 0.03}, diagnostics,
            {}, {}, True, memory,
        )
        self.assertTrue(diagnostics["stage"]["top_trim_rearmed"])
        self.assertEqual(signals[0].code, "STAGE_TOP_EXIT")

    def test_stale_bottom_memory_cannot_support_entry_after_right_side_breaks(self):
        broken = tech(
            support=39.5, resistance=42, vwap=40.2, volume_ratio=1.2,
            close=39.7, previous=40.1, open_=40.2,
            atr14=0.5, rsi14=34, previous_rsi14=38, rsi_min_5=32,
            range_position_60=0.20, recent_high_60=45, recent_low_60=35,
            ma20_slope_5d=-0.02,
        )
        memory = {
            "state": "BOTTOM_CONFIRMED",
            "started_at": "2026-08-07",
            "peak_price": 0.0,
            "bottom_started_at": "2026-08-07",
        }
        unavailable = EquityEvidence(
            symbol="600487.SH", industry_status="missing", industry_direction=None,
            announcement_status="missing", announcement_risk="none",
            summary="产业证据不可用",
        )
        diagnostics = {}
        signals = evaluate_position(
            Position(
                "600487.SH", "亨通光电", 0, 0, "optical_communications", 100, 500, (),
                SatellitePosition(), role="watchlist",
            ),
            Quote("600487.SH", "亨通光电", datetime(2026, 8, 8, 14, 15, tzinfo=TZ), 39.7, 40.1, 40.2, 40.2, 39.5, 1, 1),
            broken, "14:15", 0.0, 0.0, date(2026, 8, 8),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.0, 180000, None, None, {}, 0, unavailable,
            {}, diagnostics, {}, {}, True, memory,
            {"allowed_nodes": ["10:15", "13:15", "14:15"]},
        )
        self.assertFalse(diagnostics["stage"]["right_side_intact"])
        self.assertFalse(diagnostics["stage"]["bottom_confirmed"])
        self.assertEqual(diagnostics["stage"]["label"], "BOTTOMING")
        self.assertEqual(signals, [])

    def test_record_date_does_not_block_confirmed_stage_top_exit(self):
        top = tech(
            support=40, resistance=43, vwap=42.1, volume_ratio=1.2,
            close=41.8, previous=42.2, open_=42.2,
            atr14=1, rsi14=60, previous_rsi14=65, rsi_max_5=72,
            range_position_60=0.65, recent_high_60=43, recent_low_60=35,
        )
        top.ma5 = 43
        top.ma10 = 41
        holder = Position(
            "600362.SH", "江西铜业", 500, 20000, "copper", 100, 300, (),
            SatellitePosition(), corporate_events=({
                "type": "cash_dividend", "record_date": "2026-07-28",
                "ex_date": "2026-07-29", "cash_per_share": 0.50,
            },),
        )
        signals = evaluate_position(
            holder, quote(42), top, "13:15", -0.01, 0.001,
            date(2026, 7, 28), {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90},
            set(), 100000, 0.20, 180000, None, None, {}, 0,
            equity_evidence(0), {"full_exit_enabled": True}, {},
        )
        self.assertEqual(signals[0].code, "STAGE_TOP_EXIT")

    def test_bottom_confirmed_does_not_regress_when_evidence_unavailable(self):
        bottom = tech(
            support=39.5, resistance=42, vwap=39.6, volume_ratio=1.2,
            close=40.1, previous=39.7, open_=39.7,
            atr14=0.5, rsi14=42, previous_rsi14=36, rsi_min_5=32,
            range_position_60=0.20, recent_high_60=45, recent_low_60=35,
            ma20_slope_5d=0.0,
        )
        memory = {
            "state": "BOTTOM_CONFIRMED",
            "started_at": "2026-08-07",
            "peak_price": 0.0,
            "bottom_started_at": "2026-08-07",
            "last_updated": "2026-08-07T10:15:00+08:00",
        }
        unavailable = EquityEvidence(
            symbol="600487.SH",
            industry_status="missing",
            industry_direction=None,
            announcement_status="missing",
            announcement_risk="none",
            summary="产业证据不可用",
        )
        diagnostics = {}
        evaluate_position(
            Position(
                "600487.SH", "亨通光电", 0, 0, "optical_communications", 100, 500, (),
                SatellitePosition(), role="watchlist",
            ),
            Quote("600487.SH", "亨通光电", datetime(2026, 8, 7, 14, 15, tzinfo=TZ), 40.0, 39.8, 39.8, 40.2, 39.5, 1, 1),
            bottom, "14:15", 0.0, 0.0, date(2026, 8, 7),
            {}, {"max_loss_ratio": 0.99, "warning_ratio": 0.90}, set(),
            100000, 0.0, 180000, None, None, {}, 0, unavailable,
            {}, diagnostics, {}, {}, True, memory,
            {"allowed_nodes": ["10:15", "13:15", "14:15"]},
        )
        self.assertEqual(diagnostics["stage"]["label"], "BOTTOM_CONFIRMED")
        self.assertEqual(diagnostics["stage"]["memory_update"]["started_at"], "2026-08-07")

    def test_zero_main_position_never_generates_sell_zero_signal(self):
        candidate = Position(
            "600362.SH", "江西铜业", 0, 0, "copper", 300, 500, (),
            SatellitePosition(),
        )
        weak = tech(
            support=39.5, resistance=42, vwap=39.4, volume_ratio=1.5,
            close=39.0, previous=39.8, open_=39.6,
        )
        signals = evaluate_position(
            candidate, quote(39), weak, "13:15", -0.01, -0.005,
            date(2026, 7, 28), {}, {"max_loss_ratio": 0.25, "warning_ratio": 0.20},
            set(), 100000, 0.0, 180000, None, None, {}, 0, equity_evidence(-1),
        )
        self.assertEqual(signals, [])

    def test_overheat_watch_signal_uses_atr_and_rsi_thresholds(self):
        from astock_bot.strategy import overheat_watch_signal

        hot = tech(
            atr14=1.0, rsi14=72, previous_rsi14=70, rsi_max_5=74,
            range_position_60=0.70, recent_high_60=45, recent_low_60=35,
        )
        stage = {"atr_extension": 3.2, "label": "NEUTRAL"}
        signal = overheat_watch_signal(
            position(), quote(49.5), hot, date(2026, 8, 7), stage,
            {"overheat_watch_enabled": True, "overheat_atr_extension_min": 3.0, "overheat_rsi_min": 70},
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.code, "OVERHEAT_WATCH")
        self.assertEqual(signal.category, "reminder")
        self.assertEqual(signal.shares, 0)

    def test_aligned_short_trend_requires_both_ma_and_price(self):
        from astock_bot.strategy import _short_trend_ready

        mixed = tech()
        mixed.ma5 = 41
        mixed.ma10 = 40
        q = quote(39.5)  # below ma5
        self.assertFalse(_short_trend_ready(q, mixed, {"require_aligned_short_trend": True}))
        self.assertTrue(_short_trend_ready(q, mixed, {"require_aligned_short_trend": False}))

    def test_persistent_capital_outflow_blocks_entry_auxiliary_gate(self):
        from astock_bot.strategy import _auxiliary_allows_entry

        evidence = EquityEvidence(
            symbol="600362.SH",
            industry_status="fresh",
            industry_direction=1,
            announcement_status="fresh",
            announcement_risk="none",
            summary="x",
            capital_flow_status="fresh",
            capital_flow_signal="persistent_outflow",
        )
        self.assertFalse(_auxiliary_allows_entry(evidence))
        ok = EquityEvidence(
            symbol="600362.SH",
            industry_status="fresh",
            industry_direction=1,
            announcement_status="fresh",
            announcement_risk="none",
            summary="x",
            capital_flow_status="fresh",
            capital_flow_signal="neutral",
        )
        self.assertTrue(_auxiliary_allows_entry(ok))

    def test_shareholder_concentration_counts_as_strong_confirmation(self):
        concentrating = EquityEvidence(
            symbol="600362.SH",
            industry_status="fresh",
            industry_direction=0,
            announcement_status="fresh",
            announcement_risk="none",
            summary="x",
            shareholder_status="fresh",
            shareholder_signal="concentrating",
            shareholder_change_ratio=-0.08,
        )
        self.assertEqual(
            _strong_confirmation_count(None, None, concentrating, {}),
            1,
        )


if __name__ == "__main__":
    unittest.main()
