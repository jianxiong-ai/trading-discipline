from __future__ import annotations

import copy
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterator


_SYMBOL_RE = re.compile(r"\d{6}\.(?:SH|SZ)")
_DYNAMIC_KEYS = {
    "role",
    "main_shares",
    "economic_basis",
    "risk_principal",
    "satellite",
    "watchlist_entry_date",
    "settled_dividend_event_ids",
}
_SUPPORTED_SECTORS = (
    "copper",
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

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @classmethod
    def from_config(cls, config_path: str | Path, raw: dict[str, Any]) -> PortfolioStore | None:
        value = raw.get("portfolio", {}).get("database_path")
        if not value:
            return None
        path = Path(str(value))
        if not path.is_absolute():
            path = Path(config_path).resolve().parent / path
        return cls(path)

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
                self._set_meta(conn, "available_cash", float(portfolio.get("available_cash", 0)), now)
            count = int(conn.execute("SELECT COUNT(*) FROM portfolio_positions").fetchone()[0])
            if count:
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
        static = {str(item.get("symbol", "")).upper(): item for item in portfolio.get("positions", [])}
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
        # User-added watchlist items are not present in YAML and are fully owned by SQLite.
        merged.extend(dynamic[symbol] for symbol in sorted(dynamic))
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
    payload["analysis_profile"] = profile
    payload["peers"] = [str(peer).upper() for peer in payload.get("peers", [])]
    payload["satellite_limit"] = int(payload.get("satellite_limit", 100))
    payload["main_adjustment_shares"] = int(payload.get("main_adjustment_shares", 100))
    payload["satellite"] = dict(payload.get("satellite") or _inactive_satellite())
    payload["settled_dividend_event_ids"] = [
        str(value) for value in payload.get("settled_dividend_event_ids", [])
    ]
    payload["corporate_events"] = [
        {**dict(event), "event_id": str(event.get("event_id") or _dividend_event_id(event))}
        for event in payload.get("corporate_events", []) or []
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


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
