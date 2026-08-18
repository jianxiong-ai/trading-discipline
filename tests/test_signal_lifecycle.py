from datetime import date, datetime
import tempfile
import unittest
from zoneinfo import ZoneInfo

from astock_bot.models import Position, Quote, SatellitePosition, Technicals
from astock_bot.service import MonitorService
from astock_bot.state import StateStore


TZ = ZoneInfo("Asia/Shanghai")


class ConfigStub:
    def section(self, name):
        if name == "strategic_rules":
            return {"peer_weak_ratio": 0.0}
        if name == "false_break_rules":
            return {
                "enabled": True,
                "peer_stability_mode": "relaxed_average",
                "peer_minimum": -0.015,
                "low_volume_reclaim_bypass_peers": True,
                "reclaim_max_volume_ratio": 1.0,
            }
        return {}


class SignalLifecycleTests(unittest.TestCase):
    def test_sent_down_break_is_resolved_only_after_full_recovery_and_peer_stability(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MonitorService.__new__(MonitorService)
            service.config = ConfigStub()
            service.state = StateStore(f"{directory}/state.json")
            service.state.save_active_signal("601336.SH", {
                "code": "DOWN_BREAK",
                "event_id": "2026-07-30|601336.SH:DOWN_BREAK:62.41",
                "date": "2026-07-30",
                "key_level": 62.41,
                "position_main_shares": 1000,
            })
            position = Position(
                "601336.SH", "新华保险", 1000, 75809.93, "insurance",
                100, 300, (), SatellitePosition(),
            )
            quote = Quote(
                "601336.SH", "新华保险", datetime(2026, 7, 30, 13, 45, tzinfo=TZ),
                62.78, 62.98, 62.96, 62.90, 62.20, 1, 1,
            )
            tech = Technicals(
                ma5=63, ma10=64, ma20=65, support=62.41, resistance=63.32,
                vwap=62.70, volume_ratio=1.1, last_15m_close=62.78,
                previous_15m_close=62.25, complete_15m=True,
            )
            resolved = service._resolved_down_break_signal(
                position, quote, tech, date(2026, 7, 30), 0.0147, [], [], True, True,
            )
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.code, "FALSE_BREAK")
            self.assertEqual(resolved.shares, 0)

    def test_false_break_uses_low_volume_reclaim_when_peers_still_weak(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MonitorService.__new__(MonitorService)
            service.config = ConfigStub()
            service.state = StateStore(f"{directory}/state.json")
            service.state.save_active_signal("600362.SH", {
                "code": "DOWN_BREAK",
                "event_id": "2026-08-18|600362.SH:DOWN_BREAK:46.46",
                "date": "2026-08-18",
                "key_level": 46.46,
                "position_main_shares": 1800,
            })
            position = Position(
                "600362.SH", "江西铜业", 1800, 87720, "copper",
                300, 500, (), SatellitePosition(),
            )
            quote = Quote(
                "600362.SH", "江西铜业", datetime(2026, 8, 18, 15, 0, tzinfo=TZ),
                46.60, 47.72, 46.50, 46.94, 45.86, 1, 1,
            )
            tech = Technicals(
                ma5=46, ma10=46, ma20=44.6, support=46.46, resistance=47.74,
                vwap=46.39, volume_ratio=0.50, last_15m_close=46.60,
                previous_15m_close=46.57, complete_15m=True,
            )
            resolved = service._resolved_down_break_signal(
                position,
                quote,
                tech,
                date(2026, 8, 18),
                -0.0109,
                [("601899.SH", 0.006), ("000630.SZ", -0.0162)],
                [],
                True,
                True,
            )
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.code, "FALSE_BREAK")
            self.assertIn("缩量收回支撑", resolved.reason)
            self.assertTrue(resolved.details["low_volume_reclaim"])

    def test_position_change_clears_active_signal_without_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MonitorService.__new__(MonitorService)
            service.config = ConfigStub()
            service.state = StateStore(f"{directory}/state.json")
            service.state.save_active_signal("601336.SH", {
                "code": "DOWN_BREAK", "date": "2026-07-30", "key_level": 62.41,
                "position_main_shares": 1000,
            })
            position = Position(
                "601336.SH", "新华保险", 700, 75809.93, "insurance",
                100, 300, (), SatellitePosition(),
            )
            quote = Quote(
                "601336.SH", "新华保险", datetime(2026, 7, 30, 13, 45, tzinfo=TZ),
                62.78, 62.98, 62.96, 62.90, 62.20, 1, 1,
            )
            tech = Technicals(
                ma5=63, ma10=64, ma20=65, support=62.41, resistance=63.32,
                vwap=62.70, volume_ratio=1.1, last_15m_close=62.78,
                previous_15m_close=62.25, complete_15m=True,
            )
            resolved = service._resolved_down_break_signal(
                position, quote, tech, date(2026, 7, 30), 0.0147, [], [], True, True,
            )
            self.assertIsNone(resolved)
            self.assertEqual(service.state.active_signal("601336.SH"), {})

    def test_quality_gate_suppression_preserves_active_down_break(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MonitorService.__new__(MonitorService)
            service.config = ConfigStub()
            service.state = StateStore(f"{directory}/state.json")
            service.state.save_active_signal("600362.SH", {
                "code": "DOWN_BREAK",
                "event_id": "2026-08-18|600362.SH:DOWN_BREAK:46.46",
                "semantic_key": "600362.SH:DOWN_BREAK",
                "date": "2026-08-18",
                "key_level": 46.46,
                "position_main_shares": 1800,
            })
            service._sync_reduction_lifecycle(
                Position(
                    "600362.SH", "江西铜业", 1800, 87720, "copper",
                    300, 500, (), SatellitePosition(),
                ),
                [],
                True,
                True,
                preserve_active_reduction=True,
            )
            self.assertEqual(
                service.state.active_signal("600362.SH").get("code"),
                "DOWN_BREAK",
            )

    def test_recorded_top_sell_advances_stage_only_after_holding_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            service = MonitorService.__new__(MonitorService)
            service.state = StateStore(f"{directory}/state.json")
            service.state.save_stage_state("600362.SH", {
                "state": "STAGE_TOP_CONFIRMED",
                "top_trim_stage": 0,
                "top_executed_shares": 0,
                "top_pending_anchor_shares": 1000,
                "top_pending_shares": 200,
                "top_pending_event_id": "2026-08-08|600362.SH:STAGE_TOP_EXIT:49.50",
                "top_pending_price": 49.50,
                "top_pending_peak": 50.00,
            })
            position = Position(
                "600362.SH", "江西铜业", 800, 77820, "copper",
                300, 500, (), SatellitePosition(),
            )
            memory = service._sync_stage_execution(
                position, service.state.stage_state(position.symbol), date(2026, 8, 8), True,
            )
            self.assertEqual(memory["top_trim_stage"], 1)
            self.assertEqual(memory["top_executed_shares"], 200)
            self.assertEqual(memory["top_execution_peak"], 50.0)
            self.assertIsNone(memory["top_pending_event_id"])


if __name__ == "__main__":
    unittest.main()
