from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any


@dataclass(frozen=True)
class Bar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


@dataclass(frozen=True)
class Quote:
    symbol: str
    name: str
    timestamp: datetime
    price: float
    previous_close: float
    open: float
    high: float
    low: float
    volume: float
    amount: float

    @property
    def change_ratio(self) -> float:
        if not self.previous_close:
            return 0.0
        return self.price / self.previous_close - 1.0


@dataclass(frozen=True)
class SatellitePosition:
    active: bool = False
    shares: int = 0
    entry_price: float | None = None
    entry_date: date | None = None
    entry_support: float | None = None
    target_price: float | None = None
    stop_price: float | None = None


@dataclass(frozen=True)
class Position:
    symbol: str
    name: str
    main_shares: int
    economic_basis: float
    sector: str
    satellite_limit: int
    main_adjustment_shares: int
    peers: tuple[str, ...]
    satellite: SatellitePosition
    sizing: dict[str, float] = field(default_factory=dict)
    migration: dict[str, Any] = field(default_factory=dict)
    role: str = "holding"
    watchlist_entry_date: date | None = None
    # 现金分红等公司事件：股权登记日禁常规减仓；除息日后未调账时用于有效成本校正。
    corporate_events: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def total_shares(self) -> int:
        return self.main_shares + (self.satellite.shares if self.satellite.active else 0)


@dataclass(frozen=True)
class EvidenceItem:
    key: str
    label: str
    source: str
    source_url: str
    observed_at: datetime
    direction: int
    strength: int
    summary: str
    freshness: str = "fresh"
    fact_type: str = "sourced_fact"


@dataclass(frozen=True)
class EquityEvidence:
    symbol: str
    industry_status: str
    industry_direction: int | None
    announcement_status: str
    announcement_risk: str
    summary: str
    company_status: str = "missing"
    company_direction: int | None = None
    items: tuple[EvidenceItem, ...] = ()
    margin_status: str = "missing"
    margin_signal: str = "missing"
    margin_balance_change_5d: float | None = None
    corporate_action_status: str = "missing"
    corporate_action_direction: int | None = None
    corporate_action_strength: int = 0
    corporate_action_stage: str | None = None
    corporate_action_body_status: str = "missing"
    corporate_action_summary: str = "正向公司行动证据不可用"
    capital_flow_status: str = "missing"
    capital_flow_signal: str = "missing"
    capital_flow_net_5d: float | None = None
    capital_flow_summary: str = "资金面辅助数据不可用"
    shareholder_status: str = "missing"
    shareholder_signal: str = "missing"
    shareholder_change_ratio: float | None = None
    shareholder_summary: str = "股东户数辅助数据不可用"

    @property
    def corporate_action_confirmation(self) -> bool:
        """Whether a verified action may count as one external confirmation."""
        return (
            self.corporate_action_status == "fresh"
            and self.corporate_action_direction is not None
            and self.corporate_action_direction > 0
            and self.corporate_action_strength >= 2
            and self.corporate_action_body_status == "verified"
            and self.announcement_risk == "none"
        )

    @property
    def industry_unavailable(self) -> bool:
        """True when industry feeds failed or are incomplete — not the same as adverse."""
        return self.industry_status in {"missing", "partial", "stale"}

    @property
    def evidence_adverse(self) -> bool:
        """Directional weakness only; unreachable feeds are not treated as adverse."""
        return (
            (self.industry_status == "fresh" and self.industry_direction is not None and self.industry_direction < 0)
            or (self.company_direction is not None and self.company_direction < 0)
            or self.announcement_risk in {"caution", "critical"}
        )

    @property
    def add_ready(self) -> bool:
        return (
            self.industry_status == "fresh"
            and self.industry_direction is not None
            and self.industry_direction > 0
            and (self.company_direction is None or self.company_direction >= 0)
            and self.announcement_status in {"fresh", "not_applicable"}
            and self.announcement_risk == "none"
        )

    @property
    def satellite_entry_ready(self) -> bool:
        return (
            self.industry_status == "fresh"
            and self.industry_direction is not None
            and self.industry_direction >= 0
            and (self.company_direction is None or self.company_direction >= 0)
            and self.announcement_status in {"fresh", "not_applicable"}
            and self.announcement_risk == "none"
        )

    @property
    def downside_confirmation(self) -> bool:
        return (
            self.industry_direction is not None
            and self.industry_direction < 0
        ) or (
            self.company_direction is not None and self.company_direction < 0
        ) or self.announcement_risk in {"caution", "critical"}


@dataclass
class Technicals:
    ma5: float
    ma10: float
    ma20: float
    support: float
    resistance: float
    vwap: float | None
    volume_ratio: float | None
    last_15m_close: float | None
    previous_15m_close: float | None
    last_15m_open: float | None = None
    complete_15m: bool = False
    vwap_quality: str = "unavailable"
    volume_baseline_samples: int = 0
    warnings: list[str] = field(default_factory=list)
    last_15m_high: float | None = None
    last_15m_low: float | None = None
    previous_15m_open: float | None = None
    previous_15m_high: float | None = None
    previous_15m_low: float | None = None
    atr14: float | None = None
    rsi14: float | None = None
    previous_rsi14: float | None = None
    rsi_min_5: float | None = None
    rsi_max_5: float | None = None
    ma20_slope_5d: float | None = None
    range_position_60: float | None = None
    recent_high_60: float | None = None
    recent_low_60: float | None = None
    next_resistance: float | None = None
    last_5m_timestamp: datetime | None = None
    last_15m_timestamp: datetime | None = None
    daily_as_of: date | None = None
    adv20_shares: float | None = None
    adv_samples: int = 0


@dataclass
class Signal:
    symbol: str
    name: str
    code: str
    confidence: str
    price: float
    key_level: float
    action: str
    shares: int
    reason: str
    invalidation: str
    event_id: str
    category: str
    details: dict[str, Any] = field(default_factory=dict)
