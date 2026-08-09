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
        return {"peer_weak_ratio": 0.0} if name == "strategic_rules" else {}


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
                position, quote, tech, date(2026, 7, 30), 0.0147, [], True, True,
            )
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.code, "FALSE_BREAK")
            self.assertEqual(resolved.shares, 0)

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
                position, quote, tech, date(2026, 7, 30), 0.0147, [], True, True,
            )
            self.assertIsNone(resolved)
            self.assertEqual(service.state.active_signal("601336.SH"), {})

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
