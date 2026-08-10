from datetime import date, datetime
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from astock_bot.evidence import (
    OfficialEvidenceCollector,
    assess_corporate_action,
    classify_corporate_action_title,
    classify_operating_text,
    classify_announcement_title,
    parse_corporate_action_terms,
    parse_cash_dividend_implementation,
    parse_chinabond_curve,
    parse_miit_column_query,
    parse_miit_listing,
    parse_margin_observations,
    parse_nev_yoy,
    parse_optical_communications_yoy,
    parse_semiconductor_yoy,
    parse_shfe_copper_warrant,
)
from astock_bot.models import EvidenceItem, Position, SatellitePosition


TZ = ZoneInfo("Asia/Shanghai")


class EvidenceTests(unittest.TestCase):
    def test_cash_dividend_implementation_requires_all_settlement_terms(self):
        terms = parse_cash_dividend_implementation(
            "股权登记日：2026年8月6日；除权(息)日：2026年8月7日；"
            "现金红利发放日：2026年8月7日。每10股派发现金红利20.60元(含税)。"
        )
        self.assertEqual(terms["record_date"], "2026-08-06")
        self.assertEqual(terms["ex_date"], "2026-08-07")
        self.assertEqual(terms["payment_date"], "2026-08-07")
        self.assertEqual(terms["cash_per_share"], 2.06)
        with self.assertRaisesRegex(Exception, "缺少可核验"):
            parse_cash_dividend_implementation("股权登记日：2026年8月6日；每股派2元")

    def test_collector_reads_only_verified_dividend_implementation_pdf(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "dividends": {"lookback_calendar_days": 180, "max_pages": 1},
        })
        collector._get_json = lambda *_: {"pageHelp": {"data": [{
            "TITLE": "2025年年度权益分派实施公告",
            "ADDDATE": "2026-08-05 18:00:00",
            "URL": "/disclosure/dividend.pdf",
        }]}}
        collector._pdf_text = lambda *_: (
            "股权登记日：2026年8月6日；除权(息)日：2026年8月7日；"
            "现金红利发放日：2026年8月7日；每股派发现金红利2.06元(含税)。"
        )
        position = Position(
            "601336.SH", "新华保险", 900, 70000, "insurance", 100, 100, (), SatellitePosition(),
        )
        events, warnings = collector.collect_cash_dividend_events(
            (position,), datetime(2026, 8, 7, 15, 30, tzinfo=TZ),
        )
        self.assertEqual(warnings, [])
        self.assertEqual(events["601336.SH"][0]["cash_per_share"], 2.06)

    def test_capital_flow_falls_back_after_an_empty_endpoint_response(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {})
        requested_urls = []

        def get_json(url, _referer):
            requested_urls.append(url)
            if len(requested_urls) == 1:
                return {"rc": 100, "data": {}}
            return {
                "rc": 0,
                "data": {
                    "klines": [
                        "2026-08-07,123456789,0,0,0,0,2.35,0,0,0,0,0,0,0,0"
                    ]
                },
            }

        collector._get_json = get_json
        observed, main_net, main_pct = collector._fetch_today_main_flow("600362.SH")
        self.assertEqual(observed, date(2026, 8, 7))
        self.assertEqual(main_net, 123456789.0)
        self.assertEqual(main_pct, 2.35)
        self.assertEqual(len(requested_urls), 2)
        self.assertTrue(all("/fflow/kline/get" in url for url in requested_urls))
        self.assertTrue(all("ut=fa5fd1943c7b386f172d6893dbfba10b" in url for url in requested_urls))

    def test_capital_flow_accepts_the_current_compact_kline_format(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {})
        collector._get_json = lambda *_: {
            "rc": 0,
            "data": {
                "klines": [
                    "2026-08-10,-258254688.0,191242944.0,67011744.0,-106547792.0,-151706896.0"
                ]
            },
        }
        observed, main_net, main_pct = collector._fetch_today_main_flow("600362.SH")
        self.assertEqual(observed, date(2026, 8, 10))
        self.assertEqual(main_net, -258254688.0)
        self.assertIsNone(main_pct)

    def test_parse_miit_column_and_listing(self):
        page = """
        <script id="x" url="/api/list" queryData="{'pageId':'abc','tagId':'当前栏目_list'}"></script>
        """
        api_url, query = parse_miit_column_query(page, "https://www.miit.gov.cn/a/index.html")
        self.assertEqual(api_url, "https://www.miit.gov.cn/api/list")
        self.assertEqual(query["pageId"], "abc")
        listing = """
        <li><a href="/a/new.html" title="2026年6月汽车工业经济运行情况">摘要</a>
        <span>2026-07-09</span></li>
        """
        self.assertEqual(
            parse_miit_listing(listing, "https://www.miit.gov.cn/a/index.html"),
            [(date(2026, 7, 9), "2026年6月汽车工业经济运行情况", "https://www.miit.gov.cn/a/new.html")],
        )

    def test_parse_new_energy_vehicle_and_semiconductor_metrics(self):
        self.assertEqual(
            parse_nev_yoy(
                "6月，新能源汽车产销分别完成159.8万辆和164.3万辆，"
                "同比增长26%和23.6%；新能源汽车新车销量占比58.5%。"
            ),
            (26.0, 23.6),
        )
        self.assertEqual(
            parse_nev_yoy(
                "2月，新能源汽车产销分别完成69.4万辆和76.5万辆，"
                "同比下降21.8%和14.2%。"
            ),
            (-21.8, -14.2),
        )
        self.assertEqual(
            parse_semiconductor_yoy(
                "规模以上电子信息制造业增加值同比增长14%。"
                "主要产品中，集成电路产量1769.7亿块，同比增长24.7%。"
            ),
            (24.7, 14.0),
        )
        self.assertEqual(
            parse_optical_communications_yoy(
                "电信业务收入累计完成9055亿元，同比下降2.1%。"
                "按照上年不变价计算的电信业务总量同比增长7.7%。"
                "全国光缆线路总长度达到7612万公里，同比增长3.2%。"
            ),
            (-2.1, 7.7, 3.2),
        )

    def test_new_industry_routes_are_conservative_and_directional(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "new_energy_vehicle": {"max_age_calendar_days": 45},
            "semiconductor": {"max_age_calendar_days": 75},
            "optical_communications": {"max_age_calendar_days": 45},
            "satellite_communications": {"max_age_calendar_days": 120},
        })
        as_of = datetime(2026, 8, 2, 10, 15, tzinfo=TZ)
        collector._latest_miit_article = lambda *_: (
            date(2026, 7, 9),
            "2026年6月汽车工业经济运行情况",
            "https://www.miit.gov.cn/nev.html",
            "新能源汽车产销分别完成159.8万辆和164.3万辆，同比增长26%和23.6%。",
        )
        nev = collector._miit_new_energy_vehicle(as_of)
        self.assertEqual((nev.direction, nev.freshness, nev.strength), (1, "fresh", 2))

        collector._latest_miit_article = lambda *_: (
            date(2026, 5, 29),
            "2026年1—4月电子信息制造业运行情况",
            "https://www.miit.gov.cn/chip.html",
            "规模以上电子信息制造业增加值同比增长14%。"
            "集成电路产量1769.7亿块，同比增长24.7%。",
        )
        chip = collector._miit_semiconductor(as_of)
        self.assertEqual((chip.direction, chip.freshness, chip.strength), (1, "fresh", 2))

        collector._latest_miit_article = lambda *_: (
            date(2026, 7, 31),
            "2026年上半年通信业经济运行情况",
            "https://www.miit.gov.cn/telecom.html",
            "电信业务收入累计完成9055亿元，同比下降2.1%。"
            "按照上年不变价计算的电信业务总量同比增长7.7%。"
            "全国光缆线路总长度达到7612万公里，同比增长3.2%。",
        )
        optical = collector._miit_optical_communications(as_of)
        self.assertEqual(
            (optical.direction, optical.freshness, optical.strength),
            (1, "fresh", 1),
        )

        collector._latest_miit_article = lambda *_: (
            date(2026, 6, 17),
            "我国成功发射卫星互联网低轨22组卫星",
            "https://www.miit.gov.cn/satellite.html",
            "卫星顺利进入预定轨道，发射任务获得圆满成功。",
        )
        satellite = collector._miit_satellite_communications(as_of)
        self.assertEqual((satellite.direction, satellite.freshness), (1, "fresh"))

    def test_etf_skips_company_announcement_gate_but_keeps_industry_gate(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {"margin_financing": {"enabled": False}})
        as_of = datetime(2026, 8, 2, 10, 15, tzinfo=TZ)
        industry_item = EvidenceItem(
            key="chip-sector",
            label="半导体产业",
            source="工业和信息化部",
            source_url="https://www.miit.gov.cn/",
            observed_at=as_of,
            direction=1,
            strength=1,
            summary="半导体产业改善",
        )
        collector._industry_sources = lambda *_: [("半导体", lambda: industry_item, True)]
        collector._announcements = lambda *_: self.fail("ETF不应调用个股公告路由")
        position = Position(
            symbol="159995.SZ",
            name="华夏国证半导体芯片ETF",
            main_shares=0,
            economic_basis=0,
            sector="semiconductor",
            satellite_limit=100,
            main_adjustment_shares=1000,
            peers=(),
            satellite=SatellitePosition(),
            role="watchlist",
        )
        evidence, warnings = collector.collect((position,), as_of)
        self.assertEqual(warnings, [])
        self.assertEqual(evidence[position.symbol].announcement_status, "not_applicable")
        self.assertTrue(evidence[position.symbol].add_ready)

    def test_parse_chinabond_long_end_curve(self):
        html = """
        <div id="gjqxData"><table>
          <tr><td rowspan="9">2026-07-28</td><td>3月</td><td>1.14</td><td>0.09</td></tr>
          <tr><td>10年</td><td>1.74</td><td>0.55</td><td>0.2</td><td>0.1</td></tr>
          <tr><td>30年</td><td>2.19</td><td>0.60</td><td>-2.4</td><td>22.0</td></tr>
        </table></div>
        """
        source_date, points = parse_chinabond_curve(html)
        self.assertEqual(source_date.isoformat(), "2026-07-28")
        self.assertEqual(points[10], (1.74, 0.55, 0.2, 0.1))
        self.assertEqual(points[30], (2.19, 0.60, -2.4, 22.0))

    def test_classify_announcement_title_is_conservative(self):
        self.assertEqual(classify_announcement_title("关于收到立案调查告知书的公告"), ("critical", -1, 2))
        self.assertEqual(classify_announcement_title("关于股东减持计划的公告"), ("caution", -1, 1))
        self.assertEqual(classify_announcement_title("2026年半年度业绩预增公告"), ("none", 0, 0))

    def test_szse_structured_announcements_feed_the_same_risk_gate(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "announcements": {"lookback_calendar_days": 14, "max_pages": 2},
            "corporate_actions": {"enabled": False},
        })
        requests = []

        def post_json(url, payload, referer):
            requests.append((url, payload, referer))
            return {
                "data": [{
                    "title": "关于收到立案调查告知书的公告",
                    "publishTime": "2026-08-07 18:00:00",
                    "attachPath": "/disclosure/notice/002865_20260807.pdf",
                }],
                "announceCount": 1,
            }

        collector._post_json = post_json
        items = collector._announcements("002865.SZ", datetime(2026, 8, 8, 10, 15, tzinfo=TZ))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "深圳证券交易所")
        self.assertTrue(items[0].source_url.startswith("https://disc.static.szse.cn/"))
        self.assertEqual(items[0].direction, -1)
        self.assertEqual(requests[0][1]["stock"], ["002865"])
        self.assertEqual(requests[0][1]["channelCode"], ["listedNotice_disc"])
        self.assertEqual(requests[0][1]["seDate"], ["2026-07-25", "2026-08-08"])

    def test_corporate_action_title_is_narrow_and_lifecycle_aware(self):
        self.assertEqual(
            classify_corporate_action_title("关于董事长提议公司回购股份的公告"),
            ("share_repurchase", "proposal"),
        )
        self.assertEqual(
            classify_corporate_action_title("关于以集中竞价方式回购股份的预案"),
            ("share_repurchase", "plan"),
        )
        self.assertEqual(
            classify_corporate_action_title("关于首次回购公司股份的公告"),
            ("share_repurchase", "first_execution"),
        )
        self.assertEqual(
            classify_corporate_action_title("关于回购股份实施结果暨股份变动公告"),
            ("share_repurchase", "completed"),
        )
        self.assertIsNone(
            classify_corporate_action_title("关于回购注销部分限制性股票的公告")
        )

    def test_verified_repurchase_terms_and_strength(self):
        terms = parse_corporate_action_terms(
            "公司拟使用自有资金回购股份，回购资金总额不低于5000万元且不超过1亿元，"
            "回购价格不超过50.00元/股，用于注销并减少注册资本。",
            "share_repurchase",
            "plan",
        )
        self.assertEqual(terms["amount_min"], 50_000_000)
        self.assertEqual(terms["amount_max"], 100_000_000)
        self.assertEqual(terms["price_cap"], 50.0)
        self.assertEqual(terms["purpose"], "cancel")
        self.assertEqual(
            assess_corporate_action("share_repurchase", "plan", terms),
            (1, 2),
        )
        incentive_terms = parse_corporate_action_terms(
            "预计回购金额 10,000万元~20,000万元 回购价格上限 37.23元/股 "
            "回购用途 □减少注册资本 √用于员工持股计划或股权激励 "
            "□为维护公司价值及股东权益。其他条款提到依法注销未使用股份。",
            "share_repurchase",
            "plan",
        )
        self.assertEqual(incentive_terms["amount_min"], 100_000_000)
        self.assertEqual(incentive_terms["amount_max"], 200_000_000)
        self.assertEqual(incentive_terms["purpose"], "employee_incentive")
        self.assertEqual(
            assess_corporate_action("share_repurchase", "plan", incentive_terms),
            (1, 1),
        )

    def test_repurchase_lifecycle_is_deduplicated_and_body_required(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "retries": 0,
            "announcements": {"lookback_calendar_days": 45},
            "corporate_actions": {
                "enabled": True,
                "sse_archive_lookahead_days": 1,
                "same_event_window_calendar_days": 2,
                "minimum_body_characters": 20,
            },
        })
        requested_urls = []

        def payload(url, _):
            requested_urls.append(url)
            return {"pageHelp": {"data": [
                {
                    "TITLE": "关于董事长提议公司回购股份的公告",
                    "ADDDATE": "2026-08-03 17:07:25",
                    "URL": "/disclosure/proposal.pdf",
                },
                {
                    "TITLE": "关于以集中竞价方式回购股份的预案",
                    "ADDDATE": "2026-08-03 17:07:24",
                    "URL": "/disclosure/plan.pdf",
                },
            ], "pageCount": 1}}

        collector._get_json = payload
        collector._pdf_text = lambda *_: (
            "公司拟使用自有资金回购股份，回购资金总额不低于5000万元且不超过1亿元，"
            "回购价格不超过50元/股，目的为维护公司价值及股东权益。"
        )
        items = collector._announcements(
            "603596.SH", datetime(2026, 8, 3, 19, 30, tzinfo=TZ)
        )
        actions = [item for item in items if item.fact_type == "positive_corporate_action"]
        self.assertEqual(len(actions), 1)
        self.assertIn("/plan/verified", actions[0].label)
        self.assertEqual((actions[0].direction, actions[0].strength), (1, 2))
        self.assertIn("endDate=2026-08-04", requested_urls[0])

        collector._pdf_text = lambda *_: (_ for _ in ()).throw(
            RuntimeError("校验页")
        )
        items = collector._announcements(
            "603596.SH", datetime(2026, 8, 3, 19, 30, tzinfo=TZ)
        )
        actions = [item for item in items if item.fact_type == "corporate_action_candidate"]
        self.assertEqual(len(actions), 1)
        self.assertEqual((actions[0].direction, actions[0].strength), (0, 0))
        self.assertIn("暂不计正向确认", actions[0].summary)

    def test_sse_pdf_uses_official_big5_mirror_after_validation_page(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {"retries": 0})
        requested = []

        def request(url, _):
            requested.append(url)
            return b"<html>validation</html>" if len(requested) == 1 else b"%PDF-fake"

        class Page:
            def extract_text(self):
                return "回购公告正文"

        class Reader:
            def __init__(self, _):
                self.pages = [Page()]

        collector._request = request
        with patch("astock_bot.evidence.PdfReader", Reader):
            text = collector._pdf_text(
                "https://www.sse.com.cn/disclosure/example.pdf", 2
            )
        self.assertEqual(text, "回购公告正文")
        self.assertTrue(requested[1].startswith("https://big5.sse.com.cn/"))

    def test_shfe_copper_selects_highest_open_interest_contract(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "retries": 0,
            "copper": {"positive_change_ratio": 0.003, "negative_change_ratio": -0.003},
        })
        def payload(url, _):
            settlement = 100500 if "20260728" in url else (100000 if "20260727" in url else 99500)
            return {"o_curinstrument": [
                    {
                        "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2608", "OPENINTEREST": 100,
                        "PRESETTLEMENTPRICE": 100000, "ZD1_CHG": -100, "SETTLEMENTPRICE": 100000,
                    },
                    {
                        "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2609", "OPENINTEREST": 200,
                        "PRESETTLEMENTPRICE": 100000, "ZD1_CHG": 500, "SETTLEMENTPRICE": settlement,
                    },
                ]}
        collector._get_json = payload
        item = collector._shfe_copper(datetime(2026, 7, 29, 10, 15, tzinfo=TZ))
        self.assertEqual(item.direction, 1)
        self.assertIn("CU2609", item.summary)
        self.assertIn("+0.50%", item.summary)
        self.assertIn("3日", item.summary)

    def test_chinabond_source_builds_insurance_evidence_item(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "retries": 0,
            "insurance": {"positive_daily_bp": 1.0, "positive_monthly_bp": 0.0},
        })
        collector._get_text = lambda *_: """
        <div id="gjqxData"><table>
          <tr><td rowspan="9">2026-07-28</td><td>3月</td><td>1.14</td><td>0.09</td></tr>
          <tr><td>10年</td><td>1.74</td><td>1.20</td><td>0.4</td><td>0.1</td></tr>
          <tr><td>30年</td><td>2.19</td><td>1.40</td><td>0.6</td><td>22.0</td></tr>
        </table></div>
        """
        item = collector._chinabond_long_rates(
            datetime(2026, 7, 29, 10, 15, tzinfo=TZ), "insurance"
        )
        self.assertEqual(item.direction, 1)
        self.assertEqual(item.freshness, "fresh")
        self.assertIn("寿险利率环境", item.summary)

    def test_sse_announcement_risk_window_and_metadata(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "retries": 0,
            "announcements": {"lookback_calendar_days": 14, "risk_window_calendar_days": 7},
        })
        collector._get_json = lambda *_: {
            "pageHelp": {"data": [
                {
                    "TITLE": "关于收到行政处罚决定书的公告",
                    "ADDDATE": "2026-07-28 18:30:00",
                    "URL": "/disclosure/example.pdf",
                },
                {
                    "TITLE": "关于股东减持计划的公告",
                    "ADDDATE": "2026-07-01 18:30:00",
                    "URL": "/disclosure/old.pdf",
                },
            ]}
        }
        items = collector._announcements("600362.SH", datetime(2026, 7, 29, 10, 15, tzinfo=TZ))
        self.assertTrue(items[0].label.endswith("/caution"))
        self.assertEqual(items[1].direction, 0)
        self.assertTrue(items[0].source_url.startswith("https://www.sse.com.cn/"))

    def test_parse_shfe_copper_warrant_prefers_total(self):
        payload = {"o_cursor": [
            {"VARNAME": "铜", "WHABBRNAME": "仓库A", "WRTWGHTS": "1,200"},
            {"VARNAME": "铜", "WHABBRNAME": "总计", "WRTWGHTS": "3,500"},
            {"VARNAME": "铝", "WHABBRNAME": "总计", "WRTWGHTS": "8,000"},
        ]}
        self.assertEqual(parse_shfe_copper_warrant(payload), 3500)

    def test_operating_text_classification_is_directional_and_mixed_conservative(self):
        self.assertEqual(classify_operating_text("保费收入同比增长 8.2%", 3)[0], 1)
        self.assertEqual(classify_operating_text("净利润同比下降 6.0%", 3)[0], -1)
        self.assertEqual(
            classify_operating_text("收入同比增长 8.2%，净利润同比下降 6.0%", 3)[0],
            0,
        )
        self.assertEqual(classify_operating_text("营业成本同比增长 8.2%", 3)[0], -1)
        self.assertEqual(classify_operating_text("综合成本率同比下降 4.0%", 3)[0], 1)

    def test_parse_margin_observations_handles_sse_envelope_and_aliases(self):
        payload = {"pageHelp": {"data": [
            {"opDate": "2026-07-30", "rzye": "1,100", "rzmre": "120", "rzche": "90"},
            {"opDate": "2026-07-29", "rzye": "1,000", "rzmre": "100", "rzche": "80"},
        ]}}
        rows = parse_margin_observations(payload)
        self.assertEqual([item[0].isoformat() for item in rows], ["2026-07-29", "2026-07-30"])
        self.assertEqual(rows[-1][1:], (1100.0, 120.0, 90.0))

    def test_operating_announcement_pdf_adds_company_metric_item(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "retries": 0,
            "announcements": {
                "lookback_calendar_days": 45,
                "risk_window_calendar_days": 7,
                "operating_keywords": ["保费收入"],
                "max_operating_documents": 1,
            },
        })
        collector._get_json = lambda *_: {"pageHelp": {"data": [{
            "TITLE": "2026年上半年保费收入公告",
            "ADDDATE": "2026-07-28 18:30:00",
            "URL": "/disclosure/premium.pdf",
        }]}}
        collector._pdf_text = lambda *_: "原保险保费收入同比增长 7.5%"
        items = collector._announcements(
            "601336.SH", datetime(2026, 7, 29, 10, 15, tzinfo=TZ)
        )
        operating = [item for item in items if item.fact_type == "company_operating_metric"]
        self.assertEqual(len(operating), 1)
        self.assertEqual(operating[0].direction, 1)


if __name__ == "__main__":
    unittest.main()
