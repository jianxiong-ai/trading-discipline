from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zoneinfo import ZoneInfo

from astock_bot.config import AppConfig
from astock_bot.models import Position, Quote, SatellitePosition
from astock_bot.service import MonitorService


TZ = ZoneInfo("Asia/Shanghai")


def make_position(symbol: str, shares: int, ceiling: float, principal: float) -> Position:
    return Position(
        symbol,
        symbol,
        shares,
        principal,
        "copper" if symbol == "600362.SH" else "insurance",
        100,
        300,
        (),
        SatellitePosition(),
        {},
        {
            "enabled": True,
            "initial_ceiling_weight": ceiling,
            "risk_principal_ceiling": principal,
        },
    )


def make_quote(symbol: str, price: float) -> Quote:
    return Quote(
        symbol,
        symbol,
        datetime(2026, 7, 30, 10, 15, tzinfo=TZ),
        price,
        price,
        price,
        price,
        price,
        1,
        1,
    )


class MigrationStateTests(unittest.TestCase):
    def _config(self, state_file: Path, first_shares: int) -> AppConfig:
        raw = {
            "timezone": "Asia/Shanghai",
            "state_file": str(state_file),
            "notification": {},
            "data_source": {},
            "position_sizing": {"target_main_weight": 0.20},
            "risk": {
                "max_single_position_ratio": 0.30,
                "correlation_groups": {
                    "legacy_group": {
                        "symbols": ["600362.SH", "601318.SH"],
                        "max_ratio": 0.40,
                    }
                },
            },
            "migration_mode": {
                "enabled": True,
                "main_add_weight": 0.03,
                "ratchet_buffer_weight": 0.03,
                "rebound_trim_weight": 0.03,
                "correlation_groups": {
                    "legacy_group": {"initial_ceiling_weight": 0.90}
                },
            },
        }
        return AppConfig(
            raw,
            (
                make_position("600362.SH", first_shares, 0.65, 70000),
                make_position("601318.SH", 500, 0.30, 30000),
            ),
        )

    def test_confirmed_reduction_ratchets_position_and_group_ceiling_only_down(self):
        with TemporaryDirectory() as folder:
            state_file = Path(folder) / "state.json"
            quotes = {
                "600362.SH": make_quote("600362.SH", 60),
                "601318.SH": make_quote("601318.SH", 50),
            }
            initial = MonitorService(self._config(state_file, 1000))
            contexts, groups = initial._migration_contexts(quotes, 100000, True)
            self.assertEqual(contexts["600362.SH"]["position_ceiling"], 0.65)
            self.assertEqual(groups["legacy_group"], 0.90)

            reduced = MonitorService(self._config(state_file, 900))
            contexts, groups = reduced._migration_contexts(quotes, 94000, True)
            reduced_position_ceiling = contexts["600362.SH"]["position_ceiling"]
            reduced_group_ceiling = groups["legacy_group"]
            self.assertAlmostEqual(reduced_position_ceiling, 54000 / 94000 + 0.03)
            self.assertAlmostEqual(reduced_group_ceiling, 79000 / 94000 + 0.03)

            increased = MonitorService(self._config(state_file, 1000))
            contexts, groups = increased._migration_contexts(quotes, 100000, True)
            self.assertAlmostEqual(
                contexts["600362.SH"]["position_ceiling"], reduced_position_ceiling
            )
            self.assertAlmostEqual(groups["legacy_group"], reduced_group_ceiling)

    def test_dry_run_migration_context_does_not_persist_ratchet(self):
        with TemporaryDirectory() as folder:
            state_file = Path(folder) / "state.json"
            quotes = {
                "600362.SH": make_quote("600362.SH", 60),
                "601318.SH": make_quote("601318.SH", 50),
            }
            service = MonitorService(self._config(state_file, 900))
            service._migration_contexts(
                quotes, 94000, True, allow_state_update=False
            )
            self.assertFalse(state_file.exists())
            self.assertEqual(service.state.migration_state()["positions"], {})


if __name__ == "__main__":
    unittest.main()
