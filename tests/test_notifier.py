import unittest

from astock_bot.models import Signal
from astock_bot.notifier import feishu_signature, render_daily_summary, render_message


class NotifierTests(unittest.TestCase):
    def test_signature_is_stable(self):
        self.assertEqual(feishu_signature(1599360473, "demo"), feishu_signature(1599360473, "demo"))
        self.assertNotEqual(feishu_signature(1599360473, "demo"), feishu_signature(1599360474, "demo"))

    def test_action_first_message_contains_daily_change(self):
        signal = Signal(
            symbol="600362.SH", name="江西铜业", code="SAT_BUY", confidence="中",
            price=42.71, key_level=41.80, action="建立超短线卫星仓", shares=300,
            reason="接近日线支撑并出现企稳确认", invalidation="跌破41.50且板块转弱",
            event_id="internal-event", category="satellite",
            details={"change_pct": -1.13, "target": 44.00, "stop": 41.50, "planned_nav_ratio": 0.072},
        )
        text = render_message([signal], "14:15", "A股持仓纪律")
        self.assertIn("江西铜业（600362.SH） 42.71（-1.13%）", text)
        self.assertIn("建议：建立超短线卫星仓 300股", text)
        self.assertIn("计划金额约占账户资产7.2%", text)
        self.assertIn("原因：接近日线支撑并出现企稳确认", text)
        self.assertIn("参考：计划止盈价 44.00；风险退出价 41.50；条件失效：跌破41.50且板块转弱", text)
        self.assertIn("【操作建议】", text)
        self.assertIn("【触发原因】", text)
        self.assertIn("【执行参考】", text)
        self.assertIn("──────────", text)
        self.assertNotIn("事件键", text)
        self.assertNotIn("SAT_BUY", text)

    def test_risk_message_separates_satellite_and_main_shares(self):
        signal = Signal(
            symbol="600362.SH", name="江西铜业", code="EMERGENCY_RISK", confidence="高",
            price=36.0, key_level=36.0, action="先退出卫星仓，再降低主仓风险", shares=500,
            reason="触及单股25%硬风险上限", invalidation="风险状态解除后再评估",
            event_id="risk-event", category="risk",
            details={"change_pct": -3.0, "satellite_exit_shares": 300},
        )
        text = render_message([signal], "10:15", "A股持仓纪律")
        self.assertIn("建议：退出卫星仓300股；降低主仓500股", text)

    def test_daily_summary_is_sent_even_without_alerts(self):
        rows = [{
            "symbol": "600362.SH",
            "name": "江西铜业",
            "price": 42.59,
            "change_pct": 0.12,
            "recommendation": "继续持有观察，暂不加减仓",
            "reason": "价格仍在动态支撑与压力之间，未形成有效突破或破位；量能比0.96",
            "trigger_count": 0,
            "status_by_node": {
                "09:15": "基线",
                "10:15": "观察",
                "13:15": "观察",
                "14:15": "观察",
            },
        }]
        text = render_daily_summary(
            rows,
            ["09:15", "10:15", "13:15", "14:15"],
            "2026-07-29",
            "A股持仓纪律",
        )
        self.assertIn("日内总结 2026-07-29", text)
        self.assertIn("今日无操作信号", text)
        self.assertIn("江西铜业（600362.SH） 42.59（+0.12%）", text)
        self.assertIn("建议：继续持有观察，暂不加减仓", text)
        self.assertIn("09:15 基线｜10:15 观察｜13:15 观察｜14:15 观察", text)

    def test_stage_reentry_uses_target_not_sell_wording(self):
        signal = Signal(
            symbol="601318.SH", name="中国平安", code="STAGE_REENTRY", confidence="中",
            price=52.0, key_level=51.5, action="磨底确认后小幅恢复主仓", shares=100,
            reason="低位结构完成右侧确认", invalidation="跌破50.80或行业重新转弱",
            event_id="stage-reentry", category="strategy",
            details={"change_pct": 0.8, "target": 56.0, "stop": 50.8, "evidence": "长端利率与公告门控通过"},
        )
        text = render_message([signal], "14:15", "A股持仓纪律")
        self.assertIn("建议：磨底确认后小幅恢复主仓 100股", text)
        self.assertIn("计划目标价 56.00", text)
        self.assertNotIn("原计划止盈价", text)

    def test_false_break_cancellation_does_not_render_zero_shares(self):
        signal = Signal(
            symbol="601336.SH", name="新华保险", code="FALSE_BREAK", confidence="中",
            price=62.78, key_level=62.41, action="撤销此前减仓建议，恢复观察", shares=0,
            reason="完整15分钟重新站回关键位上方且同行止跌",
            invalidation="若再次有效破位再重新评估",
            event_id="false-break", category="strategy",
            details={"change_pct": -0.32, "evidence": "关键位已收复且同行止跌"},
        )
        text = render_message([signal], "13:45", "A股持仓纪律")
        self.assertIn("建议：撤销此前减仓建议，恢复观察", text)
        self.assertNotIn("0股", text)
        self.assertIn("证据：关键位已收复且同行止跌", text)

    def test_watchlist_entry_uses_first_position_wording_and_transition_reminder(self):
        signal = Signal(
            symbol="600000.SH", name="观察股票", code="WATCH_ENTRY", confidence="中",
            price=10.0, key_level=9.8, action="首次建立主仓起始档", shares=500,
            reason="磨底右侧确认且目标空间充足", invalidation="跌破9.60或证据转弱",
            event_id="watch-entry", category="strategy",
            details={
                "change_pct": 1.2, "target": 11.0, "stop": 9.6,
                "planned_nav_ratio": 0.05, "evidence": "产业与公告门控通过",
            },
        )
        text = render_message([signal], "14:15", "A股持仓纪律")
        self.assertIn("建议：首次建立主仓起始档 500股", text)
        self.assertIn("计划金额约占账户资产5.0%", text)
        self.assertIn("计划目标价 11.00", text)
        self.assertIn("将role改为holding", text)

    def test_watchlist_near_entry_is_explicitly_non_actionable(self):
        signal = Signal(
            symbol="600487.SH", name="亨通光电", code="WATCH_NEAR_ENTRY", confidence="中",
            price=56.51, key_level=55.30, action="临界机会观察，暂不建仓", shares=0,
            reason="技术与外部证据已就绪，但目标空间与风险预算尚未通过",
            invalidation="剩余门槛通过前不下单",
            event_id="watch-near-entry", category="observation",
            details={"change_pct": 3.86, "target": 58.18, "stop": 49.75},
        )
        text = render_message([signal], "10:15", "A股持仓纪律")
        self.assertIn("建议：临界机会观察，暂不建仓", text)
        self.assertIn("级别：提醒（非买卖指令）", text)
        self.assertIn("计划目标价 58.18", text)
        self.assertNotIn("0股", text)

    def test_margin_evidence_is_rendered_on_its_own_line_without_body_truncation(self):
        signal = Signal(
            symbol="601336.SH", name="新华保险", code="DOWN_BREAK", confidence="中",
            price=62.31, key_level=62.35, action="分批降低主仓", shares=100,
            reason="有效破位但仅一项外部确认", invalidation="重新站回62.35",
            event_id="down-break", category="strategy",
            details={
                "change_pct": -0.89,
                "evidence": (
                    "寿险利率环境：10年1.72%、30年2.19%、月变动均值-5.44bp；"
                    + "公司经营指标正文暂无可结构化结论；" * 8
                    + "两融日终：融资余额近5个样本+1.00%，"
                    "当日融资买入193227058元、偿还121389799元（2026-08-03）"
                ),
            },
        )
        text = render_message([signal], "14:15", "A股持仓纪律", evidence_char_limit=180)
        self.assertIn("证据：", text)
        self.assertIn(
            "两融：融资余额近5个样本+1.00%，当日融资买入193227058元、偿还121389799元（2026-08-03）",
            text,
        )

    def test_commodity_option_context_is_separate_and_non_actionable(self):
        signal = Signal(
            symbol="600362.SH", name="江西铜业", code="UP_BREAK", confidence="中",
            price=47.0, key_level=46.0, action="分批增加主仓", shares=100,
            reason="价格与沪铜期货共振", invalidation="跌回突破位",
            event_id="option-context", category="strategy",
            details={
                "change_pct": 1.2,
                "evidence": "沪铜期货方向与股价共振",
                "commodity_option_status": "fresh",
                "commodity_option_view": "volatility_expansion",
                "commodity_option_summary": (
                    "沪铜期权同到期近ATM双边隐波抬升；"
                    "原始权利金涨跌不等同铜价或个股涨跌。"
                ),
            },
        )
        text = render_message([signal], "14:15", "A股持仓纪律")
        self.assertIn("【商品期权辅助】", text)
        self.assertIn("原始权利金涨跌不等同", text)
        self.assertIn("不替代期货门控，不单独触发本次动作", text)
        self.assertIn("证据：沪铜期货方向与股价共振", text)

    def test_daily_summary_renders_option_context_without_counting_signal(self):
        rows = [{
            "symbol": "600362.SH", "name": "江西铜业", "price": 47.0,
            "change_pct": 1.2, "recommendation": "继续观察", "reason": "未触发动作",
            "trigger_count": 0, "status_by_node": {"14:15": "观察"},
            "commodity_option_status": "partial",
            "commodity_option_summary": "沪铜期权仅取得认购一侧，不生成方向结论。",
        }]
        text = render_daily_summary(rows, ["14:15"], "2026-08-17", "A股持仓纪律")
        self.assertIn("今日无操作信号", text)
        self.assertIn("期权辅助：沪铜期权仅取得认购一侧", text)


if __name__ == "__main__":
    unittest.main()
