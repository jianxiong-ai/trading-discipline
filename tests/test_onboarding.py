import os
import unittest
from unittest.mock import patch

from astock_bot.onboarding import StockOnboardingService


def profile(**overrides):
    value = {
        "name": "伯特利",
        "full_name": "芜湖伯特利汽车安全系统股份有限公司",
        "industry": "汽车零部件",
        "business": "汽车制动系统、智能驾驶与线控底盘产品研发制造",
        "description": "面向整车厂提供汽车安全系统产品",
        "source": "测试公司资料",
        "as_of": "2026-08-09",
    }
    value.update(overrides)
    return value


class StockOnboardingTests(unittest.TestCase):
    def test_rule_onboarding_builds_full_research_profile(self):
        service = StockOnboardingService(profile_fetcher=lambda symbol: profile())
        result = service.onboard("603596")
        self.assertEqual(result.symbol, "603596.SH")
        self.assertEqual(result.name, "伯特利")
        self.assertEqual(result.sector, "new_energy_vehicle")
        self.assertEqual(result.analysis_profile["coverage"], "full")
        self.assertEqual(result.analysis_profile["classification_source"], "rule")
        self.assertIn("新能源车产销", result.analysis_profile["evidence_route"])
        self.assertTrue(result.peers)

    def test_unknown_industry_is_kept_as_basic_tracking(self):
        service = StockOnboardingService(
            profile_fetcher=lambda symbol: profile(
                name="测试公司", industry="综合行业", business="未匹配的业务", description=""
            )
        )
        result = service.onboard("600000")
        self.assertEqual(result.sector, "generic")
        self.assertEqual(result.analysis_profile["coverage"], "basic")
        self.assertIn("不触发首次建仓", result.analysis_profile["signal_policy"])
        self.assertEqual(result.commodity_exposures, [])

    def test_copper_onboarding_maps_company_exposure_and_option_auxiliary(self):
        service = StockOnboardingService(
            profile_fetcher=lambda symbol: profile(
                name="江西铜业",
                full_name="江西铜业股份有限公司",
                industry="铜矿采选与铜冶炼",
                business="铜矿山、阴极铜冶炼、铜杆加工，并开展套期保值",
                description="覆盖资源、冶炼和加工环节",
            )
        )
        result = service.onboard("600362.SH")
        self.assertEqual(result.sector, "copper")
        self.assertEqual(result.commodity_exposures[0]["option_product"], "cu_o")
        self.assertEqual(
            result.commodity_exposures[0]["exposure_types"],
            ["mining", "smelting", "processing"],
        )
        self.assertTrue(result.commodity_exposures[0]["hedge_disclosed"])
        self.assertEqual(
            result.analysis_profile["related_derivatives"][0]["role"], "auxiliary"
        )
        self.assertIn("沪铜期权辅助", result.analysis_profile["evidence_route"])

    def test_gold_miner_maps_gold_sector_and_option(self):
        service = StockOnboardingService(
            profile_fetcher=lambda symbol: profile(
                name="山东黄金",
                full_name="山东黄金矿业股份有限公司",
                industry="黄金",
                business="金矿采选、黄金冶炼",
                description="",
            )
        )
        result = service.onboard("600547.SH")
        self.assertEqual(result.sector, "gold")
        self.assertEqual(
            [item["commodity"] for item in result.commodity_exposures],
            ["gold"],
        )
        self.assertEqual(result.analysis_profile["related_derivatives"][0]["label"], "沪金期权")
        self.assertIn("沪金期货", result.analysis_profile["evidence_route"])

    def test_silver_miner_maps_silver_sector_and_option(self):
        service = StockOnboardingService(
            profile_fetcher=lambda symbol: profile(
                name="盛达资源",
                full_name="盛达金属资源股份有限公司",
                industry="白银",
                business="银矿采选、银冶炼",
                description="",
            )
        )
        result = service.onboard("000603.SZ")
        self.assertEqual(result.sector, "silver")
        self.assertEqual(
            [item["commodity"] for item in result.commodity_exposures],
            ["silver"],
        )
        self.assertEqual(result.analysis_profile["related_derivatives"][0]["label"], "沪银期权")

    def test_diversified_miner_maps_copper_and_gold_options(self):
        service = StockOnboardingService(
            profile_fetcher=lambda symbol: profile(
                name="紫金矿业",
                full_name="紫金矿业集团股份有限公司",
                industry="有色金属",
                business="铜矿采选;铜冶炼;金矿采选;金冶炼",
                description="紫金山大型金铜矿",
            )
        )
        result = service.onboard("601899.SH")
        self.assertEqual(result.sector, "copper")
        self.assertEqual(
            [item["commodity"] for item in result.commodity_exposures],
            ["copper", "gold"],
        )
        labels = [item["label"] for item in result.analysis_profile["related_derivatives"]]
        self.assertEqual(labels, ["沪铜期权", "沪金期权"])

    def test_molybdenum_miner_still_gets_copper_option_auxiliary(self):
        service = StockOnboardingService(
            profile_fetcher=lambda symbol: profile(
                name="洛阳钼业",
                industry="小金属",
                business="钨钼系列产品",
                description="是全球领先的铜、钴、钼、钨、铌生产商",
            )
        )
        result = service.onboard("603993.SH")
        self.assertEqual(result.sector, "generic")
        self.assertEqual(result.commodity_exposures[0]["commodity"], "copper")
        self.assertEqual(result.analysis_profile["related_derivatives"][0]["label"], "沪铜期权")

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key"})
    def test_optional_llm_can_review_but_only_with_allowed_sector(self):
        service = StockOnboardingService(
            profile_fetcher=lambda symbol: profile(industry="通信设备", business="光纤光缆与海缆"),
            llm_classifier=lambda source, rule: {
                "sector": "optical_communications",
                "confidence": "high",
                "peers": ["600498.SH", "bad-code"],
                "business_summary": "光纤光缆与海缆供应商",
                "drivers": ["通信投资"],
                "risks": ["资本开支波动"],
                "monitoring_topics": ["通信业月报"],
                "rationale": "主营与光通信证据路由匹配",
            },
        )
        result = service.onboard("600487.SH", use_llm=True)
        self.assertEqual(result.sector, "optical_communications")
        self.assertEqual(result.analysis_profile["classification_source"], "llm_review")
        self.assertEqual(result.peers, ["600498.SH", "601869.SH", "600522.SH"])
        self.assertIn("行业与同行仍使用规则路径", result.analysis_profile["llm_status"])

    @patch.dict(os.environ, {"LLM_API_KEY": "test-key"})
    def test_llm_cannot_override_trading_sector_or_peers(self):
        service = StockOnboardingService(
            profile_fetcher=lambda symbol: profile(industry="汽车零部件"),
            llm_classifier=lambda source, rule: {
                "sector": "semiconductor", "confidence": "high", "peers": ["688981.SH"],
                "business_summary": "模型建议的研究摘要", "drivers": ["订单"],
                "risks": ["竞争"], "monitoring_topics": ["公告"], "rationale": "测试",
            },
        )
        result = service.onboard("603596.SH", use_llm=True)
        self.assertEqual(result.sector, "new_energy_vehicle")
        self.assertNotIn("688981.SH", result.peers)

    @patch.dict(os.environ, {"LLM_BASE_URL": "https://api.deepseek.com/v1/", "LLM_MODEL": "deepseek-v4-flash"})
    def test_deepseek_responses_endpoint_is_normalized(self):
        service = StockOnboardingService(profile_fetcher=lambda symbol: profile())
        self.assertEqual(service.llm_responses_url, "https://api.deepseek.com/v1/responses")
        self.assertEqual(service.llm_model, "deepseek-v4-flash")

    def test_manual_fallback_works_when_profile_source_fails(self):
        def fail(symbol):
            raise OSError("timeout")

        service = StockOnboardingService(profile_fetcher=fail)
        result = service.onboard("600487", manual_name="亨通光电", manual_sector="optical_communications")
        self.assertEqual(result.name, "亨通光电")
        self.assertEqual(result.analysis_profile["classification_source"], "manual")
        self.assertTrue(result.analysis_profile["profile_error"])


if __name__ == "__main__":
    unittest.main()
