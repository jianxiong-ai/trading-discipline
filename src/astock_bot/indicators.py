from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from statistics import fmean

from .models import Bar, Technicals


@dataclass(frozen=True)
class DailyLevels:
    ma5: float
    ma10: float
    ma20: float
    support: float
    resistance: float
    next_resistance: float | None = None
    atr14: float | None = None
    rsi14: float | None = None
    previous_rsi14: float | None = None
    rsi_min_5: float | None = None
    rsi_max_5: float | None = None
    ma20_slope_5d: float | None = None
    recent_high_60: float | None = None
    recent_low_60: float | None = None
    daily_as_of: date | None = None
    adv20_shares: float | None = None
    adv_samples: int = 0


@dataclass(frozen=True)
class FifteenMinuteBlock:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float


def moving_average(bars: list[Bar], periods: int) -> float:
    if len(bars) < periods:
        raise ValueError(f"至少需要{periods}根K线")
    return fmean(bar.close for bar in bars[-periods:])


def compute_daily_levels(daily: list[Bar], current_date: date) -> DailyLevels:
    # 只使用评估日之前的完整日线；历史回放时也不得读取未来K线。
    completed = [bar for bar in daily if bar.timestamp.date() < current_date]
    if len(completed) < 20:
        raise ValueError("日K不足20根")
    ma5 = moving_average(completed, 5)
    ma10 = moving_average(completed, 10)
    ma20 = moving_average(completed, 20)
    previous = completed[-1]
    windows = tuple(n for n in (5, 10, 20, 40, 60) if len(completed) >= n)
    lows_by_window = [min(x.low for x in completed[-n:]) for n in windows]
    highs_by_window = [max(x.high for x in completed[-n:]) for n in windows]
    # 以最近完整日线收盘为锚，关键位不会随盘中现价漂移。
    anchor = previous.close
    lower = [x for x in (ma5, ma10, ma20, previous.low, *lows_by_window[:3]) if x <= anchor]
    upper = [x for x in (ma5, ma10, ma20, previous.high, *highs_by_window[:3]) if x >= anchor]
    support = max(lower) if lower else min(lows_by_window)
    resistance = min(upper) if upper else max(highs_by_window)
    if resistance <= support:
        resistance = max(previous.high, max(highs_by_window))
    higher_levels = sorted({
        round(level, 6)
        for level in (previous.high, *highs_by_window, ma5, ma10, ma20)
        if level > resistance * 1.001
    })
    closes = [bar.close for bar in completed]
    rsi_values = _rolling_rsi(closes, 14)
    rsi14 = rsi_values[-1] if rsi_values else None
    previous_rsi14 = rsi_values[-2] if len(rsi_values) >= 2 else None
    recent_rsi = rsi_values[-5:]
    old_ma20 = moving_average(completed[:-5], 20) if len(completed) >= 25 else None
    recent = completed[-min(60, len(completed)):]
    adv_rows = [
        bar.amount / bar.close
        for bar in completed[-20:]
        if bar.amount > 0 and bar.close > 0
    ]
    return DailyLevels(
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        support=support,
        resistance=resistance,
        next_resistance=higher_levels[0] if higher_levels else None,
        atr14=_atr(completed, 14),
        rsi14=rsi14,
        previous_rsi14=previous_rsi14,
        rsi_min_5=min(recent_rsi) if recent_rsi else None,
        rsi_max_5=max(recent_rsi) if recent_rsi else None,
        ma20_slope_5d=(ma20 / old_ma20 - 1) if old_ma20 else None,
        recent_high_60=max(bar.high for bar in recent),
        recent_low_60=min(bar.low for bar in recent),
        daily_as_of=completed[-1].timestamp.date(),
        adv20_shares=fmean(adv_rows) if adv_rows else None,
        adv_samples=len(adv_rows),
    )


