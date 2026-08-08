from __future__ import annotations

import copy
import json
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
    "satellite",
    "watchlist_entry_date",
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
                    created_at TEXT NOT NULL
                )
                """
            )
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
                portfolio["available_cash"] = float(cash)
            rows = conn.execute("SELECT symbol, payload FROM portfolio_positions ORDER BY symbol").fetchall()
        dynamic = {str(row["symbol"]): _load(row["payload"]) for row in rows}
        static = {str(item.get("symbol", "")).upper(): item for item in portfolio.get("positions", [])}
        merged: list[dict[str, Any]] = []
        for symbol, static_item in static.items():
            record = dynamic.pop(symbol, None)
            if record is None:
                merged.append(static_item)
                continue
            item = copy.deepcopy(static_item)
            for key in _DYNAMIC_KEYS:
                if key in record:
                    item[key] = record[key]
            merged.append(item)
        # User-added watchlist items are not present in YAML and are fully owned by SQLite.
        merged.extend(dynamic[symbol] for symbol in sorted(dynamic))
        portfolio["positions"] = merged
        return result

    def snapshot(self, raw: dict[str, Any]) -> dict[str, Any]:
        merged = self.overlay(raw)
        portfolio = merged.get("portfolio", {})
        return {
            "cash": float(portfolio.get("available_cash", 0)),
            "positions": [_normalized_payload(item) for item in portfolio.get("positions", [])],
            "supported_sectors": _SUPPORTED_SECTORS,
            "database_path": str(self.path),
        }

    def transactions(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, executed_at, symbol, bucket, side, shares, price, fee, cash_delta, note "
                "FROM portfolio_transactions ORDER BY id DESC LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_watchlist(self, *, symbol: str, name: str, sector: str, peers: str = "") -> None:
        symbol = _normalize_symbol(symbol)
        name = name.strip()
        if not name:
            raise PortfolioStoreError("请填写标的名称")
        sector = sector.strip() or "generic"
        if sector not in _SUPPORTED_SECTORS:
            raise PortfolioStoreError("行业必须从页面提供的选项中选择")
        peer_list = _normalize_peers(peers)
        payload = {
            "symbol": symbol,
            "name": name,
            "role": "watchlist",
            "main_shares": 0,
            "economic_basis": 0.0,
            "sector": sector,
            "satellite_limit": 100,
            "main_adjustment_shares": 100,
            "peers": peer_list,
            "satellite": _inactive_satellite(),
            "sizing": {},
            "migration": {},
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
        price = float(price)
        fee = float(fee or 0)
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
        with self._transaction() as conn:
            record = self._position(conn, symbol)
            cash = float(self._get_meta(conn, "available_cash") or 0)
            satellite = dict(record.get("satellite") or _inactive_satellite())
            if bucket == "main":
                if side == "buy":
                    total = gross + fee
                    if cash + 1e-8 < total:
                        raise PortfolioStoreError("可用资金不足，无法确认该笔买入")
                    was_watchlist = record.get("role") == "watchlist"
                    record["main_shares"] = int(record.get("main_shares", 0)) + shares
                    record["economic_basis"] = round(float(record.get("economic_basis", 0)) + total, 2)
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
                    record["economic_basis"] = round(max(float(record.get("economic_basis", 0)) - net, 0), 2)
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
                    stop = float(stop_price) if stop_price not in (None, "") else None
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
                    record["economic_basis"] = round(max(float(record.get("economic_basis", 0)) - net, 0), 2)
                    cash_delta = net

            if int(record.get("main_shares", 0)) == 0 and not bool(record.get("satellite", {}).get("active")):
                # Closed positions remain in the immutable trade ledger; the active tracker
                # returns to watchlist mode and does not retain a phantom risk basis.
                record["role"] = "watchlist"
                record["economic_basis"] = 0.0
                record.pop("watchlist_entry_date", None)
            self._save_position(conn, record, now)
            self._set_meta(conn, "available_cash", round(cash + cash_delta, 2), now)
            conn.execute(
                "INSERT INTO portfolio_transactions(executed_at, symbol, bucket, side, shares, price, fee, cash_delta, note, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (executed, symbol, bucket, side, shares, price, fee, cash_delta, note.strip(), now),
            )

    def record_dividend(
        self, *, symbol: str, amount: float, executed_at: str, note: str = ""
    ) -> None:
        symbol = _normalize_symbol(symbol)
        amount = float(amount)
        if amount <= 0:
            raise PortfolioStoreError("分红到账金额必须大于0")
        executed = _normalize_date(executed_at)
        now = _now()
        with self._transaction() as conn:
            record = self._position(conn, symbol)
            if record.get("role") != "holding":
                raise PortfolioStoreError("只有正式持仓可以登记分红")
            cash = float(self._get_meta(conn, "available_cash") or 0)
            record["economic_basis"] = round(max(float(record.get("economic_basis", 0)) - amount, 0), 2)
            self._save_position(conn, record, now)
            self._set_meta(conn, "available_cash", round(cash + amount, 2), now)
            conn.execute(
                "INSERT INTO portfolio_transactions(executed_at, symbol, bucket, side, shares, price, fee, cash_delta, note, created_at) "
                "VALUES (?, ?, 'cash', 'dividend', 0, 0, 0, ?, ?, ?)",
                (executed, symbol, amount, note.strip(), now),
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
    payload["economic_basis"] = float(payload.get("economic_basis", 0))
    payload["sector"] = str(payload.get("sector", "generic"))
    payload["peers"] = [str(peer).upper() for peer in payload.get("peers", [])]
    payload["satellite_limit"] = int(payload.get("satellite_limit", 100))
    payload["main_adjustment_shares"] = int(payload.get("main_adjustment_shares", 100))
    payload["satellite"] = dict(payload.get("satellite") or _inactive_satellite())
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


def _normalize_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError) as exc:
        raise PortfolioStoreError("成交日期应为YYYY-MM-DD") from exc


def _positive(value: float | None, label: str) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise PortfolioStoreError(f"请填写{label}") from exc
    if number <= 0:
        raise PortfolioStoreError(f"{label}必须大于0")
    return number


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _load(value: str) -> Any:
    return json.loads(value)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
