from __future__ import annotations

from datetime import date
from math import ceil, floor
from typing import Any

from .models import EquityEvidence, Position, Quote, Signal, Technicals


LOT_SIZE = 100

# 股权登记日仅阻止普通迁移减仓/卫星止盈；有效破位、阶段顶部、硬风险
# 和卫星止损/到期退出均优先。缺失证据不构成“弱势”本身。
_RECORD_DATE_BLOCKED_CODES = {
    "MIGRATION_TRIM",
    "MIGRATION_RECOVERY_TRIM",
    "SAT_SELL",
}


def trading_days_held(entry: date, current: date, holidays: set[str]) -> int:
    """Return inclusive A-share trading days; the entry day is day one."""
    if current < entry:
        return 0
    days = 0
    cursor = entry
    while cursor <= current:
        if cursor.weekday() < 5 and cursor.isoformat() not in holidays:
            days += 1
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
    return days


def evaluate_position(
    position: Position,
    quote: Quote,
    tech: Technicals,
    node: str,
    peer_change: float | None,
    market_change: float | None,
    today: date,
    rules: dict,
    risk: dict,
    holidays: set[str],
    available_cash: float,
    position_weight: float,
    portfolio_value: float | None = None,
    correlated_weight: float | None = None,
    correlated_cap: float | None = None,
    strategic_rules: dict | None = None,
    active_satellite_count: int = 0,
    evidence: EquityEvidence | None = None,
    stage_rules: dict | None = None,
    diagnostics: dict[str, Any] | None = None,
    position_sizing: dict | None = None,
    migration_context: dict[str, Any] | None = None,
    technical_data_fresh: bool = True,
    stage_memory: dict[str, Any] | None = None,
    watchlist_rules: dict | None = None,
    execution_constraints: dict | None = None,
    liquidity_rules: dict | None = None,
) -> list[Signal]:
    strategic_rules = strategic_rules or {}
    stage_rules = stage_rules or {}
    position_sizing = position_sizing or {}
    migration_context = migration_context or {}
    watchlist_rules = watchlist_rules or {}
    execution_constraints = execution_constraints or {}
    liquidity_rules = liquidity_rules or {}
    portfolio_value = float(portfolio_value or 0)
    diagnostics = diagnostics if diagnostics is not None else {}
    diagnostics.setdefault("checks", {})
    stage = _stage_assessment(
        quote, tech, peer_change, market_change, evidence, stage_rules, stage_memory or {}
    )
    diagnostics["stage"] = stage

    if position.role == "watchlist":
        if not technical_data_fresh:
            _check(diagnostics, "data_freshness", "technical_data_fresh", False)
            return []
        candidate = _watchlist_entry_signal(
            position,
            quote,
            tech,
            node,
            peer_change,
            market_change,
            today,
            rules,
            risk,
            watchlist_rules,
            strategic_rules,
            stage_rules,
            available_cash,
            position_weight,
            portfolio_value,
            correlated_weight,
            correlated_cap,
            evidence,
            stage,
            diagnostics,
            position_sizing,
            execution_constraints,
            liquidity_rules,
        )
        return [candidate] if candidate else []

    if position.total_shares <= 0:
        _check(diagnostics, "position_state", "holding_has_shares", False)
        return []

    risk_signals = _risk_signals(
        position, quote, tech, peer_change, market_change, today, risk, rules, evidence,
        strategic_rules, diagnostics, migration_context, technical_data_fresh,
    )
    if risk_signals:
        return risk_signals
    if _is_record_date(position, today):
        _check(
            diagnostics,
            "corporate_event",
            "record_date_hold",
            False,
            today.isoformat(),
            "股权登记日禁止常规减仓",
        )
    if not technical_data_fresh and position.satellite.active:
        _check(diagnostics, "data_freshness", "technical_data_fresh", False)
        satellite = _active_satellite(
            position, quote, tech, peer_change, today, rules, holidays, evidence, stage,
            technical_data_fresh=False,
        )
        return [satellite] if satellite else []
    if not technical_data_fresh:
        _check(diagnostics, "data_freshness", "technical_data_fresh", False)
        return []

    cooldown_days = int(watchlist_rules.get("starter_cooldown_trading_days", 3))
    starter_cooldown = bool(
        position.watchlist_entry_date is not None
        and trading_days_held(position.watchlist_entry_date, today, holidays) <= cooldown_days
    )
    _check(
        diagnostics,
        "starter_cooldown",
        "elapsed",
        not starter_cooldown,
        trading_days_held(position.watchlist_entry_date, today, holidays)
        if position.watchlist_entry_date else None,
        f">{cooldown_days}个交易日",
    )

    strategic = _strategic_signal(
        position,
        quote,
        tech,
        node,
        peer_change,
        market_change,
        today,
        rules,
        risk,
        strategic_rules,
        stage_rules,
        available_cash,
        position_weight,
        portfolio_value,
        correlated_weight,
        correlated_cap,
        allow_add=not position.satellite.active and not starter_cooldown,
        evidence=evidence,
        stage=stage,
        diagnostics=diagnostics,
        position_sizing=position_sizing,
        migration_context=migration_context,
        execution_constraints=execution_constraints,
        liquidity_rules=liquidity_rules,
    )
    if strategic and strategic.code in {
        "DOWN_BREAK", "STAGE_TOP_EXIT", "MIGRATION_TRIM", "MIGRATION_RECOVERY_TRIM",
    }:
        if strategic.code in _RECORD_DATE_BLOCKED_CODES and _is_record_date(position, today):
            _check(
                diagnostics,
                "corporate_event",
                "record_date_blocks_reduction",
                False,
                strategic.code,
                "股权登记日保留分红权，不发送常规减仓",
            )
            return []
        else:
            return [strategic]

    if position.satellite.active:
        satellite = _active_satellite(
            position, quote, tech, peer_change, today, rules, holidays, evidence, stage,
            technical_data_fresh=True,
        )
        satellite_top_confirmed = bool((satellite.details or {}).get("top_confirmed")) if satellite else False
        if (
            satellite
            and satellite.code in _RECORD_DATE_BLOCKED_CODES
            and _is_record_date(position, today)
            and not satellite_top_confirmed
        ):
            _check(
                diagnostics,
                "corporate_event",
                "record_date_blocks_satellite_trim",
                False,
                satellite.code,
                "股权登记日禁止卫星仓止盈减仓",
            )
            return []
        return [satellite] if satellite else []

    if strategic:
        return [strategic]
    if starter_cooldown:
        _check(diagnostics, "satellite_entry", "starter_cooldown", False)
        return []
    if position.main_shares <= 0:
        _check(diagnostics, "satellite_entry", "main_position_exists", False)
        return []
    if node not in {"10:15", "13:15", "14:15"}:
        _check(
            diagnostics,
            "satellite_entry",
            "allowed_node",
            False,
            node,
            "10:15、13:15或14:15",
        )
        return []
    if active_satellite_count >= int(rules.get("max_active_positions", 1)):
        _check(
            diagnostics,
            "satellite_entry",
            "portfolio_satellite_capacity",
            False,
            active_satellite_count,
            f"少于{int(rules.get('max_active_positions', 1))}只",
        )
        return []
    satellite = _satellite_entry(
        position,
        quote,
        tech,
        peer_change,
        market_change,
        today,
        rules,
        risk,
        available_cash,
        position_weight,
        portfolio_value,
        correlated_weight,
        correlated_cap,
        evidence,
        stage,
        diagnostics,
        position_sizing,
        migration_context,
        execution_constraints,
        liquidity_rules,
    )
    return [satellite] if satellite else []


def _watchlist_entry_signal(
    position,
    quote,
    tech,
    node,
    peer_change,
    market_change,
    today,
    rules,
    risk,
    watchlist_rules,
    strategic_rules,
    stage_rules,
    available_cash,
    position_weight,
    portfolio_value,
    correlated_weight,
    correlated_cap,
    evidence,
    stage,
    diagnostics,
    position_sizing,
    execution_constraints,
    liquidity_rules,
):
    enabled = bool(watchlist_rules.get("enabled", True))
    allowed_nodes = {
        str(value)
        for value in watchlist_rules.get(
            "allowed_nodes", ["10:15", "13:15", "14:15"]
        )
    }
    allowed_node = node in allowed_nodes
    if not enabled or not allowed_node:
        _check(diagnostics, "watchlist_entry", "enabled", enabled)
        _check(diagnostics, "watchlist_entry", "allowed_node", allowed_node, node, sorted(allowed_nodes))
        return None

    data_ready = _intraday_data_ready(tech, rules)
    volume_mode = _satellite_volume_mode(tech, rules)
    evidence_ready = bool(evidence and evidence.add_ready)
    strong_count = _strong_confirmation_count(
        peer_change, market_change, evidence, strategic_rules
    )
    peer_floor = float(watchlist_rules.get(
        "peer_minimum", stage_rules.get("bottom_peer_minimum", -0.003)
    ))
    market_floor = float(watchlist_rules.get(
        "market_minimum", stage_rules.get("bottom_market_minimum", -0.005)
    ))
    external_ready = bool(
        peer_change is not None
        and market_change is not None
        and peer_change >= peer_floor
        and market_change >= market_floor
        and strong_count >= int(watchlist_rules.get("minimum_strong_confirmations", 2))
    )
    stage_allows = stage.get("label") not in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"}
    above_vwap = bool(tech.vwap is not None and quote.price >= tech.vwap)

    bottom_setup = bool(
        stage.get("bottom_confirmed")
        and data_ready
        and volume_mode != "invalid"
        and tech.last_15m_close is not None
        and tech.last_15m_close >= tech.support
        and quote.price >= tech.support
        and above_vwap
    )
    minimum_ma20_slope = float(
        strategic_rules.get("minimum_ma20_slope_5d", -0.002)
    )
    above_ma20 = quote.price > tech.ma20
    ma20_slope_ready = bool(
        tech.ma20_slope_5d is not None
        and tech.ma20_slope_5d >= minimum_ma20_slope
    )
    short_trend_ready = _short_trend_ready(quote, tech, strategic_rules)
    trend_ready = bool(above_ma20 and ma20_slope_ready and short_trend_ready)
    intraday_confirmed = _breakout_intraday_confirmed(tech, rules, strategic_rules)
    breakout_persistent = _breakout_persistence(tech, strategic_rules)
    price_above_resistance = quote.price > tech.resistance
    close_above_resistance = bool(
        tech.last_15m_close is not None
        and tech.last_15m_close > tech.resistance
    )
    breakout_setup = bool(
        intraday_confirmed
        and trend_ready
        and breakout_persistent
        and price_above_resistance
        and close_above_resistance
        and above_vwap
    )

    # A bottom confirmation can complete after price has already crossed the
    # current resistance.  Treat that overlap as its own setup instead of
    # keeping the now-stale resistance as the target or forcing the slower
    # MA20 trend gate used by a standalone breakout.
    bottom_breakout_transition = bool(
        bottom_setup
        and price_above_resistance
        and close_above_resistance
        and breakout_persistent
    )

    breakout_target = tech.next_resistance
    breakout_stop = tech.resistance * (
        1 - float(strategic_rules.get("breakout_stop_buffer_ratio", 0.01))
    )
    breakout_expected_spread = (
        (breakout_target - quote.price) / quote.price
        if breakout_target is not None
        and breakout_target > quote.price
        and quote.price > 0
        else -1.0
    )
    breakout_downside = (
        max(quote.price - breakout_stop, 0.01) / quote.price
        if quote.price > 0
        else 1.0
    )
    breakout_reward_risk = (
        breakout_expected_spread / breakout_downside
        if breakout_expected_spread > 0
        else -1.0
    )

    plain_bottom_setup = bool(bottom_setup and quote.price < tech.resistance)
    if bottom_breakout_transition:
        setup = "bottom_breakout_transition"
        level = round(tech.resistance, 2)
        target = tech.next_resistance
        stop, _ = _adaptive_stop(tech, rules)
        reason = (
            "观察标的磨底右侧确认后，完整15分钟已突破并守住当前压力；"
            "按高于现价的下一有效压力评估首次学习型起始仓"
        )
    elif plain_bottom_setup:
        setup = "bottom_confirmed"
        level = round(tech.support, 2)
        target = tech.resistance
        stop, _ = _adaptive_stop(tech, rules)
        reason = (
            "观察标的完成磨底右侧确认，15分钟守住动态支撑并重回近似分时均价；"
            "首次只建立学习型起始仓"
        )
    elif breakout_setup:
        setup = "breakout_confirmed"
        level = round(tech.resistance, 2)
        target = tech.next_resistance
        stop = tech.resistance * (
            1 - float(strategic_rules.get("breakout_stop_buffer_ratio", 0.01))
        )
        reason = (
            "观察标的以完整15分钟放量突破并守住动态压力，日线趋势同步改善；"
            "首次只建立学习型起始仓"
        )
    else:
        setup = "none"
        level = round(tech.support, 2)
        target = None
        stop = tech.support
        reason = ""

    target_sane, target_distance_ratio, target_atr_multiple = _watchlist_target_sanity(
        quote.price, target, tech, watchlist_rules
    )

    expected_spread = (
        (target - quote.price) / quote.price
        if target is not None and target > quote.price and quote.price > 0
        else -1.0
    )
    downside_ratio = max(quote.price - stop, 0.01) / quote.price if quote.price > 0 else 1.0
    reward_risk = expected_spread / downside_ratio if expected_spread > 0 else -1.0
    risk_amount = _entry_risk_budget(
        position,
        quote.price,
        risk,
        portfolio_value,
        _sizing_value(
            position, position_sizing, "watchlist_entry_risk_weight", 0.0035
        ),
        1.0,
        today=today,
    )
    planned_shares, planned_weight = _planned_watchlist_entry_shares(
        position, quote.price, portfolio_value, position_sizing
    )
    shares = _sized_buy_shares(
        requested=planned_shares,
        price=quote.price,
        stop=stop,
        available_cash=available_cash,
        cash_fraction=float(watchlist_rules.get("max_cash_fraction_per_entry", 1.0)),
        position_weight=position_weight,
        single_cap=_effective_single_cap(position, position_sizing, risk),
        portfolio_value=portfolio_value,
        correlated_weight=correlated_weight,
        correlated_cap=correlated_cap,
        max_risk_amount=risk_amount,
        execution_constraints=execution_constraints,
        liquidity_rules=liquidity_rules,
        tech=tech,
    )
    checks = {
        "role_is_watchlist": position.role == "watchlist",
        "setup_confirmed": plain_bottom_setup or bottom_breakout_transition or breakout_setup,
        "industry_and_announcements": evidence_ready,
        "margin_auxiliary_gate": _auxiliary_allows_entry(evidence),
        "external_confirmation": external_ready,
        "not_near_stage_top": stage_allows,
        "target_scale_sane": target_sane,
        "minimum_expected_spread": expected_spread >= float(
            watchlist_rules.get("minimum_expected_spread_ratio", 0.05)
        ),
        "minimum_reward_risk": reward_risk >= float(
            watchlist_rules.get("minimum_reward_risk", 2.0)
        ),
        "sized_at_least_one_lot": shares >= LOT_SIZE,
    }
    for name, passed in checks.items():
        _check(diagnostics, "watchlist_entry", name, passed)
    diagnostics.setdefault("metrics", {})["watchlist_entry"] = {
        "setup": setup,
        "strong_confirmations": strong_count,
        "expected_spread": expected_spread,
        "reward_risk": reward_risk,
        "target": target,
        "stop": stop,
        "target_scale_sane": target_sane,
        "target_distance_ratio": target_distance_ratio,
        "target_atr_multiple": target_atr_multiple,
        "risk_budget": risk_amount,
        "planned_weight": planned_weight,
        "sized_shares": shares,
        "volume_mode": volume_mode,
        "above_vwap": above_vwap,
        "bottom_confirmed": bool(stage.get("bottom_confirmed")),
        "bottom_breakout_transition": bottom_breakout_transition,
        "breakout_intraday_confirmed": intraday_confirmed,
        "breakout_persistence": breakout_persistent,
        "price_above_resistance": price_above_resistance,
        "close_above_resistance": close_above_resistance,
        "price_above_ma20": above_ma20,
        "ma20_slope_ready": ma20_slope_ready,
        "short_trend_ready": short_trend_ready,
        "breakout_expected_spread": breakout_expected_spread,
        "breakout_reward_risk": breakout_reward_risk,
        "breakout_target": breakout_target,
        "breakout_stop": breakout_stop,
        "minimum_expected_spread_required": float(
            watchlist_rules.get("minimum_expected_spread_ratio", 0.05)
        ),
        "minimum_reward_risk_required": float(
            watchlist_rules.get("minimum_reward_risk", 2.0)
        ),
    }
    if not all(checks.values()):
        near_signal = _watchlist_near_entry_signal(
            position=position,
            quote=quote,
            today=today,
            setup=setup,
            level=level,
            target=target,
            stop=stop,
            expected_spread=expected_spread,
            reward_risk=reward_risk,
            shares=shares,
            checks=checks,
            watchlist_rules=watchlist_rules,
            evidence=evidence,
            stage=stage,
            target_distance_ratio=target_distance_ratio,
            target_atr_multiple=target_atr_multiple,
        )
        if near_signal:
            return near_signal
        return None

    confidence = "高" if strong_count >= 3 else "中"
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="WATCH_ENTRY",
        confidence=confidence,
        price=quote.price,
        key_level=level,
        action="首次建立主仓起始档",
        shares=shares,
        reason=(
            f"{reason}；至计划目标预计空间{expected_spread:.1%}，"
            f"收益风险比{reward_risk:.2f}"
        ),
        invalidation=(
            f"完整15分钟跌破{stop:.2f}、产业证据转弱或成交前条件消失；"
            "成交后须将role改为holding"
        ),
        event_id=f"{today.isoformat()}|{position.symbol}:WATCH_ENTRY:{level:.2f}",
        category="strategy",
        details={
            "target": round(float(target), 2),
            "stop": round(stop, 2),
            "reward_risk": round(reward_risk, 2),
            "score": tech.volume_ratio,
            "evidence": _evidence_note(evidence),
            "stage": stage.get("label"),
            "entry_setup": setup,
            "role": "watchlist",
            "event_rank": 1,
            "company_direction": evidence.company_direction if evidence else None,
            "corporate_action_confirmation": bool(
                evidence and evidence.corporate_action_confirmation
            ),
            "corporate_action_strength": evidence.corporate_action_strength if evidence else 0,
            "margin_signal": evidence.margin_signal if evidence else "missing",
            "capital_flow_signal": evidence.capital_flow_signal if evidence else "missing",
            "shareholder_signal": evidence.shareholder_signal if evidence else "missing",
            **_liquidity_details(tech, shares, liquidity_rules),
            "planned_nav_ratio": round(shares * quote.price / portfolio_value, 4)
            if portfolio_value else 0.0,
        },
    )


