from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from .models import Position, SatellitePosition


_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.getenv(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]
    positions: tuple[Position, ...]

    @property
    def timezone(self) -> str:
        return self.raw.get("timezone", "Asia/Shanghai")

    @property
    def schedule(self) -> tuple[str, ...]:
        return tuple(self.raw.get("schedule", ["09:15", "10:15", "13:15", "14:15"]))

    @property
    def holidays(self) -> set[str]:
        return {str(item) for item in self.raw.get("holidays", [])}

    def section(self, name: str) -> dict[str, Any]:
        return self.raw.get(name, {})


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = _expand(yaml.safe_load(handle) or {})
    positions: list[Position] = []
    for item in raw.get("portfolio", {}).get("positions", []):
        satellite_raw = item.get("satellite", {})
        entry_date = satellite_raw.get("entry_date")
        if isinstance(entry_date, str) and entry_date:
            entry_date = date.fromisoformat(entry_date)
        satellite = SatellitePosition(
            active=bool(satellite_raw.get("active", False)),
            shares=int(satellite_raw.get("shares", 0)),
            entry_price=_optional_float(satellite_raw.get("entry_price")),
            entry_date=entry_date if isinstance(entry_date, date) else None,
            entry_support=_optional_float(satellite_raw.get("entry_support")),
            target_price=_optional_float(satellite_raw.get("target_price")),
            stop_price=_optional_float(satellite_raw.get("stop_price")),
        )
        role_raw = item.get("role")
        inferred_role = (
            "watchlist"
            if role_raw is None
            and int(item.get("main_shares", 0)) == 0
            and not satellite.active
            and satellite.shares == 0
            else "holding"
        )
        watchlist_entry_date = item.get("watchlist_entry_date")
        if isinstance(watchlist_entry_date, str) and watchlist_entry_date:
            watchlist_entry_date = date.fromisoformat(watchlist_entry_date)
        corporate_events = tuple(
            _normalize_corporate_event(event, str(item["symbol"]).upper())
            for event in item.get("corporate_events", []) or []
        )
        position = Position(
            symbol=str(item["symbol"]).upper(),
            name=str(item["name"]),
            main_shares=int(item["main_shares"]),
            economic_basis=float(item["economic_basis"]),
            sector=str(item.get("sector", "generic")),
            satellite_limit=int(item.get("satellite_limit", 100)),
            main_adjustment_shares=int(item.get("main_adjustment_shares", 100)),
            peers=tuple(str(x).upper() for x in item.get("peers", [])),
            satellite=satellite,
            role=str(role_raw or inferred_role).strip().lower(),
            sizing={str(key): float(value) for key, value in item.get("sizing", {}).items()},
            migration={str(key): value for key, value in item.get("migration", {}).items()},
            watchlist_entry_date=(
                watchlist_entry_date if isinstance(watchlist_entry_date, date) else None
            ),
            corporate_events=corporate_events,
        )
        _validate_position(position)
        positions.append(position)
    if not positions:
        raise ValueError("portfolio.positions 至少需要一只股票")
    _validate_config(raw, positions)
    return AppConfig(raw=raw, positions=tuple(positions))


def _optional_float(value: Any) -> float | None:
    return None if value in (None, "") else float(value)