def compute_technicals(
    daily: list[Bar],
    intraday: list[Bar],
    price: float,
    as_of: datetime | None = None,
) -> Technicals:
    if not intraday:
        raise ValueError("缺少分钟K")
    as_of = as_of or intraday[-1].timestamp
    current_date = as_of.date()
    levels = compute_daily_levels(daily, current_date)
    usable = [bar for bar in intraday if bar.timestamp <= _floor_five_minutes(as_of)]
    today_intraday = [bar for bar in usable if bar.timestamp.date() == current_date]
    if len(today_intraday) < 3:
        raise ValueError("当日分钟K不足3根")

    amounts = sum(x.amount for x in today_intraday)
    volumes = sum(x.volume for x in today_intraday)
    vwap = amounts / (volumes * 100.0) if volumes > 0 and amounts > 0 else None

    blocks = _fifteen_minute_blocks(usable)
    today_blocks = [block for block in blocks if block.timestamp.date() == current_date]
    last_block = today_blocks[-1] if today_blocks else None
    previous_block = blocks[blocks.index(last_block) - 1] if last_block and blocks.index(last_block) > 0 else None
    volume_ratio = None
    sample_count = 0
    if last_block:
        same_time = [
            block.volume
            for block in blocks
            if block.timestamp.date() < current_date and block.timestamp.time() == last_block.timestamp.time()
        ][-10:]
        sample_count = len(same_time)
        if sample_count >= 3:
            base = fmean(same_time)
            volume_ratio = last_block.volume / base if base else None

    warnings: list[str] = []
    if last_block is None:
        warnings.append("尚无完整15分钟K")
    if sample_count < 3:
        warnings.append("历史同时间量能样本不足3个交易日")
    if vwap is not None:
        warnings.append("分时均价为5分钟收盘价加权近似值")

    range_position = None
    if (
        levels.recent_high_60 is not None
        and levels.recent_low_60 is not None
        and levels.recent_high_60 > levels.recent_low_60
    ):
        range_position = (price - levels.recent_low_60) / (levels.recent_high_60 - levels.recent_low_60)
    next_resistance = levels.next_resistance
    if next_resistance is not None and next_resistance <= price * 1.001:
        completed = [bar for bar in daily if bar.timestamp.date() < current_date]
        candidates = sorted({
            max(bar.high for bar in completed[-n:])
            for n in (5, 10, 20, 40, 60)
            if len(completed) >= n
        })
        next_resistance = next((level for level in candidates if level > price * 1.001), None)

    return Technicals(
        ma5=levels.ma5,
        ma10=levels.ma10,
        ma20=levels.ma20,
        support=levels.support,
        resistance=levels.resistance,
        vwap=vwap,
        volume_ratio=volume_ratio,
        last_15m_close=last_block.close if last_block else None,
        previous_15m_close=previous_block.close if previous_block else None,
        last_15m_open=last_block.open if last_block else None,
        complete_15m=last_block is not None,
        vwap_quality="approximate_bar_close" if vwap is not None else "unavailable",
        volume_baseline_samples=sample_count,
        warnings=warnings,
        last_15m_high=last_block.high if last_block else None,
        last_15m_low=last_block.low if last_block else None,
        previous_15m_open=previous_block.open if previous_block else None,
        previous_15m_high=previous_block.high if previous_block else None,
        previous_15m_low=previous_block.low if previous_block else None,
        atr14=levels.atr14,
        rsi14=levels.rsi14,
        previous_rsi14=levels.previous_rsi14,
        rsi_min_5=levels.rsi_min_5,
        rsi_max_5=levels.rsi_max_5,
        ma20_slope_5d=levels.ma20_slope_5d,
        range_position_60=range_position,
        recent_high_60=levels.recent_high_60,
        recent_low_60=levels.recent_low_60,
        next_resistance=next_resistance,
        last_5m_timestamp=today_intraday[-1].timestamp,
        last_15m_timestamp=last_block.timestamp if last_block else None,
        daily_as_of=levels.daily_as_of,
        adv20_shares=levels.adv20_shares,
        adv_samples=levels.adv_samples,
    )


def _floor_five_minutes(value: datetime) -> datetime:
    return value.replace(minute=value.minute - value.minute % 5, second=0, microsecond=0)


def _fifteen_minute_blocks(bars: list[Bar]) -> list[FifteenMinuteBlock]:
    grouped: dict[tuple[date, str, int], list[tuple[int, Bar]]] = {}
    for bar in bars:
        slot = _market_slot(bar.timestamp)
        if slot is None:
            continue
        session, five_index = slot
        key = (bar.timestamp.date(), session, five_index // 3)
        grouped.setdefault(key, []).append((five_index, bar))
    result: list[FifteenMinuteBlock] = []
    for items in grouped.values():
        ordered = sorted(items, key=lambda item: item[0])
        indexes = [item[0] for item in ordered]
        if len(ordered) != 3 or indexes != list(range(indexes[0], indexes[0] + 3)) or indexes[0] % 3:
            continue
        first, last = ordered[0][1], ordered[-1][1]
        result.append(FifteenMinuteBlock(
            last.timestamp,
            first.open,
            max(item[1].high for item in ordered),
            min(item[1].low for item in ordered),
            last.close,
            sum(item[1].volume for item in ordered),
        ))
    return sorted(result, key=lambda block: block.timestamp)


def _market_slot(value: datetime) -> tuple[str, int] | None:
    minute = value.hour * 60 + value.minute
    morning_start, morning_end = 9 * 60 + 35, 11 * 60 + 30
    afternoon_start, afternoon_end = 13 * 60 + 5, 15 * 60
    if morning_start <= minute <= morning_end and (minute - morning_start) % 5 == 0:
        return "morning", (minute - morning_start) // 5
    if afternoon_start <= minute <= afternoon_end and (minute - afternoon_start) % 5 == 0:
        return "afternoon", (minute - afternoon_start) // 5
    return None


def _atr(bars: list[Bar], periods: int) -> float | None:
    if len(bars) < periods + 1:
        return None
    true_ranges = []
    for previous, current in zip(bars[-periods - 1:-1], bars[-periods:]):
        true_ranges.append(max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        ))
    return fmean(true_ranges) if true_ranges else None


def _rolling_rsi(closes: list[float], periods: int) -> list[float]:
    if len(closes) < periods + 1:
        return []
    result = []
    for end in range(periods, len(closes)):
        window = closes[end - periods:end + 1]
        changes = [current - previous for previous, current in zip(window, window[1:])]
        average_gain = fmean(max(change, 0.0) for change in changes)
        average_loss = fmean(max(-change, 0.0) for change in changes)
        if average_loss == 0:
            result.append(100.0 if average_gain > 0 else 50.0)
        else:
            result.append(100.0 - 100.0 / (1.0 + average_gain / average_loss))
    return result