def _watchlist_target_sanity(
    price: float,
    target: float | None,
    tech: Technicals,
    watchlist_rules: dict,
) -> tuple[bool, float | None, float | None]:
    """Reject stale or scale-mismatched targets before they drive an entry."""
    if target is None or price <= 0 or target <= price:
        return False, None, None
    distance_ratio = (target - price) / price
    atr_multiple = (
        (target - price) / tech.atr14
        if tech.atr14 is not None and tech.atr14 > 0
        else None
    )
    max_distance = float(
        watchlist_rules.get("maximum_target_distance_ratio", 0.50)
    )
    max_atr = float(
        watchlist_rules.get("maximum_target_atr_multiple", 8.0)
    )
    within_recent_range = bool(
        tech.recent_high_60 is None
        or target <= tech.recent_high_60 * 1.001
    )
    return (
        distance_ratio <= max_distance
        and (atr_multiple is None or atr_multiple <= max_atr)
        and within_recent_range,
        distance_ratio,
        atr_multiple,
    )


def _watchlist_near_entry_signal(
    *,
    position,
    quote,
    today,
    setup: str,
    level: float,
    target: float | None,
    stop: float,
    expected_spread: float,
    reward_risk: float,
    shares: int,
    checks: dict[str, bool],
    watchlist_rules: dict,
    evidence,
    stage: dict,
    target_distance_ratio: float | None,
    target_atr_multiple: float | None,
) -> Signal | None:
    """Send a non-action reminder only when the qualitative setup is ready."""
    if not bool(watchlist_rules.get("notify_near_entry", True)):
        return None
    foundation = (
        "setup_confirmed",
        "industry_and_announcements",
        "margin_auxiliary_gate",
        "external_confirmation",
        "not_near_stage_top",
    )
    if not all(checks.get(name, False) for name in foundation):
        return None

    blockers: list[str] = []
    if not checks.get("target_scale_sane", False):
        distance = (
            f"{target_distance_ratio:.1%}"
            if target_distance_ratio is not None else "不可计算"
        )
        atr_text = (
            f"、约{target_atr_multiple:.1f}倍ATR"
            if target_atr_multiple is not None else ""
        )
        blockers.append(f"下一压力跨度{distance}{atr_text}，需复核复权/历史尺度")
    if not (
        checks.get("minimum_expected_spread", False)
        and checks.get("minimum_reward_risk", False)
    ):
        spread_text = f"{expected_spread:.1%}" if expected_spread >= 0 else "不足"
        rr_text = f"{reward_risk:.2f}" if reward_risk >= 0 else "不足"
        blockers.append(f"目标空间{spread_text}、收益风险比{rr_text}尚未同时达标")
    if not checks.get("sized_at_least_one_lot", False):
        blockers.append("按止损风险预算不足100股")

    max_groups = int(watchlist_rules.get("near_entry_max_blocker_groups", 2))
    if not blockers or len(blockers) > max_groups:
        return None
    target_details = {"target": round(float(target), 2)} if target is not None else {}
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="WATCH_NEAR_ENTRY",
        confidence="中",
        price=quote.price,
        key_level=level,
        action="临界机会观察，暂不建仓",
        shares=0,
        reason="观察仓的技术与外部证据已就绪，但" + "；".join(blockers),
        invalidation="剩余门槛通过前不下单；结构、证据或风险收益恶化则取消临界观察",
        event_id=f"{today.isoformat()}|{position.symbol}:WATCH_NEAR_ENTRY",
        category="observation",
        details={
            **target_details,
            "stop": round(stop, 2),
            "reward_risk": round(reward_risk, 2),
            "expected_spread": round(expected_spread, 4),
            "sized_shares": shares,
            "evidence": _evidence_note(evidence),
            "stage": stage.get("label"),
            "entry_setup": setup,
            "informational_only": True,
            "event_rank": 0,
        },
    )


