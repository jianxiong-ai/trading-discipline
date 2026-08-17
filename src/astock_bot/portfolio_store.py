from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator

from .commodity_defaults import (
    default_copper_related_derivatives,
    default_legacy_copper_exposure,
)


_SYMBOL_RE = re.compile(r"\d{6}\.(?:SH|SZ)")
_DYNAMIC_KEYS = {
    "role",
    "main_shares",
    "economic_basis",
    "risk_principal",
    "satellite",
    "watchlist_entry_date",
    "settled_dividend_event_ids",
    "auto_dividend_events",
}
_SUPPORTED_SECTORS = (
    "copper",
    "gold",
    "silver",
    "insurance",
    "insurance_financial_group",
    "new_energy_vehicle",
    "satellite_communications",
    "semiconductor",
    "optical_communications",
    "generic",
)


class PortfolioStoreError(ValueError):
    """A user-facing validation error for the portfolio ledger."""


class PortfolioStore:
    """SQLite source of truth for mutable portfolio facts.

    Strategy rules remain in YAML. The database only owns cash, positions,
    confirmed transactions and user-created watchlist items.
    """

    def __init__(self, path: Path, *, seed_static: bool = True):
        self.path = path
        self.seed_static = bool(seed_static)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_config(cls, config_path: str | Path, raw: dict[str, Any]) -> PortfolioStore | None:
        value = raw.get("_workspace_database_path") or raw.get("portfolio", {}).get("database_path")
        if not value:
            return None
        path = Path(str(value))
        if not path.is_absolute():
            path = Path(config_path).resolve().parent / path
        return cls(path, seed_static=bool(raw.get("_workspace_seed_static", True)))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=8, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_positions (
                    symbol TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    is_seeded INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS portfolio_transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    executed_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    side TEXT NOT NULL,
                    shares INTEGER NOT NULL DEFAULT 0,
                    price REAL NOT NULL DEFAULT 0,
                    fee REAL NOT NULL DEFAULT 0,
                    cash_delta REAL NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    before_payload TEXT,
                    after_payload TEXT,
                    cash_before REAL,
                    cash_after REAL,
                    reversal_of INTEGER UNIQUE,
                    created_at TEXT NOT NULL
                )
                """
            )
            # Forward-compatible audit columns for databases created before
            # reversible ledger support. Historical rows stay visible but are
            # intentionally not reversible because no exact snapshot exists.
            columns = {row[1] for row in conn.execute("PRAGMA table_info(portfolio_transactions)")}
            for name, definition in (
                ("before_payload", "TEXT"), ("after_payload", "TEXT"),
                ("cash_before", "REAL"),
                ("cash_after", "REAL"),
                ("reversal_of", "INTEGER"),
            ):
                if name not in columns:
                    conn.execute(f"ALTER TABLE portfolio_transactions ADD COLUMN {name} {definition}")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_portfolio_transactions_symbol_date "
                "ON portfolio_transactions(symbol, executed_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_portfolio_transactions_date "
                "ON portfolio_transactions(executed_at DESC)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_collections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_collection_members (
                    collection_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    PRIMARY KEY(collection_id, symbol),
                    FOREIGN KEY(collection_id) REFERENCES notification_collections(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_destinations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    webhook_ciphertext TEXT NOT NULL,
                    secret_ciphertext TEXT NOT NULL DEFAULT '',
                    min_confidence TEXT NOT NULL DEFAULT '中',
                    send_actions INTEGER NOT NULL DEFAULT 1,
                    send_summary INTEGER NOT NULL DEFAULT 1,
                    receive_critical_all INTEGER NOT NULL DEFAULT 0,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_destination_collections (
                    destination_id INTEGER NOT NULL,
                    collection_key TEXT NOT NULL,
                    PRIMARY KEY(destination_id, collection_key),
                    FOREIGN KEY(destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_destination_symbols (
                    destination_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    PRIMARY KEY(destination_id, symbol),
                    FOREIGN KEY(destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_deliveries (
                    destination_id INTEGER NOT NULL,
                    delivery_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message_hash TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(destination_id, delivery_key, kind),
                    FOREIGN KEY(destination_id) REFERENCES notification_destinations(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_settings (
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    webhook_ciphertext TEXT NOT NULL,
                    secret_ciphertext TEXT NOT NULL DEFAULT '',
                    min_confidence TEXT NOT NULL DEFAULT '中',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS notification_setting_deliveries (
                    setting_id INTEGER NOT NULL DEFAULT 1,
                    delivery_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    message_hash TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(setting_id, delivery_key, kind),
                    FOREIGN KEY(setting_id) REFERENCES notification_settings(id) ON DELETE CASCADE
                )
                """
            )
            conn.execute("PRAGMA optimize")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def ensure_seed(self, portfolio: dict[str, Any]) -> None:
        """Seed once from the existing YAML configuration without overwriting edits."""
        now = _now()
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM portfolio_meta WHERE key='available_cash'").fetchone() is None:
                initial_cash = float(portfolio.get("available_cash", 0)) if self.seed_static else 0.0
                self._set_meta(conn, "available_cash", initial_cash, now)
            count = int(conn.execute("SELECT COUNT(*) FROM portfolio_positions").fetchone()[0])
            if count or not self.seed_static:
                return
            for item in portfolio.get("positions", []):
                payload = _normalized_payload(item)
                conn.execute(
                    "INSERT INTO portfolio_positions(symbol, payload, is_seeded, created_at, updated_at) VALUES (?, ?, 1, ?, ?)",
                    (payload["symbol"], _dump(payload), now, now),
                )

    def overlay(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Merge mutable database facts onto static strategy configuration."""
        result = copy.deepcopy(raw)
        portfolio = result.setdefault("portfolio", {})
        self.ensure_seed(portfolio)
        with self._connect() as conn:
            cash = self._get_meta(conn, "available_cash")
            if cash is not None:
                portfolio["available_cash"] = _finite_float(cash, "可用资金")
            rows = conn.execute("SELECT symbol, payload FROM portfolio_positions ORDER BY symbol").fetchall()
        dynamic = {str(row["symbol"]): _load(row["payload"]) for row in rows}
        static = (
            {str(item.get("symbol", "")).upper(): item for item in portfolio.get("positions", [])}
            if self.seed_static else {}
        )
        merged: list[dict[str, Any]] = []
        for symbol, static_item in static.items():
            record = dynamic.pop(symbol, None)
            if record is None:
                merged.append(static_item)
                continue
            # Database payloads created before risk_principal was introduced must
            # inherit the immutable cycle principal from the static seed rather
            # than treating the already-reduced residual basis as a new ceiling.
            if "risk_principal" not in record:
                record["risk_principal"] = float(
                    static_item.get(
                        "risk_principal",
                        static_item.get("migration", {}).get(
                            "risk_principal_ceiling",
                            max(float(static_item.get("economic_basis", 0)), 0.0),
                        ),
                    )
                )
            item = copy.deepcopy(static_item)
            for key in _DYNAMIC_KEYS:
                if key in record:
                    item[key] = record[key]
            # Seed records can contain an old copy of YAML sizing. Only merge
            # the explicit UI override, so later YAML changes to other sizing
            # keys keep taking effect.
            sizing_overrides = record.get("sizing_overrides")
            if isinstance(sizing_overrides, dict):
                item["sizing"] = {
                    **dict(item.get("sizing") or {}),
                    **sizing_overrides,
                }
            configured_events = list(item.get("corporate_events", []) or [])
            auto_events = [
                _normalized_dividend_event(event)
                for event in record.get("auto_dividend_events", []) or []
                if isinstance(event, dict)
            ]
            configured_ids = {_dividend_event_id(event) for event in configured_events}
            if auto_events:
                item["corporate_events"] = [
                    *configured_events,
                    *(event for event in auto_events if _dividend_event_id(event) not in configured_ids),
                ]
            settled = set(str(value) for value in record.get("settled_dividend_event_ids", []))
            if settled:
                item["corporate_events"] = [
                    {
                        **event,
                        "basis_adjusted": bool(event.get("basis_adjusted", False))
                        or _dividend_event_id(event) in settled,
                    }
                    for event in item.get("corporate_events", [])
                ]
            merged.append(item)
        # User-added items are fully owned by SQLite, but keep the same
        # internal override representation out of the strategy payload.
        for symbol in sorted(dynamic):
            item = copy.deepcopy(dynamic[symbol])
            sizing_overrides = item.pop("sizing_overrides", None)
            if isinstance(sizing_overrides, dict):
                item["sizing"] = {
                    **dict(item.get("sizing") or {}),
                    **sizing_overrides,
                }
            merged.append(item)
        portfolio["positions"] = merged
        return result

    def snapshot(self, raw: dict[str, Any]) -> dict[str, Any]:
        merged = self.overlay(raw)
        portfolio = merged.get("portfolio", {})
        return {
            "cash": _finite_float(portfolio.get("available_cash", 0), "可用资金"),
            "positions": [_normalized_payload(item) for item in portfolio.get("positions", [])],
            "supported_sectors": _SUPPORTED_SECTORS,
            "database_path": str(self.path),
        }

    def set_available_cash(self, *, amount: float, executed_at: str, note: str = "") -> None:
        """Set the workspace's current available cash with an append-only audit row.

        A newly created link-scoped workspace deliberately starts without any
        inherited account facts.  This small calibration action lets its owner
        establish (or later reconcile) the cash balance before recording a
        confirmed trade.  It never changes shares or economic basis.
        """
        amount = _finite_float(amount, "可用资金")
        if amount < 0:
            raise PortfolioStoreError("可用资金不能为负")
        executed = _normalize_date(executed_at)
        now = _now()
        with self._transaction() as conn:
            before_cash = _finite_float(self._get_meta(conn, "available_cash") or 0, "可用资金")
            if round(before_cash, 2) == round(amount, 2):
                raise PortfolioStoreError("可用资金未发生变化")
            cash_delta = round(amount - before_cash, 2)
            self._set_meta(conn, "available_cash", round(amount, 2), now)
            conn.execute(
                "INSERT INTO portfolio_transactions(executed_at,symbol,bucket,side,shares,price,fee,cash_delta,note,before_payload,after_payload,cash_before,cash_after,created_at) "
                "VALUES (?, '', 'cash', 'cash_adjustment', 0, 0, 0, ?, ?, NULL, NULL, ?, ?, ?)",
                (
                    executed,
                    cash_delta,
                    (note or "可用资金校准").strip(),
                    before_cash,
                    round(amount, 2),
                    now,
                ),
            )

    def set_position_weight_limits(
        self,
        *,
        symbol: str,
        target_main_weight: float,
        max_single_position_weight: float,
        portfolio_max_weight: float,
    ) -> None:
        """Persist one holding's soft target and hard concentration ceiling."""
        symbol = _normalize_symbol(symbol)
        target = _finite_float(target_main_weight, "长期目标仓位")
        single_cap = _finite_float(max_single_position_weight, "单股仓位上限")
        portfolio_cap = _finite_float(portfolio_max_weight, "账户级单股上限")
        if not 0 < target <= 1:
            raise PortfolioStoreError("长期目标仓位必须在0%到100%之间")
        if not 0 < single_cap <= 1:
            raise PortfolioStoreError("单股仓位上限必须在0%到100%之间")
        if not 0 < portfolio_cap <= 1:
            raise PortfolioStoreError("账户级单股上限配置无效")
        if target > single_cap:
            raise PortfolioStoreError("长期目标仓位不得高于单股仓位上限")
        if single_cap > portfolio_cap:
            raise PortfolioStoreError(
                f"单股仓位上限不得超过账户级上限{portfolio_cap * 100:g}%"
            )
        now = _now()
        with self._transaction() as conn:
            record = self._position(conn, symbol)
            if record.get("role") != "holding":
                raise PortfolioStoreError("只有正式持仓可以设置仓位目标与上限")
            sizing_overrides = dict(record.get("sizing_overrides") or {})
            sizing_overrides["target_main_weight"] = round(target, 8)
            sizing_overrides["max_single_position_weight"] = round(single_cap, 8)
            record["sizing_overrides"] = sizing_overrides
            self._save_position(conn, record, now)

    def transactions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, executed_at, symbol, bucket, side, shares, price, fee, cash_delta, note, reversal_of "
                "FROM portfolio_transactions ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def reverse_transaction(self, transaction_id: int, note: str = "") -> None:
        """Append a reversible correction without deleting the original record.

        A correction is accepted only while the active position and cash still
        equal the original post-trade snapshot. This prevents an old mistake
        from silently overwriting later confirmed trades.
        """
        now = _now()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM portfolio_transactions WHERE id=?", (int(transaction_id),)
            ).fetchone()
            if row is None:
                raise PortfolioStoreError("找不到该笔成交")
            if row["side"] == "reversal" or row["reversal_of"] is not None:
                raise PortfolioStoreError("冲销记录不能再次冲销")
            if conn.execute("SELECT 1 FROM portfolio_transactions WHERE reversal_of=?", (int(transaction_id),)).fetchone():
                raise PortfolioStoreError("该笔记录已经冲销")
            if not row["before_payload"] or not row["after_payload"]:
                raise PortfolioStoreError("该历史记录创建时未保存快照，无法安全冲销；请补录一笔反向成交")
            before = _load(row["before_payload"])
            after = _load(row["after_payload"])
            current = self._position(conn, str(row["symbol"]))
            cash = _finite_float(self._get_meta(conn, "available_cash") or 0, "可用资金")
            recorded_cash_after = _finite_float(row["cash_after"], "历史成交后的可用资金")
            recorded_cash_before = _finite_float(row["cash_before"], "历史成交前的可用资金")
            recorded_cash_delta = _finite_float(row["cash_delta"], "历史成交现金变动")
            if _dump(_normalized_payload(current)) != _dump(_normalized_payload(after)) or round(cash, 2) != round(recorded_cash_after, 2):
                raise PortfolioStoreError("该笔之后已有资金或仓位变化，不能安全冲销；请用反向成交更正")
            self._save_position(conn, before, now)
            self._set_meta(conn, "available_cash", recorded_cash_before, now)
            conn.execute(
                "INSERT INTO portfolio_transactions(executed_at,symbol,bucket,side,shares,price,fee,cash_delta,note,before_payload,after_payload,cash_before,cash_after,reversal_of,created_at) "
                "VALUES (?, ?, ?, 'reversal', 0, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?)",
                (date.today().isoformat(), row["symbol"], row["bucket"], -recorded_cash_delta, (note or "冲销").strip(), _dump(after), _dump(before), recorded_cash_after, recorded_cash_before, int(transaction_id), now),
            )

    def add_watchlist(
        self,
        *,
        symbol: str,
        name: str,
        sector: str,
        peers: str | list[str] = "",
        analysis_profile: dict[str, Any] | None = None,
        commodity_exposures: list[dict[str, Any]] | None = None,
    ) -> None:
        symbol = _normalize_symbol(symbol)
        name = name.strip()
        if not name:
            raise PortfolioStoreError("请填写标的名称")
        sector = sector.strip() or "generic"
        if sector not in _SUPPORTED_SECTORS:
            raise PortfolioStoreError("行业必须从页面提供的选项中选择")
        peer_list = _normalize_peers(peers) if isinstance(peers, str) else _normalize_peer_list(peers)
        profile = dict(analysis_profile or {})
        if not profile:
            coverage = "full" if sector != "generic" else "basic"
            profile = {
                "coverage": coverage,
                "coverage_label": "完整跟踪" if coverage == "full" else "待补齐公告/产业证据",
            }
        payload = {
            "symbol": symbol,
            "name": name,
            "role": "watchlist",
            "main_shares": 0,
            "economic_basis": 0.0,
            "risk_principal": 0.0,
            "sector": sector,
            "satellite_limit": 100,
            "main_adjustment_shares": 100,
            "peers": peer_list,
            "satellite": _inactive_satellite(),
            "sizing": {},
            "migration": {},
            "analysis_profile": profile,
            "commodity_exposures": [
                dict(exposure)
                for exposure in (commodity_exposures or [])
                if isinstance(exposure, dict)
            ],
        }
        now = _now()
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM portfolio_positions WHERE symbol=?", (symbol,)).fetchone():
                raise PortfolioStoreError("该标的已在持仓或观察仓中")
            conn.execute(
                "INSERT INTO portfolio_positions(symbol, payload, is_seeded, created_at, updated_at) VALUES (?, ?, 0, ?, ?)",
                (symbol, _dump(payload), now, now),
            )

    def remove_watchlist(self, symbol: str) -> None:
        symbol = _normalize_symbol(symbol)
        with self._transaction() as conn:
            record = self._position(conn, symbol)
            if record["role"] != "watchlist":
                raise PortfolioStoreError("正式持仓不能从观察仓页面直接删除")
            conn.execute("DELETE FROM portfolio_positions WHERE symbol=?", (symbol,))

    def record_trade(
        self,
        *,
        symbol: str,
        bucket: str,
        side: str,
        shares: int,
        price: float,
        fee: float,
        executed_at: str,
        note: str = "",
        entry_support: float | None = None,
        target_price: float | None = None,
        stop_price: float | None = None,
    ) -> None:
        symbol = _normalize_symbol(symbol)
        bucket = bucket.strip().lower()
        side = side.strip().lower()
        shares = int(shares)
        price = _finite_float(price, "成交价")
        fee = _finite_float(fee or 0, "费用")
        if bucket not in {"main", "satellite"}:
            raise PortfolioStoreError("仓位类型只能是主仓或卫星仓")
        if side not in {"buy", "sell"}:
            raise PortfolioStoreError("方向只能是买入或卖出")
        if shares <= 0 or shares % 100:
            raise PortfolioStoreError("股数必须是正的100股整数")
        if price <= 0 or fee < 0:
            raise PortfolioStoreError("成交价必须大于0，费用不能为负")
        executed = _normalize_date(executed_at)
        now = _now()
        gross = round(shares * price, 2)
        if side == "sell" and fee >= gross:
            raise PortfolioStoreError("卖出费用必须小于成交金额")
        with self._transaction() as conn:
            record = self._position(conn, symbol)
            record["economic_basis"] = _finite_float(record.get("economic_basis", 0), "经济投入")
            if record.get("risk_principal") is not None:
                record["risk_principal"] = _finite_float(record["risk_principal"], "风险本金")
            cash = _finite_float(self._get_meta(conn, "available_cash") or 0, "可用资金")
            before_record = copy.deepcopy(record)
            before_cash = cash
            satellite = dict(record.get("satellite") or _inactive_satellite())
            if bucket == "main":
                if side == "buy":
                    total = gross + fee
                    if cash + 1e-8 < total:
                        raise PortfolioStoreError("可用资金不足，无法确认该笔买入")
                    was_watchlist = record.get("role") == "watchlist"
                    profile = record.get("analysis_profile") or {}
                    if was_watchlist and (
                        record.get("sector") == "generic" or profile.get("coverage") != "full"
                    ):
                        raise PortfolioStoreError("该标的尚未具备完整研究覆盖，补齐公告与产业证据路由后才能转为主仓")
                    record["main_shares"] = int(record.get("main_shares", 0)) + shares
                    record["economic_basis"] = round(float(record.get("economic_basis", 0)) + total, 2)
                    record["risk_principal"] = round(float(record.get("risk_principal", 0)) + total, 2)
                    record["role"] = "holding"
                    if was_watchlist:
                        record["watchlist_entry_date"] = executed
                    cash_delta = -total
                else:
                    held = int(record.get("main_shares", 0))
                    if shares > held:
                        raise PortfolioStoreError("卖出股数超过当前主仓")
                    net = gross - fee
                    record["main_shares"] = held - shares
                    record["economic_basis"] = round(float(record.get("economic_basis", 0)) - net, 2)
                    cash_delta = net
            else:
                if side == "buy":
                    if record.get("role") != "holding" or int(record.get("main_shares", 0)) <= 0:
                        raise PortfolioStoreError("卫星仓只能附属于已有主仓的正式持仓")
                    if bool(satellite.get("active")):
                        raise PortfolioStoreError("该标的已有活动卫星仓，请先完成退出记录")
                    if shares > int(record.get("satellite_limit", 100)):
                        raise PortfolioStoreError("股数超过该标的的卫星仓上限")
                    support = _positive(entry_support, "卫星仓支撑价")
                    target = _positive(target_price, "卫星仓目标价")
                    if target <= support:
                        raise PortfolioStoreError("卫星仓目标价必须高于支撑价")
                    stop = _finite_float(stop_price, "卫星仓风险退出价") if stop_price not in (None, "") else None
                    if stop is not None and stop >= support:
                        raise PortfolioStoreError("卫星仓风险退出价必须低于支撑价")
                    total = gross + fee
                    if cash + 1e-8 < total:
                        raise PortfolioStoreError("可用资金不足，无法确认该笔卫星仓买入")
                    satellite = {
                        "active": True,
                        "shares": shares,
                        "entry_price": price,
                        "entry_date": executed,
                        "entry_support": support,
                        "target_price": target,
                        "stop_price": stop,
                    }
                    record["satellite"] = satellite
                    record["economic_basis"] = round(float(record.get("economic_basis", 0)) + total, 2)
                    record["risk_principal"] = round(float(record.get("risk_principal", 0)) + total, 2)
                    cash_delta = -total
                else:
                    if not bool(satellite.get("active")):
                        raise PortfolioStoreError("该标的没有活动卫星仓")
                    held = int(satellite.get("shares", 0))
                    if shares > held:
                        raise PortfolioStoreError("卖出股数超过当前卫星仓")
                    net = gross - fee
                    remaining = held - shares
                    if remaining:
                        satellite["shares"] = remaining
                    else:
                        satellite = _inactive_satellite()
                    record["satellite"] = satellite
                    record["economic_basis"] = round(float(record.get("economic_basis", 0)) - net, 2)
                    cash_delta = net

            if int(record.get("main_shares", 0)) == 0 and not bool(record.get("satellite", {}).get("active")):
                # Closed positions remain in the immutable trade ledger; the active tracker
                # returns to watchlist mode and does not retain a phantom risk basis.
                record["role"] = "watchlist"
                record["economic_basis"] = 0.0
                record["risk_principal"] = 0.0
                record.pop("watchlist_entry_date", None)
            self._save_position(conn, record, now)
            self._set_meta(conn, "available_cash", round(cash + cash_delta, 2), now)
            conn.execute(
                "INSERT INTO portfolio_transactions(executed_at,symbol,bucket,side,shares,price,fee,cash_delta,note,before_payload,after_payload,cash_before,cash_after,created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (executed, symbol, bucket, side, shares, price, fee, cash_delta, note.strip(), _dump(before_record), _dump(record), before_cash, round(cash + cash_delta, 2), now),
            )

    def sync_auto_dividends(
        self,
        *,
        symbol: str,
        events: list[dict[str, Any]],
        as_of: str,
    ) -> list[dict[str, Any]]:
        """Persist verified implementation notices and settle due dividends once.

        This is deliberately an end-of-day ledger action.  It does not assume
        intraday buying power from a dividend until the payment date has ended,
        and it never settles a title-only or partially parsed announcement.
        """
        symbol = _normalize_symbol(symbol)
        settlement_day = _normalize_date(as_of)
        now = _now()
        outcomes: list[dict[str, Any]] = []
        normalized_events = [_normalized_dividend_event(event) for event in events]
        with self._transaction() as conn:
            record = self._position(conn, symbol)
            if record.get("role") != "holding":
                return outcomes
            record["economic_basis"] = _finite_float(record.get("economic_basis", 0), "经济投入")
            auto_events = {
                _dividend_event_id(event): _normalized_dividend_event(event)
                for event in record.get("auto_dividend_events", []) or []
                if isinstance(event, dict)
            }
            configured_events = {
                _dividend_event_id(event): _normalized_dividend_event(event)
                for event in record.get("corporate_events", []) or []
                if isinstance(event, dict)
            }
            changed = False
            for event in normalized_events:
                event_id = _dividend_event_id(event)
                configured = configured_events.get(event_id, {})
                existing = auto_events.get(event_id, {})
                merged = {**event, **configured, **existing}
                # Fresh official source metadata may improve an older stored
                # event, but historical entitlement is immutable once derived.
                for key in ("payment_date", "source_url", "announcement_title", "announced_at", "note"):
                    if event.get(key):
                        merged[key] = event[key]
                merged["auto_discovered"] = True
                merged = _normalized_dividend_event(merged)
                if auto_events.get(event_id) != merged:
                    auto_events[event_id] = merged
                    changed = True

            settled = set(str(value) for value in record.get("settled_dividend_event_ids", []))
            cash = _finite_float(self._get_meta(conn, "available_cash") or 0, "可用资金")
            for event_id, event in sorted(auto_events.items(), key=lambda item: (
                str(item[1].get("payment_date", "")), item[0],
            )):
                payment_date = str(event.get("payment_date") or "")
                if not payment_date or payment_date > settlement_day or event_id in settled:
                    continue
                record_date = str(event["record_date"])
                eligible_shares = event.get("eligible_shares")
                if eligible_shares is None:
                    eligible_shares = self._shares_at_record_date(conn, symbol, record, record_date)
                    event = {**event, "eligible_shares": eligible_shares}
                    auto_events[event_id] = event
                    changed = True
                eligible_shares = int(eligible_shares)
                settled.add(event_id)
                if eligible_shares <= 0:
                    outcomes.append({
                        "symbol": symbol,
                        "event_id": event_id,
                        "payment_date": payment_date,
                        "amount": 0.0,
                        "status": "no_entitlement",
                    })
                    changed = True
                    continue
                amount = round(float(event["cash_per_share"]) * eligible_shares, 2)
                if amount <= 0:
                    raise PortfolioStoreError("自动分红金额必须大于0")
                before_record = copy.deepcopy(record)
                before_cash = cash
                if not bool(event.get("basis_adjusted", False)):
                    record["economic_basis"] = round(float(record["economic_basis"]) - amount, 2)
                record["settled_dividend_event_ids"] = sorted(settled)
                self._save_position(conn, record, now)
                cash = round(cash + amount, 2)
                self._set_meta(conn, "available_cash", cash, now)
                marker = f"[auto-dividend-event:{event_id}]"
                note = f"{marker} 交易所实施公告自动入账"
                conn.execute(
                    "INSERT INTO portfolio_transactions(executed_at,symbol,bucket,side,shares,price,fee,cash_delta,note,before_payload,after_payload,cash_before,cash_after,created_at) "
                    "VALUES (?, ?, 'cash', 'dividend', 0, 0, 0, ?, ?, ?, ?, ?, ?, ?)",
                    (payment_date, symbol, amount, note, _dump(before_record), _dump(record), before_cash, cash, now),
                )
                outcomes.append({
                    "symbol": symbol,
                    "event_id": event_id,
                    "payment_date": payment_date,
                    "amount": amount,
                    "eligible_shares": eligible_shares,
                    "status": "settled",
                })
                changed = True
            if changed:
                record["auto_dividend_events"] = list(auto_events.values())
                record["settled_dividend_event_ids"] = sorted(settled)
                self._save_position(conn, record, now)
        return outcomes

    def _shares_at_record_date(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        record: dict[str, Any],
        record_date: str,
    ) -> int:
        """Reconstruct entitlement from the immutable transaction ledger.

        A trade dated exactly on the record date has no timestamp in this local
        ledger; it is therefore treated as ambiguous instead of guessed.
        """
        same_day = conn.execute(
            "SELECT 1 FROM portfolio_transactions WHERE symbol=? AND executed_at=? "
            "AND side IN ('buy','sell','reversal') LIMIT 1",
            (symbol, record_date),
        ).fetchone()
        if same_day:
            raise PortfolioStoreError("股权登记日存在成交，自动分红无法安全核算应得股数")
        satellite = dict(record.get("satellite") or {})
        shares = int(record.get("main_shares", 0)) + (
            int(satellite.get("shares", 0)) if satellite.get("active") else 0
        )
        rows = conn.execute(
            "SELECT id,bucket,side,shares,reversal_of FROM portfolio_transactions "
            "WHERE symbol=? AND executed_at>? ORDER BY id DESC",
            (symbol, record_date),
        ).fetchall()
        for row in rows:
            if row["side"] in {"buy", "sell"} and row["bucket"] in {"main", "satellite"}:
                shares += -int(row["shares"]) if row["side"] == "buy" else int(row["shares"])
            elif row["side"] == "reversal" and row["reversal_of"]:
                original = conn.execute(
                    "SELECT bucket,side,shares FROM portfolio_transactions WHERE id=?",
                    (int(row["reversal_of"]),),
                ).fetchone()
                if original and original["bucket"] in {"main", "satellite"} and original["side"] in {"buy", "sell"}:
                    shares += int(original["shares"]) if original["side"] == "buy" else -int(original["shares"])
        if shares < 0:
            raise PortfolioStoreError("无法从成交台账安全还原股权登记日持股")
        return shares

    def record_dividend(
        self,
        *,
        symbol: str,
        amount: float,
        executed_at: str,
        note: str = "",
        event_id: str | None = None,
        corporate_events: list[dict[str, Any]] | None = None,
    ) -> str:
        """Record a cash dividend against one configured dividend event.

        A dividend is an accounting event, not just a cash transfer.  Requiring a
        unique configured event prevents a cash receipt from silently reducing the
        economic basis twice when the ex-dividend adjustment was already applied
        in the static portfolio configuration.
        """
        symbol = _normalize_symbol(symbol)
        amount = _finite_float(amount, "分红到账金额")
        if amount <= 0:
            raise PortfolioStoreError("分红到账金额必须大于0")
        executed = _normalize_date(executed_at)
        now = _now()
        with self._transaction() as conn:
            record = self._position(conn, symbol)
            if record.get("role") != "holding":
                raise PortfolioStoreError("只有正式持仓可以登记分红")
            record["economic_basis"] = _finite_float(record.get("economic_basis", 0), "经济投入")
            cash = _finite_float(self._get_meta(conn, "available_cash") or 0, "可用资金")
            before_record = copy.deepcopy(record)
            before_cash = cash
            events = corporate_events if corporate_events is not None else list(record.get("corporate_events", []) or [])
            matched = _match_dividend_event(events, record, amount, executed, event_id=event_id)
            if matched is None:
                raise PortfolioStoreError("未能唯一匹配分红公告；请在页面选择对应公告后再登记，以避免错误调账")
            settled = set(str(value) for value in record.get("settled_dividend_event_ids", []))
            matched_id = _dividend_event_id(matched)
            if matched_id in settled:
                raise PortfolioStoreError("该分红公告已登记到账，不能重复记录")
            pre_adjusted = bool(matched.get("basis_adjusted", False))
            if not pre_adjusted:
                record["economic_basis"] = round(float(record.get("economic_basis", 0)) - amount, 2)
            settled.add(matched_id)
            record["settled_dividend_event_ids"] = sorted(settled)
            self._save_position(conn, record, now)
            self._set_meta(conn, "available_cash", round(cash + amount, 2), now)
            conn.execute(
                "INSERT INTO portfolio_transactions(executed_at,symbol,bucket,side,shares,price,fee,cash_delta,note,before_payload,after_payload,cash_before,cash_after,created_at) "
                "VALUES (?, ?, 'cash', 'dividend', 0, 0, 0, ?, ?, ?, ?, ?, ?, ?)",
                (executed, symbol, amount, note.strip(), _dump(before_record), _dump(record), before_cash, round(cash + amount, 2), now),
            )
        return "pre_adjusted" if pre_adjusted else "basis_adjusted"

    def reconcile_pre_adjusted_dividend(
        self,
        *,
        symbol: str,
        event_id: str,
        amount: float,
        corporate_events: list[dict[str, Any]],
        note: str = "",
    ) -> None:
        """Correct a legacy double deduction while retaining the recorded cash.

        This intentionally creates an append-only zero-cash audit row instead of
        deleting or reversing the dividend receipt.  It is limited to configured
        events whose economic basis was already adjusted before cash settlement.
        """
        symbol = _normalize_symbol(symbol)
        amount = _finite_float(amount, "校准金额")
        if amount <= 0:
            raise PortfolioStoreError("校准金额必须大于0")
        event = _event_by_id(corporate_events, event_id)
        if event is None or not bool(event.get("basis_adjusted", False)):
            raise PortfolioStoreError("只能校准已在配置中预先调整经济投入的分红公告")
        now = _now()
        with self._transaction() as conn:
            record = self._position(conn, symbol)
            record["economic_basis"] = _finite_float(record.get("economic_basis", 0), "经济投入")
            settled = set(str(value) for value in record.get("settled_dividend_event_ids", []))
            correction_marker = f"[dividend-event:{event_id}]"
            if conn.execute(
                "SELECT 1 FROM portfolio_transactions WHERE symbol=? AND side='basis_correction' AND note LIKE ?",
                (symbol, f"%{correction_marker}%"),
            ).fetchone():
                raise PortfolioStoreError("该分红公告已经完成校准")
            receipts = conn.execute(
                "SELECT id FROM portfolio_transactions WHERE symbol=? AND side='dividend' "
                "AND ABS(cash_delta - ?) < 0.005 AND reversal_of IS NULL",
                (symbol, amount),
            ).fetchall()
            if len(receipts) != 1:
                raise PortfolioStoreError("未找到唯一的原始分红到账记录，不能自动校准")
            cash = _finite_float(self._get_meta(conn, "available_cash") or 0, "可用资金")
            before_record = copy.deepcopy(record)
            record["economic_basis"] = round(float(record.get("economic_basis", 0)) + amount, 2)
            settled.add(event_id)
            record["settled_dividend_event_ids"] = sorted(settled)
            self._save_position(conn, record, now)
            conn.execute(
                "INSERT INTO portfolio_transactions(executed_at,symbol,bucket,side,shares,price,fee,cash_delta,note,before_payload,after_payload,cash_before,cash_after,created_at) "
                "VALUES (?, ?, 'cash', 'basis_correction', 0, 0, 0, 0, ?, ?, ?, ?, ?, ?)",
                (date.today().isoformat(), symbol, f"{(note or '分红预先调账校准').strip()} {correction_marker}", _dump(before_record), _dump(record), cash, cash, now),
            )

    # Notification configuration intentionally lives alongside the mutable
    # portfolio ledger. Strategy configuration stays in YAML; these tables only
    # answer who receives an already-approved signal.
    def notification_key_available(self) -> bool:
        return bool(os.getenv("NOTIFICATION_ENCRYPTION_KEY", "").strip())

    # A workspace owns exactly one destination.  The older multi-recipient
    # tables remain readable for a one-time migration but are no longer used
    # by normal delivery or the simplified settings screen.
    def notification_settings(self, raw: dict[str, Any], *, include_secrets: bool = False) -> dict[str, Any]:
        self._ensure_notification_settings(raw)
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM notification_settings WHERE id=1").fetchone()
        if row is None:
            return {
                "id": 1, "webhook_configured": False, "min_confidence": "中",
                "webhook": "", "secret": "", "unreadable": False,
            }
        result = {
            "id": 1,
            "webhook_configured": bool(row["webhook_ciphertext"]),
            "min_confidence": str(row["min_confidence"]),
        }
        if include_secrets:
            try:
                result["webhook"] = self._open_notification_secret(str(row["webhook_ciphertext"]))
                result["secret"] = self._open_notification_secret(str(row["secret_ciphertext"])) if row["secret_ciphertext"] else ""
            except PortfolioStoreError:
                result.update({"webhook": "", "secret": "", "unreadable": True})
        return result

    def save_notification_settings(self, *, webhook: str, min_confidence: str, raw: dict[str, Any]) -> None:
        if min_confidence not in {"中", "高"}:
            raise PortfolioStoreError("最低置信度只能选中或高")
        if not self.notification_key_available():
            raise PortfolioStoreError("尚未配置 NOTIFICATION_ENCRYPTION_KEY，不能安全保存机器人地址")
        self._ensure_notification_settings(raw)
        webhook = str(webhook or "").strip()
        if webhook and not webhook.startswith("https://open.feishu.cn/open-apis/bot/"):
            raise PortfolioStoreError("请填写有效的飞书机器人 Webhook")
        now = _now()
        with self._transaction() as conn:
            existing = conn.execute("SELECT * FROM notification_settings WHERE id=1").fetchone()
            if existing is None:
                if not webhook:
                    raise PortfolioStoreError("请填写飞书机器人 Webhook")
                conn.execute(
                    "INSERT INTO notification_settings(id, webhook_ciphertext, min_confidence, created_at, updated_at) VALUES (1, ?, ?, ?, ?)",
                    (self._seal_notification_secret(webhook), min_confidence, now, now),
                )
                return
            fields = ["min_confidence=?", "updated_at=?"]
            values: list[Any] = [min_confidence, now]
            if webhook:
                fields.append("webhook_ciphertext=?")
                values.append(self._seal_notification_secret(webhook))
            values.append(1)
            conn.execute(f"UPDATE notification_settings SET {', '.join(fields)} WHERE id=?", values)

    def notification_setting_delivery_claimed(self, delivery_key: str, kind: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM notification_setting_deliveries WHERE setting_id=1 AND delivery_key=? AND kind=?",
                (delivery_key, kind),
            ).fetchone()
        return row is not None

    def mark_notification_setting_delivery(
        self, delivery_key: str, kind: str, status: str, message: str, error: str = "",
    ) -> None:
        now = _now()
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO notification_setting_deliveries(setting_id, delivery_key, kind, status, message_hash, error, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(setting_id, delivery_key, kind) DO UPDATE SET status=excluded.status, message_hash=excluded.message_hash, error=excluded.error, updated_at=excluded.updated_at",
                (delivery_key, kind, status, digest, error[:300], now),
            )

    def _ensure_notification_settings(self, raw: dict[str, Any]) -> None:
        """Migrate the legacy default robot into one workspace setting once."""
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM notification_settings WHERE id=1").fetchone():
                return
        if not self.notification_key_available():
            return
        webhook = ""
        secret = ""
        confidence = "中"
        with self._connect() as conn:
            legacy = conn.execute(
                "SELECT webhook_ciphertext, secret_ciphertext, min_confidence FROM notification_destinations ORDER BY id LIMIT 1"
            ).fetchone()
        if legacy is not None:
            try:
                webhook = self._open_notification_secret(str(legacy["webhook_ciphertext"]))
                secret = self._open_notification_secret(str(legacy["secret_ciphertext"])) if legacy["secret_ciphertext"] else ""
                confidence = str(legacy["min_confidence"] or "中")
            except PortfolioStoreError:
                webhook = ""
        if not webhook and bool(raw.get("_workspace_seed_static", True)):
            notification = raw.get("notification", {})
            webhook = str(notification.get("webhook", "") or "").strip()
            secret = str(notification.get("secret", "") or "")
        if not webhook:
            return
        now = _now()
        with self._transaction() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO notification_settings(id, webhook_ciphertext, secret_ciphertext, min_confidence, created_at, updated_at) VALUES (1, ?, ?, ?, ?, ?)",
                (
                    self._seal_notification_secret(webhook),
                    self._seal_notification_secret(secret) if secret else "",
                    confidence if confidence in {"中", "高"} else "中",
                    now,
                    now,
                ),
            )

    def notification_collections(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        positions = self.snapshot(raw)["positions"]
        system = [
            {
                "key": "system:holdings", "name": "全部当前持仓", "description": "自动包含所有正式持仓", "system": True,
                "symbols": [item["symbol"] for item in positions if item.get("role") == "holding"],
            },
            {
                "key": "system:watchlist", "name": "全部观察仓", "description": "自动包含所有观察标的", "system": True,
                "symbols": [item["symbol"] for item in positions if item.get("role") == "watchlist"],
            },
        ]
        with self._connect() as conn:
            rows = conn.execute("SELECT id, name, description FROM notification_collections ORDER BY name").fetchall()
            members = conn.execute("SELECT collection_id, symbol FROM notification_collection_members ORDER BY symbol").fetchall()
        member_map: dict[int, list[str]] = {}
        for row in members:
            member_map.setdefault(int(row["collection_id"]), []).append(str(row["symbol"]))
        custom = [
            {
                "key": f"collection:{int(row['id'])}", "id": int(row["id"]), "name": str(row["name"]),
                "description": str(row["description"]), "system": False,
                "symbols": member_map.get(int(row["id"]), []),
            }
            for row in rows
        ]
        return [*system, *custom]

    def create_notification_collection(self, name: str, description: str = "") -> None:
        name = str(name or "").strip()
        if not name:
            raise PortfolioStoreError("请填写关注清单名称")
        if len(name) > 40 or name.startswith("system:"):
            raise PortfolioStoreError("关注清单名称不合法")
        now = _now()
        try:
            with self._transaction() as conn:
                conn.execute(
                    "INSERT INTO notification_collections(name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (name, str(description or "").strip()[:160], now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise PortfolioStoreError("已存在同名关注清单") from exc

    def update_notification_collection_members(
        self, collection_id: int, symbols: list[str], raw: dict[str, Any]
    ) -> None:
        known = {item["symbol"] for item in self.snapshot(raw)["positions"]}
        normalized = sorted({_normalize_symbol(symbol) for symbol in symbols})
        if any(symbol not in known for symbol in normalized):
            raise PortfolioStoreError("关注清单中包含未知标的")
        now = _now()
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM notification_collections WHERE id=?", (int(collection_id),)).fetchone() is None:
                raise PortfolioStoreError("找不到该关注清单")
            conn.execute("DELETE FROM notification_collection_members WHERE collection_id=?", (int(collection_id),))
            conn.executemany(
                "INSERT INTO notification_collection_members(collection_id, symbol) VALUES (?, ?)",
                [(int(collection_id), symbol) for symbol in normalized],
            )
            conn.execute("UPDATE notification_collections SET updated_at=? WHERE id=?", (now, int(collection_id)))

    def delete_notification_collection(self, collection_id: int) -> None:
        with self._transaction() as conn:
            key = f"collection:{int(collection_id)}"
            conn.execute("DELETE FROM notification_destination_collections WHERE collection_key=?", (key,))
            deleted = conn.execute("DELETE FROM notification_collections WHERE id=?", (int(collection_id),)).rowcount
            if not deleted:
                raise PortfolioStoreError("找不到该关注清单")

    def notification_destinations(self, raw: dict[str, Any], *, include_secrets: bool = False) -> list[dict[str, Any]]:
        self._ensure_notification_default(raw)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM notification_destinations ORDER BY id").fetchall()
            collection_rows = conn.execute("SELECT destination_id, collection_key FROM notification_destination_collections ORDER BY collection_key").fetchall()
            symbol_rows = conn.execute("SELECT destination_id, symbol FROM notification_destination_symbols ORDER BY symbol").fetchall()
        collection_map: dict[int, list[str]] = {}
        for row in collection_rows:
            collection_map.setdefault(int(row["destination_id"]), []).append(str(row["collection_key"]))
        symbol_map: dict[int, list[str]] = {}
        for row in symbol_rows:
            symbol_map.setdefault(int(row["destination_id"]), []).append(str(row["symbol"]))
        result: list[dict[str, Any]] = []
        for row in rows:
            item = {
                "id": int(row["id"]), "name": str(row["name"]),
                "webhook_configured": bool(row["webhook_ciphertext"]),
                "min_confidence": str(row["min_confidence"]),
                "send_actions": bool(row["send_actions"]), "send_summary": bool(row["send_summary"]),
                "receive_critical_all": bool(row["receive_critical_all"]), "enabled": bool(row["enabled"]),
                "collection_keys": collection_map.get(int(row["id"]), []),
                "symbols": symbol_map.get(int(row["id"]), []),
            }
            if include_secrets:
                try:
                    item["webhook"] = self._open_notification_secret(str(row["webhook_ciphertext"]))
                    item["secret"] = self._open_notification_secret(str(row["secret_ciphertext"])) if row["secret_ciphertext"] else ""
                except PortfolioStoreError:
                    item["webhook"] = ""
                    item["secret"] = ""
                    item["unreadable"] = True
            result.append(item)
        return result

    def save_notification_destination(
        self, *, destination_id: int | None, name: str, webhook: str, secret: str,
        min_confidence: str, send_actions: bool, send_summary: bool,
        receive_critical_all: bool, enabled: bool, collection_keys: list[str],
        symbols: list[str], raw: dict[str, Any],
    ) -> int:
        name = str(name or "").strip()
        if not name or len(name) > 50:
            raise PortfolioStoreError("请填写不超过50字的机器人名称")
        if min_confidence not in {"中", "高"}:
            raise PortfolioStoreError("最低置信度只能选中或高")
        if not self.notification_key_available():
            raise PortfolioStoreError("尚未配置 NOTIFICATION_ENCRYPTION_KEY，不能安全保存机器人地址")
        known = {item["symbol"] for item in self.snapshot(raw)["positions"]}
        selected_symbols = sorted({_normalize_symbol(symbol) for symbol in symbols})
        if any(symbol not in known for symbol in selected_symbols):
            raise PortfolioStoreError("机器人直接订阅中包含未知标的")
        collection_keys = sorted({str(key) for key in collection_keys})
        valid_keys = {item["key"] for item in self.notification_collections(raw)}
        if any(key not in valid_keys for key in collection_keys):
            raise PortfolioStoreError("机器人订阅中包含不存在的关注清单")
        if not collection_keys and not selected_symbols and not receive_critical_all:
            raise PortfolioStoreError("请至少选择一个关注清单、直接关注标的，或启用组合风险兜底")
        webhook = str(webhook or "").strip()
        if webhook and not webhook.startswith("https://open.feishu.cn/open-apis/bot/"):
            raise PortfolioStoreError("请填写有效的飞书机器人 Webhook")
        now = _now()
        try:
            with self._transaction() as conn:
                if destination_id is None:
                    if not webhook:
                        raise PortfolioStoreError("请填写飞书机器人 Webhook")
                    cursor = conn.execute(
                        "INSERT INTO notification_destinations(name, webhook_ciphertext, secret_ciphertext, min_confidence, send_actions, send_summary, receive_critical_all, enabled, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (name, self._seal_notification_secret(webhook), self._seal_notification_secret(str(secret or "").strip()) if secret else "", min_confidence, int(send_actions), int(send_summary), int(receive_critical_all), int(enabled), now, now),
                    )
                    target_id = int(cursor.lastrowid)
                else:
                    previous = conn.execute("SELECT * FROM notification_destinations WHERE id=?", (int(destination_id),)).fetchone()
                    if previous is None:
                        raise PortfolioStoreError("找不到该机器人")
                    fields = ["name=?", "min_confidence=?", "send_actions=?", "send_summary=?", "receive_critical_all=?", "enabled=?", "updated_at=?"]
                    values: list[Any] = [name, min_confidence, int(send_actions), int(send_summary), int(receive_critical_all), int(enabled), now]
                    if webhook:
                        fields.append("webhook_ciphertext=?")
                        values.append(self._seal_notification_secret(webhook))
                    if secret:
                        fields.append("secret_ciphertext=?")
                        values.append(self._seal_notification_secret(secret.strip()))
                    values.append(int(destination_id))
                    conn.execute(f"UPDATE notification_destinations SET {', '.join(fields)} WHERE id=?", values)
                    target_id = int(destination_id)
                conn.execute("DELETE FROM notification_destination_collections WHERE destination_id=?", (target_id,))
                conn.execute("DELETE FROM notification_destination_symbols WHERE destination_id=?", (target_id,))
                conn.executemany("INSERT INTO notification_destination_collections(destination_id, collection_key) VALUES (?, ?)", [(target_id, key) for key in collection_keys])
                conn.executemany("INSERT INTO notification_destination_symbols(destination_id, symbol) VALUES (?, ?)", [(target_id, symbol) for symbol in selected_symbols])
        except sqlite3.IntegrityError as exc:
            raise PortfolioStoreError("已存在同名机器人") from exc
        return target_id

    def delete_notification_destination(self, destination_id: int) -> None:
        with self._transaction() as conn:
            deleted = conn.execute("DELETE FROM notification_destinations WHERE id=?", (int(destination_id),)).rowcount
            if not deleted:
                raise PortfolioStoreError("找不到该机器人")

    def delivery_sent(self, destination_id: int, delivery_key: str, kind: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM notification_deliveries WHERE destination_id=? AND delivery_key=? AND kind=?",
                (int(destination_id), delivery_key, kind),
            ).fetchone()
        return bool(row and row["status"] == "sent")

    def delivery_claimed(self, destination_id: int, delivery_key: str, kind: str) -> bool:
        """An uncertain timeout is also treated as delivered to avoid duplicates."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM notification_deliveries WHERE destination_id=? AND delivery_key=? AND kind=?",
                (int(destination_id), delivery_key, kind),
            ).fetchone()
        return row is not None

    def mark_delivery(self, destination_id: int, delivery_key: str, kind: str, status: str, message: str, error: str = "") -> None:
        now = _now()
        digest = hashlib.sha256(message.encode("utf-8")).hexdigest()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO notification_deliveries(destination_id, delivery_key, kind, status, message_hash, error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(destination_id, delivery_key, kind) DO UPDATE SET status=excluded.status, message_hash=excluded.message_hash, error=excluded.error, updated_at=excluded.updated_at",
                (int(destination_id), delivery_key, kind, status, digest, error[:300], now),
            )

    def _ensure_notification_default(self, raw: dict[str, Any]) -> None:
        if not self.notification_key_available():
            return
        notification = raw.get("notification", {})
        webhook = str(notification.get("webhook", "") or "").strip()
        if not webhook:
            return
        now = _now()
        with self._transaction() as conn:
            migrated = self._get_meta(conn, "notification_default_migrated")
            if migrated:
                return
            existing = conn.execute("SELECT id FROM notification_destinations WHERE name='默认通知'").fetchone()
            if existing is None:
                cursor = conn.execute(
                    "INSERT INTO notification_destinations(name, webhook_ciphertext, secret_ciphertext, min_confidence, send_actions, send_summary, receive_critical_all, enabled, created_at, updated_at) VALUES (?, ?, ?, '中', 1, 1, 1, 1, ?, ?)",
                    ("默认通知", self._seal_notification_secret(webhook), self._seal_notification_secret(str(notification.get("secret", "") or "")) if notification.get("secret") else "", now, now),
                )
                destination_id = int(cursor.lastrowid)
                conn.executemany(
                    "INSERT INTO notification_destination_collections(destination_id, collection_key) VALUES (?, ?)",
                    [(destination_id, "system:holdings"), (destination_id, "system:watchlist")],
                )
            self._set_meta(conn, "notification_default_migrated", True, now)

    def _notification_key(self) -> bytes:
        value = os.getenv("NOTIFICATION_ENCRYPTION_KEY", "").strip()
        if not value:
            raise PortfolioStoreError("尚未配置 NOTIFICATION_ENCRYPTION_KEY")
        return hashlib.sha256(value.encode("utf-8")).digest()

    def _seal_notification_secret(self, value: str) -> str:
        if not value:
            return ""
        key = self._notification_key()
        nonce = secrets.token_bytes(16)
        ciphertext = _xor_stream(value.encode("utf-8"), key, nonce)
        tag = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        return "v1:" + base64.urlsafe_b64encode(nonce + tag + ciphertext).decode("ascii")

    def _open_notification_secret(self, value: str) -> str:
        if not value:
            return ""
        if not value.startswith("v1:"):
            raise PortfolioStoreError("机器人地址格式无法识别")
        key = self._notification_key()
        try:
            payload = base64.urlsafe_b64decode(value[3:].encode("ascii"))
        except Exception as exc:
            raise PortfolioStoreError("机器人地址解密失败") from exc
        if len(payload) < 48:
            raise PortfolioStoreError("机器人地址解密失败")
        nonce, tag, ciphertext = payload[:16], payload[16:48], payload[48:]
        expected = hmac.new(key, nonce + ciphertext, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise PortfolioStoreError("机器人地址校验失败，请重新填写")
        return _xor_stream(ciphertext, key, nonce).decode("utf-8")

    def _position(self, conn: sqlite3.Connection, symbol: str) -> dict[str, Any]:
        row = conn.execute("SELECT payload FROM portfolio_positions WHERE symbol=?", (symbol,)).fetchone()
        if row is None:
            raise PortfolioStoreError("找不到该标的，请先加入观察仓")
        return _load(row["payload"])

    def _save_position(self, conn: sqlite3.Connection, payload: dict[str, Any], now: str) -> None:
        conn.execute(
            "UPDATE portfolio_positions SET payload=?, updated_at=? WHERE symbol=?",
            (_dump(payload), now, payload["symbol"]),
        )

    @staticmethod
    def _get_meta(conn: sqlite3.Connection, key: str) -> Any:
        row = conn.execute("SELECT value FROM portfolio_meta WHERE key=?", (key,)).fetchone()
        return None if row is None else _load(row["value"])

    @staticmethod
    def _set_meta(conn: sqlite3.Connection, key: str, value: Any, now: str) -> None:
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            raise PortfolioStoreError(f"{key} 不能是NaN或Inf")
        conn.execute(
            "INSERT INTO portfolio_meta(key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, _dump(value), now),
        )


def _normalized_payload(item: dict[str, Any]) -> dict[str, Any]:
    payload = copy.deepcopy(item)
    payload["symbol"] = _normalize_symbol(str(payload.get("symbol", "")))
    payload["name"] = str(payload.get("name", payload["symbol"])).strip() or payload["symbol"]
    payload["role"] = str(payload.get("role", "watchlist")).strip().lower()
    payload["main_shares"] = int(payload.get("main_shares", 0))
    payload["economic_basis"] = _finite_float(payload.get("economic_basis", 0), "经济投入")
    payload["risk_principal"] = _finite_float(
        payload.get(
            "risk_principal",
            payload.get("migration", {}).get("risk_principal_ceiling", max(payload["economic_basis"], 0.0)),
        ),
        "风险本金",
    )
    payload["sector"] = str(payload.get("sector", "generic"))
    # Existing YAML seeds predate onboarding coverage. Both exchanges now have
    # official announcement routes; ETF company-announcement gating is handled
    # as not-applicable by the evidence layer.
    profile = dict(payload.get("analysis_profile") or {})
    if not profile:
        coverage = "full" if payload["sector"] != "generic" else "basic"
        profile = {
            "coverage": coverage,
            "coverage_label": "完整跟踪" if coverage == "full" else "待补齐公告/产业证据",
        }
    exposures = [
        dict(exposure)
        for exposure in payload.get("commodity_exposures", [])
        if isinstance(exposure, dict)
    ]
    if payload["sector"] == "copper" and not exposures:
        exposures = [default_legacy_copper_exposure()]
    if payload["sector"] == "copper":
        if not profile.get("related_derivatives"):
            profile["related_derivatives"] = default_copper_related_derivatives()
        if not profile.get("commodity_exposures"):
            profile["commodity_exposures"] = exposures
        profile["evidence_route"] = "沪铜期货、铜仓单、沪铜期权辅助、同行与公司公告"
    payload["analysis_profile"] = profile
    payload["commodity_exposures"] = exposures
    payload["peers"] = [str(peer).upper() for peer in payload.get("peers", [])]
    payload["satellite_limit"] = int(payload.get("satellite_limit", 100))
    payload["main_adjustment_shares"] = int(payload.get("main_adjustment_shares", 100))
    payload["satellite"] = dict(payload.get("satellite") or _inactive_satellite())
    payload["settled_dividend_event_ids"] = [
        str(value) for value in payload.get("settled_dividend_event_ids", [])
    ]
    payload["corporate_events"] = [
        _normalized_dividend_event(event)
        for event in payload.get("corporate_events", []) or []
        if isinstance(event, dict)
    ]
    payload["auto_dividend_events"] = [
        _normalized_dividend_event(event)
        for event in payload.get("auto_dividend_events", []) or []
        if isinstance(event, dict)
    ]
    return payload


def _inactive_satellite() -> dict[str, Any]:
    return {
        "active": False,
        "shares": 0,
        "entry_price": None,
        "entry_date": None,
        "entry_support": None,
        "target_price": None,
        "stop_price": None,
    }


def _normalize_symbol(value: str) -> str:
    value = value.strip().upper()
    if re.fullmatch(r"\d{6}", value):
        value += ".SH" if value.startswith("6") else ".SZ"
    if not _SYMBOL_RE.fullmatch(value):
        raise PortfolioStoreError("证券代码应为6位代码，或6位代码.SH/.SZ")
    return value


def _normalize_peers(value: str) -> list[str]:
    result = []
    for item in re.split(r"[,，\s]+", value.strip()):
        if item:
            result.append(_normalize_symbol(item))
    return list(dict.fromkeys(result))


def _normalize_peer_list(value: list[str]) -> list[str]:
    return list(dict.fromkeys(_normalize_symbol(str(item)) for item in value))


def _normalize_date(value: str) -> str:
    try:
        normalized = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise PortfolioStoreError("成交日期应为YYYY-MM-DD") from exc
    if normalized > date.today():
        raise PortfolioStoreError("成交日期不得晚于今天")
    return normalized.isoformat()


def _dividend_event_id(event: dict[str, Any]) -> str:
    return "|".join((
        str(event.get("type", "cash_dividend")),
        str(event.get("record_date", "")),
        str(event.get("ex_date", "")),
        f"{float(event.get('cash_per_share', 0) or 0):.6f}",
    ))


def _normalized_dividend_event(event: dict[str, Any]) -> dict[str, Any]:
    if str(event.get("type", "cash_dividend")).strip().lower() != "cash_dividend":
        raise PortfolioStoreError("自动登记仅支持现金分红")
    try:
        record_date = date.fromisoformat(str(event.get("record_date", "")))
        ex_date = date.fromisoformat(str(event.get("ex_date", "")))
    except (TypeError, ValueError) as exc:
        raise PortfolioStoreError("分红公告缺少有效的登记日或除息日") from exc
    payment_raw = event.get("payment_date")
    try:
        payment_date = date.fromisoformat(str(payment_raw)) if payment_raw else None
    except (TypeError, ValueError) as exc:
        raise PortfolioStoreError("分红公告的发放日无效") from exc
    cash_per_share = _finite_float(event.get("cash_per_share", 0), "每股现金分红")
    if cash_per_share <= 0:
        raise PortfolioStoreError("每股现金分红必须大于0")
    if ex_date < record_date or (payment_date is not None and payment_date < ex_date):
        raise PortfolioStoreError("分红公告日期顺序无效")
    eligible_raw = event.get("eligible_shares")
    eligible_shares: int | None = None
    if eligible_raw not in (None, ""):
        try:
            eligible_shares = int(eligible_raw)
        except (TypeError, ValueError) as exc:
            raise PortfolioStoreError("分红应得股数必须为非负整数") from exc
        if eligible_shares < 0:
            raise PortfolioStoreError("分红应得股数必须为非负整数")
    normalized = {
        "type": "cash_dividend",
        "record_date": record_date.isoformat(),
        "ex_date": ex_date.isoformat(),
        "cash_per_share": cash_per_share,
        "eligible_shares": eligible_shares,
        "basis_adjusted": bool(event.get("basis_adjusted", False)),
        "note": str(event.get("note") or ""),
        "auto_discovered": bool(event.get("auto_discovered", False)),
        "payment_date": payment_date.isoformat() if payment_date else None,
        "source_url": str(event.get("source_url") or ""),
        "announcement_title": str(event.get("announcement_title") or ""),
        "announced_at": str(event.get("announced_at") or ""),
    }
    normalized["event_id"] = str(event.get("event_id") or _dividend_event_id(normalized))
    return normalized


def _event_by_id(events: list[dict[str, Any]], event_id: str) -> dict[str, Any] | None:
    for event in events:
        if _dividend_event_id(event) == event_id:
            return event
    return None


def _match_dividend_event(
    events: list[dict[str, Any]],
    record: dict[str, Any],
    amount: float,
    executed: str,
    *,
    event_id: str | None = None,
) -> dict[str, Any] | None:
    """Conservatively associate a cash receipt with one configured dividend event.

    Tax withholding can make the receipt smaller than the gross entitlement, so the
    matcher permits a bounded discount but refuses ambiguous candidates.
    """
    try:
        paid_on = date.fromisoformat(executed)
    except ValueError:
        return None
    current_shares = int(record.get("main_shares", 0)) + int(
        (record.get("satellite") or {}).get("shares", 0)
        if (record.get("satellite") or {}).get("active") else 0
    )
    candidates: list[tuple[float, dict[str, Any]]] = []
    for event in events:
        if event_id and _dividend_event_id(event) != event_id:
            continue
        if str(event.get("type", "cash_dividend")) != "cash_dividend":
            continue
        try:
            ex_date = date.fromisoformat(str(event.get("ex_date", "")))
            eligible_shares = event.get("eligible_shares")
            shares = int(eligible_shares) if eligible_shares is not None else current_shares
            gross = float(event.get("cash_per_share", 0) or 0) * shares
        except (TypeError, ValueError):
            continue
        if ex_date > paid_on or gross <= 0 or amount > gross * 1.001 or amount < gross * 0.50:
            continue
        candidates.append((abs(gross - amount), event))
    if len(candidates) != 1:
        return None
    return candidates[0][1]


def _positive(value: float | None, label: str) -> float:
    number = _finite_float(value, label)
    if number <= 0:
        raise PortfolioStoreError(f"{label}必须大于0")
    return number


def _finite_float(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PortfolioStoreError(f"{label}必须是有限数字") from exc
    if not math.isfinite(number):
        raise PortfolioStoreError(f"{label}不能是NaN或Inf")
    return number


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(value: str) -> Any:
    return json.loads(value)


def _xor_stream(value: bytes, key: bytes, nonce: bytes) -> bytes:
    """Authenticated-stream encryption helper for locally stored webhooks.

    The database holds only ciphertext.  The key remains in the container
    environment and is never returned by the web UI or written to event logs.
    """
    output = bytearray()
    counter = 0
    while len(output) < len(value):
        block = hmac.new(key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
        output.extend(block)
        counter += 1
    return bytes(left ^ right for left, right in zip(value, output[:len(value)]))


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
