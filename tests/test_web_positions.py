from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starlette.requests import Request

from astock_bot.portfolio_store import PortfolioStore
from astock_bot.web import _context, _review_records, set_position_target_weight, templates


def _raw() -> dict:
    return {
        "position_sizing": {
            "target_main_weight": 0.20,
            "max_single_position_weight": 0.30,
        },
        "risk": {"max_single_position_ratio": 0.30},
        "portfolio": {
            "available_cash": 10000,
            "positions": [{
                "symbol": "600362.SH",
                "name": "江西铜业",
                "role": "holding",
                "main_shares": 100,
                "economic_basis": 4000,
                "sector": "copper",
                "satellite": {"active": False, "shares": 0},
            }],
        },
    }


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/u/demo/positions",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
    })


class PositionTargetWebTests(unittest.TestCase):
    def test_positions_page_shows_editable_target_and_single_position_cap(self):
        with TemporaryDirectory() as directory:
            store = PortfolioStore(Path(directory) / "portfolio.db")
            context = _context(
                _request(), _raw(), store, workspace_id="demo", transactions=[],
            )

            html = templates.get_template("positions.html").render(context)

            self.assertIn('<details class="weight-editor">', html)
            self.assertIn("编辑", html)
            self.assertIn("<b>20.0%</b>", html)
            self.assertIn("<b>30.0%</b>", html)
            self.assertIn('name="target_weight_pct"', html)
            self.assertIn('name="max_single_position_weight_pct"', html)
            self.assertIn('value="20.0"', html)
            self.assertIn('value="30.0"', html)
            self.assertIn("目标继承全局 · 上限继承全局", html)
            self.assertIn("账户硬上限30%", html)

    def test_weight_limit_endpoint_persists_per_position_overrides(self):
        with TemporaryDirectory() as directory:
            raw = _raw()
            store = PortfolioStore(Path(directory) / "portfolio.db")
            store.ensure_seed(raw["portfolio"])
            position = SimpleNamespace(
                symbol="600362.SH", name="江西铜业", role="holding", sizing={},
            )
            config = SimpleNamespace(raw=raw, positions=(position,))

            with (
                patch("astock_bot.web._verify_form"),
                patch("astock_bot.web._require_workspace_access"),
                patch("astock_bot.web._workspace_config", return_value=(config, store)),
            ):
                response = set_position_target_weight(
                    "demo", "600362.SH", _request(),
                    csrf_token="token", target_weight_pct=18,
                    max_single_position_weight_pct=25,
                )

            self.assertEqual(response.status_code, 303)
            sizing = store.snapshot(raw)["positions"][0]["sizing"]
            self.assertEqual(sizing["target_main_weight"], 0.18)
            self.assertEqual(sizing["max_single_position_weight"], 0.25)

    def test_watchlist_renders_copper_derivative_profile_and_option_review(self):
        with TemporaryDirectory() as directory:
            raw = _raw()
            raw["portfolio"]["positions"] = []
            store = PortfolioStore(Path(directory) / "portfolio.db")
            store.add_watchlist(
                symbol="600362.SH", name="江西铜业", sector="copper",
                analysis_profile={
                    "coverage": "full", "coverage_label": "完整跟踪",
                    "sector_label": "铜产业",
                    "related_derivatives": [{
                        "kind": "commodity_option", "exchange": "SHFE",
                        "product": "CU", "label": "沪铜期权", "role": "auxiliary",
                    }],
                },
                commodity_exposures=[{
                    "commodity": "copper", "commodity_label": "铜",
                    "exposure_types": ["mining"],
                    "sensitivity": "资源端仍需核对产量、成本与套保",
                }],
            )
            audit = [{
                "timestamp": "2026-08-17T14:15:00+08:00", "node": "14:15",
                "decision": "NO_ALERT", "signals": [],
                "summaries": [{
                    "symbol": "600362.SH", "status": "NO_ALERT",
                    "commodity_option_status": "fresh",
                    "commodity_option_view": "balanced",
                    "commodity_option_summary": "沪铜期权近ATM双边结构均衡。",
                }],
            }]
            context = _context(
                _request(), raw, store, workspace_id="demo",
                audit_records=audit, latest_summary=None,
                llm_available=False, llm_model="",
            )
            html = templates.get_template("watchlist.html").render(context)
            self.assertIn("沪铜期权 · 辅助观察", html)
            self.assertIn("权利金涨跌不等同商品期货或个股涨跌", html)
            self.assertIn("期权辅助：沪铜期权近ATM双边结构均衡", html)

    def test_review_rows_keep_option_context_out_of_trading_reason(self):
        records = _review_records([{
            "timestamp": "2026-08-17T14:15:00+08:00", "node": "14:15",
            "decision": "NO_ALERT", "signals": [], "summaries": [{
                "symbol": "600362.SH", "status": "NO_ALERT", "price": 47.0,
                "commodity_option_status": "fresh",
                "commodity_option_summary": "沪铜期权辅助结论",
            }],
        }], [{"symbol": "600362.SH", "name": "江西铜业"}])
        row = records[0]["review_rows"][0]
        self.assertEqual(row["commodity_option_summary"], "沪铜期权辅助结论")
        self.assertNotIn("期权", row["reason"])


if __name__ == "__main__":
    unittest.main()
