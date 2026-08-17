import json
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from astock_bot.config import AppConfig
from astock_bot.models import Position, Quote, SatellitePosition
from astock_bot.service import MonitorService


TZ = ZoneInfo("Asia/Shanghai")


class DailySummaryTests(unittest.TestCase):
    def test_formal_records_exclude_manual_and_dry_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log_file = root / "events.jsonl"
            records = [
                self._record("2026-07-29T09:15:07+08:00", "09:15"),
                self._record("2026-07-29T10:15:12+08:00", "10:15"),
                self._record("2026-07-29T10:30:51+08:00", "10:15"),
                self._record("2026-07-29T13:15:07+08:00", "13:15", "scheduled"),
                self._record("2026-07-29T14:15:10+08:00", "14:15", "dry_run"),
            ]
            log_file.write_text(
                "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n",
                encoding="utf-8",
            )
            service = MonitorService(self._config(root))
            selected = service._formal_records_for_day(datetime(2026, 7, 29, 15, 30, tzinfo=TZ))
            self.assertEqual([record["node"] for record in selected], ["09:15", "10:15", "13:15"])
            self.assertEqual(selected[1]["timestamp"], "2026-07-29T10:15:12+08:00")

    def test_scheduled_recovery_is_included_in_daily_summary_records(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            log_file = root / "events.jsonl"
            record = self._record("2026-07-29T10:18:00+08:00", "10:15", "scheduled_recovery")
            log_file.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")
            service = MonitorService(self._config(root))
            selected = service._formal_records_for_day(datetime(2026, 7, 29, 15, 30, tzinfo=TZ))
            self.assertEqual([item["node"] for item in selected], ["10:15"])

    def test_summary_reason_distinguishes_near_top_from_confirmed_top(self):
        near = MonitorService._summary_reason({
            "status": "NO_ALERT", "price": 44, "support": 41, "resistance": 45,
            "vwap": 43.8, "volume_ratio": 1.1,
            "stage": {"label": "NEAR_STAGE_TOP"},
        })
        confirmed = MonitorService._summary_reason({
            "status": "NO_ALERT", "price": 42, "support": 41, "resistance": 45,
            "vwap": 42.3, "volume_ratio": 1.3,
            "stage": {"label": "STAGE_TOP_CONFIRMED"},
        })
        self.assertIn("尚不能定义顶部", near)
        self.assertIn("共同确认阶段顶部", confirmed)

    def test_watchlist_summary_names_trend_and_reward_risk_blockers(self):
        reason = MonitorService._summary_reason({
            "role": "watchlist",
            "status": "NO_ALERT",
            "price": 54.65,
            "support": 48.44,
            "resistance": 50.86,
            "ma20": 60.05,
            "vwap": 52.29,
            "volume_ratio": 2.71,
            "stage": {"label": "BOTTOMING", "bottom_confirmed": False},
            "checks": {"watchlist_entry": {"setup_confirmed": {"passed": False}}},
            "metrics": {"watchlist_entry": {
                "price_above_ma20": False,
                "ma20_slope_ready": False,
                "breakout_persistence": True,
                "breakout_expected_spread": 0.0866,
                "minimum_expected_spread_required": 0.05,
                "breakout_reward_risk": 1.10,
                "minimum_reward_risk_required": 2.0,
            }},
        })
        self.assertIn("放量站上动态压力", reason)
        self.assertIn("仍低于20日线60.05", reason)
        self.assertIn("收益风险比1.10低于2.00", reason)

    def test_holding_summary_names_position_ceiling_as_binding_constraint(self):
        reason = MonitorService._summary_reason({
            "role": "holding",
            "status": "NO_ALERT",
            "price": 46.57,
            "support": 42.8,
            "resistance": 44.0,
            "vwap": 45.8,
            "volume_ratio": 1.97,
            "stage": {"label": "NEUTRAL"},
            "checks": {"main_add": {
                "industry_and_announcements": {"passed": False},
                "sized_at_least_one_lot": {"passed": False},
            }},
            "metrics": {"main_add": {
                "current_weight": 0.4489,
                "target_weight": 0.4286,
            }},
        })
        self.assertIn("产业/公告证据未支持新增", reason)
        self.assertIn("现有仓位已达当前新增上限", reason)

    def test_watchlist_summary_reconstructs_old_record_breakout_blockers(self):
        reason = MonitorService._summary_reason({
            "role": "watchlist",
            "status": "NO_ALERT",
            "price": 54.65,
            "support": 48.44,
            "resistance": 50.86,
            "next_resistance": 59.38,
            "ma20": 60.05,
            "ma20_slope_5d": -0.1479,
            "vwap": 52.29,
            "volume_ratio": 2.71,
            "stage": {"label": "BOTTOMING", "bottom_confirmed": False},
            "checks": {"watchlist_entry": {"setup_confirmed": {"passed": False}}},
            "metrics": {"watchlist_entry": {"setup": "none"}},
        })
        self.assertIn("仍低于20日线60.05", reason)
        self.assertIn("20日线仍明显下行", reason)
        self.assertIn("收益风险比1.10低于2.00", reason)

    def test_watchlist_company_thesis_break_is_reported_before_price_structure(self):
        summary = {
            "role": "watchlist",
            "status": "NO_ALERT",
            "price": 58.7,
            "support": 57,
            "resistance": 60,
            "stage": {"label": "NEUTRAL", "company_thesis_break": True},
        }
        self.assertIn("暂停首次建仓", MonitorService._summary_recommendation(summary, "watchlist"))
        self.assertIn("负向证据", MonitorService._summary_reason(summary))

    def test_near_entry_reminder_is_not_counted_as_an_action_signal(self):
        with TemporaryDirectory() as directory:
            service = MonitorService(self._config(Path(directory)))
            record = self._record("2026-07-29T10:15:07+08:00", "10:15", "scheduled")
            record["summaries"][0].update({
                "role": "watchlist", "support": 39.8, "resistance": 42.0,
                "stage": {"label": "BOTTOM_CONFIRMED"},
                "commodity_option_status": "fresh",
                "commodity_option_view": "balanced",
                "commodity_option_summary": "沪铜期权近ATM双边结构均衡。",
            })
            record["signals"] = [{
                "symbol": "600362.SH",
                "code": "WATCH_NEAR_ENTRY",
                "action": "临界机会观察，暂不建仓",
                "reason": "目标空间与一手风险预算尚未同时通过",
                "details": {"notification_status": "sent", "informational_only": True},
            }]
            row = service._daily_summary_rows([record])[0]
            self.assertEqual(row["trigger_count"], 0)
            self.assertEqual(row["candidate_count"], 0)
            self.assertEqual(row["informational_count"], 1)
            self.assertEqual(row["recommendation"], "临界机会观察，暂不建仓")
            self.assertIn("风险预算", row["reason"])
            self.assertEqual(row["commodity_option_status"], "fresh")
            self.assertEqual(row["commodity_option_view"], "balanced")
            self.assertIn("近ATM", row["commodity_option_summary"])

    def test_summary_refreshes_display_quotes_at_send_time(self):
        with TemporaryDirectory() as directory:
            service = MonitorService(self._config(Path(directory)))
            rows = [{
                "symbol": "600362.SH",
                "name": "江西铜业",
                "price": 47.16,
                "change_pct": 5.72,
                "recommendation": "继续观察",
                "reason": "测试",
            }]
            warnings: list[str] = []
            case = self

            class StubSource:
                def quote(self, symbol: str) -> Quote:
                    case.assertEqual(symbol, "600362.SH")
                    return Quote(
                        symbol, "江西铜业",
                        datetime(2026, 8, 17, 15, 0, tzinfo=TZ),
                        47.72, 44.61, 45.0, 48.0, 44.5, 1.0, 1.0,
                    )

            service.source = StubSource()
            service._refresh_summary_quotes(rows, warnings)
            self.assertEqual(rows[0]["price"], 47.72)
            self.assertAlmostEqual(rows[0]["change_pct"], 6.97, places=2)
            self.assertEqual(warnings, [])

    @staticmethod
    def _record(timestamp, node, execution_type=None):
        record = {
            "timestamp": timestamp,
            "node": node,
            "decision": "NO_ALERT",
            "signals": [],
            "summaries": [{
                "symbol": "600362.SH",
                "status": "BASELINE" if node == "09:15" else "NO_ALERT",
                "price": 42.5,
                "change_pct": 0.1,
            }],
            "warnings": [],
        }
        if execution_type:
            record["execution_type"] = execution_type
        return record

    @staticmethod
    def _config(root: Path) -> AppConfig:
        position = Position(
            symbol="600362.SH",
            name="江西铜业",
            main_shares=1800,
            economic_basis=87720,
            sector="copper",
            satellite_limit=300,
            main_adjustment_shares=300,
            peers=(),
            satellite=SatellitePosition(),
        )
        return AppConfig(
            raw={
                "timezone": "Asia/Shanghai",
                "schedule": ["09:15", "10:15", "13:15", "14:15"],
                "run_window_seconds": 180,
                "state_file": str(root / "state.json"),
                "log_file": str(root / "events.jsonl"),
                "notification": {"webhook": "", "secret": ""},
                "data_source": {"provider": "tencent_public"},
                "portfolio": {"available_cash": 0},
            },
            positions=(position,),
        )


if __name__ == "__main__":
    unittest.main()
