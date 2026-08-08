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
                price=42.0, fee=5.0, executed_at="2026-08-11",
            )
            closed = store.snapshot(raw)["positions"][0]
            self.assertFalse(closed["satellite"]["active"])
            self.assertEqual(store.snapshot(raw)["cash"], 10190.0)

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


if __name__ == "__main__":
    unittest.main()
