from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from astock_bot.config import AppConfig
from astock_bot.models import Position, Quote, SatellitePosition, Signal, Technicals
from astock_bot.service import MonitorService


TZ = ZoneInfo("Asia/Shanghai")


def signal(symbol, code, category, reward_risk=0, confidence="中", stage="NEUTRAL"):
    return Signal(
        symbol=symbol,
        name=symbol,
        code=code,
        confidence=confidence,
        price=40,
        key_level=40,
        action="测试动作",
        shares=100,
        reason="测试",
        invalidation="测试",
        event_id=f"2026-07-30|{symbol}:{code}:40.00",
        category=category,
        details={"reward_risk": reward_risk, "stage": stage},
    )


class ServicePolicyTests(unittest.TestCase):
    def test_message_budget_counts_combined_node_and_never_suppresses_exit(self):
        with TemporaryDirectory() as directory:
            service = MonitorService(self._config(Path(directory)))
            now = datetime(2026, 7, 30, 10, 15, tzinfo=TZ)
            three = [
                signal("600362.SH", "DOWN_BREAK", "strategy"),
                signal("601336.SH", "DOWN_BREAK", "strategy"),
                signal("601318.SH", "DOWN_BREAK", "strategy"),
            ]
            sendable, suppressed = service._filter_sendable(three, now)
            self.assertEqual(len(sendable), 3)
            self.assertEqual(suppressed, [])
            service.state.mark_notification(now.date(), {"strategy"})
            service.state.mark_notification(now.date(), {"strategy"})
            later = [
                signal("600362.SH", "UP_BREAK", "strategy", 2.0),
                signal("601336.SH", "STAGE_TOP_EXIT", "strategy"),
            ]
            sendable, suppressed = service._filter_sendable(later, now)
            self.assertEqual([item.code for item in sendable], ["STAGE_TOP_EXIT"])
            self.assertEqual(suppressed[0]["reason"], "daily_message_budget")

    def test_satellite_can_beat_weaker_main_entry_on_risk_adjusted_score(self):
        with TemporaryDirectory() as directory:
            service = MonitorService(self._config(Path(directory)))
            main = signal("600362.SH", "UP_BREAK", "strategy", 1.8, "中")
            satellite = signal("601336.SH", "SAT_BUY", "satellite", 3.0, "高")
            selected, suppressed = service._rank_capital_entries([main, satellite])
            self.assertEqual([item.code for item in selected], ["SAT_BUY"])
            self.assertEqual(suppressed[0]["event_id"], main.event_id)
            self.assertGreater(satellite.details["capital_rank_score"], main.details["capital_rank_score"])

    def test_same_event_is_resent_only_when_severity_rank_increases(self):
        with TemporaryDirectory() as directory:
            service = MonitorService(self._config(Path(directory)))
            now = datetime(2026, 7, 30, 10, 15, tzinfo=TZ)
            first = signal("600362.SH", "EMERGENCY_RISK", "risk")
            first.details["event_rank"] = 1
            service.state.mark_sent(first.event_id, now.date(), "risk", rank=1)
            duplicate, suppressed = service._filter_sendable([first], now)
            self.assertEqual(duplicate, [])
            self.assertEqual(suppressed[0]["reason"], "duplicate_event")

            upgraded = signal("600362.SH", "EMERGENCY_RISK", "risk")
            upgraded.details["event_rank"] = 3
            sendable, suppressed = service._filter_sendable([upgraded], now)
            self.assertEqual([item.code for item in sendable], ["EMERGENCY_RISK"])
            self.assertEqual(suppressed, [])

    def test_technical_freshness_checks_bars_and_previous_daily_session(self):
        with TemporaryDirectory() as directory:
            service = MonitorService(self._config(Path(directory)))
            quote = Quote(
                "600362.SH", "江西铜业", datetime(2026, 7, 30, 10, 15, tzinfo=TZ),
                40, 40, 40, 40, 40, 1, 1,
            )
            technicals = Technicals(
                ma5=40, ma10=40, ma20=40, support=39, resistance=41,
                vwap=40, volume_ratio=1.3, last_15m_close=40,
                previous_15m_close=39.9,
                last_5m_timestamp=datetime(2026, 7, 30, 9, 45, tzinfo=TZ),
                last_15m_timestamp=datetime(2026, 7, 30, 9, 45, tzinfo=TZ),
                daily_as_of=date(2026, 7, 28),
            )
            fresh, reasons = service._technical_data_fresh(
                technicals, quote, datetime(2026, 7, 30, 10, 15, tzinfo=TZ)
            )
            self.assertFalse(fresh)
            self.assertEqual(len(reasons), 3)

    @staticmethod
    def _config(root: Path) -> AppConfig:
        position = Position(
            "600362.SH", "江西铜业", 1800, 87720, "copper", 300, 500, (),
            SatellitePosition(),
        )
        return AppConfig(
            raw={
                "timezone": "Asia/Shanghai",
                "state_file": str(root / "state.json"),
                "log_file": str(root / "events.jsonl"),
                "notification": {"webhook": "", "secret": ""},
                "data_source": {
                    "provider": "tencent_public",
                    "max_bar_lag_seconds": 300,
                    "require_previous_trading_day": True,
                },
                "portfolio": {"available_cash": 0},
                "risk": {
                    "max_strategy_alerts_per_day": 2,
                    "max_satellite_alerts_per_day": 2,
                },
                "strategic_rules": {"main_entry_priority_bonus": 0.25},
            },
            positions=(position,),
        )


if __name__ == "__main__":
    unittest.main()
