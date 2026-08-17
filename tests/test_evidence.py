from datetime import date, datetime
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from astock_bot.evidence import (
    OfficialEvidenceCollector,
    assess_corporate_action,
    classify_corporate_action_title,
    classify_operating_text,
    classify_announcement_title,
    commodity_exposure_option_note,
    finalize_commodity_option_summary,
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
    analyze_shfe_copper_option_chain,
    parse_shfe_option_contracts,
)
from astock_bot.models import (
    CommodityOptionEvidence,
    EquityEvidence,
    EvidenceItem,
    Position,
    SatellitePosition,
)
from astock_bot.strategy import _auxiliary_allows_entry


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

    def test_shfe_gold_selects_highest_open_interest_contract(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "retries": 0,
            "gold": {"positive_change_ratio": 0.003, "negative_change_ratio": -0.003},
        })
        def payload(url, _):
            settlement = 100500 if "20260728" in url else (100000 if "20260727" in url else 99500)
            return {"o_curinstrument": [
                    {
                        "PRODUCTGROUPID": "au", "DELIVERYMONTH": "2608", "OPENINTEREST": 100,
                        "PRESETTLEMENTPRICE": 100000, "ZD1_CHG": -100, "SETTLEMENTPRICE": 100000,
                    },
                    {
                        "PRODUCTGROUPID": "au", "DELIVERYMONTH": "2609", "OPENINTEREST": 200,
                        "PRESETTLEMENTPRICE": 100000, "ZD1_CHG": 500, "SETTLEMENTPRICE": settlement,
                    },
                    {
                        "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2609", "OPENINTEREST": 999,
                        "PRESETTLEMENTPRICE": 100000, "ZD1_CHG": 500, "SETTLEMENTPRICE": settlement,
                    },
                ]}
        collector._get_json = payload
        item = collector._shfe_futures_trend(
            datetime(2026, 7, 29, 10, 15, tzinfo=TZ), "au", "gold", "沪金"
        )
        self.assertEqual(item.direction, 1)
        self.assertIn("AU2609", item.summary)
        self.assertIn("沪金", item.label)

    def test_gold_industry_sources_require_futures_not_warrants(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {"cache_enabled": False})
        sources = collector._industry_sources("gold", datetime(2026, 7, 29, 10, 15, tzinfo=TZ))
        self.assertEqual([item[0] for item in sources], ["沪金行情"])
        self.assertTrue(sources[0][2])

    def test_copper_option_chain_filters_far_otm_premium_spike(self):
        option_rows = []
        strikes = [90, 95, 100, 105, 110]
        call_prices = [10.3, 5.9, 2.2, 0.85, 0.25]
        put_prices = [0.25, 0.9, 2.1, 5.9, 10.3]
        previous_calls = [9.5, 5.0, 1.8, 0.6, 0.2]
        previous_puts = [0.3, 1.0, 2.8, 6.6, 11.2]
        call_deltas = [0.9, 0.75, 0.5, 0.25, 0.1]
        put_deltas = [-0.1, -0.25, -0.5, -0.75, -0.9]
        for strike, call, put, previous_call, previous_put, call_delta, put_delta in zip(
            strikes, call_prices, put_prices, previous_calls, previous_puts,
            call_deltas, put_deltas,
        ):
            for option_type, price, previous, delta in (
                ("1", call, previous_call, call_delta),
                ("2", put, previous_put, put_delta),
            ):
                side = "C" if option_type == "1" else "P"
                option_rows.append({
                    "INSTRUMENTID": f"cu2609{side}{strike}",
                    "UNDERLYINGINSTRID": "cu2609",
                    "PRODUCTGROUPID": "cu",
                    "PRODUCTID": "cu_o",
                    "OPTIONSTYPE": option_type,
                    "STRIKEPRICE": strike,
                    "SETTLEMENTPRICE": price,
                    "PRESETTLEMENTPRICE": previous,
                    "VOLUME": 100,
                    "OPENINTEREST": 500,
                    "OPENINTERESTCHG": 0,
                    "DELTA": delta,
                })
        # This is the screenshot-style trap: a deep OTM call rises many times,
        # but it is outside the near-ATM chain and must not drive the view.
        option_rows.append({
            "INSTRUMENTID": "cu2609C130",
            "UNDERLYINGINSTRID": "cu2609",
            "PRODUCTGROUPID": "cu",
            "PRODUCTID": "cu_o",
            "OPTIONSTYPE": "1",
            "STRIKEPRICE": 130,
            "SETTLEMENTPRICE": 2.0,
            "PRESETTLEMENTPRICE": 0.02,
            "VOLUME": 10000,
            "OPENINTEREST": 1000,
            "OPENINTERESTCHG": 500,
            "DELTA": 0.05,
        })
        option_payload = {"o_curinstrument": option_rows}
        future_payload = {"o_curinstrument": [{
            "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2609",
            "SETTLEMENTPRICE": 100, "PRESETTLEMENTPRICE": 99,
        }]}
        contract_payload = {"OptionContractBaseInfo": [
            {"INSTRUMENTID": row["INSTRUMENTID"], "EXPIREDATE": "20260825"}
            for row in option_rows
        ]}
        evidence = analyze_shfe_copper_option_chain(
            option_payload=option_payload,
            future_payload=future_payload,
            contract_payload=contract_payload,
            source_date=date(2026, 8, 14),
            as_of=datetime(2026, 8, 17, 10, 15, tzinfo=TZ),
            settings={
                "min_days_to_expiry": 7,
                "max_days_to_expiry": 90,
                "max_moneyness_ratio": 0.12,
                "min_volume": 5,
                "min_open_interest": 50,
                "minimum_paired_strikes": 3,
            },
            source_url="https://www.shfe.com.cn/example",
        )
        self.assertEqual(evidence.status, "fresh")
        self.assertEqual(evidence.metrics["atm_strike"], 100)
        self.assertEqual(evidence.metrics["paired_strikes"], 5)
        self.assertNotEqual(evidence.view, "upside_demand")
        self.assertEqual(evidence.item.direction, 0)
        self.assertEqual(evidence.item.strength, 0)
        self.assertIn("权利金涨跌不等同", evidence.summary)
        with TemporaryDirectory() as directory:
            collector = OfficialEvidenceCollector("Asia/Shanghai", {
                "cache_enabled": True, "cache_dir": directory,
            })
            collector._write_commodity_option_cache("2026-08-17", "copper", evidence)
            cached = collector._read_commodity_option_cache("2026-08-17", "copper")
            self.assertIsNotNone(cached)
            self.assertEqual(cached.view, evidence.view)
            self.assertEqual(cached.metrics["atm_strike"], 100)
            self.assertEqual(cached.item.direction, 0)

    def test_copper_option_chain_requires_paired_liquid_strikes(self):
        rows = [{
            "INSTRUMENTID": f"cu2609C{strike}",
            "UNDERLYINGINSTRID": "cu2609",
            "PRODUCTGROUPID": "cu",
            "PRODUCTID": "cu_o",
            "OPTIONSTYPE": "1",
            "STRIKEPRICE": strike,
            "SETTLEMENTPRICE": 2,
            "PRESETTLEMENTPRICE": 1,
            "VOLUME": 100,
            "OPENINTEREST": 500,
            "OPENINTERESTCHG": 10,
            "DELTA": 0.5,
        } for strike in (95, 100, 105)]
        evidence = analyze_shfe_copper_option_chain(
            option_payload={"o_curinstrument": rows},
            future_payload={"o_curinstrument": [{
                "PRODUCTGROUPID": "cu", "DELIVERYMONTH": "2609",
                "SETTLEMENTPRICE": 100, "PRESETTLEMENTPRICE": 99,
            }]},
            contract_payload={"OptionContractBaseInfo": [
                {"INSTRUMENTID": row["INSTRUMENTID"], "EXPIREDATE": "20260825"}
                for row in rows
            ]},
            source_date=date(2026, 8, 14),
            as_of=datetime(2026, 8, 17, 10, 15, tzinfo=TZ),
            settings={"minimum_paired_strikes": 3},
        )
        self.assertEqual(evidence.status, "partial")
        self.assertEqual(evidence.view, "unavailable")

    def test_option_auxiliary_cannot_replace_industry_gate(self):
        blocked = EquityEvidence(
            symbol="600362.SH",
            industry_status="fresh",
            industry_direction=0,
            announcement_status="fresh",
            announcement_risk="none",
            summary="测试",
            commodity_option_status="fresh",
            commodity_option_view="upside_demand",
        )
        self.assertFalse(blocked.add_ready)
        self.assertFalse(blocked.commodity_option_confirmation)
        allowed = EquityEvidence(
            symbol="600362.SH",
            industry_status="fresh",
            industry_direction=1,
            announcement_status="fresh",
            announcement_risk="none",
            summary="测试",
            commodity_option_status="fresh",
            commodity_option_view="upside_demand",
        )
        self.assertTrue(allowed.add_ready)
        self.assertTrue(allowed.commodity_option_confirmation)

    def test_gold_option_observation_does_not_confirm_without_industry_gate(self):
        gold_only = EquityEvidence(
            symbol="600547.SH",
            industry_status="missing",
            industry_direction=None,
            announcement_status="fresh",
            announcement_risk="none",
            summary="测试",
            commodity_option_status="fresh",
            commodity_option_view="upside_demand",
            commodity_option_metrics={
                "industry_linked_view": "unavailable",
                "industry_linked_status": "not_applicable",
            },
        )
        self.assertFalse(gold_only.commodity_option_confirmation)
        self.assertFalse(gold_only.commodity_option_divergence)

    def test_gold_option_confirms_when_gold_industry_gate_is_positive(self):
        gold = EquityEvidence(
            symbol="600547.SH",
            industry_status="fresh",
            industry_direction=1,
            announcement_status="fresh",
            announcement_risk="none",
            summary="测试",
            commodity_option_status="fresh",
            commodity_option_view="upside_demand",
            commodity_option_metrics={
                "industry_linked_view": "upside_demand",
                "industry_linked_status": "fresh",
            },
        )
        self.assertTrue(gold.commodity_option_confirmation)
        self.assertFalse(gold.commodity_option_divergence)

    def test_option_divergence_blocks_confirmation_and_auxiliary_gate(self):
        diverged = EquityEvidence(
            symbol="600362.SH",
            industry_status="fresh",
            industry_direction=1,
            announcement_status="fresh",
            announcement_risk="none",
            summary="测试",
            commodity_option_status="fresh",
            commodity_option_view="downside_hedging",
        )
        self.assertTrue(diverged.commodity_option_divergence)
        self.assertFalse(diverged.commodity_option_confirmation)
        self.assertFalse(_auxiliary_allows_entry(diverged))

    def test_finalize_option_summary_adds_exposure_and_divergence_notes(self):
        context = CommodityOptionEvidence(
            status="fresh",
            view="downside_hedging",
            summary="认沽保护需求偏强",
        )
        summary = finalize_commodity_option_summary(
            context,
            ({
                "commodity": "copper",
                "exposure_types": ["smelting"],
                "sensitivity": "公司偏冶炼环节：期权多头需结合加工费，不宜等同铜价上涨",
            },),
            1,
            commodity="copper",
            industry_linked=True,
        )
        self.assertIn("加工费", summary)
        self.assertIn("背离", summary)

    def test_commodity_exposure_option_note_for_mining(self):
        note = commodity_exposure_option_note(({
            "commodity": "gold",
            "exposure_types": ["mining"],
            "sensitivity": "公司偏资源端：期权/期货多头更贴近黄金价格方向，但仍需核对产量与成本",
        },), "gold")
        self.assertIn("资源端", note)

    def test_option_source_failure_does_not_degrade_copper_futures_gate(self):
        collector = OfficialEvidenceCollector("Asia/Shanghai", {
            "commodity_options": {"enabled": True},
            "cache_enabled": False,
        })
        industry_item = EvidenceItem(
            key="cu-future", label="沪铜", source="上海期货交易所", source_url="",
            observed_at=datetime(2026, 8, 14, 16, tzinfo=TZ),
            direction=1, strength=2, summary="沪铜期货趋势向上",
        )
        collector._industry_sources = lambda *_: [
            ("沪铜行情", lambda: industry_item, True)
        ]
        collector._load_shfe_option_chain = lambda *_: (_ for _ in ()).throw(
            OSError("option timeout")
        )
        position = Position(
            symbol="159999.SZ", name="铜ETF", main_shares=0, economic_basis=0,
            sector="copper", satellite_limit=0, main_adjustment_shares=100,
            peers=(), satellite=SatellitePosition(), role="watchlist",
        )
        result, warnings = collector.collect(
            (position,), datetime(2026, 8, 17, 10, 15, tzinfo=TZ)
        )
        evidence = result[position.symbol]
        self.assertEqual(evidence.industry_status, "fresh")
        self.assertEqual(evidence.industry_direction, 1)
        self.assertTrue(evidence.add_ready)
        self.assertEqual(evidence.commodity_option_status, "missing")
        self.assertTrue(any("沪铜期权辅助" in warning for warning in warnings))

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