def _strategic_signal(
    position,
    quote,
    tech,
    node,
    peer_change,
    market_change,
    today,
    rules,
    risk,
    strategic_rules,
    stage_rules,
    available_cash,
    position_weight,
    portfolio_value,
    correlated_weight,
    correlated_cap,
    allow_add,
    evidence,
    stage,
    diagnostics,
    position_sizing,
    migration_context,
    execution_constraints,
    liquidity_rules,
):
    if node not in {"10:15", "13:15", "14:15"}:
        return None
    peer_resilient, peer_relative_excess = _peer_relative_strength(
        quote.change_ratio, peer_change, strategic_rules
    )
    weak_sources, external_divergences = _weak_confirmation_context(
        peer_change, market_change, evidence, strategic_rules, quote.change_ratio
    )
    weak_count = len(weak_sources)
    strong_count = _strong_confirmation_count(
        peer_change, market_change, evidence, strategic_rules
    )
    downside = _downside_setup(
        quote,
        tech,
        evidence,
        weak_count,
        rules,
        strategic_rules,
        peer_resilient=peer_resilient,
        market_change=market_change,
    )
    for name, value in downside["checks"].items():
        _check(diagnostics, "main_reduce", name, value[0], value[1], value[2])
    if position.main_shares > 0 and downside["triggered"]:
        high = bool(downside["high_confidence"])
        ratio = float(strategic_rules.get(
            "high_confidence_reduction_ratio" if high else "medium_confidence_reduction_ratio",
            0.25 if high else 0.15,
        ))
        shares = _planned_reduction_shares(position.main_shares, ratio)
        level = round(tech.support, 2)
        action = "分批降低主仓"
        details = {
            "score": tech.volume_ratio,
            "volume_samples": tech.volume_baseline_samples,
            "evidence": _evidence_note(evidence),
            "gap_exception": downside["gap_exception"],
            "critical_announcement": downside["critical_announcement"],
            "reduction_ratio": ratio,
            "break_depth_ratio": downside["break_depth_ratio"],
            "break_depth_atr": downside["break_depth_atr"],
            "persistent_break": downside["persistent_break"],
            "observation_only": downside["observation_only"],
            "peer_relative_resilient": peer_resilient,
            "peer_relative_excess_ratio": peer_relative_excess,
            "external_confirmations": weak_sources,
            "external_divergences": external_divergences,
            "position_main_shares": position.main_shares,
            "event_rank": 2 if high else 1,
            **_liquidity_details(tech, shares, liquidity_rules, risk_exit=True),
        }
        if position.satellite.active:
            action = "卫星仓退出，并分批降低主仓"
            details["satellite_exit_shares"] = position.satellite.shares
        if downside["gap_exception"]:
            reason_prefix = "向下跳空后未收回支撑"
        elif downside["persistent_break"]:
            reason_prefix = "连续两根完整15分钟K收在动态支撑下方"
        else:
            reason_prefix = "完整15分钟深度跌破动态支撑"
        if downside["critical_announcement"]:
            reason_prefix += "且公司公告出现高风险事项"
        external_note = "、".join(weak_sources) if weak_sources else "无"
        divergence_note = (
            f"；背离：{'、'.join(external_divergences)}"
            if external_divergences
            else ""
        )
        return Signal(
            symbol=position.symbol,
            name=position.name,
            code="DOWN_BREAK",
            confidence="高" if high else "中",
            price=quote.price,
            key_level=level,
            action=action,
            shares=shares,
            reason=(
                f"{reason_prefix}、位于近似分时均价下方；"
                f"同时间量能{_fmt(tech.volume_ratio)}倍；外部确认：{external_note}"
                f"{divergence_note}"
            ),
            invalidation=f"完整15分钟重新站回{level:.2f}上方且板块止跌",
            event_id=f"{today.isoformat()}|{position.symbol}:DOWN_BREAK:{level:.2f}",
            category="strategy",
            details=details,
        )

    top_signal = (
        _stage_top_signal(position, quote, tech, today, stage, stage_rules, evidence)
        if position.main_shares > 0
        else None
    )
    if top_signal:
        if position.satellite.active:
            top_signal.action = "先退出卫星仓，再执行阶段顶部主仓纪律"
            top_signal.details["satellite_exit_shares"] = position.satellite.shares
        return top_signal

    recovery_trim = (
        _migration_recovery_trim_signal(
            position,
            quote,
            tech,
            today,
            evidence,
            position_weight,
            portfolio_value,
            migration_context,
            stage,
            diagnostics,
            liquidity_rules,
        )
        if position.main_shares > 0
        else None
    )
    if recovery_trim:
        if position.satellite.active:
            recovery_trim.action = "先退出卫星仓，再执行超配仓回本降风险"
            recovery_trim.details["satellite_exit_shares"] = position.satellite.shares
        return recovery_trim

    migration_trim = (
        _migration_rebound_trim_signal(
            position,
            quote,
            tech,
            peer_change,
            market_change,
            today,
            evidence,
            position_weight,
            portfolio_value,
            migration_context,
            diagnostics,
            liquidity_rules,
        )
        if position.main_shares > 0
        else None
    )
    if migration_trim:
        if position.satellite.active:
            migration_trim.action = "先退出卫星仓，再执行迁移减仓"
            migration_trim.details["satellite_exit_shares"] = position.satellite.shares
        return migration_trim

    if node not in {"10:15", "13:15", "14:15"}:
        return None
    if not allow_add or not bool(strategic_rules.get("main_add_enabled", False)):
        _check(diagnostics, "main_add", "enabled_and_no_satellite", False, allow_add, True)
        return None

    bottom_reentry = _bottom_reentry_signal(
        position,
        quote,
        tech,
        peer_change,
        market_change,
        today,
        rules,
        risk,
        strategic_rules,
        stage_rules,
        available_cash,
        position_weight,
        portfolio_value,
        correlated_weight,
        correlated_cap,
        evidence,
        stage,
        strong_count,
        diagnostics,
        position_sizing,
        migration_context,
        execution_constraints,
        liquidity_rules,
    )
    if bottom_reentry:
        return bottom_reentry

    data_ready = _breakout_intraday_confirmed(tech, rules, strategic_rules)
    evidence_ready = bool(evidence and evidence.add_ready)
    enough_confirmations = strong_count >= int(
        strategic_rules.get("minimum_strong_confirmations", 2)
    )
    trend_ready = _daily_trend_ready(quote, tech, strategic_rules)
    persistence = _breakout_persistence(tech, strategic_rules)
    stage_allows = stage["label"] not in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"}
    target = tech.next_resistance
    expected_spread = ((target - quote.price) / quote.price) if target and target > quote.price else -1.0
    stop = tech.resistance * (1 - float(strategic_rules.get("breakout_stop_buffer_ratio", 0.01)))
    downside_ratio = max(quote.price - stop, 0.01) / quote.price
    reward_risk = expected_spread / downside_ratio if expected_spread > 0 else -1.0
    current_loss_ratio = _loss_ratio(
        position, quote.price, _migration_risk_principal(migration_context), today=today
    )
    risk_amount = _entry_risk_budget(
        position,
        quote.price,
        risk,
        portfolio_value,
        _sizing_value(position, position_sizing, "entry_risk_weight", 0.005),
        float(strategic_rules.get("max_remaining_risk_capacity_fraction", 0.10)),
        _migration_risk_principal(migration_context),
        today,
    )
    planned_shares, planned_weight = _planned_main_entry_shares(
        position,
        quote.price,
        portfolio_value,
        position_weight,
        risk,
        position_sizing,
        "trend_add_weight",
        migration_context,
    )
    shares = _sized_buy_shares(
        requested=planned_shares,
        price=quote.price,
        stop=stop,
        available_cash=available_cash,
        cash_fraction=float(strategic_rules.get("max_cash_fraction_per_add", 0.50)),
        position_weight=position_weight,
        single_cap=_effective_single_cap(position, position_sizing, risk, migration_context),
        portfolio_value=portfolio_value,
        correlated_weight=correlated_weight,
        correlated_cap=correlated_cap,
        max_risk_amount=risk_amount,
        execution_constraints=execution_constraints,
        liquidity_rules=liquidity_rules,
        tech=tech,
    )
    main_checks = {
        "data_and_volume": data_ready,
        "industry_and_announcements": evidence_ready,
        "margin_auxiliary_gate": _auxiliary_allows_entry(evidence),
        "external_confirmations": enough_confirmations,
        "daily_trend": trend_ready,
        "breakout_persistence_or_retest": persistence,
        "not_near_stage_top": stage_allows,
        "below_risk_warning": current_loss_ratio < float(risk.get("warning_ratio", 0.20)),
        "price_above_resistance": quote.price > tech.resistance,
        "close_above_resistance": bool(tech.last_15m_close and tech.last_15m_close > tech.resistance),
        "above_vwap": bool(tech.vwap and quote.price > tech.vwap),
        "next_resistance_available": target is not None and target > quote.price,
        "minimum_expected_spread": expected_spread >= float(strategic_rules.get("minimum_expected_spread_ratio", 0.04)),
        "minimum_reward_risk": reward_risk >= float(strategic_rules.get("minimum_reward_risk", 1.8)),
        "sized_at_least_one_lot": shares >= LOT_SIZE,
    }
    for name, passed in main_checks.items():
        _check(diagnostics, "main_add", name, passed)
    diagnostics.setdefault("metrics", {})["main_add"] = {
        "strong_confirmations": strong_count,
        "expected_spread": expected_spread,
        "reward_risk": reward_risk,
        "target": target,
        "stop": stop,
        "risk_budget": risk_amount,
        "planned_weight": planned_weight,
        "current_weight": position_weight,
        "target_weight": float(migration_context.get(
            "position_ceiling",
            _sizing_value(position, position_sizing, "target_main_weight", 0.20),
        )),
        "sized_shares": shares,
    }
    if not all(main_checks.values()):
        return None
    level = round(tech.resistance, 2)
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="UP_BREAK",
        confidence="高" if strong_count >= 3 else "中",
        price=quote.price,
        key_level=level,
        action=(
            "迁移模式下小幅恢复主仓"
            if migration_context.get("enabled")
            else "小幅分批增加主仓"
        ),
        shares=shares,
        reason=(
            f"完整15分钟确认突破并守住动态压力，位于近似分时均价上方，"
            f"同时间量能{tech.volume_ratio:.2f}倍；至下一压力预计空间{expected_spread:.1%}，"
            f"收益风险比{reward_risk:.2f}"
        ),
        invalidation=f"完整15分钟重新跌回{stop:.2f}下方或产业证据转弱",
        event_id=f"{today.isoformat()}|{position.symbol}:UP_BREAK:{level:.2f}",
        category="strategy",
        details={
            "target": round(float(target), 2),
            "stop": round(stop, 2),
            "reward_risk": round(reward_risk, 2),
            "score": tech.volume_ratio,
            "volume_samples": tech.volume_baseline_samples,
            "evidence": _evidence_note(evidence),
            "stage": stage["label"],
            "migration": bool(migration_context.get("enabled")),
            "event_rank": 1,
            "company_direction": evidence.company_direction if evidence else None,
            "corporate_action_confirmation": bool(
                evidence and evidence.corporate_action_confirmation
            ),
            "corporate_action_strength": evidence.corporate_action_strength if evidence else 0,
            "margin_signal": evidence.margin_signal if evidence else "missing",
            "capital_flow_signal": evidence.capital_flow_signal if evidence else "missing",
            "shareholder_signal": evidence.shareholder_signal if evidence else "missing",
            **_liquidity_details(tech, shares, liquidity_rules),
            "planned_nav_ratio": round(shares * quote.price / portfolio_value, 4) if portfolio_value else 0.0,
        },
    )


def _bottom_reentry_signal(
    position,
    quote,
    tech,
    peer_change,
    market_change,
    today,
    rules,
    risk,
    strategic_rules,
    stage_rules,
    available_cash,
    position_weight,
    portfolio_value,
    correlated_weight,
    correlated_cap,
    evidence,
    stage,
    strong_count,
    diagnostics,
    position_sizing,
    migration_context,
    execution_constraints,
    liquidity_rules,
):
    data_ready = _intraday_data_ready(tech, rules)
    volume_mode = _satellite_volume_mode(tech, rules)
    evidence_ready = bool(evidence and evidence.add_ready)
    external_ready = bool(
        peer_change is not None
        and market_change is not None
        and peer_change >= float(stage_rules.get("bottom_peer_minimum", -0.003))
        and market_change >= float(stage_rules.get("bottom_market_minimum", -0.005))
        and strong_count >= int(strategic_rules.get("minimum_strong_confirmations", 2))
    )
    target = tech.resistance
    stop, _ = _adaptive_stop(tech, rules)
    expected_spread = (target - quote.price) / quote.price if target > quote.price else -1.0
    downside_ratio = max(quote.price - stop, 0.01) / quote.price
    reward_risk = expected_spread / downside_ratio if expected_spread > 0 else -1.0
    risk_amount = _entry_risk_budget(
        position,
        quote.price,
        risk,
        portfolio_value,
        _sizing_value(position, position_sizing, "entry_risk_weight", 0.005),
        float(strategic_rules.get("max_remaining_risk_capacity_fraction", 0.10)),
        _migration_risk_principal(migration_context),
        today,
    )
    planned_shares, planned_weight = _planned_main_entry_shares(
        position,
        quote.price,
        portfolio_value,
        position_weight,
        risk,
        position_sizing,
        "initial_main_weight",
        migration_context,
    )
    shares = _sized_buy_shares(
        requested=planned_shares,
        price=quote.price,
        stop=stop,
        available_cash=available_cash,
        cash_fraction=float(strategic_rules.get("max_cash_fraction_per_add", 0.50)),
        position_weight=position_weight,
        single_cap=_effective_single_cap(position, position_sizing, risk, migration_context),
        portfolio_value=portfolio_value,
        correlated_weight=correlated_weight,
        correlated_cap=correlated_cap,
        max_risk_amount=risk_amount,
        execution_constraints=execution_constraints,
        liquidity_rules=liquidity_rules,
        tech=tech,
    )
    checks = {
        "bottom_confirmed": bool(stage.get("bottom_confirmed")),
        "data_ready": data_ready,
        "reversal_volume_mode": volume_mode != "invalid",
        "industry_and_announcements": evidence_ready,
        "margin_auxiliary_gate": _auxiliary_allows_entry(evidence),
        "external_confirmation": external_ready,
        "below_risk_warning": _loss_ratio(
            position, quote.price, _migration_risk_principal(migration_context), today=today
        ) < float(risk.get("warning_ratio", 0.20)),
        "minimum_expected_spread": expected_spread >= float(stage_rules.get("bottom_reentry_minimum_spread_ratio", 0.05)),
        "minimum_reward_risk": reward_risk >= float(strategic_rules.get("minimum_reward_risk", 1.8)),
        "sized_at_least_one_lot": shares >= LOT_SIZE,
    }
    for name, passed in checks.items():
        _check(diagnostics, "bottom_reentry", name, passed)
    diagnostics.setdefault("metrics", {})["bottom_reentry"] = {
        "expected_spread": expected_spread,
        "reward_risk": reward_risk,
        "target": target,
        "stop": stop,
        "risk_budget": risk_amount,
        "planned_weight": planned_weight,
        "current_weight": position_weight,
        "target_weight": float(migration_context.get(
            "position_ceiling",
            _sizing_value(position, position_sizing, "target_main_weight", 0.20),
        )),
        "sized_shares": shares,
        "volume_mode": volume_mode,
    }
    if not all(checks.values()):
        return None
    level = round(tech.support, 2)
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="STAGE_REENTRY",
        confidence="高" if stage.get("bottom_score", 0) >= 5 and strong_count >= 3 else "中",
        price=quote.price,
        key_level=level,
        action=(
            "磨底确认后小幅恢复迁移主仓"
            if migration_context.get("enabled")
            else "磨底确认后小幅恢复主仓"
        ),
        shares=shares,
        reason=(
            f"处于60日区间低位，RSI从低位回升且MA20跌势趋缓；"
            f"15分钟企稳并重回近似分时均价，至动态压力预计空间{expected_spread:.1%}"
        ),
        invalidation=f"完整15分钟跌破{stop:.2f}或产业证据重新恶化",
        event_id=f"{today.isoformat()}|{position.symbol}:STAGE_REENTRY:{level:.2f}",
        category="strategy",
        details={
            "target": round(target, 2),
            "stop": round(stop, 2),
            "reward_risk": round(reward_risk, 2),
            "evidence": _evidence_note(evidence),
            "stage": stage["label"],
            "migration": bool(migration_context.get("enabled")),
            "event_rank": 1,
            "company_direction": evidence.company_direction if evidence else None,
            "corporate_action_confirmation": bool(
                evidence and evidence.corporate_action_confirmation
            ),
            "corporate_action_strength": evidence.corporate_action_strength if evidence else 0,
            "margin_signal": evidence.margin_signal if evidence else "missing",
            "capital_flow_signal": evidence.capital_flow_signal if evidence else "missing",
            "shareholder_signal": evidence.shareholder_signal if evidence else "missing",
            **_liquidity_details(tech, shares, liquidity_rules),
            "planned_nav_ratio": round(shares * quote.price / portfolio_value, 4) if portfolio_value else 0.0,
        },
    )