def _normalize_corporate_event(raw: Any, symbol: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError(f"{symbol} corporate_events 条目必须是对象")
    event_type = str(raw.get("type", "cash_dividend")).strip().lower()
    if event_type != "cash_dividend":
        raise ValueError(f"{symbol} corporate_events.type 暂仅支持 cash_dividend")
    record_date = raw.get("record_date")
    ex_date = raw.get("ex_date")
    if not record_date or not ex_date:
        raise ValueError(f"{symbol} cash_dividend 必须配置 record_date 与 ex_date")
    record = date.fromisoformat(str(record_date))
    ex = date.fromisoformat(str(ex_date))
    if ex < record:
        raise ValueError(f"{symbol} cash_dividend ex_date 不得早于 record_date")
    cash = float(raw.get("cash_per_share", 0) or 0)
    if cash < 0:
        raise ValueError(f"{symbol} cash_per_share 不得为负")
    return {
        "type": event_type,
        "record_date": record.isoformat(),
        "ex_date": ex.isoformat(),
        "cash_per_share": cash,
        "basis_adjusted": bool(raw.get("basis_adjusted", False)),
        "note": str(raw.get("note") or ""),
    }


def _validate_position(position: Position) -> None:
    if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", position.symbol):
        raise ValueError(f"{position.symbol} 证券代码必须为6位代码.SH或.SZ")
    if position.role not in {"holding", "watchlist"}:
        raise ValueError(f"{position.symbol} role 只能是 holding 或 watchlist")
    if position.main_shares < 0 or position.main_shares % 100:
        raise ValueError(f"{position.symbol} main_shares 必须是非负100股整数")
    if position.satellite_limit <= 0 or position.satellite_limit % 100:
        raise ValueError(f"{position.symbol} satellite_limit 必须是正100股整数")
    if position.main_adjustment_shares <= 0 or position.main_adjustment_shares % 100:
        raise ValueError(f"{position.symbol} main_adjustment_shares 必须是正100股整数")
    if position.economic_basis < 0:
        raise ValueError(f"{position.symbol} economic_basis 不得小于0")
    if position.total_shares > 0 and position.economic_basis <= 0:
        raise ValueError(f"{position.symbol} 有持仓时 economic_basis 必须大于0")
    if position.role == "holding" and position.total_shares <= 0:
        raise ValueError(f"{position.symbol} holding 必须有实际持仓；空仓请改为watchlist")
    if position.watchlist_entry_date is not None and position.watchlist_entry_date > date.today():
        raise ValueError(f"{position.symbol} watchlist_entry_date 不得晚于今天")
    allowed_sizing_keys = {
        "initial_main_weight",
        "trend_add_weight",
        "target_main_weight",
        "satellite_weight",
        "max_single_position_weight",
        "entry_risk_weight",
        "watchlist_initial_weight",
        "watchlist_entry_risk_weight",
    }
    unknown_sizing_keys = set(position.sizing) - allowed_sizing_keys
    if unknown_sizing_keys:
        raise ValueError(f"{position.symbol} sizing 包含未知配置: {sorted(unknown_sizing_keys)}")
    for key, value in position.sizing.items():
        if not 0 < float(value) <= 1:
            raise ValueError(f"{position.symbol} sizing.{key} 必须在(0, 1]之间")
    if position.migration:
        allowed_migration_keys = {
            "enabled",
            "initial_ceiling_weight",
            "risk_principal_ceiling",
        }
        unknown_migration_keys = set(position.migration) - allowed_migration_keys
        if unknown_migration_keys:
            raise ValueError(
                f"{position.symbol} migration 包含未知配置: {sorted(unknown_migration_keys)}"
            )
        if bool(position.migration.get("enabled", False)):
            ceiling = float(position.migration.get("initial_ceiling_weight", 0))
            principal = float(position.migration.get("risk_principal_ceiling", 0))
            if not 0 < ceiling <= 1:
                raise ValueError(
                    f"{position.symbol} migration.initial_ceiling_weight 必须在(0, 1]之间"
                )
            if principal <= 0:
                raise ValueError(
                    f"{position.symbol} migration.risk_principal_ceiling 必须大于0"
                )
    if position.satellite.active:
        if (
            position.satellite.shares <= 0
            or position.satellite.shares > position.satellite_limit
            or position.satellite.shares % 100
        ):
            raise ValueError(f"{position.symbol} 活动卫星仓股数无效")
        if (
            not position.satellite.entry_date
            or not position.satellite.entry_price
            or not position.satellite.entry_support
            or not position.satellite.target_price
        ):
            raise ValueError(
                f"{position.symbol} 活动卫星仓必须配置 entry_date、entry_price、entry_support 和 target_price"
            )
        if position.satellite.target_price <= position.satellite.entry_support:
            raise ValueError(f"{position.symbol} 卫星仓 target_price 必须高于 entry_support")
        if (
            position.satellite.stop_price is not None
            and position.satellite.stop_price >= position.satellite.entry_support
        ):
            raise ValueError(f"{position.symbol} 卫星仓 stop_price 必须低于 entry_support")
    elif position.satellite.shares != 0:
        raise ValueError(f"{position.symbol} 未激活卫星仓时 shares 必须为0")
    if position.role == "watchlist":
        if position.main_shares != 0 or position.economic_basis != 0:
            raise ValueError(f"{position.symbol} watchlist 必须保持 main_shares=0 且 economic_basis=0")
        if position.satellite.active or position.satellite.shares != 0:
            raise ValueError(f"{position.symbol} watchlist 不得持有卫星仓")
        if bool(position.migration.get("enabled", False)):
            raise ValueError(f"{position.symbol} watchlist 不得启用存量仓迁移模式")
        if position.watchlist_entry_date is not None:
            raise ValueError(f"{position.symbol} watchlist 尚未成交，不得配置 watchlist_entry_date")


def _validate_config(raw: dict[str, Any], positions: list[Position]) -> None:
    position_symbols = [position.symbol for position in positions]
    if len(position_symbols) != len(set(position_symbols)):
        raise ValueError("portfolio.positions 不得包含重复证券代码")
    if float(raw.get("portfolio", {}).get("available_cash", 0)) < 0:
        raise ValueError("portfolio.available_cash 不得小于0")
    summary = raw.get("daily_summary", {})
    if bool(summary.get("enabled", False)) and not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d",
        str(summary.get("time", "15:30")),
    ):
        raise ValueError("daily_summary.time 必须是 HH:MM 格式")
    risk = raw.get("risk", {})
    warning = float(risk.get("warning_ratio", 0.20))
    near = float(risk.get("near_limit_ratio", 0.225))
    hard = float(risk.get("max_loss_ratio", 0.25))
    if not 0 < warning < near < hard < 1:
        raise ValueError("risk 比例必须满足 0 < warning_ratio < near_limit_ratio < max_loss_ratio < 1")
    cap = float(risk.get("max_single_position_ratio", 0.45))
    if not 0 < cap <= 1:
        raise ValueError("max_single_position_ratio 必须在(0, 1]之间")
    for key in ("max_strategy_alerts_per_day", "max_satellite_alerts_per_day"):
        value = int(risk.get(key, 3))
        if not 1 <= value <= 10:
            raise ValueError(f"risk.{key} 必须在[1, 10]之间")
    data_source = raw.get("data_source", {})
    if int(data_source.get("max_quote_delay_seconds", 120)) <= 0:
        raise ValueError("data_source.max_quote_delay_seconds 必须大于0")
    if int(data_source.get("max_bar_lag_seconds", 300)) <= 0:
        raise ValueError("data_source.max_bar_lag_seconds 必须大于0")
    sizing = raw.get("position_sizing", {})
    sizing_defaults = {
        "initial_main_weight": 0.08,
        "trend_add_weight": 0.06,
        "target_main_weight": 0.20,
        "satellite_weight": 0.03,
        "max_single_position_weight": 0.30,
        "entry_risk_weight": 0.005,
        "watchlist_initial_weight": 0.05,
        "watchlist_entry_risk_weight": 0.0035,
    }
    effective_sizing = {
        key: float(sizing.get(key, default)) for key, default in sizing_defaults.items()
    }
    for key, value in effective_sizing.items():
        if not 0 < value <= 1:
            raise ValueError(f"position_sizing.{key} 必须在(0, 1]之间")
    if effective_sizing["target_main_weight"] > effective_sizing["max_single_position_weight"]:
        raise ValueError("target_main_weight 不得高于 max_single_position_weight")
    if effective_sizing["watchlist_initial_weight"] > effective_sizing["max_single_position_weight"]:
        raise ValueError("watchlist_initial_weight 不得高于 max_single_position_weight")
    if effective_sizing["watchlist_entry_risk_weight"] > effective_sizing["watchlist_initial_weight"]:
        raise ValueError("watchlist_entry_risk_weight 不得高于 watchlist_initial_weight")
    if effective_sizing["max_single_position_weight"] > cap:
        raise ValueError("position_sizing.max_single_position_weight 不得高于 risk.max_single_position_ratio")
    for position in positions:
        effective = {**effective_sizing, **position.sizing}
        if effective["target_main_weight"] > effective["max_single_position_weight"]:
            raise ValueError(f"{position.symbol} 目标主仓比例不得高于单股上限")
        if effective["watchlist_initial_weight"] > effective["max_single_position_weight"]:
            raise ValueError(f"{position.symbol} 观察标的起始比例不得高于单股上限")
        if effective["watchlist_entry_risk_weight"] > effective["watchlist_initial_weight"]:
            raise ValueError(f"{position.symbol} 观察标的风险预算不得高于起始比例")
        if effective["max_single_position_weight"] > cap:
            raise ValueError(f"{position.symbol} 单股上限不得高于 risk.max_single_position_ratio")
    migration = raw.get("migration_mode", {})
    if bool(migration.get("enabled", False)):
        for key in ("main_add_weight", "ratchet_buffer_weight", "rebound_trim_weight"):
            value = float(migration.get(key, 0))
            if not 0 < value <= 1:
                raise ValueError(f"migration_mode.{key} 必须在(0, 1]之间")
        groups = migration.get("correlation_groups", {})
        known_groups = set(risk.get("correlation_groups", {}))
        if set(groups) - known_groups:
            raise ValueError("migration_mode.correlation_groups 包含未知相关性分组")
        for name, group in groups.items():
            initial = float(group.get("initial_ceiling_weight", 0))
            standard = float(risk["correlation_groups"][name].get("max_ratio", 0))
            if not standard <= initial <= 1:
                raise ValueError(
                    f"迁移分组{name}初始上限不得低于长期上限且不得高于1"
                )
    daily_lookback = int(raw.get("data_source", {}).get("daily_lookback", 45))
    if raw.get("stage_rules") and daily_lookback < 65:
        raise ValueError("启用阶段状态识别时 data_source.daily_lookback 至少为65")
    known = {position.symbol for position in positions}
    grouped_symbols: dict[str, str] = {}
    for name, group in risk.get("correlation_groups", {}).items():
        symbols = {str(symbol).upper() for symbol in group.get("symbols", [])}
        if not symbols or not symbols.issubset(known):
            raise ValueError(f"相关性分组{name}包含未知或空证券列表")
        group_cap = float(group.get("max_ratio", 0))
        if not 0 < group_cap <= 1:
            raise ValueError(f"相关性分组{name}的 max_ratio 必须在(0, 1]之间")
        for symbol in symbols:
            previous = grouped_symbols.get(symbol)
            if previous is not None:
                raise ValueError(
                    f"{symbol} 同时属于相关性分组{previous}和{name}，当前版本不允许重叠"
                )
            grouped_symbols[symbol] = name
    evidence = raw.get("evidence", {})
    if bool(raw.get("strategic_rules", {}).get("main_add_enabled", False)) and not bool(
        evidence.get("enabled", False)
    ):
        raise ValueError("启用主仓加仓时必须启用 evidence 产业与公告证据")
    supported_sectors = {
        "copper",
        "insurance",
        "insurance_financial_group",
        "new_energy_vehicle",
        "satellite_communications",
        "semiconductor",
        "optical_communications",
    }
    unknown_sectors = {
        position.sector for position in positions
        if position.role != "watchlist"
    } - supported_sectors
    if bool(evidence.get("enabled", False)) and unknown_sectors:
        raise ValueError(f"以下行业缺少证据路由: {sorted(unknown_sectors)}")
    copper = evidence.get("copper", {})
    if float(copper.get("negative_change_ratio", -0.003)) >= float(
        copper.get("positive_change_ratio", 0.003)
    ):
        raise ValueError("evidence.copper 涨跌阈值顺序无效")
    if float(copper.get("negative_trend_ratio", -0.006)) >= float(
        copper.get("positive_trend_ratio", 0.006)
    ):
        raise ValueError("evidence.copper 趋势阈值顺序无效")
    if float(copper.get("warrant_change_ratio", 0.03)) <= 0:
        raise ValueError("evidence.copper.warrant_change_ratio 必须大于0")
    announcements = evidence.get("announcements", {})
    if int(announcements.get("max_operating_documents", 2)) < 0:
        raise ValueError("evidence.announcements.max_operating_documents 不得小于0")
    if int(announcements.get("max_pdf_pages", 20)) <= 0:
        raise ValueError("evidence.announcements.max_pdf_pages 必须大于0")
    if float(announcements.get("operating_change_threshold_pct", 3.0)) <= 0:
        raise ValueError("evidence.announcements.operating_change_threshold_pct 必须大于0")
    corporate_actions = evidence.get("corporate_actions", {})
    if bool(corporate_actions.get("enabled", False)):
        if int(corporate_actions.get("max_age_calendar_days", 60)) <= 0:
            raise ValueError("evidence.corporate_actions.max_age_calendar_days 必须大于0")
        if int(corporate_actions.get("max_pdf_pages", 25)) <= 0:
            raise ValueError("evidence.corporate_actions.max_pdf_pages 必须大于0")
        if int(corporate_actions.get("minimum_body_characters", 80)) < 20:
            raise ValueError("evidence.corporate_actions.minimum_body_characters 不得小于20")
        archive_lookahead = int(
            corporate_actions.get("sse_archive_lookahead_days", 1)
        )
        if not 0 <= archive_lookahead <= 1:
            raise ValueError("evidence.corporate_actions.sse_archive_lookahead_days 必须为0或1")
    insurance = evidence.get("insurance", {})
    if float(insurance.get("negative_daily_bp", -1.0)) >= float(
        insurance.get("positive_daily_bp", 1.0)
    ):
        raise ValueError("evidence.insurance 利率阈值顺序无效")
    if float(insurance.get("negative_monthly_bp", -3.0)) >= float(
        insurance.get("positive_monthly_bp", 0.0)
    ):
        raise ValueError("evidence.insurance 月度利率阈值顺序无效")
    new_energy_vehicle = evidence.get("new_energy_vehicle", {})
    if float(new_energy_vehicle.get("negative_sales_yoy_pct", -5.0)) >= float(
        new_energy_vehicle.get("positive_sales_yoy_pct", 5.0)
    ):
        raise ValueError("evidence.new_energy_vehicle 产销阈值顺序无效")
    semiconductor = evidence.get("semiconductor", {})
    if float(semiconductor.get("negative_ic_output_yoy_pct", -5.0)) >= float(
        semiconductor.get("positive_ic_output_yoy_pct", 5.0)
    ):
        raise ValueError("evidence.semiconductor 集成电路产量阈值顺序无效")
    if float(semiconductor.get("negative_industry_value_yoy_pct", -2.0)) >= float(
        semiconductor.get("positive_industry_value_yoy_pct", 3.0)
    ):
        raise ValueError("evidence.semiconductor 行业增加值阈值顺序无效")
    optical = evidence.get("optical_communications", {})
    if float(optical.get("negative_business_volume_yoy_pct", -3.0)) >= float(
        optical.get("positive_business_volume_yoy_pct", 5.0)
    ):
        raise ValueError("evidence.optical_communications 业务总量阈值顺序无效")
    if float(optical.get("negative_fiber_length_yoy_pct", -2.0)) >= float(
        optical.get("positive_fiber_length_yoy_pct", 2.0)
    ):
        raise ValueError("evidence.optical_communications 光缆线路阈值顺序无效")
    if float(optical.get("negative_revenue_yoy_pct", -8.0)) >= float(
        optical.get("minimum_revenue_yoy_pct", -3.0)
    ):
        raise ValueError("evidence.optical_communications 收入阈值顺序无效")
    strategic = raw.get("strategic_rules", {})
    if float(strategic.get("minimum_reward_risk", 1.8)) <= 0:
        raise ValueError("strategic_rules.minimum_reward_risk 必须大于0")
    if float(strategic.get("main_entry_priority_bonus", 0.25)) < 0:
        raise ValueError("strategic_rules.main_entry_priority_bonus 不得小于0")
    for key in (
        "max_remaining_risk_capacity_fraction",
        "medium_confidence_reduction_ratio",
        "high_confidence_reduction_ratio",
    ):
        value = float(strategic.get(key, 0.01))
        if not 0 < value <= 1:
            raise ValueError(f"strategic_rules.{key} 必须在(0, 1]之间")
    if float(strategic.get("medium_confidence_reduction_ratio", 0.15)) > float(
        strategic.get("high_confidence_reduction_ratio", 0.25)
    ):
        raise ValueError("主仓中置信度减仓比例不得高于高置信度")
    down_break_depth = float(strategic.get("down_break_min_depth_ratio", 0.003))
    if not 0 < down_break_depth <= 0.05:
        raise ValueError("strategic_rules.down_break_min_depth_ratio 必须在(0, 0.05]之间")
    down_break_atr = float(strategic.get("down_break_atr_multiplier", 0.10))
    if not 0 < down_break_atr <= 1:
        raise ValueError("strategic_rules.down_break_atr_multiplier 必须在(0, 1]之间")
    peer_relative_buffer = float(
        strategic.get("peer_relative_strength_buffer_ratio", 0.01)
    )
    if not 0 <= peer_relative_buffer <= 0.10:
        raise ValueError(
            "strategic_rules.peer_relative_strength_buffer_ratio 必须在[0, 0.10]之间"
        )
    high_confirmations = int(
        strategic.get("high_break_minimum_weak_confirmations", 2)
    )
    if not 2 <= high_confirmations <= 3:
        raise ValueError("strategic_rules.high_break_minimum_weak_confirmations 必须在[2, 3]之间")
    notification = raw.get("notification", {})
    evidence_limit = int(notification.get("evidence_char_limit", 240))
    margin_limit = int(notification.get("margin_char_limit", 120))
    if not 100 <= evidence_limit <= 1000:
        raise ValueError("notification.evidence_char_limit 必须在[100, 1000]之间")
    if not 60 <= margin_limit <= 500:
        raise ValueError("notification.margin_char_limit 必须在[60, 500]之间")
    watchlist = raw.get("watchlist_rules", {})
    allowed_watch_nodes = {"10:15", "13:15", "14:15"}
    configured_watch_nodes = {
        str(value)
        for value in watchlist.get(
            "allowed_nodes", ["10:15", "13:15", "14:15"]
        )
    }
    if not configured_watch_nodes or not configured_watch_nodes.issubset(allowed_watch_nodes):
        raise ValueError("watchlist_rules.allowed_nodes 只能包含10:15、13:15和14:15")
    for key, default in (
        ("minimum_expected_spread_ratio", 0.05),
        ("minimum_reward_risk", 2.0),
        ("max_cash_fraction_per_entry", 1.0),
    ):
        value = float(watchlist.get(key, default))
        if not 0 < value <= 1 and key != "minimum_reward_risk":
            raise ValueError(f"watchlist_rules.{key} 必须在(0, 1]之间")
        if key == "minimum_reward_risk" and value <= 0:
            raise ValueError("watchlist_rules.minimum_reward_risk 必须大于0")
    watch_confirmations = int(watchlist.get("minimum_strong_confirmations", 2))
    if not 2 <= watch_confirmations <= 4:
        raise ValueError("watchlist_rules.minimum_strong_confirmations 必须在[2, 4]之间")
    if int(watchlist.get("starter_cooldown_trading_days", 3)) < 0:
        raise ValueError("watchlist_rules.starter_cooldown_trading_days 不得小于0")
    execution = raw.get("execution_constraints", {})
    for key in ("cash_reserve_amount", "fixed_buy_cost_buffer"):
        if float(execution.get(key, 0)) < 0:
            raise ValueError(f"execution_constraints.{key} 不得小于0")
    variable_buffer = float(execution.get("variable_buy_cost_buffer_ratio", 0.0))
    if not 0 <= variable_buffer < 1:
        raise ValueError(
            "execution_constraints.variable_buy_cost_buffer_ratio 必须在[0, 1)之间"
        )
    liquidity = raw.get("liquidity", {})
    if bool(liquidity.get("enabled", False)):
        if int(liquidity.get("adv_lookback_days", 20)) != 20:
            raise ValueError("当前版本 liquidity.adv_lookback_days 固定为20")
        if not 1 <= int(liquidity.get("minimum_adv_samples", 15)) <= 20:
            raise ValueError("liquidity.minimum_adv_samples 必须在[1, 20]之间")
        for key, default in (
            ("max_entry_adv_ratio", 0.01),
            ("max_routine_trim_adv_ratio", 0.05),
            ("stressed_exit_adv_ratio", 0.025),
        ):
            value = float(liquidity.get(key, default))
            if not 0 < value <= 1:
                raise ValueError(f"liquidity.{key} 必须在(0, 1]之间")
    margin = raw.get("margin_financing", {})
    if bool(margin.get("enabled", False)):
        if int(margin.get("minimum_observations", 5)) < 2:
            raise ValueError("margin_financing.minimum_observations 至少为2")
        crowded = float(margin.get("crowded_change_ratio", 0.08))
        deleveraging = float(margin.get("deleveraging_change_ratio", -0.05))
        extreme = float(margin.get("extreme_crowding_ratio", 0.12))
        if not deleveraging < 0 < crowded <= extreme:
            raise ValueError("margin_financing 两融变化阈值顺序无效")
    capital_flow = raw.get("capital_flow", {})
    if bool(capital_flow.get("enabled", False)):
        if int(capital_flow.get("lookback_sessions", 5)) < 1:
            raise ValueError("capital_flow.lookback_sessions 至少为1")
        if float(capital_flow.get("persistent_outflow_yuan", -1)) >= 0:
            raise ValueError("capital_flow.persistent_outflow_yuan 必须为负")
        if float(capital_flow.get("persistent_inflow_yuan", 1)) <= 0:
            raise ValueError("capital_flow.persistent_inflow_yuan 必须为正")
    shareholder = raw.get("shareholder_count", {})
    if bool(shareholder.get("enabled", False)):
        concentrate = float(shareholder.get("concentrate_change_ratio", -0.05))
        disperse = float(shareholder.get("disperse_change_ratio", 0.05))
        if not concentrate < 0 < disperse:
            raise ValueError("shareholder_count 集中/分散阈值顺序无效")
    satellite = raw.get("satellite_rules", {})
    if float(satellite.get("minimum_reward_risk", 1.8)) <= 0:
        raise ValueError("satellite_rules.minimum_reward_risk 必须大于0")
    if not 0 < float(satellite.get("max_remaining_risk_capacity_fraction", 0.10)) <= 1:
        raise ValueError("satellite_rules.max_remaining_risk_capacity_fraction 必须在(0, 1]之间")
    if not (
        0 < float(satellite.get("support_distance_min_ratio", 0.004))
        <= float(satellite.get("support_distance_max_ratio", 0.012))
        < 1
    ):
        raise ValueError("卫星仓支撑距离上下限顺序无效")
    if not (
        0 < float(satellite.get("stop_buffer_min_ratio", 0.010))
        <= float(satellite.get("stop_buffer_max_ratio", 0.025))
        < 1
    ):
        raise ValueError("卫星仓止损缓冲上下限顺序无效")
    stage = raw.get("stage_rules", {})
    if stage:
        bottom_range = float(stage.get("bottom_range_max", 0.25))
        bottom_tracking = float(stage.get("bottom_tracking_range_max", 0.35))
        top_range = float(stage.get("top_range_min", 0.80))
        if not 0 < bottom_range < bottom_tracking < top_range < 1:
            raise ValueError("阶段低位与高位区间阈值顺序无效")
        top_distance = float(stage.get("top_high_distance_ratio", 0.04))
        top_tracking = float(stage.get("top_tracking_drawdown_ratio", 0.08))
        if not 0 < top_distance <= top_tracking < 1:
            raise ValueError("阶段顶部距离与跟踪回撤阈值顺序无效")
        if int(stage.get("top_state_max_calendar_days", 45)) <= 0:
            raise ValueError("stage_rules.top_state_max_calendar_days 必须大于0")
        reset_drawdown = float(stage.get("top_state_reset_drawdown_ratio", 0.20))
        if not 0 < reset_drawdown < 1:
            raise ValueError("stage_rules.top_state_reset_drawdown_ratio 必须在(0, 1)之间")
        if not 1 <= int(stage.get("bottom_confirmation_score", 5)) <= 6:
            raise ValueError("bottom_confirmation_score 必须在[1, 6]范围")
        if not 1 <= int(stage.get("top_confirmation_score", 5)) <= 7:
            raise ValueError("top_confirmation_score 必须在[1, 7]范围")
        for key in ("top_trim_ratio", "high_top_trim_ratio"):
            value = float(stage.get(key, 0.25))
            if not 0 < value <= 1:
                raise ValueError(f"stage_rules.{key} 必须在(0, 1]之间")
