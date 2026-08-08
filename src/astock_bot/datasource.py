from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .models import Bar, Quote


class DataSourceError(RuntimeError):
    pass


class EastmoneyPublicSource:
    QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
    KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(self, timezone: str, timeout: int = 8, retries: int = 2):
        self.tz = ZoneInfo(timezone)
        self.timeout = timeout
        self.retries = retries

    def quote(self, symbol: str) -> Quote:
        data = self._get_json(self.QUOTE_URL, {
            "secid": _secid(symbol),
            "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60,f124",
        }).get("data")
        if not data:
            raise DataSourceError(f"{symbol} 无行情快照")
        timestamp = datetime.fromtimestamp(int(data.get("f124") or 0), tz=self.tz)
        return Quote(
            symbol=symbol,
            name=str(data.get("f58") or symbol),
            timestamp=timestamp,
            price=_scaled(data.get("f43")),
            previous_close=_scaled(data.get("f60")),
            open=_scaled(data.get("f46")),
            high=_scaled(data.get("f44")),
            low=_scaled(data.get("f45")),
            volume=float(data.get("f47") or 0),
            amount=float(data.get("f48") or 0),
        )

    def daily_bars(self, symbol: str, limit: int = 45) -> list[Bar]:
        return self._bars(symbol, 101, limit)

    def five_minute_bars(self, symbol: str, limit: int = 120) -> list[Bar]:
        return self._bars(symbol, 5, limit)

    def _bars(self, symbol: str, interval: int, limit: int) -> list[Bar]:
        payload = self._get_json(self.KLINE_URL, {
            "secid": _secid(symbol), "klt": interval, "fqt": 1, "lmt": limit,
            "fields1": "f1,f2,f3,f4,f5,f6", "fields2": "f51,f52,f53,f54,f55,f56,f57",
        })
        rows = (payload.get("data") or {}).get("klines") or []
        bars: list[Bar] = []
        for row in rows:
            parts = row.split(",")
            if len(parts) < 7:
                continue
            stamp = datetime.strptime(parts[0], "%Y-%m-%d" if interval == 101 else "%Y-%m-%d %H:%M")
            bars.append(Bar(
                timestamp=stamp.replace(tzinfo=self.tz),
                open=float(parts[1]), close=float(parts[2]), high=float(parts[3]), low=float(parts[4]),
                volume=float(parts[5]), amount=float(parts[6]),
            ))
        if not bars:
            raise DataSourceError(f"{symbol} 无K线数据")
        return bars

    def _get_json(self, base: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"{base}?{urlencode(params)}"
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0 astock-discipline-bot/0.1"})
                with urlopen(req, timeout=self.timeout) as response:
                    return json.loads(response.read().decode("utf-8"))
            except Exception as exc:  # network failures are downgraded by the service
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise DataSourceError(f"公开行情请求失败: {last_error}")


def _scaled(value: Any) -> float:
    return float(value or 0) / 100.0


def _secid(symbol: str) -> str:
    code, exchange = symbol.upper().split(".")
    if exchange == "SH":
        return f"1.{code}"
    if exchange == "SZ":
        return f"0.{code}"
    raise ValueError(f"不支持的证券代码: {symbol}")


class TencentPublicSource:
    QUOTE_URL = "https://qt.gtimg.cn/q="
    DAY_URL = "https://ifzq.gtimg.cn/appstock/app/fqkline/get"
    MINUTE_URL = "https://ifzq.gtimg.cn/appstock/app/kline/mkline"

    def __init__(self, timezone: str, timeout: int = 8, retries: int = 2):
        self.tz = ZoneInfo(timezone)
        self.timeout = timeout
        self.retries = retries

    def quote(self, symbol: str) -> Quote:
        ticker = _tencent_ticker(symbol)
        raw = self._get_bytes(f"{self.QUOTE_URL}{ticker}").decode("gb18030", errors="replace")
        if '="' not in raw:
            raise DataSourceError(f"{symbol} 无行情快照")
        fields = raw.split('="', 1)[1].rsplit('"', 1)[0].split("~")
        if len(fields) < 36 or not fields[3]:
            raise DataSourceError(f"{symbol} 行情字段不完整")
        timestamp = datetime.strptime(fields[30], "%Y%m%d%H%M%S").replace(tzinfo=self.tz)
        amount = 0.0
        if len(fields) > 35 and "/" in fields[35]:
            parts = fields[35].split("/")
            if len(parts) >= 3:
                amount = float(parts[2] or 0)
        return Quote(
            symbol=symbol, name=fields[1] or symbol, timestamp=timestamp,
            price=float(fields[3]), previous_close=float(fields[4] or 0),
            open=float(fields[5] or 0), high=float(fields[33] or 0), low=float(fields[34] or 0),
            volume=float(fields[6] or 0), amount=amount,
        )

    def daily_bars(self, symbol: str, limit: int = 45) -> list[Bar]:
        ticker = _tencent_ticker(symbol)
        payload = self._get_json(self.DAY_URL, {"param": f"{ticker},day,,,{limit},qfq"})
        block = (payload.get("data") or {}).get(ticker) or {}
        rows = block.get("qfqday") or block.get("day") or []
        bars = []
        for row in rows:
            if len(row) < 6:
                continue
            stamp = datetime.strptime(row[0], "%Y-%m-%d").replace(tzinfo=self.tz)
            close, volume = float(row[2]), float(row[5])
            bars.append(Bar(stamp, float(row[1]), float(row[3]), float(row[4]), close, volume, close * volume * 100))
        if not bars:
            raise DataSourceError(f"{symbol} 无日K数据")
        return bars

    def five_minute_bars(self, symbol: str, limit: int = 120) -> list[Bar]:
        ticker = _tencent_ticker(symbol)
        payload = self._get_json(self.MINUTE_URL, {"param": f"{ticker},m5,,{limit}"})
        rows = ((payload.get("data") or {}).get(ticker) or {}).get("m5") or []
        bars = []
        for row in rows:
            if len(row) < 6:
                continue
            stamp = datetime.strptime(row[0], "%Y%m%d%H%M").replace(tzinfo=self.tz)
            close, volume = float(row[2]), float(row[5])
            bars.append(Bar(stamp, float(row[1]), float(row[3]), float(row[4]), close, volume, close * volume * 100))
        if not bars:
            raise DataSourceError(f"{symbol} 无5分钟K数据")
        return bars

    def _get_json(self, base: str, params: dict[str, Any]) -> dict[str, Any]:
        return json.loads(self._get_bytes(f"{base}?{urlencode(params)}").decode("utf-8"))

    def _get_bytes(self, url: str) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                req = Request(url, headers={"User-Agent": "Mozilla/5.0 astock-discipline-bot/0.1", "Referer": "https://gu.qq.com/"})
                with urlopen(req, timeout=self.timeout) as response:
                    return response.read()
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise DataSourceError(f"公开行情请求失败: {last_error}")


def _tencent_ticker(symbol: str) -> str:
    code, exchange = symbol.upper().split(".")
    if exchange == "SH":
        return f"sh{code}"
    if exchange == "SZ":
        return f"sz{code}"
    raise ValueError(f"不支持的证券代码: {symbol}")