def _stage_top_signal(position, quote, tech, today, stage, stage_rules, evidence):
    if not stage.get("top_confirmed"):
        return None
    full_exit = bool(
        stage_rules.get("full_exit_enabled", True)
        and stage.get("full_exit_ready")
    )
    # A completed top trim starts a new management rung.  Do not keep
    # trimming the same distribution merely because the 45-day context is
    # still remembered: a normal trim must first be re-armed by a new high.
    # A full-exit signal remains available because it represents a materially
    # worse state (trend damage plus thesis/broad-market confirmation).
    if not full_exit and not stage.get("top_trim_rearmed", True):
        return None
    if full_exit:
        shares = position.main_shares
        action = "阶段顶部确认，清理本轮主仓"
        confidence = "高"
        event_rank = 3
    else:
        ratio = float(stage_rules.get("top_trim_ratio", 0.25))
        if stage.get("top_score", 0) >= int(stage_rules.get("high_top_score", 5)):
            ratio = float(stage_rules.get("high_top_trim_ratio", 0.50))
        # 减仓向下取整到整手，避免小仓位把 50% 放大成 60%。
        shares = _planned_reduction_shares(position.main_shares, ratio)
        action = "阶段顶部确认，分批降低主仓"
        confidence = "高" if ratio >= 0.50 else "中"
        event_rank = 2 if ratio >= 0.50 else 1
    level = round(float(tech.recent_high_60 or quote.price), 2)
    range_pos = stage.get("range_position_60")
    drawdown = stage.get("drawdown_from_high")
    range_text = (
        f"60日区间位置{float(range_pos):.0%}"
        if range_pos is not None
        else "区间位置暂缺"
    )
    drawdown_text = (
        f"相对阶段峰值回撤{float(drawdown):.1%}"
        if drawdown is not None
        else "峰值回撤暂缺"
    )
    memory_note = "（含跨日顶部记忆）" if stage.get("remembered_top") else ""
    capital_note = ""
    if evidence is not None and evidence.capital_flow_status == "fresh":
        if evidence.capital_flow_signal in {"persistent_inflow", "single_day_inflow"}:
            capital_note = "；资金面仍呈主力净流入，与顶部派发背离，请人工复核"
            if confidence == "高" and not full_exit:
                confidence = "中"
                event_rank = min(event_rank, 1)
        elif evidence.capital_flow_signal in {"persistent_outflow", "single_day_outflow"}:
            capital_note = "；资金面同步主力净流出，强化顶部确认"
    holder_note = ""
    if evidence is not None and evidence.shareholder_status == "fresh":
        if evidence.shareholder_signal == "dispersing":
            holder_note = "；股东户数上升，筹码趋向分散"
        elif evidence.shareholder_signal == "concentrating":
            holder_note = "；股东户数下降，筹码趋向集中"
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="STAGE_TOP_EXIT",
        confidence=confidence,
        price=quote.price,
        key_level=level,
        action=action,
        shares=shares,
        reason=(
            f"{range_text}，{drawdown_text}{memory_note}；"
            f"近期RSI高位回落，日线跌回短均线；"
            f"15分钟转弱并位于近似分时均价下方，顶部确认得分{stage.get('top_score', 0)}"
            f"{capital_note}{holder_note}"
        ),
        invalidation="完整日线与15分钟结构重新转强、站回阶段高位后再评估；不得自动追买",
        event_id=f"{today.isoformat()}|{position.symbol}:STAGE_TOP_EXIT:{level:.2f}",
        category="strategy",
        details={
            "stage": stage["label"],
            "top_score": stage.get("top_score"),
            "range_position_60": stage.get("range_position_60"),
            "drawdown_from_high": stage.get("drawdown_from_high"),
            "evidence": _evidence_note(evidence),
            "capital_flow_signal": evidence.capital_flow_signal if evidence else "missing",
            "shareholder_signal": evidence.shareholder_signal if evidence else "missing",
            "event_rank": event_rank,
            # Persist an instruction as pending; the service advances the
            # trim rung only after an actual recorded sell reduces holdings.
            "position_main_shares": position.main_shares,
            "tracked_peak": stage.get("tracked_peak"),
            "full_exit": full_exit,
            "top_trim_stage": int(stage.get("top_trim_stage", 0) or 0) + 1,
            "top_trim_rearmed": bool(stage.get("top_trim_rearmed", True)),
        },
    )


def _migration_recovery_trim_signal(
    position,
    quote,
    tech,
    today,
    evidence,
    position_weight,
    portfolio_value,
    migration_context,
    stage,
    diagnostics,
    liquidity_rules,
):
    """Reduce a materially overweight legacy holding after it recovers its cycle cost.

    Cost recovery is only a gate, never a forecast.  The action also requires
    technical overheat or rejection, and a verified strong breakout with
    improving company evidence suppresses the trim.
    """
    if not bool(migration_context.get("enabled", False)) or not bool(
        migration_context.get("recovery_trim_enabled", True)
    ):
        return None

    anchor_price = float(migration_context.get("recovery_anchor_price", 0.0) or 0.0)
    cost_buffer = float(migration_context.get("recovery_trim_cost_buffer_ratio", 0.005))
    recovery_price = anchor_price * (1 + cost_buffer)
    long_term_target = float(migration_context.get("long_term_target_weight", 0.20))
    target_buffer = float(
        migration_context.get("recovery_trim_target_buffer_weight", 0.03)
    )
    retained_target_weight = min(long_term_target + target_buffer, 1.0)
    data_ready = _intraday_data_ready(tech, migration_context)
    valuation_ready = bool(
        migration_context.get("valuation_ready", portfolio_value > 0)
        and portfolio_value > 0
    )

    atr_extension = stage.get("atr_extension")
    rsi_overheated = bool(
        tech.rsi14 is not None
        and tech.rsi14 >= float(migration_context.get("recovery_trim_rsi_min", 70.0))
    )
    atr_overheated = bool(
        atr_extension is not None
        and float(atr_extension) >= float(
            migration_context.get("recovery_trim_atr_extension_min", 3.0)
        )
    )
    failed_break = bool(
        tech.last_15m_high is not None
        and tech.last_15m_close is not None
        and tech.last_15m_high >= tech.resistance
        and tech.last_15m_close < tech.resistance
    )
    bearish_reversal = bool(
        tech.last_15m_open is not None
        and tech.last_15m_close is not None
        and tech.previous_15m_close is not None
        and tech.last_15m_close < tech.last_15m_open
        and tech.last_15m_close < tech.previous_15m_close
    )
    below_vwap = bool(tech.vwap is not None and quote.price < tech.vwap)
    rejection = failed_break or bearish_reversal or below_vwap
    overheat_or_rejection = rsi_overheated or atr_overheated or rejection

    industry_improving = bool(
        evidence
        and evidence.industry_status == "fresh"
        and evidence.industry_direction is not None
        and evidence.industry_direction > 0
        and evidence.industry_strength >= 2
    )
    company_improving = bool(
        evidence
        and (
            (
                evidence.company_status == "fresh"
                and evidence.company_direction is not None
                and evidence.company_direction > 0
            )
            or evidence.corporate_action_confirmation
        )
    )
    strong_breakout = bool(
        data_ready
        and tech.last_15m_close is not None
        and tech.last_15m_close > tech.resistance
        and quote.price > tech.resistance
        and tech.vwap is not None
        and quote.price > tech.vwap
        and tech.volume_ratio is not None
        and tech.volume_ratio >= float(
            migration_context.get("recovery_trim_breakout_volume_ratio", 1.30)
        )
        and industry_improving
        and company_improving
    )

    target_main_shares = (
        _round_down_lot(portfolio_value * retained_target_weight / quote.price)
        if valuation_ready and quote.price > 0
        else 0
    )
    if position.main_shares >= LOT_SIZE:
        # This rule reduces concentration; it must not turn into a thesis-free
        # full exit merely because the target value rounds below one board lot.
        target_main_shares = max(target_main_shares, LOT_SIZE)
    desired_shares = _round_down_lot(
        max(position.main_shares - target_main_shares, 0)
    )
    shares = min(position.main_shares, desired_shares)
    if bool(liquidity_rules.get("enabled", False)):
        shares = min(
            shares,
            _adv_capacity(tech, liquidity_rules, "max_routine_trim_adv_ratio"),
        )

    checks = {
        "fixed_recovery_anchor_available": anchor_price > 0,
        "above_recovery_price": anchor_price > 0 and quote.price >= recovery_price,
        "materially_overweight": position_weight > retained_target_weight,
        "portfolio_valuation_ready": valuation_ready,
        "data_ready": data_ready,
        "overheat_or_rejection": overheat_or_rejection,
        "strong_breakout_not_confirmed": not strong_breakout,
        "sized_at_least_one_lot": shares >= LOT_SIZE,
    }
    for name, passed in checks.items():
        _check(diagnostics, "migration_recovery_trim", name, passed)
    diagnostics.setdefault("metrics", {})["migration_recovery_trim"] = {
        "recovery_anchor_price": anchor_price,
        "recovery_price": recovery_price,
        "cost_buffer_ratio": cost_buffer,
        "current_weight": position_weight,
        "long_term_target_weight": long_term_target,
        "retained_target_weight": retained_target_weight,
        "target_main_shares": target_main_shares,
        "desired_reduction_shares": desired_shares,
        "sized_shares": shares,
        "rsi_overheated": rsi_overheated,
        "atr_overheated": atr_overheated,
        "failed_break": failed_break,
        "bearish_reversal": bearish_reversal,
        "below_vwap": below_vwap,
        "strong_breakout": strong_breakout,
    }
    if not all(checks.values()):
        return None

    condition_text: list[str] = []
    if rsi_overheated:
        condition_text.append(f"RSI{float(tech.rsi14):.0f}过热")
    if atr_overheated:
        condition_text.append(f"偏离MA20约{float(atr_extension):.2f}个ATR")
    if rejection:
        condition_text.append("15分钟冲高受阻或回到分时均价下方")
    level = round(recovery_price, 2)
    confidence = "高" if rejection and (rsi_overheated or atr_overheated) else "中"
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="MIGRATION_RECOVERY_TRIM",
        confidence=confidence,
        price=quote.price,
        key_level=level,
        action="超配仓回本后降低主仓至长期目标附近",
        shares=shares,
        reason=(
            f"存量主仓占比{position_weight:.1%}，显著高于长期目标{long_term_target:.1%}；"
            f"股价已超过本轮固定回本线{recovery_price:.2f}；"
            f"{'、'.join(condition_text)}。成本线只作为降集中度窗口，不用于预测后续涨跌"
        ),
        invalidation=(
            f"价格重新低于{recovery_price:.2f}，或放量站稳动态压力且产业与公司证据同步增强"
        ),
        event_id=(
            f"{today.isoformat()}|{position.symbol}:MIGRATION_RECOVERY_TRIM:"
            f"{recovery_price:.2f}:{retained_target_weight:.4f}"
        ),
        category="strategy",
        details={
            "evidence": _evidence_note(evidence),
            "planned_nav_ratio": round(shares * quote.price / portfolio_value, 4),
            "migration": True,
            "event_rank": 2 if confidence == "高" else 1,
            "position_main_shares": position.main_shares,
            "recovery_anchor_price": round(anchor_price, 4),
            "recovery_price": round(recovery_price, 4),
            "current_weight": round(position_weight, 4),
            "retained_target_weight": round(retained_target_weight, 4),
            "desired_reduction_shares": desired_shares,
            **_liquidity_details(tech, shares, liquidity_rules),
        },
    )


def _migration_rebound_trim_signal(
    position,
    quote,
    tech,
    peer_change,
    market_change,
    today,
    evidence,
    position_weight,
    portfolio_value,
    migration_context,
    diagnostics,
    liquidity_rules,
):
    """Trim an overweight legacy position only after a confirmed rebound rejection."""
    if not bool(migration_context.get("enabled", False)):
        return None
    target_weight = float(migration_context.get("long_term_target_weight", 0.20))
    data_ready = _intraday_data_ready(tech, migration_context)
    resistance_distance = (tech.resistance - quote.price) / quote.price
    near_tolerance = float(migration_context.get("rebound_resistance_distance_ratio", 0.01))
    near_resistance = bool(abs(resistance_distance) <= near_tolerance)
    failed_break = bool(
        tech.last_15m_high is not None
        and tech.last_15m_close is not None
        and tech.last_15m_high >= tech.resistance
        and tech.last_15m_close < tech.resistance
    )
    bearish_reversal = bool(
        tech.last_15m_open is not None
        and tech.last_15m_close is not None
        and tech.previous_15m_close is not None
        and tech.last_15m_close < tech.last_15m_open
        and tech.last_15m_close < tech.previous_15m_close
    )
    below_vwap = bool(tech.vwap and quote.price < tech.vwap)
    volume_ready = bool(
        tech.volume_ratio is not None
        and tech.volume_ratio >= float(migration_context.get("rebound_trim_volume_ratio", 1.0))
    )
    external_available = bool(peer_change is not None and market_change is not None)
    # Missing industry/company evidence is neutral. Only explicit fresh weak
    # evidence may support the “external not strong” leg of a trim.
    explicit_weak_evidence = bool(
        evidence
        and (
            (
                evidence.industry_status == "fresh"
                and evidence.industry_direction is not None
                and evidence.industry_direction <= 0
            )
            or (
                evidence.company_status == "fresh"
                and evidence.company_direction is not None
                and evidence.company_direction < 0
            )
            or (
                evidence.announcement_status == "fresh"
                and evidence.announcement_risk in {"caution", "critical"}
            )
        )
    )
    external_not_strong = bool(
        external_available
        and (
            peer_change <= 0
            or market_change <= 0
            or explicit_weak_evidence
        )
    )
    trim_weight = float(migration_context.get("rebound_trim_weight", 0.03))
    main_weight = (
        position.main_shares * quote.price / portfolio_value
        if portfolio_value and quote.price > 0 else 0.0
    )
    excess_weight = max(main_weight - target_weight, 0.0)
    planned_weight = min(trim_weight, excess_weight)
    planned_shares = (
        _round_down_lot(portfolio_value * planned_weight / quote.price)
        if portfolio_value and quote.price > 0
        else 0
    )
    shares = min(position.main_shares, planned_shares)
    if bool(liquidity_rules.get("enabled", False)):
        liquidity_capacity = _adv_capacity(
            tech,
            liquidity_rules,
            "max_routine_trim_adv_ratio",
        )
        shares = min(shares, liquidity_capacity)
    checks = {
        "above_long_term_target": main_weight > target_weight,
        "data_ready": data_ready,
        "near_dynamic_resistance": near_resistance,
        "rebound_rejection": failed_break or bearish_reversal,
        "below_vwap": below_vwap,
        "volume_confirmed": volume_ready,
        "external_not_strong": external_not_strong,
        "sized_at_least_one_lot": shares >= LOT_SIZE,
    }
    for name, passed in checks.items():
        _check(diagnostics, "migration_trim", name, passed)
    diagnostics.setdefault("metrics", {})["migration_trim"] = {
        "resistance_distance": resistance_distance,
        "target_weight": target_weight,
        "current_weight": main_weight,
        "planned_weight": planned_weight,
        "sized_shares": shares,
    }
    if not all(checks.values()):
        return None
    level = round(tech.resistance, 2)
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="MIGRATION_TRIM",
        confidence="高" if failed_break and peer_change < 0 and market_change < 0 else "中",
        price=quote.price,
        key_level=level,
        action="反弹受阻，分批降低迁移主仓",
        shares=shares,
        reason=(
            "存量仓仍高于长期目标，反弹接近动态压力后15分钟转弱，"
            f"回到近似分时均价下方且同时间量能{tech.volume_ratio:.2f}倍"
        ),
        invalidation=f"完整15分钟放量站稳{level:.2f}上方且板块同步转强",
        event_id=f"{today.isoformat()}|{position.symbol}:MIGRATION_TRIM:{level:.2f}",
        category="strategy",
        details={
            "evidence": _evidence_note(evidence),
            "planned_nav_ratio": round(shares * quote.price / portfolio_value, 4),
            "migration": True,
            "event_rank": 1,
            **_liquidity_details(tech, shares, liquidity_rules),
        },
    )


