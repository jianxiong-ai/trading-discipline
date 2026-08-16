from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from starlette.requests import Request

from astock_bot.portfolio_store import PortfolioStore
from astock_bot.web import _context, set_position_target_weight, templates


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


if __name__ == "__main__":
    unittest.main()
