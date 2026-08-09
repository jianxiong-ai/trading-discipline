from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from astock_bot.portfolio_store import PortfolioStore


def portfolio() -> dict:
    return {
        "available_cash": 10000.0,
        "positions": [
            {
                "symbol": "600362.SH",
                "name": "江西铜业",
                "role": "holding",
                "main_shares": 200,
                "economic_basis": 8000.0,
                "sector": "copper",
                "satellite_limit": 300,
                "main_adjustment_shares": 100,
                "peers": ["601899.SH"],
                "satellite": {"active": False, "shares": 0},
                "migration": {},
            }
        ],
    }


class PortfolioStoreTests(unittest.TestCase):
    def test_seed_and_main_trade_update_shared_snapshot(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            first = store.snapshot(raw)
            self.assertEqual(first["cash"], 10000.0)
            self.assertEqual(first["positions"][0]["main_shares"], 200)

            store.record_trade(
                symbol="600362.SH", bucket="main", side="sell", shares=100,
                price=45.0, fee=5.0, executed_at="2026-08-08", note="纪律减仓",
            )
            after = store.snapshot(raw)
            position = after["positions"][0]
            self.assertEqual(after["cash"], 14495.0)
            self.assertEqual(position["main_shares"], 100)
            self.assertEqual(position["economic_basis"], 3505.0)
            self.assertEqual(store.transactions()[0]["side"], "sell")

    def test_satellite_round_trip_updates_cash_and_lifecycle(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            store.ensure_seed(raw["portfolio"])
            store.record_trade(
                symbol="600362.SH", bucket="satellite", side="buy", shares=100,
                price=40.0, fee=5.0, executed_at="2026-08-08",
                entry_support=39.5, target_price=42.0, stop_price=39.0,
            )
            opened = store.snapshot(raw)["positions"][0]
            self.assertTrue(opened["satellite"]["active"])
            self.assertEqual(opened["satellite"]["shares"], 100)
            self.assertEqual(store.snapshot(raw)["cash"], 5995.0)

            store.record_trade(
                symbol="600362.SH", bucket="satellite", side="sell", shares=100,
                price=42.0, fee=5.0, executed_at="2026-08-09",
            )
            closed = store.snapshot(raw)["positions"][0]
            self.assertFalse(closed["satellite"]["active"])
            self.assertEqual(store.snapshot(raw)["cash"], 10190.0)

    def test_profitable_partial_sell_keeps_cycle_risk_principal(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            store.ensure_seed(raw["portfolio"])
            store.record_trade(
                symbol="600362.SH", bucket="main", side="sell", shares=100,
                price=100.0, fee=5.0, executed_at="2026-08-08",
            )
            item = store.snapshot(raw)["positions"][0]
            self.assertEqual(item["main_shares"], 100)
            self.assertEqual(item["economic_basis"], -1995.0)
            self.assertEqual(item["risk_principal"], 8000.0)

    def test_recorded_dividend_marks_configured_event_as_settled_once(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            raw["portfolio"]["positions"][0]["corporate_events"] = [{
                "type": "cash_dividend", "record_date": "2026-08-06",
                "ex_date": "2026-08-07", "cash_per_share": 2.0,
                "basis_adjusted": False,
            }]
            store.ensure_seed(raw["portfolio"])
            store.record_dividend(
                symbol="600362.SH", amount=400.0, executed_at="2026-08-08"
            )
            item = store.snapshot(raw)["positions"][0]
            self.assertEqual(item["economic_basis"], 7600.0)
            self.assertTrue(item["corporate_events"][0]["basis_adjusted"])
            with self.assertRaisesRegex(ValueError, "已登记到账"):
                store.record_dividend(
                    symbol="600362.SH", amount=400.0, executed_at="2026-08-08"
                )

    def test_pre_adjusted_dividend_only_increases_cash_and_uses_record_date_shares(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            event = {
                "type": "cash_dividend", "record_date": "2026-08-06",
                "ex_date": "2026-08-07", "cash_per_share": 2.0,
                "eligible_shares": 200, "basis_adjusted": True,
            }
            raw["portfolio"]["positions"][0]["corporate_events"] = [event]
            store.ensure_seed(raw["portfolio"])
            # A later trade changes current shares; dividend matching must retain
            # the record-date entitlement rather than infer it from that balance.
            store.record_trade(
                symbol="600362.SH", bucket="main", side="sell", shares=100,
                price=45.0, fee=5.0, executed_at="2026-08-08",
            )
            mode = store.record_dividend(
                symbol="600362.SH", amount=400.0, executed_at="2026-08-09",
                corporate_events=[event],
            )
            item = store.snapshot(raw)["positions"][0]
            self.assertEqual(mode, "pre_adjusted")
            self.assertEqual(item["economic_basis"], 3505.0)
            self.assertEqual(store.snapshot(raw)["cash"], 14895.0)

    def test_reconcile_legacy_pre_adjusted_dividend_keeps_cash_and_adds_audit_row(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            event = {
                "type": "cash_dividend", "record_date": "2026-08-06",
                "ex_date": "2026-08-07", "cash_per_share": 2.0,
                "eligible_shares": 200, "basis_adjusted": True,
            }
            raw["portfolio"]["positions"][0]["corporate_events"] = [event]
            store.ensure_seed(raw["portfolio"])
            # Simulate the legacy behavior: cash was correct but basis was reduced again.
            store.record_dividend(
                symbol="600362.SH", amount=400.0, executed_at="2026-08-08",
                corporate_events=[{**event, "basis_adjusted": False}],
            )
            event_id = "cash_dividend|2026-08-06|2026-08-07|2.000000"
            store.reconcile_pre_adjusted_dividend(
                symbol="600362.SH", event_id=event_id, amount=400.0,
                corporate_events=[event], note="修复历史重复扣减",
            )
            item = store.snapshot(raw)["positions"][0]
            self.assertEqual(item["economic_basis"], 8000.0)
            self.assertEqual(store.snapshot(raw)["cash"], 10400.0)
            self.assertEqual(store.transactions()[0]["side"], "basis_correction")
            self.assertEqual(store.transactions()[0]["cash_delta"], 0.0)

    def test_rejects_future_date_and_sell_fee_larger_than_gross(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            store.ensure_seed(raw["portfolio"])
            with self.assertRaisesRegex(ValueError, "不得晚于今天"):
                store.record_trade(
                    symbol="600362.SH", bucket="main", side="sell", shares=100,
                    price=45.0, fee=5.0, executed_at="2099-01-01",
                )

    def test_recent_trade_can_be_reversed_without_deleting_audit(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            store.ensure_seed(raw["portfolio"])
            store.record_trade(
                symbol="600362.SH", bucket="main", side="sell", shares=100,
                price=45.0, fee=5.0, executed_at="2026-08-08",
            )
            original = store.transactions()[0]
            store.reverse_transaction(original["id"])
            item = store.snapshot(raw)["positions"][0]
            self.assertEqual(item["main_shares"], 200)
            self.assertEqual(store.snapshot(raw)["cash"], 10000.0)
            self.assertEqual(store.transactions()[0]["side"], "reversal")
            with self.assertRaisesRegex(ValueError, "费用必须小于"):
                store.record_trade(
                    symbol="600362.SH", bucket="main", side="sell", shares=100,
                    price=45.0, fee=4500.0, executed_at="2026-08-08",
                )

    def test_rejects_nan_and_infinite_trade_or_dividend_values(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            store.ensure_seed(raw["portfolio"])
            with self.assertRaisesRegex(ValueError, "NaN|有限"):
                store.record_trade(
                    symbol="600362.SH", bucket="main", side="buy", shares=100,
                    price=float("nan"), fee=5.0, executed_at="2026-08-08",
                )
            with self.assertRaisesRegex(ValueError, "Inf|有限"):
                store.record_trade(
                    symbol="600362.SH", bucket="main", side="buy", shares=100,
                    price=40.0, fee=float("inf"), executed_at="2026-08-08",
                )
            raw["portfolio"]["positions"][0]["corporate_events"] = [{
                "type": "cash_dividend", "record_date": "2026-08-06",
                "ex_date": "2026-08-07", "cash_per_share": 2.0,
            }]
            # The event is supplied at registration time, as the UI does.
            with self.assertRaisesRegex(ValueError, "NaN|有限"):
                store.record_dividend(
                    symbol="600362.SH", amount=float("nan"), executed_at="2026-08-08",
                    corporate_events=raw["portfolio"]["positions"][0]["corporate_events"],
                )

    def test_watchlist_becomes_holding_on_first_confirmed_main_buy(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            store.ensure_seed(raw["portfolio"])
            store.add_watchlist(symbol="603596.SH", name="伯特利", sector="new_energy_vehicle")
            store.record_trade(
                symbol="603596.SH", bucket="main", side="buy", shares=100,
                price=50.0, fee=5.0, executed_at="2026-08-08",
            )
            item = next(p for p in store.snapshot(raw)["positions"] if p["symbol"] == "603596.SH")
            self.assertEqual(item["role"], "holding")
            self.assertEqual(item["main_shares"], 100)
            self.assertEqual(item["watchlist_entry_date"], "2026-08-08")

    def test_smart_watchlist_profile_is_persisted(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            store.ensure_seed(raw["portfolio"])
            store.add_watchlist(
                symbol="600487.SH",
                name="亨通光电",
                sector="optical_communications",
                peers=["600498.SH", "601869.SH"],
                analysis_profile={
                    "coverage": "full",
                    "coverage_label": "完整跟踪",
                    "sector_label": "光通信",
                },
            )
            item = next(p for p in store.snapshot(raw)["positions"] if p["symbol"] == "600487.SH")
            self.assertEqual(item["analysis_profile"]["coverage"], "full")
            self.assertEqual(item["peers"], ["600498.SH", "601869.SH"])

    def test_basic_tracking_cannot_be_confirmed_as_a_main_position(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            raw = {"portfolio": portfolio()}
            store.ensure_seed(raw["portfolio"])
            store.add_watchlist(
                symbol="600000.SH",
                name="测试标的",
                sector="generic",
                analysis_profile={"coverage": "basic"},
            )
            with self.assertRaisesRegex(ValueError, "完整研究覆盖"):
                store.record_trade(
                    symbol="600000.SH", bucket="main", side="buy", shares=100,
                    price=10.0, fee=5.0, executed_at="2026-08-08",
                )


if __name__ == "__main__":
    unittest.main()