def _satellite_entry(
    position,
    quote,
    tech,
    peer_change,
    market_change,
    today,
    rules,
    risk,
    available_cash,
    position_weight,
    portfolio_value,
    correlated_weight,
    correlated_cap,
    evidence,
    stage,
    diagnostics,
    position_sizing,
    migration_context,
    execution_constraints,
    liquidity_rules,
):
    data_ready = _intraday_data_ready(tech, rules)
    if peer_change is None or market_change is None:
        _check(diagnostics, "satellite_entry", "fresh_peer_and_market", False)
        return None
    evidence_ready = bool(evidence and evidence.satellite_entry_ready)
    support_distance = (quote.price - tech.support) / quote.price
    atr_ratio = (tech.atr14 / quote.price) if tech.atr14 and quote.price > 0 else None
    support_limit = _clamp(
        (atr_ratio or float(rules.get("support_distance_ratio", 0.008)))
        * float(rules.get("support_distance_atr_multiplier", 0.50)),
        float(rules.get("support_distance_min_ratio", 0.004)),
        float(rules.get("support_distance_max_ratio", 0.012)),
    )
    above_vwap = bool(tech.vwap and quote.price >= tech.vwap)
    recovered = bool(tech.last_15m_close and tech.last_15m_close >= tech.support)
    false_break_reclaim = bool(
        tech.last_15m_low is not None
        and tech.last_15m_close is not None
        and tech.last_15m_low <= tech.support
        and tech.last_15m_close > tech.support
    )
    bullish_recovery = bool(
        tech.last_15m_open is not None
        and tech.last_15m_close is not None
        and tech.last_15m_close > tech.last_15m_open
        and (
            tech.previous_15m_close is None
            or tech.last_15m_close > tech.previous_15m_close
        )
    )
    higher_low = bool(
        tech.last_15m_low is not None
        and tech.previous_15m_low is not None
        and tech.last_15m_low > tech.previous_15m_low
        and tech.last_15m_close is not None
        and tech.previous_15m_close is not None
        and tech.last_15m_close > tech.previous_15m_close
    )
    reversal = false_break_reclaim or bullish_recovery or higher_low
    volume_mode = _satellite_volume_mode(tech, rules)
    external_ok = (
        peer_change >= float(rules.get("minimum_peer_change_ratio", -0.003))
        and market_change >= float(rules.get("minimum_market_change_ratio", -0.005))
    )
    expected_spread = (tech.resistance - quote.price) / quote.price
    minimum_spread = max(
        float(rules.get("minimum_expected_spread_ratio", 0.03)),
        (atr_ratio or 0.0) * float(rules.get("minimum_target_atr_multiplier", 1.50)),
    )
    stop, stop_buffer = _adaptive_stop(tech, rules)
    downside = max(quote.price - stop, 0.01) / quote.price
    reward_risk = expected_spread / downside
    max_risk_amount = _entry_risk_budget(
        position,
        quote.price,
        risk,
        portfolio_value,
        float(rules.get("entry_risk_weight", 0.0025)),
        float(rules.get("max_remaining_risk_capacity_fraction", 0.10)),
        _migration_risk_principal(migration_context),
        today,
    )
    planned_shares, planned_weight = _planned_satellite_entry_shares(
        position,
        quote.price,
        portfolio_value,
        position_sizing,
        migration_context,
        rules,
    )
    shares = _sized_buy_shares(
        requested=planned_shares,
        price=quote.price,
        stop=stop,
        available_cash=available_cash,
        cash_fraction=float(rules.get("max_cash_fraction_per_trade", 0.50)),
        position_weight=position_weight,
        single_cap=_effective_satellite_cap(
            position, position_sizing, risk, migration_context
        ),
        portfolio_value=portfolio_value,
        correlated_weight=correlated_weight,
        correlated_cap=correlated_cap,
        max_risk_amount=max_risk_amount,
        execution_constraints=execution_constraints,
        liquidity_rules=liquidity_rules,
        tech=tech,
    )
    checks = {
        "no_pending_reduction_or_cooldown": not bool(
            migration_context.get("satellite_entry_block_reason")
        ),
        "data_ready": data_ready,
        "industry_and_announcements": evidence_ready,
        "margin_auxiliary_gate": _auxiliary_allows_entry(evidence),
        "not_in_stage_top": stage["label"] not in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"},
        "below_risk_warning": _loss_ratio(
            position, quote.price, _migration_risk_principal(migration_context), today=today
        ) < float(risk.get("warning_ratio", 0.20)),
        "at_or_above_support": support_distance >= 0,
        "within_atr_support_distance": support_distance <= support_limit,
        "above_vwap": above_vwap,
        "support_recovered": recovered,
        "reversal_structure": reversal,
        "valid_volume_mode": volume_mode != "invalid",
        "external_not_worsening": external_ok,
        "sized_at_least_one_lot": shares >= LOT_SIZE,
        "minimum_expected_spread": expected_spread >= minimum_spread,
        "minimum_reward_risk": reward_risk >= float(rules.get("minimum_reward_risk", 1.8)),
    }
    for name, passed in checks.items():
        _check(diagnostics, "satellite_entry", name, passed)
    diagnostics.setdefault("metrics", {})["satellite_entry"] = {
        "support_distance": support_distance,
        "support_distance_limit": support_limit,
        "expected_spread": expected_spread,
        "minimum_spread": minimum_spread,
        "reward_risk": reward_risk,
        "target": tech.resistance,
        "stop": stop,
        "stop_buffer_ratio": stop_buffer,
        "volume_mode": volume_mode,
        "risk_budget": max_risk_amount,
        "planned_weight": planned_weight,
        "current_weight": position_weight,
        "position_cap": _effective_satellite_cap(
            position, position_sizing, risk, migration_context
        ),
        "entry_block_reason": migration_context.get("satellite_entry_block_reason"),
        "sized_shares": shares,
    }
    if not all(checks.values()):
        return None
    level = round(tech.support, 2)
    confidence = "高" if (
        stage.get("bottom_confirmed")
        and evidence.industry_direction is not None
        and evidence.industry_direction > 0
        and peer_change > 0
        and market_change > 0
    ) else "中"
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="SAT_BUY",
        confidence=confidence,
        price=quote.price,
        key_level=level,
        action="建立超短线卫星仓",
        shares=shares,
        reason=(
            f"价格距动态支撑{support_distance:.2%}，15分钟出现企稳/收复结构并位于近似分时均价上方；"
            f"量能模式为{_volume_mode_text(volume_mode)}，至固定目标预计空间{expected_spread:.1%}，"
            f"收益风险比{reward_risk:.2f}"
        ),
        invalidation=f"完整15分钟跌破固定风险退出位{stop:.2f}，或向下跳空明显跌破时退出",
        event_id=f"{today.isoformat()}|{position.symbol}:SAT_BUY:{level:.2f}",
        category="satellite",
        details={
            "target": round(tech.resistance, 2),
            "stop": round(stop, 2),
            "reward_risk": round(reward_risk, 2),
            "volume_mode": volume_mode,
            "volume_samples": tech.volume_baseline_samples,
            "evidence": _evidence_note(evidence),
            "stage": stage["label"],
            "event_rank": 1,
            "company_direction": evidence.company_direction if evidence else None,
            "corporate_action_confirmation": bool(
                evidence and evidence.corporate_action_confirmation
            ),
            "corporate_action_strength": evidence.corporate_action_strength if evidence else 0,
            "margin_signal": evidence.margin_signal if evidence else "missing",
            "capital_flow_signal": evidence.capital_flow_signal if evidence else "missing",
            "shareholder_signal": evidence.shareholder_signal if evidence else "missing",
            **_liquidity_details(tech, shares, liquidity_rules),
            "planned_nav_ratio": round(shares * quote.price / portfolio_value, 4) if portfolio_value else 0.0,
        },
    )


def _active_satellite(
    position, quote, tech, peer_change, today, rules, holidays, evidence, stage,
    technical_data_fresh=True,
):
    sat = position.satellite
    held = trading_days_held(sat.entry_date, today, holidays)
    max_days = int(rules.get("max_holding_trading_days", 10))
    support = sat.entry_support
    target = sat.target_price
    stop = sat.stop_price or support * (1 - float(rules.get("invalidation_buffer_ratio", 0.015)))
    emergency_gap = float(rules.get("emergency_gap_below_stop_ratio", 0.01))
    confirmed_break = bool(
        technical_data_fresh and tech.complete_15m
        and tech.last_15m_close is not None and tech.last_15m_close < stop
    )
    gap_break = quote.price <= stop * (1 - emergency_gap)
    announcement_break = bool(
        evidence
        and evidence.announcement_risk == "critical"
        and technical_data_fresh
        and tech.complete_15m
        and (
            (tech.vwap is not None and quote.price < tech.vwap)
            or (tech.last_15m_close is not None and tech.last_15m_close < tech.support)
        )
    )
    confidence = "中"
    if quote.price < stop and (confirmed_break or gap_break):
        code, action, level = "SAT_EXIT", "退出卫星仓", stop
        reason = "跌破建仓时固定风险退出位；个股止损不等待同行确认"
        confidence = "高" if gap_break else "中"
    elif announcement_break:
        code, action, level = "SAT_EXIT", "退出卫星仓并重新审视主仓", quote.price
        reason = "交易所公告出现高风险事项且价格反应偏弱，短线逻辑失效"
        confidence = "高"
    elif technical_data_fresh and stage.get("top_confirmed"):
        code, action, level = "SAT_SELL", "阶段顶部确认，止盈退出卫星仓", quote.price
        reason = "阶段顶部反转结构已确认，卫星仓优先兑现"
        confidence = "高"
    elif quote.price >= target:
        code, action, level = "SAT_SELL", "止盈卖出卫星仓", target
        reason = "达到建仓时固定止盈目标"
    else:
        near_target = quote.price >= target * (1 - float(rules.get("target_near_ratio", 0.005)))
        reversal = (
            technical_data_fresh
            and tech.complete_15m
            and tech.last_15m_close is not None
            and tech.last_15m_open is not None
            and tech.last_15m_close < tech.last_15m_open
        )
        if near_target and reversal:
            code, action, level = "SAT_SELL", "止盈卖出卫星仓", target
            reason = "接近固定止盈目标且完整15分钟出现回落"
        elif held >= max_days:
            code, action, level = "SAT_EXIT", "到期重新评估，默认退出卫星仓", quote.price
            reason = f"含买入日已持有{held}个交易日，达到最长持有期"
        else:
            return None
    level = round(level, 2)
    peer_note = "同行走弱" if peer_change is not None and peer_change < 0 else "同行未同步走弱"
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code=code,
        confidence=confidence,
        price=quote.price,
        key_level=level,
        action=action,
        shares=sat.shares,
        reason=f"{reason}；{peer_note}",
        invalidation="本次卫星仓计划结束；不得自动转为主仓，重新建仓需重新满足全部条件",
        event_id=f"{today.isoformat()}|{position.symbol}:{code}:{level:.2f}",
        category="satellite",
        details={
            "holding_days": held,
            "target": round(target, 2),
            "stop": round(stop, 2),
            "evidence": _evidence_note(evidence),
            "stage": stage["label"],
            "top_confirmed": bool(stage.get("top_confirmed")),
            "event_rank": 2 if code == "SAT_EXIT" else 1,
            "technical_data_fresh": bool(technical_data_fresh),
        },
    )


def _risk_signals(
    position,
    quote,
    tech,
    peer_change,
    market_change,
    today,
    risk,
    rules,
    evidence,
    strategic_rules,
    diagnostics,
    migration_context,
    technical_data_fresh=True,
):
    if (
        _effective_risk_principal(
            position, _migration_risk_principal(migration_context)
        ) <= 0
        or position.total_shares <= 0
    ):
        return []
    loss_ratio = _loss_ratio(
        position,
        quote.price,
        _migration_risk_principal(migration_context),
        today=today,
    )
    hard = float(risk.get("max_loss_ratio", 0.25))
    near = float(risk.get("near_limit_ratio", 0.225))
    warning = float(risk.get("warning_ratio", 0.20))
    peer_resilient, _ = _peer_relative_strength(
        quote.change_ratio, peer_change, strategic_rules
    )
    weak_count = _weak_confirmation_count(
        peer_change, market_change, evidence, strategic_rules, quote.change_ratio
    )
    downside = _downside_setup(
        quote,
        tech,
        evidence,
        weak_count,
        rules,
        strategic_rules,
        peer_resilient=peer_resilient,
        market_change=market_change,
    )
    technical_weak = bool(technical_data_fresh and downside["triggered"])
    _check(diagnostics, "economic_risk", "warning_zone", loss_ratio >= warning, loss_ratio, warning)
    _check(diagnostics, "economic_risk", "technical_weakness", technical_weak)
    if loss_ratio < hard and not (loss_ratio >= warning and technical_weak):
        return []
    level = round(quote.price, 2)
    if loss_ratio >= hard:
        wording, confidence, reduction_ratio, event_rank = "触及单股25%硬风险上限", "高", 1.0, 3
    elif loss_ratio >= near:
        wording, confidence, reduction_ratio, event_rank = "接近硬风险上限且技术与板块继续恶化", "高", 0.20, 2
    else:
        wording, confidence, reduction_ratio, event_rank = "进入单股风险预警区且技术与板块同步转弱", "中", 0.10, 1
    if reduction_ratio >= 1.0:
        main_reduction = position.main_shares
    else:
        main_reduction = _planned_reduction_shares(position.main_shares, reduction_ratio)
    effective_basis = _effective_economic_basis(position, today)
    details = {
        "evidence": _evidence_note(evidence),
        "event_rank": event_rank,
        "loss_ratio": round(loss_ratio, 4),
        "economic_basis": position.economic_basis,
        "effective_economic_basis": effective_basis,
        "unapplied_dividend_cash": round(
            max(position.economic_basis - effective_basis, 0.0), 2
        ),
        **_liquidity_details(tech, main_reduction, {}, risk_exit=True),
    }
    action = "执行硬风险上限纪律，退出全部主仓" if loss_ratio >= hard else "优先降低主仓风险"
    if position.satellite.active:
        action = (
            "退出卫星仓及全部主仓"
            if loss_ratio >= hard else "先退出卫星仓，再降低主仓风险"
        )
        details["satellite_exit_shares"] = position.satellite.shares
    return [Signal(
        symbol=position.symbol,
        name=position.name,
        code="EMERGENCY_RISK",
        confidence=confidence,
        price=quote.price,
        key_level=level,
        action=action,
        shares=main_reduction,
        reason=wording,
        invalidation="仅在可靠价格确认风险状态解除后重新评估；不得用补仓重置既有风险",
        event_id=f"{today.isoformat()}|{position.symbol}:EMERGENCY_RISK",
        category="risk",
        details=details,
    )]


