import unittest

from astock_bot.config import _validate_config


class CommodityOptionConfigTests(unittest.TestCase):
    @staticmethod
    def _raw(**overrides):
        options = {
            "enabled": True,
            "max_age_calendar_days": 4,
            "min_days_to_expiry": 7,
            "max_days_to_expiry": 90,
            "max_moneyness_ratio": 0.12,
            "min_volume": 5,
            "min_open_interest": 50,
            "minimum_paired_strikes": 3,
            "risk_free_rate": 0.015,
        }
        options.update(overrides)
        return {"evidence": {"commodity_options": options}}

    def test_valid_option_window_passes(self):
        _validate_config(self._raw(), [])

    def test_disabled_option_layer_preserves_old_config_compatibility(self):
        _validate_config({"evidence": {"commodity_options": {"enabled": False}}}, [])
        _validate_config({}, [])

    def test_invalid_option_windows_and_filters_are_rejected(self):
        invalid = (
            ({"min_days_to_expiry": 90, "max_days_to_expiry": 7}, "到期期限"),
            ({"max_moneyness_ratio": 0.0}, "max_moneyness_ratio"),
            ({"min_volume": -1}, "min_volume"),
            ({"min_open_interest": -1}, "min_open_interest"),
            ({"minimum_paired_strikes": 0}, "minimum_paired_strikes"),
            ({"max_age_calendar_days": 0}, "max_age_calendar_days"),
            ({"risk_free_rate": 0.25}, "risk_free_rate"),
            ({"volatility_expansion_threshold": 0}, "volatility_expansion_threshold"),
            ({"put_call_low": 1.5, "put_call_high": 1.2}, "Put/Call"),
        )
        for overrides, message in invalid:
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(ValueError, message):
                    _validate_config(self._raw(**overrides), [])

    def test_invalid_option_routes_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "futures_product 不能为空"):
            _validate_config(self._raw(routes={"gold": {"exchange": "SHFE"}}), [])

    def test_gold_and_silver_trend_thresholds_are_validated(self):
        with self.assertRaisesRegex(ValueError, "evidence.gold 涨跌阈值顺序无效"):
            _validate_config(
                {
                    "evidence": {
                        "gold": {
                            "negative_change_ratio": 0.01,
                            "positive_change_ratio": 0.003,
                        }
                    }
                },
                [],
            )


if __name__ == "__main__":
    unittest.main()
