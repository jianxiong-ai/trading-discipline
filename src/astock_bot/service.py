from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from statistics import fmean
from zoneinfo import ZoneInfo

from .config import AppConfig
from .datasource import EastmoneyPublicSource, TencentPublicSource
from .evidence import OfficialEvidenceCollector
from .indicators import compute_daily_levels, compute_technicals
from .models import Signal
from .notifier import FeishuNotifier, render_daily_summary, render_message
from .portfolio_store import PortfolioStore
from .state import StateStore
from .strategy import evaluate_position, overheat_watch_signal


class MonitorService:
    _PERSISTENT_REDUCTION_CODES = {
        "EMERGENCY_RISK", "DOWN_BREAK", "STAGE_TOP_EXIT", "MIGRATION_TRIM",
        "SAT_EXIT", "SAT_SELL",
    }

    @classmethod
    def _semantic_event_key(cls, signal: Signal) -> str:
        """Keep an unchanged reduction signal from repeating on every date."""
        if signal.code in cls._PERSISTENT_REDUCTION_CODES:
            return signal.event_id.split("|", 1)[1] if "|" in signal.event_id else signal.event_id
        return signal.event_id

    def _trading_days_since(self, start: str | None, current) -> int:
        if not start:
            return 0
        try:
            cursor = current.fromisoformat(str(start))
        except (TypeError, ValueError):
            return 0
        if cursor >= current:
            return 0
        days = 0
        while cursor < current:
            cursor = cursor.fromordinal(cursor.toordinal() + 1)
            if cursor.weekday() < 5 and cursor.isoformat() not in self.config.holidays:
                days += 1
        return days

    def _pending_reduction_reminder_due(
        self, signal: Signal, now: datetime, sent_rank: int | None
    ) -> bool:
        if signal.code not in self._PERSISTENT_REDUCTION_CODES or sent_rank is None:
            return False
        active = self.state.active_signal(signal.symbol)
        semantic_key = self._semantic_event_key(signal)
        active_key = str(active.get("semantic_key") or active.get("event_id", "")).split("|", 1)[-1]
        if active_key != semantic_key or not active.get("last_notified_date"):
            return False
        settings = self.config.section("notification")
        first_days = max(int(settings.get("pending_reduction_reminder_first_days", 1)), 1)
        repeat_days = max(int(settings.get("pending_reduction_reminder_repeat_days", 2)), 1)
        if signal.code == "EMERGENCY_RISK":
            # 硬风险未处理时每天复提醒，不等待普通减仓的冷却周期。
            repeat_days = 1
        reminder_count = int(active.get("reminder_count", 0) or 0)
        interval = first_days if reminder_count == 0 else repeat_days
        elapsed = self._trading_days_since(active.get("last_notified_date"), now.date())
        return elapsed >= interval

    def _record_reduction_notification(self, signal: Signal, day) -> None:
        if signal.code not in self._PERSISTENT_REDUCTION_CODES:
            return
        active = self.state.active_signal(signal.symbol)
        if not active:
            return
        if str(active.get("semantic_key") or "") != self._semantic_event_key(signal):
            return
        active["last_notified_date"] = day.isoformat()
        if signal.details.get("pending_reminder"):
            active["reminder_count"] = int(active.get("reminder_count", 0) or 0) + 1
        else:
            active.setdefault("reminder_count", 0)
        self.state.save_active_signal(signal.symbol, active)

    def _sync_stage_execution(self, position, stage_memory: dict, today, allow_state_update: bool) -> dict:
        """Confirm a notified top tranche only after the recorded holding shrinks.

        A top signal is an instruction, not an execution. Keeping the planned
        tranche pending until the user records a sell prevents the strategy
        from silently advancing its top-management ladder.
        """
        memory = dict(stage_memory or {})
        anchor = int(memory.get("top_pending_anchor_shares", 0) or 0)
        if anchor <= position.main_shares:
            return memory
        executed = anchor - position.main_shares
        if executed < 100:
            return memory
        memory.update({
            "top_trim_stage": int(memory.get("top_trim_stage", 0) or 0) + 1,
            "top_executed_shares": int(memory.get("top_executed_shares", 0) or 0) + executed,
            "top_last_execution_date": today.isoformat(),
            "top_last_execution_price": memory.get("top_pending_price"),
            "top_execution_peak": memory.get("top_pending_peak"),
            "top_pending_anchor_shares": None,
            "top_pending_shares": 0,
            "top_pending_event_id": None,
            "top_pending_price": None,
            "top_pending_peak": None,
            "top_pending_full_exit": False,
        })
        if allow_state_update:
            self.state.save_stage_state(position.symbol, memory)
        return memory

    def _record_stage_top_signal(self, signal: Signal) -> None:
        """Store the notified top tranche as pending until a recorded trade confirms it."""
        if signal.code != "STAGE_TOP_EXIT":
            return
        memory = self.state.stage_state(signal.symbol)
        pending_event = memory.get("top_pending_event_id")
        if pending_event == signal.event_id:
            return
        memory.update({
            "top_pending_anchor_shares": int(signal.details.get("position_main_shares", 0) or 0),
            "top_pending_shares": int(signal.shares or 0),
            "top_pending_event_id": signal.event_id,
            "top_pending_price": signal.price,
            "top_pending_peak": signal.details.get("tracked_peak"),
            "top_pending_full_exit": bool(signal.details.get("full_exit")),
        })
        self.state.save_stage_state(signal.symbol, memory)

    def _sync_reduction_lifecycle(
        self,
        position,
        signals: list[Signal],
        technical_fresh: bool,
        allow_state_update: bool,
    ) -> None:
        """Persist an active reduction and clear its dedupe when it resolves.

        This is deliberately gated by fresh technical data. A stale/missing
        quote must not clear a live reduction and make it eligible for a
        duplicate alert on the next trading day.
        """
        if not allow_state_update or not technical_fresh:
            return
        active = self.state.active_signal(position.symbol)
        candidates = [
            signal for signal in signals
            if signal.code in self._PERSISTENT_REDUCTION_CODES
        ]
        if candidates:
            current = min(candidates, key=self._signal_priority)
            semantic_key = self._semantic_event_key(current)
            old_key = str(active.get("semantic_key") or active.get("event_id", "")).split("|", 1)[-1]
            if old_key and old_key != semantic_key and active.get("code") in self._PERSISTENT_REDUCTION_CODES:
                self.state.clear_sent_semantic_key(old_key)
            same_signal = bool(old_key and old_key == semantic_key and active.get("code") == current.code)
            self.state.save_active_signal(position.symbol, {
                "code": current.code,
                "event_id": current.event_id,
                "semantic_key": semantic_key,
                "date": current.details.get("signal_date") or current.event_id.split("|", 1)[0],
                "first_seen_date": active.get("first_seen_date") if same_signal else current.event_id.split("|", 1)[0],
                "last_notified_date": active.get("last_notified_date") if same_signal else None,
                "reminder_count": int(active.get("reminder_count", 0) or 0) if same_signal else 0,
                "key_level": current.key_level,
                "position_main_shares": int(current.details.get("position_main_shares", position.main_shares) or position.main_shares),
            })
            return
        if active.get("code") in self._PERSISTENT_REDUCTION_CODES:
            old_key = str(active.get("semantic_key") or active.get("event_id", "")).split("|", 1)[-1]
            if old_key:
                self.state.clear_sent_semantic_key(old_key)
            self.state.clear_active_signal(position.symbol)

    def __init__(self, config: AppConfig):
        self.config = config
        ds = config.section("data_source")
        provider = str(ds.get("provider", "tencent_public"))
        source_class = TencentPublicSource if provider == "tencent_public" else EastmoneyPublicSource
        self.source = source_class(config.timezone, int(ds.get("timeout_seconds", 8)), int(ds.get("retries", 2)))
        self.state = StateStore(str(config.raw.get("state_file", "/app/data/state.json")))
        notify = config.section("notification")
        self.notifier = FeishuNotifier(str(notify.get("webhook", "")), str(notify.get("secret", "")))
        self.portfolio_store = PortfolioStore.from_config(
            Path(os.getenv("ASTOCK_CONFIG", "config.yaml")), config.raw
        )
        self.evidence_char_limit = int(notify.get("evidence_char_limit", 240))
        self.margin_char_limit = int(notify.get("margin_char_limit", 120))
        self.tz = ZoneInfo(config.timezone)

    @staticmethod
    def _confidence_rank(value: str) -> int:
        return {"低": 1, "中": 2, "高": 3}.get(str(value), 0)

    @staticmethod
    def _critical_signal(signal: Signal) -> bool:
        return signal.category == "risk" or signal.code in {"EMERGENCY_RISK", "STAGE_TOP_EXIT"}

    def _notification_targets(self) -> list[dict]:
        """Return the workspace's one all-portfolio Feishu destination."""
        if self.portfolio_store is not None:
            settings = self.portfolio_store.notification_settings(self.config.raw, include_secrets=True)
            if settings.get("webhook") and not settings.get("unreadable"):
                return [{
                    "id": int(settings["id"]), "name": "默认通知",
                    "webhook": settings["webhook"], "secret": settings.get("secret", ""),
                    "min_confidence": settings.get("min_confidence", "中"),
                }]
        notification = self.config.section("notification")
        if bool(self.config.raw.get("_workspace_seed_static", True)) and str(notification.get("webhook", "")).strip():
            return [{
                "id": None, "name": "默认通知（兼容）", "webhook": str(notification.get("webhook", "")),
                "secret": str(notification.get("secret", "")), "min_confidence": "中",
            }]
        return []

    def _target_accepts_signal(self, target: dict, signal: Signal) -> bool:
        # Every configured workspace receives its entire portfolio and watchlist.
        # The confidence threshold is intentionally the only routing rule.
        return self._confidence_rank(signal.confidence) >= self._confidence_rank(target.get("min_confidence", "中"))

    def _route_action_signals(self, signals: list[Signal]) -> dict[int | None, tuple[dict, list[Signal]]]:
        routed: dict[int | None, tuple[dict, list[Signal]]] = {}
        for target in self._notification_targets():
            matched = [signal for signal in signals if self._target_accepts_signal(target, signal)]
            if matched:
                routed[target.get("id")] = (target, matched)
        return routed

    def _target_accepts_summary_row(self, target: dict, row: dict) -> bool:
        return bool(target.get("webhook"))

    def _delivery_claimed(self, target: dict, key: str, kind: str) -> bool:
        if target.get("id") is None or self.portfolio_store is None:
            return False
        return self.portfolio_store.notification_setting_delivery_claimed(key, kind)

    def _send_target(self, target: dict, key: str, kind: str, message: str) -> bool:
        if target.get("id") is not None and self.portfolio_store is not None:
            # Claim before the external call: a client timeout may still mean
            # Feishu accepted the message, so silently retrying would duplicate it.
            if self._delivery_claimed(target, key, kind):
                return False
            self.portfolio_store.mark_notification_setting_delivery(key, kind, "claimed", message)
        try:
            FeishuNotifier(str(target["webhook"]), str(target.get("secret", ""))).send(message)
        except Exception as exc:
            if target.get("id") is not None and self.portfolio_store is not None:
                self.portfolio_store.mark_notification_setting_delivery(key, kind, "uncertain", message, str(exc))
            raise
        if target.get("id") is not None and self.portfolio_store is not None:
            self.portfolio_store.mark_notification_setting_delivery(key, kind, "sent", message)
        return True

    def run_node(
        self,
        node: str,
        dry_run: bool = False,
        now: datetime | None = None,
        execution_type: str = "manual",
    ) -> dict:
        now = now or datetime.now(self.tz)
        if dry_run:
            execution_type = "dry_run"
        if now.weekday() >= 5 or now.date().isoformat() in self.config.holidays:
            return self._result(node, "NO_ALERT", [], ["休市日"])
        quotes = {}
        daily = {}
        intraday = {}
        warnings: list[str] = []
        symbols = {p.symbol for p in self.config.positions}
        symbols.update(x for p in self.config.positions for x in p.peers)
        symbols.update(self.config.raw.get("market_indices", []))
        for symbol in symbols:
            try:
                quotes[symbol] = self.source.quote(symbol)
            except Exception as exc:
                warnings.append(f"{symbol} quote: {exc}")
        evidence_by_symbol = {}
        evidence_config = self.config.section("evidence")
        if bool(evidence_config.get("enabled", False)):
            collector_config = dict(evidence_config)
            collector_config["margin_financing"] = self.config.section("margin_financing")
            collector_config["capital_flow"] = self.config.section("capital_flow")
            collector_config["shareholder_count"] = self.config.section("shareholder_count")
            collector = OfficialEvidenceCollector(self.config.timezone, collector_config)
            evidence_by_symbol, evidence_warnings = collector.collect(self.config.positions, now)
            warnings.extend(evidence_warnings)
        max_delay = int(self.config.section("data_source").get("max_quote_delay_seconds", 120))
        if node != "09:15":
            for position in self.config.positions:
                quote = quotes.get(position.symbol)
                if not quote or not self._is_fresh(quote, now, max_delay):
                    warnings.append(f"{position.symbol} 行情延迟超过{max_delay}秒")
        signals: list[Signal] = []
        summaries: list[dict] = []
        cash = float(self.config.raw.get("portfolio", {}).get("available_cash", 0))
        invested_positions = [
            position for position in self.config.positions if position.total_shares > 0
        ]
        complete_portfolio_quotes = all(
            position.symbol in quotes
            and self._is_fresh(quotes[position.symbol], now, max_delay)
            for position in invested_positions
        )
        capital_cash = cash if complete_portfolio_quotes else 0.0
        if not complete_portfolio_quotes:
            warnings.append("持仓行情不完整，已关闭本节点所有新增仓位建议")
        portfolio_value = (
            cash
            + sum(
                position.total_shares * quotes[position.symbol].price
                for position in invested_positions
            )
            if complete_portfolio_quotes
            else 0.0
        )
        migration_contexts, migration_group_caps = self._migration_contexts(
            quotes,
            portfolio_value,
            complete_portfolio_quotes,
            allow_state_update=not dry_run,
            today=now.date(),
        )
        active_satellite_count = sum(
            1 for position in self.config.positions
            if position.role == "holding" and position.satellite.active
        )
        for position in self.config.positions:
            quote = quotes.get(position.symbol)
            equity_evidence = evidence_by_symbol.get(position.symbol)
            if not quote:
                summaries.append({
                    "symbol": position.symbol,
                    "role": position.role,
                    "status": "DATA_MISSING",
                    "evidence": equity_evidence.summary if equity_evidence else "证据不可用",
                })
                continue
            try:
                daily[position.symbol] = self.source.daily_bars(position.symbol, int(self.config.section("data_source").get("daily_lookback", 45)))
                if node != "09:15":
                    intraday[position.symbol] = self.source.five_minute_bars(position.symbol, int(self.config.section("data_source").get("intraday_lookback", 120)))
                else:
                    intraday[position.symbol] = []
                if node == "09:15":
                    levels = compute_daily_levels(daily[position.symbol], now.date())
                    daily_fresh, daily_reason = self._daily_as_of_fresh(
                        levels.daily_as_of, now.date()
                    )
                    if not daily_fresh:
                        warnings.append(f"{position.symbol} {daily_reason}")
                    summaries.append({
                        "symbol": position.symbol,
                        "role": position.role,
                        "status": "BASELINE" if daily_fresh else "STALE_TECH",
                        "price": quote.price,
                        "change_pct": quote.change_ratio * 100,
                        "support": round(levels.support, 2),
                        "resistance": round(levels.resistance, 2),
                        "ma5": round(levels.ma5, 2),
                        "ma10": round(levels.ma10, 2),
                        "ma20": round(levels.ma20, 2),
                        "next_resistance": round(levels.next_resistance, 2) if levels.next_resistance else None,
                        "atr14": round(levels.atr14, 4) if levels.atr14 is not None else None,
                        "rsi14": round(levels.rsi14, 2) if levels.rsi14 is not None else None,
                        "rsi_min_5": round(levels.rsi_min_5, 2) if levels.rsi_min_5 is not None else None,
                        "rsi_max_5": round(levels.rsi_max_5, 2) if levels.rsi_max_5 is not None else None,
                        "ma20_slope_5d": levels.ma20_slope_5d,
                        "recent_high_60": round(levels.recent_high_60, 2) if levels.recent_high_60 else None,
                        "recent_low_60": round(levels.recent_low_60, 2) if levels.recent_low_60 else None,
                        "daily_as_of": levels.daily_as_of.isoformat() if levels.daily_as_of else None,
                        "adv20_shares": round(levels.adv20_shares, 2) if levels.adv20_shares else None,
                        "adv_samples": levels.adv_samples,
                        "evidence": equity_evidence.summary if equity_evidence else "证据不可用",
                        "evidence_add_ready": bool(equity_evidence and equity_evidence.add_ready),
                        "corporate_action_status": (
                            equity_evidence.corporate_action_status if equity_evidence else "missing"
                        ),
                        "corporate_action_confirmation": bool(
                            equity_evidence and equity_evidence.corporate_action_confirmation
                        ),
                        "corporate_action_summary": (
                            equity_evidence.corporate_action_summary
                            if equity_evidence else "正向公司行动证据不可用"
                        ),
                        "evidence_items": self._evidence_items(equity_evidence),
                    })
                    continue
                if any(item.startswith(f"{position.symbol} 行情延迟") for item in warnings):
                    summaries.append({
                        "symbol": position.symbol,
                        "role": position.role,
                        "status": "STALE",
                        "price": quote.price,
                        "change_pct": quote.change_ratio * 100,
                    })
                    continue
                tech = compute_technicals(daily[position.symbol], intraday[position.symbol], quote.price, quote.timestamp)
                technical_fresh, freshness_reasons = self._technical_data_fresh(tech, quote, now)
                if not technical_fresh:
                    warnings.extend(f"{position.symbol} {reason}" for reason in freshness_reasons)
                peer_changes = [
                    quotes[x].change_ratio for x in position.peers
                    if x in quotes and self._is_fresh(quotes[x], now, max_delay)
                ]
                index_changes = [
                    quotes[x].change_ratio for x in self.config.raw.get("market_indices", [])
                    if x in quotes and self._is_fresh(quotes[x], now, max_delay)
                ]
                minimum_peers = min(
                    len(position.peers),
                    int(self.config.section("data_source").get("minimum_fresh_peer_count", 2)),
                )
                minimum_indices = int(self.config.section("data_source").get("minimum_fresh_index_count", 3))
                peer_change = fmean(peer_changes) if len(peer_changes) >= minimum_peers and minimum_peers > 0 else None
                market_change = fmean(index_changes) if len(index_changes) >= minimum_indices else None
                if peer_change is None:
                    warnings.append(f"{position.symbol} 同行新鲜样本不足")
                if market_change is None:
                    warnings.append(f"{position.symbol} 市场指数新鲜样本不足")
                correlated_weight, correlated_cap = self._correlated_exposure(
                    position.symbol,
                    quotes,
                    portfolio_value,
                    migration_group_caps,
                )
                diagnostics: dict = {}
                stage_memory = self._sync_stage_execution(
                    position,
                    self.state.stage_state(position.symbol),
                    now.date(),
                    allow_state_update=not dry_run,
                )
                migration_context = self._migration_satellite_context(
                    position,
                    migration_contexts.get(position.symbol, {}),
                    stage_memory,
                    now.date(),
                )
                if migration_context:
                    migration_contexts[position.symbol] = migration_context
                found = evaluate_position(
                    position, quote, tech, node, peer_change, market_change, now.date(),
                    self.config.section("satellite_rules"), self.config.section("risk"), self.config.holidays,
                    capital_cash,
                    (position.total_shares * quote.price / portfolio_value) if portfolio_value else 1.0,
                    portfolio_value,
                    correlated_weight,
                    correlated_cap,
                    self.config.section("strategic_rules"),
                    active_satellite_count,
                    equity_evidence,
                    self.config.section("stage_rules"),
                    diagnostics,
                    self.config.section("position_sizing"),
                    migration_context,
                    technical_fresh,
                    stage_memory,
                    self.config.section("watchlist_rules"),
                    self.config.section("execution_constraints"),
                    self.config.section("liquidity"),
                )
                resolved = self._resolved_down_break_signal(
                    position,
                    quote,
                    tech,
                    now.date(),
                    peer_change,
                    found,
                    technical_fresh,
                    allow_state_update=not dry_run,
                )
                if resolved:
                    # 先明确撤销旧减仓建议；本节点不同时生成新的加仓建议。
                    found = [resolved]
                exit_codes = {
                    "DOWN_BREAK", "STAGE_TOP_EXIT", "MIGRATION_TRIM",
                    "SAT_EXIT", "SAT_SELL", "EMERGENCY_RISK",
                }
                if (
                    node != "09:15"
                    and technical_fresh
                    and not any(signal.code in exit_codes for signal in found)
                ):
                    reminder = overheat_watch_signal(
                        position,
                        quote,
                        tech,
                        now.date(),
                        diagnostics.get("stage", {}),
                        self.config.section("stage_rules"),
                    )
                    if reminder:
                        found = [*found, reminder]
                # A late recovery is still an official node result. Persisting
                # its stage snapshot prevents missed top/bottom transitions
                # when a container wakes after the normal node window.
                if execution_type in {"scheduled", "scheduled_recovery"} and technical_fresh:
                    memory_update = diagnostics.get("stage", {}).get("memory_update")
                    if memory_update:
                        self.state.save_stage_state(position.symbol, memory_update)
                self._sync_reduction_lifecycle(
                    position, found, technical_fresh, allow_state_update=not dry_run
                )
                for signal in found:
                    signal.details["change_pct"] = quote.change_ratio * 100
                signals.extend(found)
                summaries.append({
                    "symbol": position.symbol,
                    "role": position.role,
                    "status": "ALERT" if found else ("STALE_TECH" if not technical_fresh else "NO_ALERT"),
                    "price": quote.price,
                    "change_pct": quote.change_ratio * 100,
                    "support": round(tech.support, 2), "resistance": round(tech.resistance, 2),
                    "next_resistance": round(tech.next_resistance, 2) if tech.next_resistance else None,
                    "vwap": round(tech.vwap, 2) if tech.vwap else None,
                    "vwap_quality": tech.vwap_quality,
                    "volume_ratio": round(tech.volume_ratio, 2) if tech.volume_ratio is not None else None,
                    "volume_samples": tech.volume_baseline_samples,
                    "adv20_shares": round(tech.adv20_shares, 2) if tech.adv20_shares else None,
                    "adv_samples": tech.adv_samples,
                    "complete_15m": tech.complete_15m,
                    "ma5": round(tech.ma5, 2),
                    "ma10": round(tech.ma10, 2),
                    "ma20": round(tech.ma20, 2),
                    "atr14": round(tech.atr14, 4) if tech.atr14 is not None else None,
                    "rsi14": round(tech.rsi14, 2) if tech.rsi14 is not None else None,
                    "range_position_60": tech.range_position_60,
                    "ma20_slope_5d": tech.ma20_slope_5d,
                    "peer_change_pct": peer_change * 100 if peer_change is not None else None,
                    "market_change_pct": market_change * 100 if market_change is not None else None,
                    "stage": diagnostics.get("stage", {}),
                    "checks": diagnostics.get("checks", {}),
                    "metrics": diagnostics.get("metrics", {}),
                    "migration": migration_contexts.get(position.symbol, {}),
                    "evidence": equity_evidence.summary if equity_evidence else "证据不可用",
                    "industry_status": equity_evidence.industry_status if equity_evidence else "missing",
                    "industry_direction": equity_evidence.industry_direction if equity_evidence else None,
                    "announcement_status": equity_evidence.announcement_status if equity_evidence else "missing",
                    "announcement_risk": equity_evidence.announcement_risk if equity_evidence else "unknown",
                    "company_direction": equity_evidence.company_direction if equity_evidence else None,
                    "corporate_action_status": (
                        equity_evidence.corporate_action_status if equity_evidence else "missing"
                    ),
                    "corporate_action_direction": (
                        equity_evidence.corporate_action_direction if equity_evidence else None
                    ),
                    "corporate_action_strength": (
                        equity_evidence.corporate_action_strength if equity_evidence else 0
                    ),
                    "corporate_action_stage": (
                        equity_evidence.corporate_action_stage if equity_evidence else None
                    ),
                    "corporate_action_body_status": (
                        equity_evidence.corporate_action_body_status if equity_evidence else "missing"
                    ),
                    "corporate_action_confirmation": bool(
                        equity_evidence and equity_evidence.corporate_action_confirmation
                    ),
                    "corporate_action_summary": (
                        equity_evidence.corporate_action_summary
                        if equity_evidence else "正向公司行动证据不可用"
                    ),
                    "margin_status": equity_evidence.margin_status if equity_evidence else "missing",
                    "margin_signal": equity_evidence.margin_signal if equity_evidence else "missing",
                    "margin_balance_change_5d": (
                        equity_evidence.margin_balance_change_5d if equity_evidence else None
                    ),
                    "capital_flow_status": (
                        equity_evidence.capital_flow_status if equity_evidence else "missing"
                    ),
                    "capital_flow_signal": (
                        equity_evidence.capital_flow_signal if equity_evidence else "missing"
                    ),
                    "capital_flow_net_5d": (
                        equity_evidence.capital_flow_net_5d if equity_evidence else None
                    ),
                    "capital_flow_summary": (
                        equity_evidence.capital_flow_summary
                        if equity_evidence else "资金面辅助数据不可用"
                    ),
                    "shareholder_status": (
                        equity_evidence.shareholder_status if equity_evidence else "missing"
                    ),
                    "shareholder_signal": (
                        equity_evidence.shareholder_signal if equity_evidence else "missing"
                    ),
                    "shareholder_change_ratio": (
                        equity_evidence.shareholder_change_ratio if equity_evidence else None
                    ),
                    "shareholder_summary": (
                        equity_evidence.shareholder_summary
                        if equity_evidence else "股东户数辅助数据不可用"
                    ),
                    "evidence_items": self._evidence_items(equity_evidence),
                })
            except Exception as exc:
                warnings.append(f"{position.symbol} analysis: {exc}")
                summaries.append({
                    "symbol": position.symbol,
                    "role": position.role,
                    "status": "DATA_MISSING",
                    "price": quote.price,
                    "change_pct": quote.change_ratio * 100,
                })

        selected, selection_suppressed = self._rank_capital_entries(signals)
        sendable, notification_suppressed = self._filter_sendable(selected, now)
        suppressed = [*selection_suppressed, *notification_suppressed]
        routed = self._route_action_signals(sendable) if not dry_run else {}
        sent_ids: set[str] = set()
        suppressed_by_id = {item["event_id"]: item["reason"] for item in suppressed}
        for signal in signals:
            if signal.event_id in {item.event_id for item in sendable}:
                signal.details["notification_status"] = "would_send" if dry_run else (
                    "routed" if routed else "no_subscriber"
                )
            elif signal.event_id in suppressed_by_id:
                signal.details["notification_status"] = "suppressed"
                signal.details["suppression_reason"] = suppressed_by_id[signal.event_id]
            else:
                signal.details["notification_status"] = "candidate"
        delivered: list[Signal] = []
        if sendable and not dry_run and routed:
            title = str(self.config.section("notification").get("title", "A股持仓纪律"))
            for target, target_signals in routed.values():
                message = render_message(
                    target_signals, node, title,
                    self.evidence_char_limit, self.margin_char_limit,
                )
                delivery_key = ";".join(sorted(signal.event_id for signal in target_signals))
                try:
                    if self._send_target(target, delivery_key, "action", message):
                        delivered.extend(target_signals)
                except Exception as exc:
                    warnings.append(f"{target.get('name', '机器人')} 飞书发送失败: {exc}")
            sent_ids = {signal.event_id for signal in delivered}
            for signal in signals:
                if signal.event_id in sent_ids:
                    signal.details["notification_status"] = "sent"
            for signal in sendable:
                if signal.event_id not in sent_ids:
                    continue
                self.state.mark_sent(
                    signal.event_id,
                    now.date(),
                    signal.category,
                    int(signal.details.get("event_rank", 1) or 1),
                    semantic_key=self._semantic_event_key(signal),
                )
                self._record_reduction_notification(signal, now.date())
                self._record_stage_top_signal(signal)
                if signal.code == "FALSE_BREAK":
                    self.state.clear_active_signal(signal.symbol)
            self.state.mark_notification(
                now.date(),
                {
                    signal.category
                    for signal in delivered
                    if signal.category in {"strategy", "satellite", "reminder"}
                },
            )
        result = self._result(
            node,
            "ALERT" if delivered or dry_run and sendable else "NO_ALERT",
            signals,
            warnings,
            summaries,
            sent_signals=delivered if not dry_run else sendable,
            suppressed=suppressed,
        )
        self._append_log(result, now, execution_type)
        return result

    def run_daily_summary(self, dry_run: bool = False, now: datetime | None = None) -> dict:
        now = now or datetime.now(self.tz)
        if now.weekday() >= 5 or now.date().isoformat() in self.config.holidays:
            return self._result("15:30", "NO_SUMMARY", [], ["休市日"])
        dividend_settlements: list[dict] = []
        dividend_warnings: list[str] = []
        if not dry_run:
            dividend_settlements, dividend_warnings = self._sync_auto_dividends(now)
        records = self._formal_records_for_day(now)
        rows = self._daily_summary_rows(records)
        nodes = list(self.config.schedule)
        present_nodes = {str(record.get("node")) for record in records}
        missing_nodes = [node for node in nodes if node not in present_nodes]
        warnings = [
            str(warning)
            for record in records
            for warning in record.get("warnings", [])
            if warning
        ]
        warnings.extend(dividend_warnings)
        if missing_nodes:
            warnings.append("缺少正式节点：" + "、".join(missing_nodes))
        title = str(self.config.section("notification").get("title", "A股持仓纪律"))
        ledger_notes = [
            f"自动分红入账：{item['name']} ¥{float(item['amount']):.2f}（发放日 {item['payment_date']}）"
            for item in dividend_settlements
            if item.get("status") == "settled"
        ]
        message = render_daily_summary(
            rows, nodes, now.date().isoformat(), title, warnings, ledger_notes,
        )
        result = {
            "node": "15:30",
            "decision": "SUMMARY_DRY_RUN" if dry_run else "SUMMARY_SENT",
            "signals": [],
            "summaries": rows,
            "warnings": warnings,
            "source_nodes": [record.get("node") for record in records],
            "dividend_settlements": dividend_settlements,
        }
        if not dry_run:
            targets = self._notification_targets()
            delivered_to = 0
            for target in targets:
                filtered_rows = [row for row in rows if self._target_accepts_summary_row(target, row)]
                if not filtered_rows:
                    continue
                target_message = render_daily_summary(
                    filtered_rows, nodes, now.date().isoformat(), title, warnings, ledger_notes,
                )
                try:
                    if self._send_target(target, now.date().isoformat(), "summary", target_message):
                        delivered_to += 1
                except Exception as exc:
                    warnings.append(f"{target.get('name', '机器人')} 飞书总结发送失败: {exc}")
            result["destination_count"] = delivered_to
            if targets and not delivered_to:
                result["decision"] = "SUMMARY_NOT_DELIVERED"
        self._append_log(result, now, "summary_dry_run" if dry_run else "summary")
        return {**result, "message": message}

    def _sync_auto_dividends(self, now: datetime) -> tuple[list[dict], list[str]]:
        """Settle only fully verified implementation notices after the session."""
        if self.portfolio_store is None:
            return [], []
        evidence_config = self.config.section("evidence")
        if not bool(evidence_config.get("enabled", False)):
            return [], []
        dividend_settings = dict(evidence_config.get("dividends", {}) or {})
        if not bool(dividend_settings.get("enabled", True)):
            return [], []
        collector_config = dict(evidence_config)
        collector = OfficialEvidenceCollector(self.config.timezone, collector_config)
        events_by_symbol, warnings = collector.collect_cash_dividend_events(
            self.config.positions, now,
        )
        names = {position.symbol: position.name for position in self.config.positions}
        outcomes: list[dict] = []
        for symbol, events in events_by_symbol.items():
            try:
                for outcome in self.portfolio_store.sync_auto_dividends(
                    symbol=symbol, events=events, as_of=now.date().isoformat(),
                ):
                    outcomes.append({**outcome, "name": names.get(symbol, symbol)})
            except Exception as exc:
                warnings.append(f"{symbol} 自动分红登记未完成: {exc}")
        return outcomes, warnings

    def _formal_records_for_day(self, now: datetime) -> list[dict]:
        path = Path(str(self.config.raw.get("log_file", "/app/data/events.jsonl")))
        if not path.exists():
            return []
        explicit: dict[str, list[tuple[datetime, dict]]] = {node: [] for node in self.config.schedule}
        legacy: dict[str, list[tuple[float, datetime, dict]]] = {node: [] for node in self.config.schedule}
        window = int(self.config.raw.get("run_window_seconds", 180))
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
                node = str(record.get("node", ""))
                if node not in explicit:
                    continue
                timestamp = datetime.fromisoformat(str(record["timestamp"]))
                timestamp = timestamp.astimezone(self.tz)
            except (ValueError, TypeError, KeyError, json.JSONDecodeError):
                continue
            if timestamp.date() != now.date():
                continue
            execution_type = record.get("execution_type")
            if execution_type in {"scheduled", "scheduled_recovery"}:
                explicit[node].append((timestamp, record))
                continue
            if execution_type is not None:
                continue
            hour, minute = (int(part) for part in node.split(":"))
            scheduled = timestamp.replace(hour=hour, minute=minute, second=0, microsecond=0)
            delta = (timestamp - scheduled).total_seconds()
            if 0 <= delta <= window:
                legacy[node].append((delta, timestamp, record))
        selected = []
        for node in self.config.schedule:
            if explicit[node]:
                selected.append(max(explicit[node], key=lambda item: item[0])[1])
            elif legacy[node]:
                selected.append(min(legacy[node], key=lambda item: item[0])[2])
        return selected

    def _daily_summary_rows(self, records: list[dict]) -> list[dict]:
        rows = []
        status_text = {
            "BASELINE": "基线",
            "NO_ALERT": "观察",
            "ALERT": "触发",
            "DATA_MISSING": "数据缺失",
            "STALE": "行情延迟",
            "STALE_TECH": "K线延迟",
        }
        for position in self.config.positions:
            per_node: dict[str, dict] = {}
            signals = []
            for record in records:
                node = str(record.get("node", ""))
                summary = next(
                    (item for item in record.get("summaries", []) if item.get("symbol") == position.symbol),
                    None,
                )
                if summary:
                    per_node[node] = summary
                signals.extend(
                    signal for signal in record.get("signals", [])
                    if signal.get("symbol") == position.symbol
                )
            latest_node = next((node for node in reversed(self.config.schedule) if node in per_node), None)
            latest = dict(per_node.get(latest_node, {})) if latest_node else {}
            baseline = per_node.get("09:15", {}) or {}
            # 日线均线只在基线节点计算一次；合并到尾盘结论，
            # 便于总结准确说明“为什么不追涨/不建仓”。
            for key in (
                "ma5", "ma10", "ma20", "daily_as_of", "recent_high_60", "recent_low_60",
            ):
                if latest.get(key) is None and baseline.get(key) is not None:
                    latest[key] = baseline[key]
            latest_status = str(latest.get("status", "DATA_MISSING"))
            informational_codes = {"OVERHEAT_WATCH", "WATCH_NEAR_ENTRY"}
            routed_signals = [
                signal for signal in signals
                if (signal.get("details") or {}).get("notification_status")
                in {None, "sent", "would_send", "candidate"}
            ]
            actionable_signals = [
                signal for signal in routed_signals
                if signal.get("code") not in informational_codes
            ]
            informational_signals = [
                signal for signal in routed_signals
                if signal.get("code") in informational_codes
            ]
            action_candidates = [
                signal for signal in signals
                if signal.get("code") not in informational_codes
            ]
            reason_override = None
            if actionable_signals:
                signal = actionable_signals[-1]
                shares = int(signal.get("shares", 0) or 0)
                share_text = f" {shares}股" if shares else ""
                recommendation = f"复核今日规则生成的“{signal.get('action', '纪律动作')}{share_text}”；未确认成交前不视为已执行"
            elif action_candidates:
                reason = (action_candidates[-1].get("details") or {}).get("suppression_reason", "组合择优或通知门控")
                recommendation = f"今日曾出现候选动作，但已被{reason}抑制，不作为执行建议"
            elif informational_signals:
                signal = informational_signals[-1]
                recommendation = str(signal.get("action") or "临界机会观察，暂不操作")
                reason_override = str(signal.get("reason") or "仅作观察提醒，不构成买卖指令")
            elif latest_status in {"DATA_MISSING", "STALE"}:
                recommendation = "暂不操作，等待行情与证据恢复"
            else:
                recommendation = self._summary_recommendation(latest, position.role)
            reason = reason_override or self._summary_reason(latest)
            rows.append({
                "symbol": position.symbol,
                "name": position.name,
                "price": latest.get("price"),
                "change_pct": latest.get("change_pct"),
                "recommendation": recommendation,
                "reason": reason,
                "trigger_count": len(actionable_signals),
                "candidate_count": len(action_candidates),
                "informational_count": len(informational_signals),
                "latest_node": latest_node,
                "stage": latest.get("stage", {}),
                "role": position.role,
                "status_by_node": {
                    node: status_text.get(str(per_node[node].get("status")), str(per_node[node].get("status")))
                    for node in self.config.schedule if node in per_node
                },
            })
        return rows

    @staticmethod
    def _summary_recommendation(summary: dict, role: str) -> str:
        status = str(summary.get("status", "DATA_MISSING"))
        if status in {"DATA_MISSING", "STALE", "STALE_TECH"}:
            return "暂不操作，等待行情与证据恢复"
        stage_label = str((summary.get("stage") or {}).get("label", "NEUTRAL"))
        price = summary.get("price")
        support = summary.get("support")
        resistance = summary.get("resistance")
        above_resistance = bool(
            price is not None and resistance is not None and float(price) > float(resistance)
        )
        below_support = bool(
            price is not None and support is not None and float(price) < float(support)
        )
        if role == "watchlist":
            if bool((summary.get("stage") or {}).get("company_thesis_break")):
                return "暂停首次建仓，等待公司负向证据解除并重新通过技术门槛"
            if stage_label in {"NEAR_STAGE_TOP", "STAGE_TOP_CONFIRMED"}:
                return "放弃追涨，继续等待新的低风险首次建仓机会"
            if above_resistance:
                return "当日转强但不追涨；等待日线趋势和收益风险比同时通过"
            if stage_label == "BOTTOM_CONFIRMED":
                return "磨底已确认，等待首次建仓的剩余条件通过"
            if stage_label == "BOTTOMING":
                return "继续观察磨底，未右侧确认前不提前建仓"
            return "继续观察，等待磨底确认或高质量突破"
        if stage_label == "STAGE_TOP_CONFIRMED":
            return "顶部结构已确认，优先防守并等待减仓执行条件"
        if stage_label == "NEAR_STAGE_TOP":
            return "停止追涨，收紧观察，暂不因“接近顶部”单独卖出"
        atr_extension = (summary.get("stage") or {}).get("atr_extension")
        rsi = summary.get("rsi14")
        try:
            if (
                atr_extension is not None
                and rsi is not None
                and float(atr_extension) >= 3.0
                and float(rsi) >= 70.0
            ):
                return "急涨过热，不追涨；纪律上仍按破位/顶部确认再减仓"
        except (TypeError, ValueError):
            pass
        if stage_label == "BOTTOM_CONFIRMED":
            return "磨底已确认，但新增条件未全部通过，继续等待"
        if stage_label == "BOTTOMING":
            return "继续持有观察，未右侧确认前不提前加仓"
        if above_resistance:
            return "保持主仓，不追涨；只在全部新增条件通过后小幅加仓"
        if below_support:
            return "暂不减仓；等待破位的持续性、量能与外部证据确认"
        return "继续持有观察，暂不加减仓"

    @staticmethod
    def _summary_reason(summary: dict) -> str:
        status = str(summary.get("status", "DATA_MISSING"))
        if status == "STALE":
            return "最新节点行情超过新鲜度限制，未生成操作建议"
        if status == "STALE_TECH":
            return "K线时点未通过新鲜度校验，未生成操作建议"
        if status == "DATA_MISSING" or not summary:
            return "最新节点数据不完整，未生成操作建议"
        price = summary.get("price")
        support = summary.get("support")
        resistance = summary.get("resistance")
        vwap = summary.get("vwap")
        volume_ratio = summary.get("volume_ratio")
        if price is None or support is None or resistance is None:
            return "节点结论未触发可执行动作"
        stage = summary.get("stage", {}) or {}
        stage_label = str(stage.get("label", "NEUTRAL"))
        role = str(summary.get("role", "holding"))
        checks = summary.get("checks", {}) or {}
        metrics = summary.get("metrics", {}) or {}

        def failed(group: str, name: str) -> bool:
            item = (checks.get(group, {}) or {}).get(name)
            return isinstance(item, dict) and not bool(item.get("passed", False))

        def ratio_text(value) -> str | None:
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                return None
            return f"{numeric:.1%}" if numeric >= 0 else None

        if role == "watchlist":
            entry = metrics.get("watchlist_entry", {}) or {}
            blockers: list[str] = []
            if stage.get("company_thesis_break"):
                return "公司经营/公告出现负向证据，首次建仓门控未通过"
            if float(price) > float(resistance):
                ma20 = summary.get("ma20")
                price_above_ma20 = entry.get("price_above_ma20")
                if price_above_ma20 is None and ma20 is not None:
                    price_above_ma20 = float(price) > float(ma20)
                if price_above_ma20 is False:
                    blockers.append(
                        f"仍低于20日线{float(ma20):.2f}"
                        if ma20 is not None else "仍低于20日线"
                    )
                ma20_slope_ready = entry.get("ma20_slope_ready")
                if ma20_slope_ready is None and summary.get("ma20_slope_5d") is not None:
                    ma20_slope_ready = float(summary["ma20_slope_5d"]) >= -0.002
                if ma20_slope_ready is False:
                    blockers.append("20日线仍明显下行")
                if entry.get("breakout_persistence") is False:
                    blockers.append("15分钟突破持续/回踩未确认")
                if failed("watchlist_entry", "target_scale_sane"):
                    target_distance = ratio_text(entry.get("target_distance_ratio"))
                    blockers.append(
                        f"下一压力跨度{target_distance}需复核复权/历史尺度"
                        if target_distance else "下一压力需复核复权/历史尺度"
                    )
                breakout_spread = entry.get("breakout_expected_spread")
                if breakout_spread is None and summary.get("next_resistance") is not None:
                    next_resistance = float(summary["next_resistance"])
                    breakout_spread = (
                        (next_resistance - float(price)) / float(price)
                        if next_resistance > float(price) else -1.0
                    )
                required_spread_value = entry.get("minimum_expected_spread_required", 0.05)
                spread = ratio_text(breakout_spread)
                required_spread = ratio_text(required_spread_value)
                rr = entry.get("breakout_reward_risk")
                if rr is None and breakout_spread is not None and float(breakout_spread) > 0:
                    fallback_stop = float(resistance) * 0.99
                    fallback_downside = max(float(price) - fallback_stop, 0.01) / float(price)
                    rr = float(breakout_spread) / fallback_downside
                rr_required = entry.get("minimum_reward_risk_required", 2.0)
                if spread is not None and required_spread is not None:
                    try:
                        if float(breakout_spread) < float(required_spread_value):
                            blockers.append(f"至下一压力空间{spread}低于{required_spread}")
                    except (TypeError, ValueError):
                        pass
                try:
                    if float(rr) >= 0 and float(rr) < float(rr_required):
                        blockers.append(
                            f"按当前失效位估算收益风险比{float(rr):.2f}低于{float(rr_required):.2f}"
                        )
                except (TypeError, ValueError):
                    pass
                if failed("watchlist_entry", "industry_and_announcements"):
                    blockers.append("产业/公告证据未支持首次建仓")
                if failed("watchlist_entry", "external_confirmation"):
                    blockers.append("同行与市场同步确认不足")
                lead = "虽已站上动态压力"
                if vwap is not None and float(price) >= float(vwap):
                    lead += "并位于VWAP上方"
                if volume_ratio is not None and float(volume_ratio) >= 1.3:
                    lead = "虽放量站上动态压力并位于VWAP上方"
                return lead + ("；" + "；".join(blockers[:3]) if blockers else "，但首次建仓条件未全部通过")
            if stage_label == "BOTTOMING" and not stage.get("bottom_confirmed"):
                return "位于60日低位区，但RSI回升、20日线趋缓或盘中企稳尚未共同确认"
            if stage_label == "BOTTOM_CONFIRMED":
                blockers = []
                if failed("watchlist_entry", "industry_and_announcements"):
                    blockers.append("产业/公告证据不足")
                if failed("watchlist_entry", "external_confirmation"):
                    blockers.append("同行与市场确认不足")
                if failed("watchlist_entry", "target_scale_sane"):
                    blockers.append("下一压力的复权/历史尺度需复核")
                if failed("watchlist_entry", "minimum_expected_spread"):
                    blockers.append("目标空间不足")
                if failed("watchlist_entry", "minimum_reward_risk"):
                    blockers.append("收益风险比未达标")
                return "磨底已确认" + ("；" + "；".join(blockers[:3]) if blockers else "，但未通过首次建仓全部门槛")
        stage_prefix = {
            "BOTTOMING": "60日区间低位并曾出现弱势指标，属于磨底候选，尚未完成右侧确认",
            "BOTTOM_CONFIRMED": "低位、RSI回升、趋势趋缓及盘中企稳共同确认磨底",
            "NEAR_STAGE_TOP": "价格接近60日高位且有过热特征，但反转证据不足，尚不能定义顶部",
            "STAGE_TOP_CONFIRMED": "高位、动量回落、短趋势与盘中反转共同确认阶段顶部",
        }.get(stage_label)
        if float(price) > float(resistance):
            main = metrics.get("main_add", {}) or {}
            blockers = []
            if failed("main_add", "breakout_persistence_or_retest"):
                blockers.append("15分钟突破持续/回踩未确认")
            if failed("main_add", "daily_trend"):
                blockers.append("日线趋势未转强")
            if failed("main_add", "industry_and_announcements"):
                blockers.append("产业/公告证据未支持新增")
            if failed("main_add", "external_confirmations"):
                blockers.append("同行与市场确认不足")
            if failed("main_add", "minimum_expected_spread"):
                spread = ratio_text(main.get("expected_spread"))
                blockers.append(f"至下一压力空间仅{spread}" if spread else "至下一压力空间不足")
            if failed("main_add", "minimum_reward_risk"):
                try:
                    blockers.append(f"收益风险比仅{float(main.get('reward_risk')):.2f}")
                except (TypeError, ValueError):
                    blockers.append("收益风险比未达标")
            if failed("main_add", "sized_at_least_one_lot"):
                try:
                    if float(main.get("current_weight")) >= float(main.get("target_weight")) - 0.0005:
                        blockers.append("现有仓位已达当前新增上限")
                    else:
                        blockers.append("现金、风险或流动性约束使计划不足一手")
                except (TypeError, ValueError):
                    blockers.append("仓位/风险约束不允许新增")
            strength = "价格放量站上动态压力" if volume_ratio is not None and float(volume_ratio) >= 1.3 else "价格站上动态压力"
            structure = strength + ("；" + "；".join(blockers[:3]) if blockers else "，但新增条件未全部通过")
        elif float(price) < float(support):
            relative_gate = (
                summary.get("checks", {})
                .get("main_reduce", {})
                .get("shallow_relative_strength_gate", {})
            )
            if relative_gate and not relative_gate.get("passed", True):
                structure = "价格仅浅破支撑，但相对同行抗跌且大盘未弱，已降级为观察"
            else:
                structure = "价格跌破支撑，但持续性、VWAP或量能确认不足"
        else:
            structure = "价格仍在动态支撑与压力之间，未形成有效突破或破位"
        details = []
        if vwap is not None:
            details.append("位于VWAP上方" if float(price) >= float(vwap) else "位于VWAP下方")
        if volume_ratio is not None:
            details.append(f"量能比{float(volume_ratio):.2f}")
        atr_extension = stage.get("atr_extension")
        rsi = summary.get("rsi14")
        try:
            if (
                atr_extension is not None
                and rsi is not None
                and float(atr_extension) >= 3.0
                and float(rsi) >= 70.0
            ):
                details.append(
                    f"急涨过热（ATR延伸{float(atr_extension):.2f}、RSI{float(rsi):.0f}）"
                )
        except (TypeError, ValueError):
            pass
        capital_signal = summary.get("capital_flow_signal")
        if capital_signal in {"persistent_inflow", "single_day_inflow"}:
            details.append("主力资金净流入")
        elif capital_signal in {"persistent_outflow", "single_day_outflow"}:
            details.append("主力资金净流出")
        holder_signal = summary.get("shareholder_signal")
        if holder_signal == "concentrating":
            details.append("股东户数下降（筹码集中）")
        elif holder_signal == "dispersing":
            details.append("股东户数上升（筹码分散）")
        base = stage_prefix or structure
        return base + ("；" + "、".join(details) if details else "")

    def _filter_sendable(
        self, signals: list[Signal], now: datetime
    ) -> tuple[list[Signal], list[dict]]:
        risk = self.config.section("risk")
        result: list[Signal] = []
        suppressed: list[dict] = []
        unique: list[Signal] = []
        for signal in sorted(signals, key=self._signal_priority):
            semantic_key = self._semantic_event_key(signal)
            sent_rank = (
                self.state.sent_rank_for_semantic_key(semantic_key)
                if signal.code in self._PERSISTENT_REDUCTION_CODES
                else self.state.sent_rank(signal.event_id)
            )
            current_rank = int(signal.details.get("event_rank", 1) or 1)
            reminder_due = self._pending_reduction_reminder_due(signal, now, sent_rank)
            if sent_rank is not None and current_rank <= sent_rank and not reminder_due:
                reason = "duplicate_event" if current_rank == sent_rank else "non_upgrade_event"
                suppressed.append({"event_id": signal.event_id, "reason": reason})
                continue
            if reminder_due:
                signal.details["pending_reminder"] = True
            unique.append(signal)

        result.extend(signal for signal in unique if signal.category == "risk")
        exit_codes = {
            "DOWN_BREAK", "STAGE_TOP_EXIT", "MIGRATION_TRIM", "SAT_EXIT", "SAT_SELL",
        }
        for category in ("strategy", "satellite", "reminder"):
            category_signals = [signal for signal in unique if signal.category == category]
            if not category_signals:
                continue
            if category == "reminder":
                limit = int(risk.get("max_reminder_alerts_per_day", 3))
            else:
                limit_key = (
                    "max_satellite_alerts_per_day"
                    if category == "satellite"
                    else "max_strategy_alerts_per_day"
                )
                limit = int(risk.get(limit_key, 3))
            used = self.state.notification_count(now.date(), category)
            if used < limit:
                result.extend(category_signals)
                continue
            for signal in category_signals:
                if signal.code in exit_codes:
                    result.append(signal)
                else:
                    suppressed.append({"event_id": signal.event_id, "reason": "daily_message_budget"})
        return sorted(result, key=self._signal_priority), suppressed

    def _resolved_down_break_signal(
        self,
        position,
        quote,
        tech,
        today,
        peer_change,
        current_signals: list[Signal],
        technical_fresh: bool,
        allow_state_update: bool,
    ) -> Signal | None:
        active = self.state.active_signal(position.symbol)
        if active.get("code") != "DOWN_BREAK":
            return None
        stored_shares = int(active.get("position_main_shares", 0) or 0)
        if stored_shares and stored_shares != position.main_shares:
            # 配置仓位变化视为用户已自行处理，避免错误撤销一个已经执行的建议。
            if allow_state_update:
                self.state.clear_active_signal(position.symbol)
            return None
        blocking_codes = {
            "DOWN_BREAK", "EMERGENCY_RISK", "STAGE_TOP_EXIT", "MIGRATION_TRIM",
            "SAT_EXIT", "SAT_SELL",
        }
        if any(signal.code in blocking_codes for signal in current_signals):
            return None
        key_level = float(active.get("key_level", 0) or 0)
        recovered = bool(
            technical_fresh
            and tech.complete_15m
            and tech.last_15m_close is not None
            and key_level > 0
            and tech.last_15m_close > key_level
            and quote.price > key_level
        )
        peer_floor = float(self.config.section("strategic_rules").get("peer_weak_ratio", 0.0))
        peers_stable = peer_change is not None and peer_change >= peer_floor
        if not (recovered and peers_stable):
            return None
        return Signal(
            symbol=position.symbol,
            name=position.name,
            code="FALSE_BREAK",
            confidence="中",
            price=quote.price,
            key_level=key_level,
            action="撤销此前减仓建议，恢复观察",
            shares=0,
            reason=(
                f"完整15分钟重新站回{key_level:.2f}上方，同行均值"
                f"{peer_change:+.2%}，此前破位未延续"
            ),
            invalidation="若再次出现深度或连续15分钟破位，且外部证据同步走弱，再重新评估",
            event_id=f"{today.isoformat()}|{position.symbol}:FALSE_BREAK:{key_level:.2f}",
            category="strategy",
            details={
                "previous_event_id": active.get("event_id"),
                "evidence": "关键位已收复且同行止跌",
                "position_main_shares": position.main_shares,
            },
        )

    def _rank_capital_entries(self, signals: list[Signal]) -> tuple[list[Signal], list[dict]]:
        entry_codes = {"SAT_BUY", "UP_BREAK", "STAGE_REENTRY", "WATCH_ENTRY"}
        entries = [signal for signal in signals if signal.code in entry_codes]
        if len(entries) <= 1:
            return signals, []
        main_bonus = float(
            self.config.section("strategic_rules").get("main_entry_priority_bonus", 0.25)
        )

        def score(signal: Signal) -> float:
            reward_risk = float(signal.details.get("reward_risk", 0) or 0)
            confidence = 0.75 if signal.confidence == "高" else 0.25
            stage_bonus = 0.35 if signal.details.get("stage") == "BOTTOM_CONFIRMED" else 0.0
            evidence_bonus = 0.20 if int(signal.details.get("company_direction", 0) or 0) > 0 else 0.0
            action_bonus = (
                0.15 if bool(signal.details.get("corporate_action_confirmation")) else 0.0
            )
            role_bonus = main_bonus if signal.code in {"UP_BREAK", "STAGE_REENTRY", "WATCH_ENTRY"} else 0.0
            margin_signal = str(signal.details.get("margin_signal", "missing"))
            margin_penalty = 0.25 if margin_signal == "deleveraging" else (
                0.10 if margin_signal in {"crowded", "extreme_crowding"} else 0.0
            )
            capital_signal = str(signal.details.get("capital_flow_signal", "missing"))
            capital_penalty = 0.20 if capital_signal == "persistent_outflow" else (
                0.08 if capital_signal == "single_day_outflow" else 0.0
            )
            capital_bonus = 0.08 if capital_signal == "persistent_inflow" else 0.0
            holder_signal = str(signal.details.get("shareholder_signal", "missing"))
            holder_bonus = 0.05 if holder_signal == "concentrating" else 0.0
            value = (
                reward_risk + confidence + stage_bonus + evidence_bonus
                + action_bonus + role_bonus + capital_bonus + holder_bonus
                - margin_penalty - capital_penalty
            )
            signal.details["capital_rank_score"] = round(value, 4)
            signal.details["margin_rank_penalty"] = margin_penalty
            signal.details["capital_flow_rank_penalty"] = capital_penalty
            signal.details["corporate_action_rank_bonus"] = action_bonus
            return value

        best = max(entries, key=score)
        suppressed = [
            {"event_id": signal.event_id, "reason": f"capital_competition:{best.symbol}"}
            for signal in entries if signal is not best
        ]
        return [signal for signal in signals if signal.code not in entry_codes or signal is best], suppressed

    @staticmethod
    def _signal_priority(signal: Signal) -> tuple[int, str]:
        if signal.code == "EMERGENCY_RISK":
            return 0, signal.symbol
        if signal.code in {"DOWN_BREAK", "STAGE_TOP_EXIT", "MIGRATION_TRIM", "SAT_EXIT", "SAT_SELL"}:
            return 1, signal.symbol
        if signal.code == "FALSE_BREAK":
            return 2, signal.symbol
        if signal.code == "OVERHEAT_WATCH":
            return 3, signal.symbol
        if signal.code == "WATCH_NEAR_ENTRY":
            return 4, signal.symbol
        return 2, signal.symbol

    def _technical_data_fresh(self, tech, quote, now: datetime) -> tuple[bool, list[str]]:
        settings = self.config.section("data_source")
        max_lag = int(settings.get("max_bar_lag_seconds", 300))
        reasons: list[str] = []
        for label, timestamp in (
            ("最后5分钟K", tech.last_5m_timestamp),
            ("最后15分钟K", tech.last_15m_timestamp),
        ):
            if timestamp is None:
                reasons.append(f"{label}时间缺失")
                continue
            lag = (quote.timestamp - timestamp).total_seconds()
            if lag < 0 or lag > max_lag:
                reasons.append(f"{label}与快照相差{int(lag)}秒")
        if bool(settings.get("require_previous_trading_day", True)):
            daily_fresh, reason = self._daily_as_of_fresh(tech.daily_as_of, now.date())
            if not daily_fresh:
                reasons.append(reason)
        return not reasons, reasons

    def _daily_as_of_fresh(self, observed, current) -> tuple[bool, str]:
        expected = self._previous_trading_day(current)
        if observed == expected:
            return True, ""
        observed_text = observed.isoformat() if observed else "缺失"
        return False, f"最近完整日K为{observed_text}，应为{expected.isoformat()}"

    def _previous_trading_day(self, current):
        candidate = current.fromordinal(current.toordinal() - 1)
        while candidate.weekday() >= 5 or candidate.isoformat() in self.config.holidays:
            candidate = candidate.fromordinal(candidate.toordinal() - 1)
        return candidate

    def _append_log(self, result: dict, now: datetime, execution_type: str) -> None:
        path = Path(str(self.config.raw.get("log_file", "/app/data/events.jsonl")))
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": now.isoformat(), "workspace_id": self.config.workspace_id,
            "execution_type": execution_type, **result,
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    @staticmethod
    def _is_fresh(quote, now: datetime, max_delay: int) -> bool:
        return abs((now - quote.timestamp).total_seconds()) <= max_delay

    def _correlated_exposure(
        self,
        symbol: str,
        quotes: dict,
        portfolio_value: float,
        migration_group_caps: dict[str, float] | None = None,
    ) -> tuple[float | None, float | None]:
        if portfolio_value <= 0:
            return None, None
        groups = self.config.section("risk").get("correlation_groups", {})
        # Include zero-share watchlist candidates in membership resolution. Their
        # current exposure is still zero, but they must inherit the same sector
        # cap before a proposed first buy is sized. Filtering to holdings here
        # previously returned (None, None) for a watchlist insurance/sector name,
        # allowing its starter position to bypass the group ceiling.
        positions = {
            position.symbol: position
            for position in self.config.positions
            if position.symbol in quotes
        }
        matches: list[tuple[float, float, float, str]] = []
        for name, group in groups.items():
            symbols = self._correlation_group_symbols(group, positions)
            if symbol not in symbols:
                continue
            value = sum(
                positions[item].total_shares * quotes[item].price
                for item in symbols if item in positions and item in quotes
            )
            cap = float(group.get("max_ratio"))
            if migration_group_caps and name in migration_group_caps:
                cap = float(migration_group_caps[name])
            weight = value / portfolio_value
            matches.append((cap - weight, weight, cap, name))
        if matches:
            # A security can match several groups through overlapping explicit
            # codes and sectors. Bind sizing to the smallest remaining room,
            # not to the first YAML group encountered.
            _, weight, cap, _ = min(matches, key=lambda item: item[0])
            return weight, cap
        return None, None

    @staticmethod
    def _correlation_group_symbols(group: dict, positions: dict) -> set[str]:
        """Combine explicit symbols with sector membership for dynamic watchlist names."""
        symbols = {str(item).upper() for item in group.get("symbols", [])}
        sectors = {str(item).strip().lower() for item in group.get("sectors", []) if str(item).strip()}
        if sectors:
            symbols.update(
                symbol for symbol, position in positions.items()
                if str(position.sector).strip().lower() in sectors
            )
        return symbols

    def _migration_contexts(
        self,
        quotes: dict,
        portfolio_value: float,
        complete_quotes: bool,
        allow_state_update: bool = True,
        today=None,
    ) -> tuple[dict[str, dict], dict[str, float]]:
        config = self.config.section("migration_mode")
        if not bool(config.get("enabled", False)):
            return {}, {}
        # StateStore exposes its live dictionary. Always work on a copy so dry-runs and
        # incomplete valuation checks cannot leak a ratchet into later persistence.
        state = deepcopy(self.state.migration_state())
        position_state = state.setdefault("positions", {})
        group_state = state.setdefault("groups", {})
        position_sizing = self.config.section("position_sizing")
        risk = self.config.section("risk")
        buffer_weight = float(config.get("ratchet_buffer_weight", 0.03))
        changed = False
        contexts: dict[str, dict] = {}
        positions = {position.symbol: position for position in self.config.positions}
        invested = {
            symbol: position
            for symbol, position in positions.items()
            if position.total_shares > 0
        }
        valuation_ready = bool(
            complete_quotes
            and portfolio_value > 0
            and all(symbol in quotes for symbol in invested)
        )
        main_weights = (
            {
                symbol: position.main_shares * quotes[symbol].price / portfolio_value
                for symbol, position in invested.items()
            }
            if valuation_ready
            else {}
        )

        for symbol, position in positions.items():
            settings = position.migration
            if not bool(settings.get("enabled", False)):
                continue
            configured_ceiling = float(settings["initial_ceiling_weight"])
            created = symbol not in position_state
            current = position_state.get(symbol, {
                "ceiling_weight": configured_ceiling,
                "reference_main_shares": position.main_shares,
            })
            old_ceiling = min(
                float(current.get("ceiling_weight", configured_ceiling)),
                configured_ceiling,
            )
            reference_shares = int(current.get("reference_main_shares", position.main_shares))
            ceiling = old_ceiling
            last_main_reduction_date = current.get("last_main_reduction_date")
            if valuation_ready and position.main_shares < reference_shares:
                long_term_target = float(
                    position.sizing.get(
                        "target_main_weight",
                        position_sizing.get("target_main_weight", 0.20),
                    )
                )
                ceiling = min(
                    ceiling,
                    max(long_term_target, main_weights.get(symbol, 0.0) + buffer_weight),
                )
                if today is not None:
                    last_main_reduction_date = today.isoformat()
            updated = {
                "ceiling_weight": round(ceiling, 8),
                "reference_main_shares": position.main_shares,
                **(
                    {"last_main_reduction_date": last_main_reduction_date}
                    if last_main_reduction_date
                    else {}
                ),
            }
            if valuation_ready and current != updated:
                position_state[symbol] = updated
                changed = True
            elif valuation_ready and created:
                position_state[symbol] = updated
                changed = True
            contexts[symbol] = {
                **config,
                "enabled": True,
                "position_ceiling": ceiling,
                "risk_principal_ceiling": float(settings["risk_principal_ceiling"]),
                "long_term_target_weight": float(
                    position.sizing.get(
                        "target_main_weight",
                        position_sizing.get("target_main_weight", 0.20),
                    )
                ),
                "valuation_ready": valuation_ready,
                "last_main_reduction_date": last_main_reduction_date,
            }

        group_caps: dict[str, float] = {}
        group_configs = config.get("correlation_groups", {})
        for name, settings in group_configs.items():
            group = risk.get("correlation_groups", {}).get(name, {})
            symbols = sorted(self._correlation_group_symbols(group, positions) & set(positions))
            if not symbols:
                continue
            configured_ceiling = float(settings["initial_ceiling_weight"])
            references = {symbol: positions[symbol].main_shares for symbol in symbols}
            created = name not in group_state
            current = group_state.get(name, {
                "ceiling_weight": configured_ceiling,
                "reference_main_shares": references,
            })
            old_ceiling = min(
                float(current.get("ceiling_weight", configured_ceiling)),
                configured_ceiling,
            )
            previous_references = current.get("reference_main_shares", references)
            reduced = any(
                positions[symbol].main_shares < int(previous_references.get(symbol, positions[symbol].main_shares))
                for symbol in symbols
            )
            ceiling = old_ceiling
            if valuation_ready and reduced:
                # A temporary satellite overlay must count against the live
                # group cap, but must not permanently inflate the ratcheting
                # migration ceiling when a main-position reduction is recorded.
                current_weight = sum(main_weights.get(symbol, 0.0) for symbol in symbols)
                standard_cap = float(group.get("max_ratio", 0.40))
                ceiling = min(
                    ceiling,
                    max(standard_cap, current_weight + buffer_weight),
                )
            updated = {
                "ceiling_weight": round(ceiling, 8),
                "reference_main_shares": references,
            }
            if valuation_ready and current != updated:
                group_state[name] = updated
                changed = True
            elif valuation_ready and created:
                group_state[name] = updated
                changed = True
            group_caps[name] = ceiling

        if changed and allow_state_update:
            self.state.save_migration_state(state)
        return contexts, group_caps

    def _migration_satellite_context(
        self,
        position,
        migration_context: dict,
        stage_memory: dict,
        today,
    ) -> dict:
        """Attach migration-only guards that keep the overlay tactical."""
        context = dict(migration_context or {})
        if not bool(context.get("enabled", False)):
            return context
        reasons: list[str] = []
        if bool(context.get("block_satellite_on_pending_reduction", True)):
            active = self.state.active_signal(position.symbol)
            if active.get("code") in self._PERSISTENT_REDUCTION_CODES:
                reasons.append(f"存在待处理{active['code']}减风险信号")
            if stage_memory.get("top_pending_event_id"):
                reasons.append("存在尚未确认成交的阶段顶部减仓")
        last_reduction = context.get("last_main_reduction_date")
        cooldown_days = int(
            context.get("satellite_reduction_cooldown_trading_days", 1)
        )
        elapsed = self._trading_days_since(last_reduction, today)
        if last_reduction and elapsed <= cooldown_days:
            reasons.append(
                f"主仓减仓后需经过{cooldown_days}个完整交易日冷静期"
            )
        context["satellite_reduction_cooldown_elapsed"] = elapsed
        context["satellite_entry_block_reason"] = "；".join(reasons) or None
        return context

    @staticmethod
    def _evidence_items(evidence) -> list[dict]:
        if not evidence:
            return []
        return [
            {
                "key": item.key,
                "label": item.label,
                "source": item.source,
                "source_url": item.source_url,
                "observed_at": item.observed_at.isoformat(),
                "direction": item.direction,
                "strength": item.strength,
                "freshness": item.freshness,
                "fact_type": item.fact_type,
                "summary": item.summary,
            }
            for item in evidence.items
        ]

    @staticmethod
    def _result(
        node,
        decision,
        signals,
        warnings,
        summaries=None,
        sent_signals=None,
        suppressed=None,
    ):
        return {
            "node": node, "decision": decision,
            "signals": [signal.__dict__ for signal in signals],
            "sent_signals": [signal.__dict__ for signal in (sent_signals or [])],
            "suppressed_signals": suppressed or [],
            "summaries": summaries or [], "warnings": warnings,
        }