def _stage_assessment(
    quote,
    tech,
    peer_change,
    market_change,
    evidence,
    rules,
    memory: dict[str, Any] | None = None,
) -> dict[str, Any]:
    memory = memory or {}
    range_position = tech.range_position_60
    rsi = tech.rsi14
    previous_rsi = tech.previous_rsi14
    rsi_min = tech.rsi_min_5
    rsi_max = tech.rsi_max_5
    ma20_slope = tech.ma20_slope_5d
    atr = tech.atr14
    remembered_peak = float(memory.get("peak_price") or 0)
    stage_peak = max(float(tech.recent_high_60 or 0), remembered_peak, quote.price)
    drawdown = quote.price / stage_peak - 1 if stage_peak > 0 else None
    atr_extension = (
        (quote.price - tech.ma20) / atr if atr and atr > 0 else None
    )
    low_zone = range_position is not None and range_position <= float(rules.get("bottom_range_max", 0.25))
    oversold_seen = rsi_min is not None and rsi_min <= float(rules.get("bottom_rsi_seen_max", 38.0))
    bottom_context = bool(
        range_position is not None
        and range_position <= float(rules.get("bottom_tracking_range_max", 0.35))
        and oversold_seen
    )
    rsi_recovery = bool(
        rsi is not None
        and previous_rsi is not None
        and rsi > previous_rsi
        and rsi >= float(rules.get("bottom_rsi_recovery_min", 35.0))
    )
    slope_flattening = ma20_slope is not None and ma20_slope >= float(rules.get("bottom_ma20_slope_min", -0.012))
    intraday_recovery = bool(
        tech.complete_15m
        and tech.last_15m_close is not None
        and tech.last_15m_open is not None
        and tech.vwap is not None
        and tech.last_15m_close >= tech.support
        and tech.last_15m_close > tech.last_15m_open
        and quote.price >= tech.vwap
    )
    external_not_weak = bool(
        peer_change is not None
        and market_change is not None
        and peer_change >= float(rules.get("bottom_peer_minimum", -0.003))
        and market_change >= float(rules.get("bottom_market_minimum", -0.005))
        and evidence is not None
        and evidence.satellite_entry_ready
    )
    bottom_parts = [low_zone, oversold_seen, rsi_recovery, slope_flattening, intraday_recovery, external_not_weak]
    bottom_score = sum(bottom_parts)
    bottoming = low_zone and oversold_seen
    # “磨底确认”必须包含右侧价格确认和外部证据，不能仅凭超卖或低位猜底。
    bottom_confirmed = bool(
        bottom_context
        and intraday_recovery
        and external_not_weak
        and bottom_score >= int(rules.get("bottom_confirmation_score", 5))
    )
    right_side_intact = bool(rsi_recovery and slope_flattening and intraday_recovery)
    # 证据暂不可达时，不把已确认磨底回退为候选；仅在证据明确转弱或结构破坏时降级。
    memory_state = str(memory.get("state") or "NEUTRAL")
    if (
        not bottom_confirmed
        and memory_state == "BOTTOM_CONFIRMED"
        and bottom_context
        and right_side_intact
        and not _evidence_is_adverse(evidence)
        and peer_change is not None
        and market_change is not None
        and peer_change >= float(rules.get("bottom_peer_minimum", -0.003))
        and market_change >= float(rules.get("bottom_market_minimum", -0.005))
        and evidence is not None
        and (evidence.industry_unavailable or evidence.announcement_status == "missing")
    ):
        bottom_confirmed = True

    memory_started = str(memory.get("started_at") or "")
    memory_age = None
    if memory_started:
        try:
            memory_age = (quote.timestamp.date() - date.fromisoformat(memory_started)).days
        except ValueError:
            memory_age = None
    max_tracking_days = int(rules.get("top_state_max_calendar_days", 45))
    reset_drawdown = float(rules.get("top_state_reset_drawdown_ratio", 0.20))
    remembered_top = bool(
        memory_state in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"}
        and (memory_age is None or memory_age <= max_tracking_days)
        and (drawdown is None or drawdown >= -reset_drawdown)
    )
    high_zone = bool(
        (range_position is not None and range_position >= float(rules.get("top_range_min", 0.80)))
        or (drawdown is not None and drawdown >= -float(rules.get("top_high_distance_ratio", 0.04)))
    )
    recent_overbought_seen = rsi_max is not None and rsi_max >= float(rules.get("top_rsi_seen_min", 68.0))
    overbought_seen = bool(recent_overbought_seen or remembered_top)
    top_context = bool(
        high_zone or remembered_top
        or (
            overbought_seen
            and drawdown is not None
            and drawdown >= -float(rules.get("top_tracking_drawdown_ratio", 0.08))
        )
    )
    extended = atr_extension is not None and atr_extension >= float(rules.get("top_atr_extension_min", 1.5))
    rsi_rollover = bool(rsi is not None and previous_rsi is not None and rsi < previous_rsi)
    daily_rollover = quote.price < tech.ma5
    intraday_reversal = bool(
        tech.complete_15m
        and tech.last_15m_close is not None
        and tech.last_15m_open is not None
        and tech.vwap is not None
        and tech.last_15m_close < tech.last_15m_open
        and quote.price < tech.vwap
        and tech.volume_ratio is not None
        and tech.volume_ratio >= float(rules.get("top_reversal_volume_ratio", 1.0))
    )
    external_not_strong = bool(
        (peer_change is not None and peer_change <= float(rules.get("top_peer_strong_limit", 0.0)))
        or (market_change is not None and market_change <= float(rules.get("top_market_strong_limit", 0.0)))
        or (evidence is not None and evidence.downside_confirmation)
    )
    company_thesis_break = bool(
        evidence is not None
        and (
            evidence.company_direction is not None
            and evidence.company_direction < 0
            or evidence.announcement_risk == "critical"
        )
    )
    broad_external_weak = bool(
        peer_change is not None and peer_change < 0
        and market_change is not None and market_change < 0
    )
    top_parts = [top_context, overbought_seen, extended, rsi_rollover, daily_rollover, intraday_reversal, external_not_strong]
    top_score = sum(top_parts)
    near_top = bool((high_zone and (overbought_seen or extended)) or remembered_top)
    # “接近顶部”只用于停止追涨；顶部确认还必须有盘中反转、动量与短趋势回落、外部不再走强。
    top_confirmed = bool(
        top_context
        and overbought_seen
        and intraday_reversal
        and external_not_strong
        and rsi_rollover
        and daily_rollover
        and top_score >= int(rules.get("top_confirmation_score", 5))
    )
    full_exit_external_ready = (
        company_thesis_break
        if bool(rules.get("full_exit_requires_company_thesis_break", True))
        else (company_thesis_break or broad_external_weak)
    )
    full_exit_ready = bool(
        top_confirmed
        and drawdown is not None
        and drawdown <= -float(rules.get("top_full_exit_drawdown_ratio", 0.03))
        and quote.price < tech.ma10
        and full_exit_external_ready
    )
    top_trim_stage = int(memory.get("top_trim_stage", 0) or 0)
    execution_peak = float(memory.get("top_execution_peak") or 0)
    rearm_ratio = float(rules.get("top_rearm_new_high_ratio", 0.03))
    top_trim_rearmed = bool(
        top_trim_stage <= 0
        or execution_peak <= 0
        or stage_peak >= execution_peak * (1 + rearm_ratio)
    )
    if top_confirmed:
        label = "STAGE_TOP_CONFIRMED"
    elif near_top:
        label = "NEAR_STAGE_TOP"
    elif bottom_confirmed:
        label = "BOTTOM_CONFIRMED"
    elif bottoming:
        label = "BOTTOMING"
    else:
        label = "NEUTRAL"
    memory_update = _build_stage_memory_update(
        label=label,
        quote=quote,
        memory=memory,
        memory_state=memory_state,
        memory_started=memory_started,
        remembered_top=remembered_top,
        stage_peak=stage_peak,
    )
    return {
        "label": label,
        "bottoming": bottoming,
        "bottom_context": bottom_context,
        "bottom_confirmed": bottom_confirmed,
        "bottom_score": bottom_score,
        "right_side_intact": right_side_intact,
        "near_top": near_top,
        "top_context": top_context,
        "top_confirmed": top_confirmed,
        "top_score": top_score,
        "recent_overbought_seen": recent_overbought_seen,
        "remembered_top": remembered_top,
        "tracked_peak": stage_peak,
        "memory_update": memory_update,
        "full_exit_ready": full_exit_ready,
        "top_trim_stage": top_trim_stage,
        "top_trim_rearmed": top_trim_rearmed,
        "top_execution_peak": execution_peak or None,
        "company_thesis_break": company_thesis_break,
        "broad_external_weak": broad_external_weak,
        "range_position_60": range_position,
        "drawdown_from_high": drawdown,
        "rsi14": rsi,
        "rsi_min_5": rsi_min,
        "rsi_max_5": rsi_max,
        "ma20_slope_5d": ma20_slope,
        "atr_extension": atr_extension,
    }


def _downside_setup(
    quote,
    tech,
    evidence,
    weak_count,
    rules,
    strategic_rules,
    peer_resilient=False,
    market_change=None,
) -> dict[str, Any]:
    data_ready = _intraday_data_ready(tech, rules)
    below_support = bool(
        tech.last_15m_close is not None
        and quote.price < tech.support
        and tech.last_15m_close < tech.support
    )
    below_vwap = bool(tech.vwap is not None and quote.price < tech.vwap)
    break_depth_ratio = (
        max((tech.support - tech.last_15m_close) / tech.support, 0.0)
        if tech.last_15m_close is not None and tech.support > 0
        else 0.0
    )
    break_depth_atr = (
        max((tech.support - tech.last_15m_close) / tech.atr14, 0.0)
        if tech.last_15m_close is not None and tech.atr14 and tech.atr14 > 0
        else None
    )
    minimum_depth_ratio = max(
        float(strategic_rules.get("down_break_min_depth_ratio", 0.003)),
        (
            tech.atr14 / tech.support
            * float(strategic_rules.get("down_break_atr_multiplier", 0.10))
            if tech.atr14 and tech.atr14 > 0 and tech.support > 0
            else 0.0
        ),
    )
    deep_break = break_depth_ratio >= minimum_depth_ratio
    persistent_break = bool(
        below_support
        and tech.previous_15m_close is not None
        and tech.previous_15m_close < tech.support
    )
    confirmed_break = below_support and (deep_break or persistent_break)
    volume_confirmed = bool(
        tech.volume_ratio is not None
        and tech.volume_ratio >= float(rules.get("volume_confirmation_ratio", 1.30))
    )
    gap_ratio = quote.open / quote.previous_close - 1 if quote.previous_close > 0 and quote.open > 0 else 0.0
    gap_exception = bool(
        gap_ratio <= -float(strategic_rules.get("emergency_gap_down_ratio", 0.02))
        and below_support
    )
    critical = bool(evidence and evidence.announcement_risk == "critical")
    market_weak = bool(
        market_change is not None
        and market_change < float(strategic_rules.get("market_weak_ratio", -0.003))
    )
    observation_only = bool(
        strategic_rules.get("shallow_relative_strength_observation_only", True)
        and persistent_break
        and not deep_break
        and peer_resilient
        and not market_weak
        and not gap_exception
        and not critical
    )
    normal = data_ready and confirmed_break and below_vwap and volume_confirmed and weak_count >= int(
        strategic_rules.get("minimum_weak_confirmations", 1)
    ) and not observation_only
    exceptional = data_ready and below_support and below_vwap and (
        (gap_exception and (weak_count >= 1 or critical))
        or critical
    )
    high = bool(
        gap_exception
        or critical
        or (
            normal
            and (
                deep_break
                or not bool(strategic_rules.get("high_break_requires_depth", True))
            )
            and weak_count >= int(
                strategic_rules.get("high_break_minimum_weak_confirmations", 2)
            )
        )
    )
    return {
        "triggered": normal or exceptional,
        "high_confidence": high,
        "gap_exception": gap_exception,
        "critical_announcement": critical,
        "break_depth_ratio": break_depth_ratio,
        "break_depth_atr": break_depth_atr,
        "minimum_depth_ratio": minimum_depth_ratio,
        "deep_break": deep_break,
        "persistent_break": persistent_break,
        "observation_only": observation_only,
        "checks": {
            "data_ready": (data_ready, tech.volume_baseline_samples, f">={int(rules.get('minimum_volume_baseline_samples', 3))}个样本"),
            "below_support": (below_support, quote.price, f"<{tech.support:.2f}"),
            "break_depth_or_persistence": (
                confirmed_break or gap_exception or critical,
                {
                    "depth_ratio": round(break_depth_ratio, 6),
                    "minimum_ratio": round(minimum_depth_ratio, 6),
                    "persistent": persistent_break,
                },
                "达到动态深度阈值或连续两根完整15分钟K位于支撑下方",
            ),
            "below_vwap": (below_vwap, quote.price, f"<{_fmt(tech.vwap)}"),
            "volume_or_exception": (volume_confirmed or gap_exception or critical, tech.volume_ratio, f">={float(rules.get('volume_confirmation_ratio', 1.30)):.2f}或紧急例外"),
            "external_or_company_confirmation": (weak_count >= 1 or critical, weak_count, ">=1或高风险公告"),
            "shallow_relative_strength_gate": (
                not observation_only,
                {
                    "shallow_break": persistent_break and not deep_break,
                    "relative_resilient": bool(peer_resilient),
                    "market_weak": market_weak,
                },
                "浅破位且相对同行抗跌、大盘未弱时仅观察",
            ),
        },
    }


def _breakout_persistence(tech: Technicals, rules: dict) -> bool:
    if tech.last_15m_close is None or tech.previous_15m_close is None:
        return False
    two_closes = tech.last_15m_close > tech.resistance and tech.previous_15m_close > tech.resistance
    retest_tolerance = float(rules.get("breakout_retest_tolerance_ratio", 0.002))
    retest = bool(
        tech.last_15m_low is not None
        and tech.last_15m_low >= tech.resistance * (1 - retest_tolerance)
        and tech.last_15m_low <= tech.resistance * (1 + retest_tolerance)
        and tech.last_15m_close > tech.resistance
    )
    return two_closes or retest


def _intraday_data_ready(tech: Technicals, rules: dict) -> bool:
    return bool(
        tech.complete_15m
        and tech.last_15m_close is not None
        and tech.vwap is not None
        and tech.volume_ratio is not None
        and tech.volume_baseline_samples >= int(rules.get("minimum_volume_baseline_samples", 3))
    )


def _volume_confirmation_ratio(strategic_rules: dict, rules: dict) -> float:
    if "volume_confirmation_ratio" in strategic_rules:
        return float(strategic_rules["volume_confirmation_ratio"])
    return float(rules.get("volume_confirmation_ratio", 1.3))


def _breakout_intraday_confirmed(
    tech: Technicals,
    rules: dict,
    strategic_rules: dict,
) -> bool:
    """Confirm breakouts with standard volume or a relaxed, persistence-backed path."""
    if not _intraday_data_ready(tech, rules):
        return False
    volume_ratio = float(tech.volume_ratio or 0)
    if volume_ratio >= _volume_confirmation_ratio(strategic_rules, rules):
        return True
    relaxed = float(strategic_rules.get("breakout_relaxed_volume_ratio", 0.72))
    if volume_ratio < relaxed:
        return False
    if not _breakout_persistence(tech, strategic_rules):
        return False
    return bool(
        tech.last_15m_close is not None
        and tech.last_15m_close > tech.resistance
    )


def _confirmed_intraday(tech: Technicals, rules: dict) -> bool:
    return bool(
        _intraday_data_ready(tech, rules)
        and tech.volume_ratio >= float(rules.get("volume_confirmation_ratio", 1.3))
    )


def _satellite_volume_mode(tech: Technicals, rules: dict) -> str:
    if not _intraday_data_ready(tech, rules):
        return "invalid"
    expansion = float(rules.get("reversal_expansion_volume_ratio", 1.20))
    contraction_min = float(rules.get("contraction_volume_ratio_min", 0.60))
    if tech.volume_ratio >= expansion:
        return "expansion_reversal"
    controlled = bool(
        contraction_min <= tech.volume_ratio < expansion
        and tech.last_15m_close is not None
        and tech.last_15m_open is not None
        and tech.last_15m_close > tech.last_15m_open
        and tech.last_15m_low is not None
        and tech.last_15m_low >= tech.support
    )
    return "contraction_hold" if controlled else "invalid"


def _adaptive_stop(tech: Technicals, rules: dict) -> tuple[float, float]:
    atr_ratio = tech.atr14 / tech.support if tech.atr14 and tech.support > 0 else None
    fallback = float(rules.get("invalidation_buffer_ratio", 0.015))
    buffer_ratio = _clamp(
        (atr_ratio or fallback) * float(rules.get("stop_atr_multiplier", 0.90)),
        float(rules.get("stop_buffer_min_ratio", 0.010)),
        float(rules.get("stop_buffer_max_ratio", 0.025)),
    )
    return tech.support * (1 - buffer_ratio), buffer_ratio


def _sizing_value(
    position: Position,
    position_sizing: dict,
    key: str,
    default: float,
) -> float:
    return float(position.sizing.get(key, position_sizing.get(key, default)))


def _effective_single_cap(
    position: Position,
    position_sizing: dict,
    risk: dict,
    migration_context: dict[str, Any] | None = None,
) -> float:
    standard_cap = min(
        float(risk.get("max_single_position_ratio", 0.30)),
        _sizing_value(position, position_sizing, "max_single_position_weight", 0.30),
    )
    migration_context = migration_context or {}
    if bool(migration_context.get("enabled", False)):
        ceiling = float(migration_context.get("position_ceiling", standard_cap))
        return max(min(ceiling, 1.0), 0.0)
    return standard_cap


def _effective_satellite_cap(
    position: Position,
    position_sizing: dict,
    risk: dict,
    migration_context: dict[str, Any] | None = None,
) -> float:
    """Return the temporary total-position cap used by a satellite entry.

    A migrated main position keeps its ratcheting main-position ceiling.  The
    satellite is a separately risk-budgeted, time-limited overlay, so it may
    temporarily sit above that ceiling by only the configured overlay amount.
    Non-migrated holdings retain the ordinary single-name concentration cap.
    """
    migration_context = migration_context or {}
    if not bool(migration_context.get("enabled", False)):
        return _effective_single_cap(
            position, position_sizing, risk, migration_context
        )
    main_ceiling = float(migration_context.get("position_ceiling", 0.0))
    overlay = float(
        migration_context.get("satellite_overlay_max_weight", 0.035)
    )
    return max(min(main_ceiling + overlay, 1.0), 0.0)


def _planned_main_entry_shares(
    position: Position,
    price: float,
    portfolio_value: float,
    position_weight: float,
    risk: dict,
    position_sizing: dict,
    tranche_key: str,
    migration_context: dict[str, Any] | None = None,
) -> tuple[int, float]:
    if price <= 0 or portfolio_value <= 0:
        return 0, 0.0
    migration_context = migration_context or {}
    tranche_weight = _sizing_value(position, position_sizing, tranche_key, 0.08)
    target_weight = _sizing_value(position, position_sizing, "target_main_weight", 0.20)
    if bool(migration_context.get("enabled", False)):
        tranche_weight = float(migration_context.get("main_add_weight", tranche_weight))
        target_weight = float(migration_context.get("position_ceiling", target_weight))
    effective_target = min(
        target_weight,
        _effective_single_cap(position, position_sizing, risk, migration_context),
    )
    target_room = max(effective_target - position_weight, 0.0)
    planned_weight = min(tranche_weight, target_room)
    proportional_shares = _round_down_lot(portfolio_value * planned_weight / price)
    requested = min(proportional_shares, position.main_adjustment_shares)
    return requested, requested * price / portfolio_value


def _planned_watchlist_entry_shares(
    position: Position,
    price: float,
    portfolio_value: float,
    position_sizing: dict,
) -> tuple[int, float]:
    if price <= 0 or portfolio_value <= 0:
        return 0, 0.0
    starter_weight = _sizing_value(
        position, position_sizing, "watchlist_initial_weight", 0.05
    )
    proportional_shares = _round_down_lot(
        portfolio_value * starter_weight / price
    )
    requested = min(proportional_shares, position.main_adjustment_shares)
    return requested, requested * price / portfolio_value


def _planned_satellite_entry_shares(
    position: Position,
    price: float,
    portfolio_value: float,
    position_sizing: dict,
    migration_context: dict[str, Any] | None = None,
    satellite_rules: dict | None = None,
) -> tuple[int, float]:
    if price <= 0 or portfolio_value <= 0:
        return 0, 0.0
    satellite_rules = satellite_rules or {}
    satellite_weight = _sizing_value(position, position_sizing, "satellite_weight", 0.03)
    proportional_shares = _round_down_lot(portfolio_value * satellite_weight / price)
    # A-share board lots create a sharp discontinuity: a valid one-lot setup
    # used to become zero merely because 100 shares cost slightly more than the
    # target weight.  Permit exactly one lot within a small, explicit tolerance;
    # all cash, issuer, group, stop-risk and liquidity caps still apply later.
    one_lot_weight = LOT_SIZE * price / portfolio_value
    one_lot_max_weight = float(
        satellite_rules.get("one_lot_tolerance_max_weight", satellite_weight)
    )
    if (
        proportional_shares < LOT_SIZE
        and position.satellite_limit >= LOT_SIZE
        and one_lot_weight <= one_lot_max_weight
    ):
        proportional_shares = LOT_SIZE
    requested = min(proportional_shares, position.satellite_limit)
    return requested, requested * price / portfolio_value


def _entry_risk_budget(
    position,
    price,
    risk,
    portfolio_value,
    nav_risk_ratio,
    remaining_fraction,
    risk_principal_ceiling=None,
    today: date | None = None,
) -> float:
    if portfolio_value <= 0:
        return 0.0
    nav_budget = portfolio_value * nav_risk_ratio
    if position.total_shares <= 0:
        return nav_budget
    basis = _effective_economic_basis(position, today)
    current_loss = max(basis - position.total_shares * price, 0.0)
    risk_principal = _effective_risk_principal(position, risk_principal_ceiling)
    if risk_principal <= 0:
        return 0.0
    hard_capacity = risk_principal * float(risk.get("max_loss_ratio", 0.25))
    remaining_capacity = max(hard_capacity - current_loss, 0.0)
    return min(
        nav_budget,
        remaining_capacity * remaining_fraction,
    )


def _weak_confirmation_count(
    peer_change, market_change, evidence, rules, stock_change=None
) -> int:
    sources, _ = _weak_confirmation_context(
        peer_change, market_change, evidence, rules, stock_change
    )
    return len(sources)


def _peer_relative_strength(stock_change, peer_change, rules) -> tuple[bool, float | None]:
    if stock_change is None or peer_change is None:
        return False, None
    excess = float(stock_change) - float(peer_change)
    peer_weak = float(peer_change) < float(rules.get("peer_weak_ratio", 0.0))
    buffer_ratio = float(rules.get("peer_relative_strength_buffer_ratio", 0.01))
    return bool(peer_weak and excess >= buffer_ratio), excess


def _weak_confirmation_context(
    peer_change, market_change, evidence, rules, stock_change=None
) -> tuple[list[str], list[str]]:
    sources: list[str] = []
    divergences: list[str] = []
    peer_resilient, peer_relative_excess = _peer_relative_strength(
        stock_change, peer_change, rules
    )
    peer_weak = bool(
        peer_change is not None
        and peer_change < float(rules.get("peer_weak_ratio", 0.0))
        and not peer_resilient
    )
    market_weak = bool(
        market_change is not None
        and market_change < float(rules.get("market_weak_ratio", -0.003))
    )
    evidence_weak = bool(evidence is not None and evidence.downside_confirmation)
    if peer_weak:
        sources.append(f"同行均值{peer_change:+.2%}")
    elif peer_resilient and peer_relative_excess is not None:
        divergences.append(
            f"相对同行抗跌（超额{peer_relative_excess * 100:+.2f}个百分点）"
        )
    elif peer_change is not None:
        divergences.append(f"同行未同步走弱（{peer_change:+.2%}）")
    if market_weak:
        sources.append(f"市场均值{market_change:+.2%}")
    elif market_change is not None:
        divergences.append(f"市场未达到弱势阈值（{market_change:+.2%}）")
    if evidence_weak:
        sources.append("产业或公司证据转弱")
    elif evidence is not None:
        divergences.append("产业与公告未确认利空")
    margin_deleveraging = bool(
        evidence is not None
        and evidence.margin_status == "fresh"
        and evidence.margin_signal == "deleveraging"
    )
    if margin_deleveraging:
        sources.append("融资余额出现去杠杆")
    capital_outflow = bool(
        evidence is not None
        and evidence.capital_flow_status == "fresh"
        and evidence.capital_flow_signal in {"persistent_outflow", "single_day_outflow"}
    )
    if capital_outflow:
        sources.append("主力资金净流出")
    holder_dispersing = bool(
        evidence is not None
        and evidence.shareholder_status == "fresh"
        and evidence.shareholder_signal == "dispersing"
    )
    if holder_dispersing:
        sources.append("股东户数上升（筹码分散）")
    return sources, divergences


def _strong_confirmation_count(peer_change, market_change, evidence, rules) -> int:
    return sum((
        peer_change is not None and peer_change > float(rules.get("peer_strong_ratio", 0.002)),
        market_change is not None and market_change > float(rules.get("market_strong_ratio", 0.003)),
        evidence is not None
        and evidence.industry_direction is not None
        and evidence.industry_direction > 0
        and evidence.industry_strength >= 2,
        evidence is not None and evidence.company_direction is not None and evidence.company_direction > 0,
        # A verified corporate action is one confirmation at most. It never
        # replaces the technical/volume gates or the fresh industry gate.
        evidence is not None and evidence.corporate_action_confirmation,
        # Commodity options are one auxiliary confirmation only when the
        # underlying industry evidence is already fresh and positive. The
        # option layer can never replace the futures gate or trigger alone.
        evidence is not None and evidence.commodity_option_confirmation,
        # 股东户数下降（集中）可作为一项弱正向确认，不能替代产业门控。
        evidence is not None
        and evidence.shareholder_status == "fresh"
        and evidence.shareholder_signal == "concentrating",
    ))


def _short_trend_ready(quote, tech, strategic_rules: dict) -> bool:
    aligned = bool(strategic_rules.get("require_aligned_short_trend", True))
    if aligned:
        return bool(tech.ma5 >= tech.ma10 and quote.price >= tech.ma5)
    return bool(tech.ma5 >= tech.ma10 or quote.price >= tech.ma5)


def _daily_trend_ready(quote, tech, strategic_rules: dict) -> bool:
    slope_min = float(strategic_rules.get("minimum_ma20_slope_5d", 0.0))
    return bool(
        quote.price > tech.ma20
        and tech.ma20_slope_5d is not None
        and tech.ma20_slope_5d >= slope_min
        and _short_trend_ready(quote, tech, strategic_rules)
    )


def _margin_allows_entry(evidence: EquityEvidence | None) -> bool:
    # 缺失或陈旧时不猜测；两融只在官方日终数据新鲜且出现极端拥挤/去杠杆时
    # 作为新增仓位的否决性辅助门槛，永远不单独产生买卖信号。
    if evidence is None or evidence.margin_status != "fresh":
        return True
    return evidence.margin_signal not in {"extreme_crowding", "deleveraging"}


def _capital_flow_allows_entry(evidence: EquityEvidence | None) -> bool:
    if evidence is None or evidence.capital_flow_status != "fresh":
        return True
    return evidence.capital_flow_signal != "persistent_outflow"


def _auxiliary_allows_entry(evidence: EquityEvidence | None) -> bool:
    if evidence is not None and evidence.commodity_option_divergence:
        return False
    return _margin_allows_entry(evidence) and _capital_flow_allows_entry(evidence)


def overheat_watch_signal(
    position: Position,
    quote: Quote,
    tech: Technicals,
    today: date,
    stage: dict[str, Any],
    stage_rules: dict,
) -> Signal | None:
    """提醒级急涨过热：不产生买卖，只提示回踩风险。"""
    if not bool(stage_rules.get("overheat_watch_enabled", True)):
        return None
    atr_extension = stage.get("atr_extension")
    rsi = tech.rsi14
    atr_min = float(stage_rules.get("overheat_atr_extension_min", 3.0))
    rsi_min = float(stage_rules.get("overheat_rsi_min", 70.0))
    if atr_extension is None or rsi is None:
        return None
    if float(atr_extension) < atr_min or float(rsi) < rsi_min:
        return None
    level = round(quote.price, 2)
    return Signal(
        symbol=position.symbol,
        name=position.name,
        code="OVERHEAT_WATCH",
        confidence="中",
        price=quote.price,
        key_level=level,
        action="急涨过热提醒，不建议追涨",
        shares=0,
        reason=(
            f"价格相对MA20延伸{float(atr_extension):.2f}倍ATR，RSI14={float(rsi):.1f}；"
            f"短线偏离过大，回踩风险上升。本提醒不构成减仓指令"
        ),
        invalidation="RSI回落且价格重新贴近MA20后再评估追涨风险",
        event_id=f"{today.isoformat()}|{position.symbol}:OVERHEAT_WATCH",
        category="reminder",
        details={
            "atr_extension": atr_extension,
            "rsi14": rsi,
            "stage": stage.get("label"),
            "event_rank": 1,
        },
    )


def _loss_ratio(
    position: Position,
    price: float,
    risk_principal_ceiling: float | None = None,
    today: date | None = None,
) -> float:
    basis = _effective_economic_basis(position, today) if today is not None else position.economic_basis
    risk_principal = _effective_risk_principal(position, risk_principal_ceiling)
    if risk_principal <= 0:
        return 0.0
    economic_loss = basis - position.total_shares * price
    return economic_loss / risk_principal


def _migration_risk_principal(migration_context: dict[str, Any] | None) -> float | None:
    migration_context = migration_context or {}
    if not bool(migration_context.get("enabled", False)):
        return None
    value = migration_context.get("risk_principal_ceiling")
    return float(value) if value is not None else None


def _effective_risk_principal(position: Position, ceiling: float | None = None) -> float:
    """Return the immutable cycle risk denominator, never the residual basis.

    Migration mode explicitly supplies the risk principal agreed at system entry;
    subsequent sells and dividends must not shrink that denominator.
    """
    if ceiling is not None:
        return float(ceiling)
    value = position.risk_principal
    return float(value) if value is not None else max(float(position.economic_basis), 0.0)


def _is_record_date(position: Position, today: date) -> bool:
    for event in position.corporate_events:
        if str(event.get("type") or "cash_dividend") != "cash_dividend":
            continue
        record = str(event.get("record_date") or "")
        if record and record == today.isoformat():
            return True
    return False


def _unapplied_dividend_cash(position: Position, today: date) -> float:
    """Cash dividends already past ex-date but not yet subtracted from economic_basis."""
    total = 0.0
    for event in position.corporate_events:
        if str(event.get("type") or "cash_dividend") != "cash_dividend":
            continue
        if bool(event.get("basis_adjusted")):
            continue
        ex_raw = str(event.get("ex_date") or "")
        if not ex_raw:
            continue
        try:
            ex_date = date.fromisoformat(ex_raw)
        except ValueError:
            continue
        if today < ex_date:
            continue
        cash = float(event.get("cash_per_share") or 0)
        if cash <= 0:
            continue
        total += cash * position.total_shares
    return total


def _effective_economic_basis(position: Position, today: date | None = None) -> float:
    if today is None:
        return position.economic_basis
    return position.economic_basis - _unapplied_dividend_cash(position, today)


def _evidence_is_adverse(evidence: EquityEvidence | None) -> bool:
    if evidence is None:
        return False
    return bool(evidence.evidence_adverse)


def _build_stage_memory_update(
    *,
    label: str,
    quote: Quote,
    memory: dict[str, Any],
    memory_state: str,
    memory_started: str,
    remembered_top: bool,
    stage_peak: float,
) -> dict[str, Any]:
    today_iso = quote.timestamp.date().isoformat()
    now_iso = quote.timestamp.isoformat()
    bottom_started = str(memory.get("bottom_started_at") or "")
    top_execution = {
        "top_trim_stage": int(memory.get("top_trim_stage", 0) or 0),
        "top_executed_shares": int(memory.get("top_executed_shares", 0) or 0),
        "top_last_execution_date": memory.get("top_last_execution_date"),
        "top_last_execution_price": memory.get("top_last_execution_price"),
        "top_execution_peak": memory.get("top_execution_peak"),
        "top_pending_anchor_shares": memory.get("top_pending_anchor_shares"),
        "top_pending_shares": int(memory.get("top_pending_shares", 0) or 0),
        "top_pending_event_id": memory.get("top_pending_event_id"),
        "top_pending_price": memory.get("top_pending_price"),
        "top_pending_peak": memory.get("top_pending_peak"),
        "top_pending_full_exit": bool(memory.get("top_pending_full_exit", False)),
    }

    if label in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"}:
        started_at = (
            memory_started
            if memory_state in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"} and memory_started
            else today_iso
        )
        return {
            "state": label,
            "started_at": started_at,
            "peak_price": round(stage_peak, 4),
            "bottom_started_at": bottom_started or None,
            "last_updated": now_iso,
            **top_execution,
        }

    # 显式退出顶部跟踪窗口后才清峰；日常 NEUTRAL/BOTTOM 不得覆盖仍有效的顶部记忆。
    if memory_state in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"} and not remembered_top:
        return {
            "state": "NEUTRAL",
            "started_at": today_iso,
            "peak_price": 0.0,
            "bottom_started_at": None,
            "last_updated": now_iso,
            "top_trim_stage": 0,
            "top_executed_shares": 0,
            "top_last_execution_date": None,
            "top_last_execution_price": None,
            "top_execution_peak": None,
            "top_pending_anchor_shares": None,
            "top_pending_shares": 0,
            "top_pending_event_id": None,
            "top_pending_price": None,
            "top_pending_peak": None,
            "top_pending_full_exit": False,
        }
    if memory_state in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"} and remembered_top:
        # remembered_top 为真时 label 至少为 NEAR_STAGE_TOP；此处仅防御异常路径。
        return {
            "state": label if label in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"} else memory_state,
            "started_at": memory_started or today_iso,
            "peak_price": round(max(float(memory.get("peak_price") or 0), stage_peak), 4),
            "bottom_started_at": bottom_started or None,
            "last_updated": now_iso,
            **top_execution,
        }

    if label in {"BOTTOMING", "BOTTOM_CONFIRMED"}:
        if memory_state in {"BOTTOMING", "BOTTOM_CONFIRMED"} and (bottom_started or memory_started):
            started_at = bottom_started or memory_started
        else:
            started_at = today_iso
        return {
            "state": label,
            "started_at": started_at,
            "peak_price": 0.0,
            "bottom_started_at": started_at,
            "last_updated": now_iso,
            **top_execution,
        }

    return {
        "state": "NEUTRAL",
        "started_at": memory_started if memory_state == "NEUTRAL" and memory_started else today_iso,
        "peak_price": 0.0,
        "bottom_started_at": None,
        "last_updated": now_iso,
        **top_execution,
    }


def _evidence_note(evidence: EquityEvidence | None) -> str:
    return evidence.summary if evidence else "产业或公告证据不可用"


def _sized_buy_shares(
    requested: int,
    price: float,
    stop: float,
    available_cash: float,
    cash_fraction: float,
    position_weight: float,
    single_cap: float,
    portfolio_value: float,
    correlated_weight: float | None,
    correlated_cap: float | None,
    max_risk_amount: float | None,
    execution_constraints: dict | None = None,
    liquidity_rules: dict | None = None,
    tech: Technicals | None = None,
) -> int:
    if price <= 0 or portfolio_value <= 0:
        return 0
    execution_constraints = execution_constraints or {}
    liquidity_rules = liquidity_rules or {}
    reserve = float(execution_constraints.get("cash_reserve_amount", 0.0))
    fixed_buffer = float(execution_constraints.get("fixed_buy_cost_buffer", 0.0))
    variable_buffer = float(
        execution_constraints.get("variable_buy_cost_buffer_ratio", 0.0)
    )
    deployable_cash = max(available_cash - reserve, 0.0)
    cash_budget = deployable_cash * max(min(cash_fraction, 1.0), 0.0)
    cash_capacity = _round_down_lot(
        max(cash_budget - fixed_buffer, 0.0) / (price * (1 + variable_buffer))
    )
    capacities = [requested, cash_capacity]
    single_room = max(single_cap - position_weight, 0.0) * portfolio_value
    capacities.append(_round_down_lot(single_room / price))
    if correlated_weight is not None and correlated_cap is not None:
        group_room = max(correlated_cap - correlated_weight, 0.0) * portfolio_value
        capacities.append(_round_down_lot(group_room / price))
    if max_risk_amount is not None:
        per_share_risk = max(price - stop, 0.01)
        capacities.append(_round_down_lot(max_risk_amount / per_share_risk))
    if bool(liquidity_rules.get("enabled", False)):
        capacities.append(_adv_capacity(tech, liquidity_rules, "max_entry_adv_ratio"))
    return max(min(capacities), 0)


def _adv_capacity(
    tech: Technicals | None,
    liquidity_rules: dict,
    ratio_key: str,
) -> int:
    if tech is None or tech.adv20_shares is None or tech.adv20_shares <= 0:
        return 0
    minimum_samples = int(liquidity_rules.get("minimum_adv_samples", 15))
    if tech.adv_samples < minimum_samples:
        return 0
    ratio = float(liquidity_rules.get(ratio_key, 0.01))
    return _round_down_lot(tech.adv20_shares * max(ratio, 0.0))


def _liquidity_details(
    tech: Technicals | None,
    shares: int,
    liquidity_rules: dict,
    risk_exit: bool = False,
) -> dict[str, Any]:
    adv = tech.adv20_shares if tech is not None else None
    details: dict[str, Any] = {
        "adv20_shares": round(float(adv), 2) if adv is not None else None,
        "adv_samples": tech.adv_samples if tech is not None else 0,
        "order_adv_ratio": round(shares / adv, 6) if adv and shares > 0 else None,
    }
    if risk_exit:
        normal_ratio = float(liquidity_rules.get("max_routine_trim_adv_ratio", 0.05))
        stress_ratio = float(liquidity_rules.get("stressed_exit_adv_ratio", 0.025))
        details.update({
            "liquidity_does_not_block_exit": True,
            "normal_exit_tranche": _round_down_lot(adv * normal_ratio) if adv else None,
            "stressed_exit_tranche": _round_down_lot(adv * stress_ratio) if adv else None,
            "estimated_exit_days": ceil(shares / max(adv * normal_ratio, 1))
            if adv and shares > 0 else None,
        })
    return details


def _check(diagnostics, group, name, passed, observed=None, required=None) -> None:
    diagnostics.setdefault("checks", {}).setdefault(group, {})[name] = {
        "passed": bool(passed),
        "observed": observed,
        "required": required,
    }


def _fmt(value) -> str:
    return "暂缺" if value is None else f"{float(value):.2f}"


def _volume_mode_text(mode: str) -> str:
    return {
        "expansion_reversal": "放量反转",
        "contraction_hold": "缩量企稳",
    }.get(mode, "未确认")


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def _round_down_lot(shares: float) -> int:
    return int(floor(max(shares, 0) / LOT_SIZE) * LOT_SIZE)


def _planned_reduction_shares(current_shares: int, ratio: float) -> int:
    if current_shares <= 0:
        return 0
    return min(
        current_shares,
        max(LOT_SIZE, _round_down_lot(current_shares * ratio)),
    )


def _round_up_lot(shares: float) -> int:
    return int(ceil(max(shares, 0) / LOT_SIZE) * LOT_SIZE)
