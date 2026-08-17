from __future__ import annotations

import ast
import calendar
import gzip
import json
import hashlib
from io import BytesIO
import math
import re
import time
from datetime import date, datetime, timedelta
from html import unescape
from pathlib import Path
from statistics import NormalDist
from typing import Any
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    from pypdf import PdfReader
except ImportError:  # Unit tests may run before optional Docker dependencies are installed.
    PdfReader = None

from .models import (
    CommodityOptionEvidence,
    EquityEvidence,
    EvidenceItem,
    OptionContractSnapshot,
    Position,
)


SSE_ANNOUNCEMENT_URL = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
SZSE_ANNOUNCEMENT_URL = "https://www.szse.cn/api/disc/announcement/annList"
SSE_MARGIN_URL = "https://query.sse.com.cn/commonSoaQuery.do"
SZSE_MARGIN_URL = "https://www.szse.cn/api/report/ShowReport/data"
# 东方财富原 daykline 路径已逐步返回 rc=100/空 K 线。优先使用仍有数据的
# kline 路径，并在不同节点间回退；本地滚动缓存负责补齐日终历史序列。
EASTMONEY_FFLOW_URLS = (
    "https://push2delay.eastmoney.com/api/qt/stock/fflow/kline/get",
    "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
    "https://push2his.eastmoney.com/api/qt/stock/fflow/kline/get",
)
EASTMONEY_FFLOW_UT = "fa5fd1943c7b386f172d6893dbfba10b"
EASTMONEY_HOLDER_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
SHFE_DAILY_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/kx{date}.dat"
SHFE_OPTION_DAILY_URL = "https://www.shfe.com.cn/data/tradedata/option/dailydata/kx{date}.dat"
SHFE_OPTION_CONTRACT_URL = (
    "https://www.shfe.com.cn/data/busiparamdata/option/ContractBaseInfo{date}.dat"
)
SHFE_WARRANT_URLS = (
    "https://www.shfe.com.cn/data/tradedata/future/dailydata/{date}dailystock.dat",
    "https://www.shfe.com.cn/data/dailydata/{year}/{date}dailystock.dat",
)
CHINABOND_CURVE_URL = (
    "https://yield.chinabond.com.cn/cbweb-czb-web/czb/moreInfo?locale=cn_ZH&nameType=1"
)
MIIT_NEV_INDEX_URL = "https://www.miit.gov.cn/jgsj/zbys/qcgy/index.html"
MIIT_ELECTRONICS_INDEX_URL = "https://www.miit.gov.cn/jgsj/yxj/xxfb/index.html"
MIIT_SATELLITE_INDEX_URL = "https://www.miit.gov.cn/jgsj/wgj/gzdt/index.html"
MIIT_TELECOM_INDEX_URL = "https://www.miit.gov.cn/gxsj/tjfx/txy/index.html"

DEFAULT_CRITICAL_KEYWORDS = (
    "立案调查",
    "立案告知书",
    "重大违法",
    "财务造假",
    "债务逾期",
    "无法清偿",
    "破产",
    "暂停上市",
    "终止上市",
    "偿付能力不足",
    "业绩预亏",
    "重大资产减值",
    "评级下调",
    "被接管",
)
DEFAULT_CAUTION_KEYWORDS = (
    "诉讼",
    "仲裁",
    "行政处罚",
    "监管警示",
    "纪律处分",
    "问询函",
    "风险提示",
    "整改",
    "减持",
    "对外担保",
)

CORPORATE_ACTION_STAGE_RANK = {
    "proposal": 1,
    "plan": 2,
    "approved": 2,
    "first_execution": 3,
    "progress": 4,
    "completed": 5,
    "terminated": 6,
}

CORPORATE_ACTION_STAGE_LABELS = {
    "proposal": "提议",
    "plan": "预案/方案",
    "approved": "审议通过",
    "first_execution": "首次实施",
    "progress": "实施进展",
    "completed": "实施完成",
    "terminated": "终止/未实施",
}

DEFAULT_COMMODITY_OPTION_ROUTES: dict[str, dict[str, Any]] = {
    "copper": {
        "commodity": "copper",
        "industry_sector": "copper",
        "exchange": "SHFE",
        "futures_product": "cu",
        "option_product": "cu_o",
        "label": "沪铜",
    },
    "gold": {
        "commodity": "gold",
        "industry_sector": "gold",
        "exchange": "SHFE",
        "futures_product": "au",
        "option_product": "au_o",
        "label": "沪金",
    },
    "silver": {
        "commodity": "silver",
        "industry_sector": "silver",
        "exchange": "SHFE",
        "futures_product": "ag",
        "option_product": "ag_o",
        "label": "沪银",
    },
}


def commodity_option_routes(settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not bool(settings.get("enabled", False)):
        return {}
    configured = settings.get("routes") or {}
    routes: dict[str, dict[str, Any]] = {}
    for key, default in DEFAULT_COMMODITY_OPTION_ROUTES.items():
        merged = {**default, **(configured.get(key) or {})}
        if merged.get("exchange") == "SHFE" and merged.get("futures_product"):
            merged["commodity"] = str(merged.get("commodity") or key)
            routes[key] = merged
    for key, route in configured.items():
        if key in routes or not isinstance(route, dict):
            continue
        merged = {**route}
        if merged.get("exchange") == "SHFE" and merged.get("futures_product"):
            merged["commodity"] = str(merged.get("commodity") or key)
            routes[key] = merged
    return routes


def position_commodity_keys(position) -> set[str]:
    keys = {
        str(exposure.get("commodity"))
        for exposure in getattr(position, "commodity_exposures", ())
        if isinstance(exposure, dict) and exposure.get("commodity")
    }
    if position.sector in DEFAULT_COMMODITY_OPTION_ROUTES:
        keys.add(position.sector)
    return keys


def resolve_commodity_option_routes(
    position,
    routes: dict[str, dict[str, Any]],
    *,
    enabled: bool,
) -> tuple[list[tuple[str, dict[str, Any]]], str | None]:
    """Return matching (route_key, route) pairs and an optional disabled reason."""
    wanted = position_commodity_keys(position)
    matched = [
        (key, route)
        for key, route in routes.items()
        if key in wanted
        or str(route.get("commodity") or "") in wanted
        or route.get("industry_sector") == position.sector
        or route.get("sector") == position.sector
    ]
    if not matched and not wanted:
        return [], None
    if not matched:
        return [], None
    if not enabled:
        return [], "disabled"
    return matched, None


def resolve_commodity_option_route(
    position,
    routes: dict[str, dict[str, Any]],
    *,
    enabled: bool,
) -> tuple[str | None, dict[str, Any] | None, str | None]:
    matched, reason = resolve_commodity_option_routes(position, routes, enabled=enabled)
    if not matched:
        return None, None, reason
    key, route = matched[0]
    return key, route, reason


def commodity_exposure_option_note(
    exposures: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    commodity: str | None = None,
) -> str:
    if not exposures:
        return ""
    selected = next(
        (
            exposure for exposure in exposures
            if commodity and str(exposure.get("commodity") or "") == commodity
        ),
        exposures[0],
    )
    types = [str(value) for value in selected.get("exposure_types") or []]
    sensitivity = str(selected.get("sensitivity") or "").strip()
    if selected.get("hedge_disclosed"):
        hedge_note = "公司资料提及套保，期权信号需结合实际对冲头寸解读"
        return "；".join(part for part in (sensitivity, hedge_note) if part)
    return sensitivity


def finalize_commodity_option_summary(
    context: "CommodityOptionEvidence",
    exposures: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    industry_direction: int | None,
    *,
    commodity: str | None = None,
    industry_linked: bool = False,
) -> str:
    parts = [context.summary]
    exposure_note = commodity_exposure_option_note(exposures, commodity)
    if exposure_note and exposure_note not in context.summary:
        parts.append(exposure_note)
    if (
        industry_linked
        and context.status == "fresh"
        and context.view == "downside_hedging"
        and industry_direction is not None
        and industry_direction > 0
    ):
        parts.append("期货证据与期权保护需求背离，不作新增辅助确认")
    elif context.status == "fresh" and not industry_linked:
        parts.append("该期权链未与当前行业门控对齐，只作观察、不加分也不单独交易")
    return "；".join(part for part in parts if part)


def merge_commodity_option_contexts(
    matched: list[tuple[str, dict[str, Any], "CommodityOptionEvidence"]],
    exposures: tuple[dict[str, Any], ...] | list[dict[str, Any]],
    industry_direction: int | None,
    position_sector: str,
) -> tuple["CommodityOptionEvidence", list["EvidenceItem"]]:
    if not matched:
        empty = CommodityOptionEvidence()
        return empty, []
    summaries: list[str] = []
    items: list[EvidenceItem] = []
    by_commodity: dict[str, Any] = {}
    statuses: list[str] = []
    industry_linked_view = "unavailable"
    industry_linked_status = "not_applicable"
    display_view = "unavailable"
    for key, route, context in matched:
        commodity = str(route.get("commodity") or key)
        industry_linked = route.get("industry_sector") == position_sector or (
            position_sector == commodity
        )
        summary = finalize_commodity_option_summary(
            context,
            exposures,
            industry_direction,
            commodity=commodity,
            industry_linked=industry_linked,
        )
        by_commodity[commodity] = {
            "status": context.status,
            "view": context.view,
            "summary": summary,
            "industry_linked": industry_linked,
            **dict(context.metrics or {}),
        }
        statuses.append(context.status)
        summaries.append(summary)
        if industry_linked:
            industry_linked_view = context.view
            industry_linked_status = context.status
            display_view = context.view
        elif display_view == "unavailable" and context.view != "unavailable":
            display_view = context.view
        if context.item is not None:
            items.append(EvidenceItem(
                key=context.item.key,
                label=context.item.label,
                source=context.item.source,
                source_url=context.item.source_url,
                observed_at=context.item.observed_at,
                direction=0,
                strength=0,
                summary=summary,
                freshness=context.item.freshness,
                fact_type=context.item.fact_type,
            ))
    status = "fresh" if "fresh" in statuses else (
        "partial" if "partial" in statuses else (
            "missing" if "missing" in statuses else statuses[0]
        )
    )
    merged = CommodityOptionEvidence(
        status=status,
        view=display_view,
        summary=" ｜ ".join(summaries),
        metrics={
            "by_commodity": by_commodity,
            "industry_linked_view": industry_linked_view,
            "industry_linked_status": industry_linked_status,
        },
        item=items[0] if items else None,
    )
    return merged, items


class EvidenceSourceError(RuntimeError):
    pass


class OfficialEvidenceCollector:
    """Collect conservative, source-labelled evidence from public primary sources.

    Industry observations are end-of-day inputs. Announcement titles only provide
    a conservative risk gate; selected operating disclosures may add direction only
    when a numeric year-on-year metric can be extracted from the exchange-hosted PDF.
    """

    def __init__(self, timezone: str, config: dict[str, Any]):
        self.tz = ZoneInfo(timezone)
        self.config = config
        self.timeout = int(config.get("timeout_seconds", 8))
        self.retries = int(config.get("retries", 2))
        cache_enabled = bool(config.get("cache_enabled", True))
        cache_dir = config.get("cache_dir", "data/evidence_cache")
        self.cache_dir = Path(cache_dir) if cache_enabled else None
        if self.cache_dir is not None:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

    def collect(
        self,
        positions: tuple[Position, ...],
        as_of: datetime,
    ) -> tuple[dict[str, EquityEvidence], list[str]]:
        warnings: list[str] = []
        industry: dict[str, list[EvidenceItem]] = {}
        industry_statuses: dict[str, str] = {}
        for sector in {position.sector for position in positions}:
            industry[sector] = []
            try:
                routes = self._industry_sources(sector, as_of)
            except Exception as exc:
                warnings.append(f"{sector} 产业证据路由: {exc}")
                industry_statuses[sector] = "missing"
                continue
            required_total = 0
            required_fresh = 0
            required_failed = 0
            for route in routes:
                if len(route) == 2:
                    label, source = route
                    required = True
                else:
                    label, source, required = route
                if required:
                    required_total += 1
                try:
                    item = self._load_industry_item(sector, label, as_of, source)
                    industry[sector].append(item)
                    if required and item.freshness == "fresh":
                        required_fresh += 1
                except Exception as exc:
                    if required:
                        required_failed += 1
                    warnings.append(f"{sector} {label}: {exc}")
            if required_total == 0:
                industry_statuses[sector] = "missing"
            elif required_fresh == required_total and required_failed == 0:
                industry_statuses[sector] = "fresh"
            elif required_fresh:
                industry_statuses[sector] = "partial"
            elif industry[sector]:
                industry_statuses[sector] = "stale"
            else:
                industry_statuses[sector] = "missing"

        option_contexts: dict[str, CommodityOptionEvidence] = {}
        option_settings = self.config.get("commodity_options", {})
        option_routes = commodity_option_routes(option_settings)
        option_enabled = bool(option_settings.get("enabled", False))
        needed_keys: set[str] = set()
        for position in positions:
            wanted = position_commodity_keys(position)
            for key, route in option_routes.items():
                if (
                    key in wanted
                    or str(route.get("commodity") or "") in wanted
                    or route.get("industry_sector") == position.sector
                    or route.get("sector") == position.sector
                ):
                    needed_keys.add(key)
        for route_key, route in option_routes.items():
            if route_key not in needed_keys:
                continue
            cache_day = as_of.astimezone(self.tz).date().isoformat()
            cached_option = self._read_commodity_option_cache(cache_day, route_key)
            if cached_option is not None:
                option_contexts[route_key] = cached_option
                continue
            try:
                if str(route.get("exchange")).upper() == "SHFE":
                    option_contexts[route_key] = self._load_shfe_option_chain(route, as_of)
                else:
                    raise EvidenceSourceError(f"未支持的期权交易所: {route.get('exchange')}")
                self._write_commodity_option_cache(
                    cache_day, route_key, option_contexts[route_key]
                )
            except Exception as exc:
                label = str(route.get("label") or route_key)
                warnings.append(f"{route.get('commodity', route_key)} {label}期权辅助: {exc}")
                option_contexts[route_key] = CommodityOptionEvidence(
                    status="missing",
                    view="unavailable",
                    summary=f"{label}期权辅助数据不可用；不影响对应期货产业门控",
                )

        result: dict[str, EquityEvidence] = {}
        for position in positions:
            announcement_items: list[EvidenceItem] = []
            announcement_status = "not_applicable" if _is_etf(position) else "missing"
            if not _is_etf(position):
                try:
                    announcement_items = self._announcements(position.symbol, as_of)
                    announcement_status = "fresh"
                except Exception as exc:
                    warnings.append(f"{position.symbol} 公告证据: {exc}")

            margin_item: EvidenceItem | None = None
            margin_status = "missing"
            margin_signal = "missing"
            margin_change = None
            if bool(self.config.get("margin_financing", {}).get("enabled", False)):
                try:
                    margin_item, margin_signal, margin_change = self._margin_financing(
                        position.symbol, as_of
                    )
                    margin_status = margin_item.freshness
                except Exception as exc:
                    warnings.append(f"{position.symbol} 两融证据: {exc}")

            capital_item: EvidenceItem | None = None
            capital_status = "missing"
            capital_signal = "missing"
            capital_net_5d = None
            if bool(self.config.get("capital_flow", {}).get("enabled", False)):
                try:
                    capital_item, capital_signal, capital_net_5d = self._capital_flow(
                        position.symbol, as_of
                    )
                    capital_status = capital_item.freshness
                except Exception as exc:
                    warnings.append(f"{position.symbol} 资金面证据: {exc}")

            holder_item: EvidenceItem | None = None
            holder_status = "missing"
            holder_signal = "missing"
            holder_change = None
            if bool(self.config.get("shareholder_count", {}).get("enabled", False)):
                try:
                    holder_item, holder_signal, holder_change = self._shareholder_count(
                        position.symbol, as_of
                    )
                    holder_status = holder_item.freshness
                except Exception as exc:
                    warnings.append(f"{position.symbol} 股东户数证据: {exc}")

            industry_items = industry.get(position.sector, [])
            fresh_industry = [item for item in industry_items if item.freshness == "fresh"]
            industry_status = industry_statuses.get(position.sector, "missing")
            industry_direction = _aggregate_direction(fresh_industry)
            industry_strength = max(
                (
                    int(item.strength)
                    for item in fresh_industry
                    if item.direction > 0
                ),
                default=0,
            )
            matched_routes, disabled_reason = resolve_commodity_option_routes(
                position, option_routes, enabled=option_enabled
            )
            if matched_routes:
                option_context, option_items = merge_commodity_option_contexts(
                    [
                        (
                            key,
                            route,
                            option_contexts.get(
                                key,
                                CommodityOptionEvidence(
                                    status="missing",
                                    view="unavailable",
                                    summary=f"{route.get('label', key)}期权辅助数据不可用",
                                ),
                            ),
                        )
                        for key, route in matched_routes
                    ],
                    position.commodity_exposures,
                    industry_direction,
                    position.sector,
                )
            elif disabled_reason == "disabled":
                option_context = CommodityOptionEvidence(
                    status="disabled",
                    view="unavailable",
                    summary="商品期权辅助已关闭",
                )
                option_items = []
            else:
                option_context = CommodityOptionEvidence(
                    status="not_applicable",
                    view="unavailable",
                    summary="商品期权不适用",
                )
                option_items = []
            option_summary = option_context.summary
            company_items = [
                item for item in announcement_items
                if item.fact_type == "company_operating_metric"
            ]
            company_status = "fresh" if company_items else "missing"
            company_direction = _aggregate_direction(company_items)
            announcement_risk = _highest_announcement_risk(announcement_items)
            corporate_action_items = [
                item for item in announcement_items
                if item.fact_type in {
                    "positive_corporate_action", "corporate_action_candidate"
                }
            ]
            corporate_action_item = corporate_action_items[0] if corporate_action_items else None
            if _is_etf(position):
                corporate_action_status = "not_applicable"
                corporate_action_direction = None
                corporate_action_strength = 0
                corporate_action_stage = None
                corporate_action_body_status = "not_applicable"
            elif corporate_action_item is None:
                corporate_action_status = "none" if announcement_status == "fresh" else "missing"
                corporate_action_direction = None
                corporate_action_strength = 0
                corporate_action_stage = None
                corporate_action_body_status = "missing"
            else:
                corporate_action_status = corporate_action_item.freshness
                corporate_action_direction = corporate_action_item.direction
                corporate_action_strength = corporate_action_item.strength
                corporate_action_stage, corporate_action_body_status = _corporate_action_item_state(
                    corporate_action_item
                )
            summary_parts = [
                "；".join(item.summary for item in industry_items) if industry_items else "产业证据不可用",
                _corporate_action_summary(
                    corporate_action_item, announcement_status, _is_etf(position)
                ),
                _company_summary(company_items),
                _announcement_summary(announcement_items, announcement_status),
                margin_item.summary if margin_item else "两融辅助数据不可用",
                capital_item.summary if capital_item else "资金面辅助数据不可用",
                holder_item.summary if holder_item else "股东户数辅助数据不可用",
            ]
            selected_announcements = announcement_items[:5]
            first_risk = next((item for item in announcement_items if item.direction < 0), None)
            if first_risk and first_risk not in selected_announcements:
                selected_announcements.append(first_risk)
            first_operating = next(
                (item for item in announcement_items if item.fact_type == "company_operating_metric"),
                None,
            )
            if first_operating and first_operating not in selected_announcements:
                selected_announcements.append(first_operating)
            if corporate_action_item and corporate_action_item not in selected_announcements:
                selected_announcements.append(corporate_action_item)
            items = tuple(
                industry_items
                + selected_announcements
                + ([margin_item] if margin_item else [])
                + ([capital_item] if capital_item else [])
                + ([holder_item] if holder_item else [])
                + option_items
            )
            result[position.symbol] = EquityEvidence(
                symbol=position.symbol,
                industry_status=industry_status,
                industry_direction=industry_direction,
                announcement_status=announcement_status,
                announcement_risk=announcement_risk,
                summary="；".join(summary_parts),
                industry_strength=industry_strength,
                industry_is_macro_proxy=position.sector in {
                    "insurance", "insurance_financial_group"
                },
                company_status=company_status,
                company_direction=company_direction,
                items=items,
                margin_status=margin_status,
                margin_signal=margin_signal,
                margin_balance_change_5d=margin_change,
                corporate_action_status=corporate_action_status,
                corporate_action_direction=corporate_action_direction,
                corporate_action_strength=corporate_action_strength,
                corporate_action_stage=corporate_action_stage,
                corporate_action_body_status=corporate_action_body_status,
                corporate_action_summary=_corporate_action_summary(
                    corporate_action_item, announcement_status, _is_etf(position)
                ),
                capital_flow_status=capital_status,
                capital_flow_signal=capital_signal,
                capital_flow_net_5d=capital_net_5d,
                capital_flow_summary=(
                    capital_item.summary if capital_item else "资金面辅助数据不可用"
                ),
                shareholder_status=holder_status,
                shareholder_signal=holder_signal,
                shareholder_change_ratio=holder_change,
                shareholder_summary=(
                    holder_item.summary if holder_item else "股东户数辅助数据不可用"
                ),
                commodity_option_status=option_context.status,
                commodity_option_view=option_context.view,
                commodity_option_summary=option_summary,
                commodity_option_metrics=option_context.metrics,
            )
        return result, warnings

    def _load_industry_item(self, sector: str, label: str, as_of: datetime, source):
        day = as_of.astimezone(self.tz).date().isoformat()
        cached = self._read_industry_cache(day, sector, label)
        if cached is not None:
            return cached
        item = source()
        self._write_industry_cache(day, sector, label, item)
        return item

    def _cache_path(self, day: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"{day}.json"

    def _read_industry_cache(self, day: str, sector: str, label: str) -> EvidenceItem | None:
        path = self._cache_path(day)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = payload.get(sector, {}).get(label)
            if not isinstance(raw, dict):
                return None
            return EvidenceItem(
                key=str(raw["key"]),
                label=str(raw["label"]),
                source=str(raw["source"]),
                source_url=str(raw["source_url"]),
                observed_at=datetime.fromisoformat(str(raw["observed_at"])),
                direction=int(raw["direction"]),
                strength=int(raw["strength"]),
                summary=str(raw["summary"]),
                freshness=str(raw.get("freshness", "fresh")),
                fact_type=str(raw.get("fact_type", "sourced_fact")),
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_industry_cache(
        self, day: str, sector: str, label: str, item: EvidenceItem
    ) -> None:
        path = self._cache_path(day)
        if path is None:
            return
        try:
            payload: dict[str, Any] = {}
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
            sector_bucket = payload.setdefault(sector, {})
            sector_bucket[label] = {
                "key": item.key,
                "label": item.label,
                "source": item.source,
                "source_url": item.source_url,
                "observed_at": item.observed_at.isoformat(),
                "direction": item.direction,
                "strength": item.strength,
                "summary": item.summary,
                "freshness": item.freshness,
                "fact_type": item.fact_type,
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except (OSError, ValueError, TypeError):
            return

    def _read_commodity_option_cache(
        self, day: str, sector: str
    ) -> CommodityOptionEvidence | None:
        path = self._cache_path(day)
        if path is None or not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            raw = payload.get("commodity_options", {}).get(sector)
            if not isinstance(raw, dict):
                return None
            item_raw = raw.get("item")
            item = None
            if isinstance(item_raw, dict):
                item = EvidenceItem(
                    key=str(item_raw["key"]),
                    label=str(item_raw["label"]),
                    source=str(item_raw["source"]),
                    source_url=str(item_raw["source_url"]),
                    observed_at=datetime.fromisoformat(str(item_raw["observed_at"])),
                    direction=0,
                    strength=0,
                    summary=str(item_raw["summary"]),
                    freshness=str(item_raw.get("freshness", raw.get("status", "fresh"))),
                    fact_type="commodity_option_context",
                )
            return CommodityOptionEvidence(
                status=str(raw["status"]),
                view=str(raw["view"]),
                summary=str(raw["summary"]),
                metrics=dict(raw.get("metrics") or {}),
                item=item,
            )
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_commodity_option_cache(
        self, day: str, sector: str, evidence: CommodityOptionEvidence
    ) -> None:
        path = self._cache_path(day)
        if path is None:
            return
        try:
            payload: dict[str, Any] = {}
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
            bucket = payload.setdefault("commodity_options", {})
            item = evidence.item
            bucket[sector] = {
                "status": evidence.status,
                "view": evidence.view,
                "summary": evidence.summary,
                "metrics": evidence.metrics,
                "item": ({
                    "key": item.key,
                    "label": item.label,
                    "source": item.source,
                    "source_url": item.source_url,
                    "observed_at": item.observed_at.isoformat(),
                    "summary": item.summary,
                    "freshness": item.freshness,
                } if item else None),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except (OSError, ValueError, TypeError):
            return

    def _industry_sources(self, sector: str, as_of: datetime):
        # 每项为 (label, loader, required)。可选源失败时不拖累 industry_status=fresh。
        if sector in DEFAULT_COMMODITY_OPTION_ROUTES:
            route = DEFAULT_COMMODITY_OPTION_ROUTES[sector]
            product = str(route["futures_product"])
            label = str(route["label"])
            sources = [(
                f"{label}行情",
                lambda product=product, label=label, sector=sector: self._shfe_futures_trend(
                    as_of, product, sector, label
                ),
                True,
            )]
            if sector == "copper" and bool(self.config.get("copper", {}).get("warrant_enabled", True)):
                sources.append(("沪铜仓单", lambda: self._shfe_copper_warrant(as_of), False))
            return sources
        if sector in {"insurance", "insurance_financial_group"}:
            return [("长端利率", lambda: self._chinabond_long_rates(as_of, sector), True)]
        if sector == "new_energy_vehicle":
            return [("新能源车产销", lambda: self._miit_new_energy_vehicle(as_of), True)]
        if sector == "satellite_communications":
            return [("卫星通信产业进展", lambda: self._miit_satellite_communications(as_of), True)]
        if sector == "semiconductor":
            return [("半导体产业运行", lambda: self._miit_semiconductor(as_of), True)]
        if sector == "optical_communications":
            return [("光通信产业运行", lambda: self._miit_optical_communications(as_of), True)]
        raise EvidenceSourceError(f"未配置行业证据路由: {sector}")

    def _miit_new_energy_vehicle(self, as_of: datetime) -> EvidenceItem:
        settings = self.config.get("new_energy_vehicle", {})
        published, title, source_url, text = self._latest_miit_article(
            MIIT_NEV_INDEX_URL,
            lambda value: "汽车工业经济运行情况" in value,
            as_of,
        )
        production_yoy, sales_yoy = parse_nev_yoy(text)
        positive = float(settings.get("positive_sales_yoy_pct", 5.0))
        negative = float(settings.get("negative_sales_yoy_pct", -5.0))
        direction = (
            -1 if min(production_yoy, sales_yoy) <= negative
            else (1 if sales_yoy >= positive and production_yoy >= 0 else 0)
        )
        max_age = int(settings.get("max_age_calendar_days", 45))
        age = (as_of.date() - published).days
        return EvidenceItem(
            key=f"miit-nev-{published.isoformat()}",
            label="工信部新能源汽车月度产销",
            source="工业和信息化部/中国汽车工业协会",
            source_url=source_url,
            observed_at=datetime.combine(published, datetime.min.time(), self.tz).replace(hour=19),
            direction=direction,
            strength=2 if direction > 0 and min(production_yoy, sales_yoy) >= 10 else (1 if direction else 0),
            summary=(
                f"新能源车产量同比{production_yoy:+.1f}%、销量同比{sales_yoy:+.1f}%"
                f"（{published.isoformat()}发布，{title}）"
            ),
            freshness="fresh" if 0 <= age <= max_age else "stale",
            fact_type="sector_operating_metric",
        )

    def _miit_semiconductor(self, as_of: datetime) -> EvidenceItem:
        settings = self.config.get("semiconductor", {})
        published, title, source_url, text = self._latest_miit_article(
            MIIT_ELECTRONICS_INDEX_URL,
            lambda value: "电子信息制造业运行情况" in value,
            as_of,
        )
        ic_output_yoy, industry_value_yoy = parse_semiconductor_yoy(text)
        positive_output = float(settings.get("positive_ic_output_yoy_pct", 5.0))
        negative_output = float(settings.get("negative_ic_output_yoy_pct", -5.0))
        positive_value = float(settings.get("positive_industry_value_yoy_pct", 3.0))
        negative_value = float(settings.get("negative_industry_value_yoy_pct", -2.0))
        direction = (
            -1 if ic_output_yoy <= negative_output or industry_value_yoy <= negative_value
            else (
                1
                if ic_output_yoy >= positive_output and industry_value_yoy >= positive_value
                else 0
            )
        )
        max_age = int(settings.get("max_age_calendar_days", 75))
        age = (as_of.date() - published).days
        return EvidenceItem(
            key=f"miit-semiconductor-{published.isoformat()}",
            label="工信部电子信息制造业月度运行",
            source="工业和信息化部/国家统计局",
            source_url=source_url,
            observed_at=datetime.combine(published, datetime.min.time(), self.tz).replace(hour=19),
            direction=direction,
            strength=2 if direction > 0 and ic_output_yoy >= 15 else (1 if direction else 0),
            summary=(
                f"集成电路产量同比{ic_output_yoy:+.1f}%、电子信息制造业增加值同比"
                f"{industry_value_yoy:+.1f}%（{published.isoformat()}发布，{title}）"
            ),
            freshness="fresh" if 0 <= age <= max_age else "stale",
            fact_type="sector_operating_metric",
        )

    def _miit_optical_communications(self, as_of: datetime) -> EvidenceItem:
        settings = self.config.get("optical_communications", {})
        published, title, source_url, text = self._latest_miit_article(
            MIIT_TELECOM_INDEX_URL,
            lambda value: "通信业经济运行情况" in value,
            as_of,
        )
        revenue_yoy, business_volume_yoy, fiber_length_yoy = (
            parse_optical_communications_yoy(text)
        )
        positive_volume = float(settings.get("positive_business_volume_yoy_pct", 5.0))
        negative_volume = float(settings.get("negative_business_volume_yoy_pct", -3.0))
        positive_fiber = float(settings.get("positive_fiber_length_yoy_pct", 2.0))
        negative_fiber = float(settings.get("negative_fiber_length_yoy_pct", -2.0))
        minimum_revenue = float(settings.get("minimum_revenue_yoy_pct", -3.0))
        negative_revenue = float(settings.get("negative_revenue_yoy_pct", -8.0))
        direction = (
            -1
            if (
                business_volume_yoy <= negative_volume
                or fiber_length_yoy <= negative_fiber
                or revenue_yoy <= negative_revenue
            )
            else (
                1
                if (
                    business_volume_yoy >= positive_volume
                    and fiber_length_yoy >= positive_fiber
                    and revenue_yoy >= minimum_revenue
                )
                else 0
            )
        )
        max_age = int(settings.get("max_age_calendar_days", 45))
        age = (as_of.date() - published).days
        return EvidenceItem(
            key=f"miit-optical-communications-{published.isoformat()}",
            label="工信部通信业运行月报",
            source="工业和信息化部",
            source_url=source_url,
            observed_at=datetime.combine(published, datetime.min.time(), self.tz).replace(hour=19),
            direction=direction,
            strength=2 if direction > 0 and revenue_yoy >= 0 else (1 if direction else 0),
            summary=(
                f"通信业收入同比{revenue_yoy:+.1f}%、业务总量同比"
                f"{business_volume_yoy:+.1f}%、光缆线路长度同比{fiber_length_yoy:+.1f}%"
                f"（{published.isoformat()}发布，{title}）"
            ),
            freshness="fresh" if 0 <= age <= max_age else "stale",
            fact_type="sector_operating_metric",
        )

    def _miit_satellite_communications(self, as_of: datetime) -> EvidenceItem:
        settings = self.config.get("satellite_communications", {})
        published, title, source_url, text = self._latest_miit_article(
            MIIT_SATELLITE_INDEX_URL,
            lambda value: any(
                keyword in value
                for keyword in ("卫星互联网", "卫星通信", "低轨卫星", "空间无线电台")
            ),
            as_of,
        )
        positive_keywords = tuple(settings.get(
            "positive_keywords",
            ("成功发射", "顺利进入预定轨道", "批量颁发", "商用试验", "正式开通"),
        ))
        negative_keywords = tuple(settings.get(
            "negative_keywords",
            ("发射失败", "任务失败", "发生异常", "事故", "暂停", "撤销许可"),
        ))
        combined = f"{title} {text}"
        direction = (
            -1 if any(keyword in combined for keyword in negative_keywords)
            else (1 if any(keyword in combined for keyword in positive_keywords) else 0)
        )
        max_age = int(settings.get("max_age_calendar_days", 120))
        age = (as_of.date() - published).days
        return EvidenceItem(
            key=f"miit-satellite-communications-{published.isoformat()}",
            label="工信部卫星通信产业进展",
            source="工业和信息化部",
            source_url=source_url,
            observed_at=datetime.combine(published, datetime.min.time(), self.tz).replace(hour=17),
            direction=direction,
            strength=2 if direction != 0 else 0,
            summary=f"卫星通信产业事件：{title}（{published.isoformat()}）",
            freshness="fresh" if 0 <= age <= max_age else "stale",
            fact_type="sector_catalyst",
        )

    def _latest_miit_article(
        self,
        index_url: str,
        title_matches,
        as_of: datetime,
    ) -> tuple[date, str, str, str]:
        index_html = self._get_text(index_url, "https://www.miit.gov.cn/")
        api_url, query = parse_miit_column_query(index_html, index_url)
        listing_payload = self._get_json(
            f"{api_url}?{urlencode(query)}",
            index_url,
        )
        listing_html = str((listing_payload.get("data") or {}).get("html") or "")
        entries = parse_miit_listing(listing_html, index_url)
        match = next(
            (
                entry for entry in entries
                if entry[0] <= as_of.date() and title_matches(entry[1])
            ),
            None,
        )
        if match is None:
            raise EvidenceSourceError("工信部栏目未找到匹配的近期产业文章")
        published, title, source_url = match
        article_html = self._get_text(source_url, index_url)
        article_date = parse_miit_publication_date(article_html) or published
        if article_date > as_of.date():
            raise EvidenceSourceError("工信部产业文章日期晚于分析时点")
        return article_date, title, source_url, _html_text(article_html)

    def _shfe_copper(self, as_of: datetime) -> EvidenceItem:
        return self._shfe_futures_trend(as_of, "cu", "copper", "沪铜")

    def _shfe_futures_trend(
        self,
        as_of: datetime,
        product: str,
        sector_key: str,
        label: str,
    ) -> EvidenceItem:
        settings = self.config.get(sector_key) or self.config.get("copper") or {}
        max_age = int(settings.get("max_age_calendar_days", 4))
        trend_sessions = int(settings.get("trend_lookback_sessions", 3))
        product_id = product.lower()
        observations: list[tuple[date, dict[str, Any], str]] = []
        # 日行情通常在收盘后发布；盘中只使用最近已完成交易日。
        for offset in range(1, max_age + trend_sessions + 8):
            candidate = as_of.date() - timedelta(days=offset)
            if candidate.weekday() >= 5:
                continue
            url = SHFE_DAILY_URL.format(date=candidate.strftime("%Y%m%d"))
            try:
                payload = self._get_json(url, "https://www.shfe.com.cn/")
                observations.append((candidate, payload, url))
                if len(observations) >= trend_sessions:
                    break
            except Exception:
                continue
        if not observations:
            raise EvidenceSourceError(f"未取得最近上期所{label}日行情")
        source_date, payload, source_url = observations[0]
        candidates = [
            row for row in payload.get("o_curinstrument", [])
            if str(row.get("PRODUCTGROUPID", "")).lower() == product_id
            and str(row.get("DELIVERYMONTH", "")).isdigit()
        ]
        if not candidates:
            raise EvidenceSourceError(f"上期所日行情缺少{label}合约")
        main = max(candidates, key=lambda row: float(row.get("OPENINTEREST") or 0))
        previous = float(main.get("PRESETTLEMENTPRICE") or 0)
        change = float(main.get("ZD1_CHG") or 0)
        if previous <= 0:
            raise EvidenceSourceError(f"{label}前结算价无效")
        change_ratio = change / previous
        positive = float(settings.get("positive_change_ratio", 0.003))
        negative = float(settings.get("negative_change_ratio", -0.003))
        positive_trend = float(settings.get("positive_trend_ratio", 0.004))
        negative_trend = float(settings.get("negative_trend_ratio", -0.006))
        delivery_month = str(main.get("DELIVERYMONTH"))
        settlements: list[tuple[date, float]] = []
        for observed_date, observed_payload, _ in observations:
            matching = next(
                (
                    row for row in observed_payload.get("o_curinstrument", [])
                    if str(row.get("PRODUCTGROUPID", "")).lower() == product_id
                    and str(row.get("DELIVERYMONTH")) == delivery_month
                ),
                None,
            )
            if matching and float(matching.get("SETTLEMENTPRICE") or 0) > 0:
                settlements.append((observed_date, float(matching["SETTLEMENTPRICE"])))
        trend_ratio = None
        if len(settlements) >= 2:
            trend_ratio = settlements[0][1] / settlements[-1][1] - 1
        negative_signal = change_ratio <= negative or (
            trend_ratio is not None and trend_ratio <= negative_trend
        )
        positive_signal = (
            len(settlements) >= trend_sessions
            and trend_ratio is not None
            and trend_ratio >= positive_trend
            and change_ratio >= -positive
        )
        direction = -1 if negative_signal else (1 if positive_signal else 0)
        age = (as_of.date() - source_date).days
        freshness = "fresh" if age <= max_age else "stale"
        contract = f"{product.upper()}{delivery_month}"
        trend_text = f"、{len(settlements)}日{trend_ratio:+.2%}" if trend_ratio is not None else "、趋势样本不足"
        return EvidenceItem(
            key=f"shfe-{sector_key}-{source_date.isoformat()}-{contract}",
            label=f"上期所{label}主力日行情",
            source="上海期货交易所",
            source_url=source_url,
            observed_at=datetime.combine(source_date, datetime.min.time(), self.tz).replace(hour=15),
            direction=direction,
            strength=(
                2 if direction > 0 and change_ratio >= positive
                else (1 if direction else 0)
            ),
            summary=(
                f"上期所{contract}较前结算{change_ratio:+.2%}{trend_text}"
                f"（{source_date.isoformat()}日终）"
            ),
            freshness=freshness,
        )

    def _chinabond_long_rates(self, as_of: datetime, sector: str) -> EvidenceItem:
        settings = self.config.get("insurance", {})
        max_age = int(settings.get("max_age_calendar_days", 4))
        html = self._get_text(CHINABOND_CURVE_URL, "https://yield.chinabond.com.cn/")
        source_date, points = parse_chinabond_curve(html)
        if 10 not in points or 30 not in points:
            raise EvidenceSourceError("中债收益率页面缺少10年或30年期限")
        daily_bp = (points[10][1] + points[30][1]) / 2
        monthly_bp = (points[10][2] + points[30][2]) / 2
        positive = float(settings.get("positive_daily_bp", 1.0))
        negative = float(settings.get("negative_daily_bp", -1.0))
        positive_monthly = float(settings.get("positive_monthly_bp", 0.0))
        negative_monthly = float(settings.get("negative_monthly_bp", -3.0))
        direction = (
            1 if daily_bp >= positive and monthly_bp >= positive_monthly
            else (-1 if daily_bp <= negative or monthly_bp <= negative_monthly else 0)
        )
        age = (as_of.date() - source_date).days
        freshness = "fresh" if 0 <= age <= max_age else "stale"
        suffix = "寿险利率环境" if sector == "insurance" else "综合金融利率环境"
        return EvidenceItem(
            key=f"chinabond-long-rates-{source_date.isoformat()}",
            label="财政部-中国国债收益率曲线",
            source="财政部/中央国债登记结算公司",
            source_url=CHINABOND_CURVE_URL,
            observed_at=datetime.combine(source_date, datetime.min.time(), self.tz).replace(hour=17, minute=30),
            direction=direction,
            strength=1 if direction else 0,
            summary=(
                f"{suffix}：10年{points[10][0]:.2f}%、30年{points[30][0]:.2f}%、"
                f"日变动均值{daily_bp:+.2f}bp、月变动均值{monthly_bp:+.2f}bp"
                f"（{source_date.isoformat()}日终）"
            ),
            freshness=freshness,
        )

    def _shfe_copper_warrant(self, as_of: datetime) -> EvidenceItem:
        settings = self.config.get("copper", {})
        max_age = int(settings.get("max_age_calendar_days", 4))
        observations: list[tuple[date, float, str]] = []
        for offset in range(1, max_age + 12):
            candidate = as_of.date() - timedelta(days=offset)
            if candidate.weekday() >= 5:
                continue
            date_text = candidate.strftime("%Y%m%d")
            payload = None
            source_url = ""
            for template in SHFE_WARRANT_URLS:
                url = template.format(date=date_text, year=candidate.strftime("%Y"))
                try:
                    payload = self._get_json(url, "https://www.shfe.com.cn/")
                    source_url = url
                    break
                except Exception:
                    continue
            if payload is None:
                continue
            total = parse_shfe_copper_warrant(payload)
            if total is None:
                continue
            observations.append((candidate, total, source_url))
            if len(observations) >= 2:
                break
        if len(observations) < 2:
            raise EvidenceSourceError("未取得两个交易日的上期所铜仓单")
        latest_date, latest, source_url = observations[0]
        _, previous, _ = observations[1]
        change_ratio = latest / previous - 1 if previous > 0 else 0.0
        threshold = float(settings.get("warrant_change_ratio", 0.03))
        direction = -1 if change_ratio >= threshold else (1 if change_ratio <= -threshold else 0)
        age = (as_of.date() - latest_date).days
        return EvidenceItem(
            key=f"shfe-copper-warrant-{latest_date.isoformat()}",
            label="上期所铜仓单日报",
            source="上海期货交易所",
            source_url=source_url,
            observed_at=datetime.combine(latest_date, datetime.min.time(), self.tz).replace(hour=16),
            direction=direction,
            strength=1 if direction else 0,
            summary=f"上期所铜仓单{latest:.0f}吨、较前次{change_ratio:+.2%}（{latest_date.isoformat()}日终）",
            freshness="fresh" if age <= max_age else "stale",
        )

    def _load_shfe_option_chain(
        self, route: dict[str, Any], as_of: datetime
    ) -> CommodityOptionEvidence:
        """Build an end-of-day option-chain context that is separate from industry direction."""
        settings = self.config.get("commodity_options", {})
        futures_product = str(route.get("futures_product") or "cu").lower()
        commodity_label = str(route.get("label") or futures_product.upper())
        max_age = int(settings.get("max_age_calendar_days", 4))
        latest: tuple[date, dict[str, Any], dict[str, Any], dict[str, Any], str] | None = None
        last_error: Exception | None = None
        for offset in range(1, max_age + 12):
            candidate = as_of.date() - timedelta(days=offset)
            if candidate.weekday() >= 5:
                continue
            date_text = candidate.strftime("%Y%m%d")
            option_url = SHFE_OPTION_DAILY_URL.format(date=date_text)
            try:
                option_payload = self._get_json(option_url, "https://www.shfe.com.cn/")
                future_payload = self._get_json(
                    SHFE_DAILY_URL.format(date=date_text), "https://www.shfe.com.cn/"
                )
                try:
                    contract_payload = self._get_json(
                        SHFE_OPTION_CONTRACT_URL.format(date=date_text),
                        "https://www.shfe.com.cn/",
                    )
                except Exception:
                    contract_payload = {}
                if parse_shfe_option_contracts(option_payload, futures_product):
                    latest = (
                        candidate,
                        option_payload,
                        future_payload,
                        contract_payload,
                        option_url,
                    )
                    break
            except Exception as exc:
                last_error = exc
        if latest is None:
            raise EvidenceSourceError(
                f"未取得最近上期所{commodity_label}期权日行情"
                f"{': ' + str(last_error) if last_error else ''}"
            )
        source_date, option_payload, future_payload, contract_payload, source_url = latest
        if source_date > as_of.date():
            raise EvidenceSourceError(f"{commodity_label}期权行情日期晚于分析时点")
        return analyze_shfe_option_chain(
            option_payload=option_payload,
            future_payload=future_payload,
            contract_payload=contract_payload,
            source_date=source_date,
            as_of=as_of,
            settings=settings,
            source_url=source_url,
            futures_product=futures_product,
            commodity_label=commodity_label,
        )

    def _announcements(self, symbol: str, as_of: datetime) -> list[EvidenceItem]:
        code = symbol.split(".")[0]
        settings = self.config.get("announcements", {})
        lookback = int(settings.get("lookback_calendar_days", 14))
        risk_window = int(settings.get("risk_window_calendar_days", 7))
        critical_window = int(
            settings.get("critical_risk_window_calendar_days", lookback)
        )
        action_settings = self.config.get("corporate_actions", {})
        action_enabled = bool(action_settings.get("enabled", True))
        begin = as_of.date() - timedelta(days=lookback)
        # 交易所盘后公告有时归入下一自然日归档桶。向后查询一天后仍严格按
        # 公告时间 <= as_of 过滤，既避免漏掉盘后公告，也不会读取未来信息。
        query_end = as_of.date() + timedelta(
            days=int(action_settings.get("sse_archive_lookahead_days", 1))
            if action_enabled else 0
        )
        max_pages = max(int(settings.get("max_pages", 5)), 1)
        exchange = "上海证券交易所" if symbol.endswith(".SH") else "深圳证券交易所"
        critical = tuple(settings.get("critical_keywords", DEFAULT_CRITICAL_KEYWORDS))
        caution = tuple(settings.get("caution_keywords", DEFAULT_CAUTION_KEYWORDS))
        rows = self._announcement_rows(symbol, begin, query_end, max_pages)
        result: list[EvidenceItem] = []
        operating_keywords = tuple(settings.get(
            "operating_keywords",
            ("保费收入", "经营情况", "主要经营数据", "季度报告", "年度报告"),
        ))
        max_documents = int(settings.get("max_operating_documents", 2))
        parsed_documents = 0
        action_candidates: list[dict[str, Any]] = []
        for row in rows:
            title = _clean_text(str(row.get("TITLE") or row.get("title") or ""))
            if not title:
                continue
            published_at = _parse_announcement_datetime(row, self.tz)
            if published_at > as_of:
                continue
            age = (as_of.date() - published_at.date()).days
            risk, direction, strength = classify_announcement_title(title, critical, caution)
            active_window = critical_window if risk == "critical" else risk_window
            # 高风险事项使用更长观察窗；普通风险标题到期后只保留上下文。
            if age < 0 or age > active_window:
                risk, direction, strength = "none", 0, 0
            relative_url = str(row.get("URL") or row.get("attachPath") or row.get("adjunctUrl") or "").replace("\\/", "/")
            base_url = "https://www.sse.com.cn" if symbol.endswith(".SH") else "https://disc.static.szse.cn"
            source_url = relative_url if relative_url.startswith("http") else urljoin(base_url + "/", relative_url.lstrip("/"))
            title_item = EvidenceItem(
                key=(
                    f"{'sse' if symbol.endswith('.SH') else 'szse'}-announcement-{code}-{published_at.isoformat()}-"
                    f"{hashlib.sha256(title.encode('utf-8')).hexdigest()[:12]}"
                ),
                label=f"公司公告/{risk}",
                source=exchange,
                source_url=source_url,
                observed_at=published_at,
                direction=direction,
                strength=strength,
                summary=title,
                fact_type="announcement_title_metadata",
            )
            result.append(title_item)
            action = classify_corporate_action_title(title) if action_enabled else None
            if action is not None and source_url.lower().endswith(".pdf"):
                action_type, stage = action
                action_candidates.append({
                    "title": title,
                    "observed_at": published_at,
                    "source_url": source_url,
                    "action_type": action_type,
                    "stage": stage,
                    "title_key": title_item.key,
                })
            if (
                parsed_documents < max_documents
                and risk == "none"
                and any(keyword in title for keyword in operating_keywords)
                and source_url.lower().endswith(".pdf")
            ):
                try:
                    text = self._pdf_text(source_url, int(settings.get("max_pdf_pages", 20)))
                    direction, metric_summary = classify_operating_text(
                        text,
                        float(settings.get("operating_change_threshold_pct", 3.0)),
                    )
                    if direction is not None:
                        result.append(EvidenceItem(
                            key=f"{title_item.key}-operating",
                            label="公司经营指标",
                            source=f"{exchange}/公司公告正文",
                            source_url=source_url,
                            observed_at=published_at,
                            direction=direction,
                            strength=1 if direction else 0,
                            summary=metric_summary,
                            fact_type="company_operating_metric",
                        ))
                    parsed_documents += 1
                except Exception:
                    # 正文解析失败只降低公司证据完整性，不把缺失猜成正向或负向。
                    pass
        action_item = self._corporate_action_item(action_candidates, as_of, action_settings)
        if action_item is not None:
            result.append(action_item)
        return sorted(result, key=lambda item: item.observed_at, reverse=True)

    def _announcement_rows(
        self,
        symbol: str,
        begin: date,
        query_end: date,
        max_pages: int,
    ) -> list[dict[str, Any]]:
        """Read the exchange announcement ledger without inferring any facts."""
        code = symbol.split(".")[0]
        rows: list[dict[str, Any]] = []
        if symbol.endswith(".SH"):
            params = {
                "isPagination": "true", "productId": code, "keyWord": "",
                "securityType": "0101,120100,020100,020200,120200",
                "reportType2": "", "reportType": "ALL", "beginDate": begin.isoformat(),
                "endDate": query_end.isoformat(), "pageHelp.pageSize": 25,
                "pageHelp.pageNo": 1, "pageHelp.beginPage": 1,
                "pageHelp.cacheSize": 1, "pageHelp.endPage": 1,
            }
            for page_no in range(1, max_pages + 1):
                params.update({"pageHelp.pageNo": page_no, "pageHelp.beginPage": page_no, "pageHelp.endPage": page_no})
                payload = self._get_json(f"{SSE_ANNOUNCEMENT_URL}?{urlencode(params)}", "https://www.sse.com.cn/assortment/stock/list/info/announcement/")
                page_help = payload.get("pageHelp") or {}
                page_rows = page_help.get("data") or payload.get("result") or []
                rows.extend(page_rows)
                if page_no >= int(page_help.get("pageCount") or 1) or not page_rows:
                    break
        elif symbol.endswith(".SZ"):
            # The live SZSE page now posts JSON to annList.  The older GET
            # shape (channelCode=fixed_disc/secCode/seDate=range) returns 500
            # in production even though the endpoint path still exists.
            page_size = 50
            for page_no in range(1, max_pages + 1):
                payload = self._post_json(
                    f"{SZSE_ANNOUNCEMENT_URL}?random=0.5",
                    {
                        "stock": [code],
                        "channelCode": ["listedNotice_disc"],
                        "seDate": [begin.isoformat(), query_end.isoformat()],
                        "pageSize": page_size,
                        "pageNum": page_no,
                    },
                    "https://www.szse.cn/disclosure/listed/notice/index.html",
                )
                page_rows = payload.get("data") or []
                rows.extend(page_rows)
                total_count = int(payload.get("announceCount") or 0)
                if (
                    not page_rows
                    or len(page_rows) < page_size
                    or (total_count and len(rows) >= total_count)
                ):
                    break
        else:
            raise EvidenceSourceError("不支持的交易所代码")
        return rows

    def collect_cash_dividend_events(
        self,
        positions: tuple[Position, ...],
        as_of: datetime,
    ) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        """Return only fully verified cash-dividend implementation notices.

        A board proposal or a title-only match is never enough to move cash.  The
        exchange-hosted PDF must explicitly contain the record date, ex-dividend
        date, payment date and a positive per-share cash amount.
        """
        settings = dict(self.config.get("dividends", {}) or {})
        if not bool(settings.get("enabled", True)):
            return {}, []
        lookback = max(int(settings.get("lookback_calendar_days", 180)), 30)
        max_pages = max(int(settings.get("max_pages", 12)), 1)
        max_documents = max(int(settings.get("max_documents_per_symbol", 4)), 1)
        max_pdf_pages = max(int(settings.get("max_pdf_pages", 12)), 1)
        events: dict[str, list[dict[str, Any]]] = {}
        warnings: list[str] = []
        for position in positions:
            if position.role != "holding" or position.total_shares <= 0 or _is_etf(position):
                continue
            try:
                rows = self._announcement_rows(
                    position.symbol,
                    as_of.date() - timedelta(days=lookback),
                    as_of.date(),
                    max_pages,
                )
            except Exception as exc:
                warnings.append(f"{position.symbol} 分红公告读取失败: {exc}")
                continue
            found: dict[str, dict[str, Any]] = {}
            parsed = 0
            for row in rows:
                title = _clean_text(str(row.get("TITLE") or row.get("title") or ""))
                if not _is_cash_dividend_implementation_title(title):
                    continue
                try:
                    published_at = _parse_announcement_datetime(row, self.tz)
                except EvidenceSourceError:
                    continue
                if published_at > as_of or parsed >= max_documents:
                    continue
                relative_url = str(
                    row.get("URL") or row.get("attachPath") or row.get("adjunctUrl") or ""
                ).replace("\\/", "/")
                base_url = (
                    "https://www.sse.com.cn"
                    if position.symbol.endswith(".SH")
                    else "https://disc.static.szse.cn"
                )
                source_url = (
                    relative_url
                    if relative_url.startswith("http")
                    else urljoin(base_url + "/", relative_url.lstrip("/"))
                )
                if not source_url.lower().endswith(".pdf"):
                    continue
                parsed += 1
                try:
                    terms = parse_cash_dividend_implementation(
                        self._pdf_text(source_url, max_pdf_pages)
                    )
                except Exception:
                    # Missing/unclear terms must remain absent rather than be
                    # guessed from an announcement title.
                    continue
                event = {
                    "type": "cash_dividend",
                    **terms,
                    "auto_discovered": True,
                    "source_url": source_url,
                    "announcement_title": title,
                    "announced_at": published_at.isoformat(),
                    "note": "交易所实施公告自动识别",
                }
                key = _cash_dividend_key(event)
                found[key] = event
            if found:
                events[position.symbol] = sorted(
                    found.values(), key=lambda item: (item["payment_date"], item["record_date"])
                )
        return events, warnings

    def _corporate_action_item(
        self,
        candidates: list[dict[str, Any]],
        as_of: datetime,
        settings: dict[str, Any],
    ) -> EvidenceItem | None:
        """Return one deduplicated lifecycle observation for the latest action.

        Multiple notices for the same action (proposal, plan and board resolution)
        must not be counted as multiple positive confirmations. The latest calendar
        cluster is reduced to its most advanced lifecycle stage.
        """
        if not candidates:
            return None
        candidates = sorted(
            candidates, key=lambda item: item["observed_at"], reverse=True
        )
        newest = candidates[0]["observed_at"]
        association_days = int(settings.get("same_event_window_calendar_days", 2))
        cluster = [
            item for item in candidates
            if 0 <= (newest.date() - item["observed_at"].date()).days <= association_days
        ]
        selected = max(
            cluster,
            key=lambda item: (
                CORPORATE_ACTION_STAGE_RANK.get(item["stage"], 0),
                item["observed_at"],
            ),
        )
        age = (as_of.date() - selected["observed_at"].date()).days
        freshness = (
            "fresh"
            if 0 <= age <= int(settings.get("max_age_calendar_days", 60))
            else "stale"
        )
        body_status = "unavailable"
        direction = 0
        strength = 0
        terms: dict[str, Any] = {}
        body_error = ""
        try:
            body = self._pdf_text(
                selected["source_url"], int(settings.get("max_pdf_pages", 25))
            )
            if len(_clean_text(body)) < int(settings.get("minimum_body_characters", 80)):
                raise EvidenceSourceError("公告正文有效文本过短")
            body_status = "verified"
            terms = parse_corporate_action_terms(
                body, selected["action_type"], selected["stage"]
            )
            direction, strength = assess_corporate_action(
                selected["action_type"], selected["stage"], terms
            )
        except Exception as exc:
            body_error = _clean_text(str(exc))[:80]

        action_label = corporate_action_type_label(selected["action_type"])
        stage_label = CORPORATE_ACTION_STAGE_LABELS.get(
            selected["stage"], selected["stage"]
        )
        if body_status != "verified":
            summary = (
                f"正向公司行动候选：{action_label}{stage_label}；"
                "公告正文不可用，暂不计正向确认"
            )
            if body_error:
                summary += f"（{body_error}）"
        elif selected["stage"] == "terminated":
            summary = f"公司行动已终止：{action_label}，不再作为正向确认"
        else:
            summary = format_corporate_action_summary(
                action_label, stage_label, terms, strength
            )
        fact_type = (
            "positive_corporate_action"
            if direction > 0 else "corporate_action_candidate"
        )
        return EvidenceItem(
            key=f"{selected['title_key']}-corporate-action",
            label=(
                f"正向公司行动/{selected['action_type']}/{selected['stage']}/"
                f"{body_status}"
            ),
            source=f"{('上海证券交易所' if selected['source_url'].startswith('https://www.sse.com.cn') else '深圳证券交易所')}/公司公告正文",
            source_url=selected["source_url"],
            observed_at=selected["observed_at"],
            direction=direction,
            strength=strength,
            summary=summary,
            freshness=freshness,
            fact_type=fact_type,
        )

    def _margin_financing(
        self, symbol: str, as_of: datetime
    ) -> tuple[EvidenceItem, str, float]:
        settings = self.config.get("margin_financing", {})
        lookback = int(settings.get("lookback_calendar_days", 20))
        minimum = int(settings.get("minimum_observations", 5))
        begin = as_of.date() - timedelta(days=lookback)
        code = symbol.split(".")[0]
        if symbol.endswith(".SH"):
            params = {
                "isPagination": "true",
                "pageHelp.pageSize": max(lookback, minimum),
                "pageHelp.pageNo": 1,
                "pageHelp.beginPage": 1,
                "pageHelp.cacheSize": 1,
                "pageHelp.endPage": 1,
                "preStockCode": code,
                "beginDate": begin.strftime("%Y%m%d"),
                "endDate": as_of.date().strftime("%Y%m%d"),
                "sqlId": "RZRQ_MX_INFO",
            }
            source_url = f"{SSE_MARGIN_URL}?{urlencode(params)}"
            payload = self._get_json(
                source_url,
                "https://www.sse.com.cn/market/othersdata/margin/detail/",
            )
        elif symbol.endswith(".SZ"):
            # 深交所当前官方报表只接受单日查询；tab2才是证券级明细。
            # 因此逐个已过去的工作日取样，空日/休市日明确跳过。
            normalized_rows: list[dict[str, Any]] = []
            source_url = SZSE_MARGIN_URL
            for offset in range(0, lookback + 8):
                candidate = as_of.date() - timedelta(days=offset)
                if candidate.weekday() >= 5:
                    continue
                params = {
                    "SHOWTYPE": "JSON",
                    "CATALOGID": "1837_xxpl",
                    "TABKEY": "tab2",
                    "txtDate": candidate.isoformat(),
                    "txtZqdm": code,
                }
                candidate_url = f"{SZSE_MARGIN_URL}?{urlencode(params)}"
                try:
                    candidate_payload = self._get_json(
                        candidate_url,
                        "https://www.szse.cn/disclosure/margin/margin/",
                    )
                except Exception:
                    continue
                rows = _nested_dicts(candidate_payload)
                matching = next(
                    (row for row in rows if str(row.get("zqdm") or "") == code),
                    None,
                )
                if matching is None:
                    continue
                balance = _coerce_number(matching.get("jrrzye"))
                buy = _coerce_number(matching.get("jrrzmr"))
                if balance is None:
                    continue
                normalized_rows.append({
                    "opDate": candidate.isoformat(),
                    # 深交所证券级融资余额/买入额的网页单位均为亿元。
                    "rzye": balance * 100_000_000,
                    "rzmre": (buy or 0.0) * 100_000_000,
                })
                source_url = candidate_url
                if len(normalized_rows) >= minimum:
                    break
            payload = {"data": normalized_rows}
        else:
            raise EvidenceSourceError(f"不支持的证券交易所: {symbol}")
        observations = parse_margin_observations(payload)
        if len(observations) < minimum:
            raise EvidenceSourceError(
                f"两融日终样本不足：{len(observations)}<{minimum}"
            )
        selected = observations[-minimum:]
        if symbol.endswith(".SZ"):
            # 官方说明：当日融资余额=前日余额+融资买入-融资偿还。
            selected = [
                (
                    observed,
                    balance,
                    buy,
                    max(selected[index - 1][1] + buy - balance, 0.0)
                    if index > 0 else 0.0,
                )
                for index, (observed, balance, buy, _) in enumerate(selected)
            ]
        latest_date, latest_balance, latest_buy, latest_repay = selected[-1]
        first_balance = selected[0][1]
        change = latest_balance / first_balance - 1 if first_balance > 0 else 0.0
        crowded = float(settings.get("crowded_change_ratio", 0.08))
        deleveraging = float(settings.get("deleveraging_change_ratio", -0.05))
        extreme = float(settings.get("extreme_crowding_ratio", 0.12))
        signal = (
            "extreme_crowding" if change >= extreme
            else ("crowded" if change >= crowded
                  else ("deleveraging" if change <= deleveraging else "neutral"))
        )
        age = (as_of.date() - latest_date).days
        max_age = int(settings.get("max_age_calendar_days", 4))
        freshness = "fresh" if 0 <= age <= max_age else "stale"
        source_name = "上海证券交易所" if symbol.endswith(".SH") else "深圳证券交易所"
        item = EvidenceItem(
            key=f"margin-{symbol}-{latest_date.isoformat()}",
            label="融资融券日终辅助",
            source=source_name,
            source_url=source_url,
            observed_at=datetime.combine(
                latest_date, datetime.min.time(), self.tz
            ).replace(hour=18),
            direction=0,
            strength=1 if signal != "neutral" else 0,
            summary=(
                f"两融日终：融资余额近{minimum}个样本{change:+.2%}，"
                f"当日融资买入{latest_buy:.0f}元、偿还{latest_repay:.0f}元"
                f"（{latest_date.isoformat()}）"
            ),
            freshness=freshness,
            fact_type="margin_financing",
        )
        return item, signal, change

    def _capital_flow(
        self, symbol: str, as_of: datetime
    ) -> tuple[EvidenceItem, str, float]:
        """东财主力净流入辅助层；仅完整交易日可参与门控。"""
        settings = self.config.get("capital_flow", {})
        lookback = int(settings.get("lookback_sessions", 5))
        observed_date, main_net, main_pct = self._fetch_today_main_flow(symbol)
        history = self._load_fflow_history(symbol)
        local_now = as_of.astimezone(self.tz)
        intraday_partial = (
            observed_date == local_now.date()
            and (local_now.hour, local_now.minute) < (15, 10)
        )
        if intraday_partial:
            # The feed updates through the day. Do not overwrite a completed-day
            # observation or call it "日终" before the market has closed.
            item = EvidenceItem(
                key=f"capital-flow-intraday-{symbol}-{observed_date.isoformat()}",
                label="主力资金流向（盘中，仅展示）",
                source="东方财富",
                source_url=f"https://data.eastmoney.com/zjlx/{symbol.split('.')[0]}.html",
                observed_at=local_now,
                direction=0,
                strength=0,
                summary=(
                    f"盘中主力净流入暂值{main_net / 1e8:+.2f}亿元"
                    + (
                        f"（占换手{main_pct:+.2f}%）"
                        if main_pct is not None
                        else ""
                    )
                    + "；未收盘，不参与新增、减仓或择优门控"
                ),
                freshness="partial",
                fact_type="capital_flow_intraday_partial",
            )
            return item, "intraday_partial", 0.0
        history[observed_date.isoformat()] = {
            "main_net": main_net,
            "main_pct": main_pct,
        }
        # 只保留近 45 个自然日样本，避免文件膨胀。
        cutoff = (as_of.date() - timedelta(days=45)).isoformat()
        history = {key: value for key, value in history.items() if key >= cutoff}
        self._save_fflow_history(symbol, history)
        ordered = sorted(history.items())[-lookback:]
        nets = [float(item[1]["main_net"]) for item in ordered]
        net_sum = sum(nets)
        positive_days = sum(1 for value in nets if value > 0)
        negative_days = sum(1 for value in nets if value < 0)
        inflow_threshold = float(settings.get("persistent_inflow_yuan", 300_000_000))
        outflow_threshold = float(settings.get("persistent_outflow_yuan", -300_000_000))
        majority = max(1, (len(nets) + 1) // 2)
        if len(nets) >= lookback and net_sum >= inflow_threshold and positive_days >= majority:
            signal = "persistent_inflow"
            direction = 1
        elif len(nets) >= lookback and net_sum <= outflow_threshold and negative_days >= majority:
            signal = "persistent_outflow"
            direction = -1
        elif main_net >= float(settings.get("single_day_inflow_yuan", 100_000_000)):
            signal = "single_day_inflow"
            direction = 1
        elif main_net <= float(settings.get("single_day_outflow_yuan", -100_000_000)):
            signal = "single_day_outflow"
            direction = -1
        else:
            signal = "neutral"
            direction = 0
        age = (as_of.date() - observed_date).days
        max_age = int(settings.get("max_age_calendar_days", 2))
        freshness = "fresh" if 0 <= age <= max_age else "stale"
        sample_note = f"近{len(nets)}日累计{net_sum / 1e8:+.2f}亿元"
        item = EvidenceItem(
            key=f"capital-flow-{symbol}-{observed_date.isoformat()}",
            label="主力资金流向辅助",
            source="东方财富",
            source_url=f"https://data.eastmoney.com/zjlx/{symbol.split('.')[0]}.html",
            observed_at=datetime.combine(
                observed_date, datetime.min.time(), self.tz
            ).replace(hour=16),
            direction=direction,
            strength=2 if signal.startswith("persistent") else (1 if direction else 0),
            summary=(
                f"资金日终：主力净流入当日{main_net / 1e8:+.2f}亿元"
                + (f"（占换手{main_pct:+.2f}%）" if main_pct is not None else "")
                + f"，{sample_note}"
                f"（{observed_date.isoformat()}）"
            ),
            freshness=freshness,
            fact_type="capital_flow",
        )
        return item, signal, net_sum

    def _fetch_today_main_flow(self, symbol: str) -> tuple[date, float, float | None]:
        code = symbol.split(".")[0]
        market = "1" if symbol.endswith(".SH") else "0"
        params = {
            "lmt": "5",
            "klt": "101",
            "secid": f"{market}.{code}",
            "fields1": "f1,f2,f3,f7",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
            "ut": EASTMONEY_FFLOW_UT,
        }
        referer = f"https://data.eastmoney.com/zjlx/{code}.html"
        errors: list[str] = []
        for endpoint in EASTMONEY_FFLOW_URLS:
            try:
                url = f"{endpoint}?{urlencode(params)}"
                payload = self._get_json(url, referer)
                klines = ((payload.get("data") or {}) or {}).get("klines") or []
                if not klines:
                    rc = payload.get("rc")
                    errors.append(f"{endpoint.split('/')[2]} 空数据(rc={rc})")
                    continue
                parts = str(klines[-1]).split(",")
                if len(parts) < 2:
                    errors.append(f"{endpoint.split('/')[2]} 字段不足")
                    continue
                observed = date.fromisoformat(parts[0][:10])
                main_net = float(parts[1])
                # 新版 kline 响应仅返回日期和五档净流入，不再提供换手占比。
                main_pct = float(parts[6]) if len(parts) > 6 else None
                return observed, main_net, main_pct
            except (TypeError, ValueError, EvidenceSourceError) as exc:
                errors.append(f"{endpoint.split('/')[2]} {str(exc)[:80]}")
        detail = "；".join(errors[-3:]) or "未返回可解析响应"
        raise EvidenceSourceError(f"资金流向数据不可用：{detail}")

    def _fflow_history_path(self, symbol: str) -> Path | None:
        if self.cache_dir is None:
            return None
        return self.cache_dir / f"fflow_{symbol.replace('.', '_')}.json"

    def _load_fflow_history(self, symbol: str) -> dict[str, Any]:
        path = self._fflow_history_path(symbol)
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_fflow_history(self, symbol: str, history: dict[str, Any]) -> None:
        path = self._fflow_history_path(symbol)
        if path is None:
            return
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            return

    def _shareholder_count(
        self, symbol: str, as_of: datetime
    ) -> tuple[EvidenceItem, str, float]:
        """股东户数环比：下降视为筹码趋向集中，上升视为分散；不单独触发买卖。"""
        settings = self.config.get("shareholder_count", {})
        code = symbol.split(".")[0]
        params = {
            "reportName": "RPT_F10_EH_HOLDERNUM",
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{code}")',
            "pageNumber": "1",
            "pageSize": "4",
            "sortTypes": "-1",
            "sortColumns": "END_DATE",
            "source": "WEB",
            "client": "WEB",
        }
        url = f"{EASTMONEY_HOLDER_URL}?{urlencode(params)}"
        payload = self._get_json(url, f"https://data.eastmoney.com/gdhs/detail/{code}.html")
        rows = ((payload.get("result") or {}) or {}).get("data") or []
        if not rows:
            raise EvidenceSourceError("股东户数记录为空")
        latest = rows[0]
        end_raw = str(latest.get("END_DATE") or "")[:10]
        end_date = date.fromisoformat(end_raw)
        holders = int(float(latest.get("HOLDER_TOTAL_NUM") or 0))
        change_pct = latest.get("TOTAL_NUM_RATIO")
        if change_pct is None:
            raise EvidenceSourceError("股东户数缺少环比字段")
        change_ratio = float(change_pct) / 100.0
        concentrate = float(settings.get("concentrate_change_ratio", -0.05))
        disperse = float(settings.get("disperse_change_ratio", 0.05))
        if change_ratio <= concentrate:
            signal = "concentrating"
            direction = 1
        elif change_ratio >= disperse:
            signal = "dispersing"
            direction = -1
        else:
            signal = "neutral"
            direction = 0
        notice_raw = str(latest.get("NOTICE_DATE") or end_raw)[:10]
        try:
            notice_date = date.fromisoformat(notice_raw)
        except ValueError:
            notice_date = end_date
        age = (as_of.date() - notice_date).days
        max_age = int(settings.get("max_age_calendar_days", 120))
        freshness = "fresh" if 0 <= age <= max_age else "stale"
        focus = str(latest.get("HOLD_FOCUS") or "").strip()
        focus_note = f"，集中度标签{focus}" if focus else ""
        avg_shares = latest.get("AVG_FREE_SHARES")
        avg_note = (
            f"，户均流通股{int(float(avg_shares)):,}"
            if avg_shares not in (None, "")
            else ""
        )
        item = EvidenceItem(
            key=f"shareholder-{symbol}-{end_date.isoformat()}",
            label="股东户数辅助",
            source="东方财富数据中心",
            source_url=f"https://data.eastmoney.com/gdhs/detail/{code}.html",
            observed_at=datetime.combine(
                notice_date, datetime.min.time(), self.tz
            ).replace(hour=18),
            direction=direction,
            strength=1 if direction else 0,
            summary=(
                f"股东户数：截至{end_date.isoformat()}共{holders:,}户，"
                f"环比{change_ratio:+.2%}"
                f"{avg_note}{focus_note}"
            ),
            freshness=freshness,
            fact_type="shareholder_count",
        )
        return item, signal, change_ratio

    def _get_json(self, url: str, referer: str) -> dict[str, Any]:
        return json.loads(self._request(url, referer).decode("utf-8"))

    def _post_json(
        self, url: str, payload: dict[str, Any], referer: str
    ) -> dict[str, Any]:
        return json.loads(
            self._request(url, referer, method="POST", json_payload=payload).decode(
                "utf-8"
            )
        )

    def _get_text(self, url: str, referer: str) -> str:
        return self._request(url, referer).decode("utf-8", errors="replace")

    def _pdf_text(self, url: str, max_pages: int) -> str:
        if PdfReader is None:
            raise EvidenceSourceError("未安装pypdf，无法解析公告正文")
        candidates = [url]
        official_prefix = "https://www.sse.com.cn/"
        if url.startswith(official_prefix):
            candidates.append(
                "https://big5.sse.com.cn/site/cht/www.sse.com.cn/"
                + url[len(official_prefix):]
            )
        errors: list[str] = []
        for candidate in candidates:
            try:
                payload = self._request(candidate, "https://www.sse.com.cn/")
                if not payload.startswith(b"%PDF"):
                    preview = payload[:200].lower()
                    if b"html" in preview or b"<!doctype" in preview:
                        raise EvidenceSourceError("交易所公告正文返回网页校验页")
                    raise EvidenceSourceError("交易所公告正文不是有效PDF")
                reader = PdfReader(BytesIO(payload))
                return "\n".join(
                    (page.extract_text() or "") for page in reader.pages[:max_pages]
                )
            except Exception as exc:
                errors.append(str(exc))
        raise EvidenceSourceError("；".join(errors[-2:]))

    def _request(
        self,
        url: str,
        referer: str,
        *,
        method: str = "GET",
        json_payload: dict[str, Any] | None = None,
    ) -> bytes:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                body = (
                    json.dumps(json_payload, ensure_ascii=False).encode("utf-8")
                    if json_payload is not None
                    else None
                )
                request = Request(
                    url,
                    data=body,
                    method=method,
                    headers={
                        "User-Agent": "Mozilla/5.0 astock-discipline-bot/0.2",
                        "Referer": referer,
                        "Accept": "application/json,text/html,*/*",
                        "X-Request-Type": "ajax",
                        "X-Requested-With": "XMLHttpRequest",
                        **(
                            {"Content-Type": "application/json"}
                            if body is not None
                            else {}
                        ),
                    },
                )
                with urlopen(request, timeout=self.timeout) as response:
                    payload = response.read()
                    encoding = str(response.headers.get("Content-Encoding") or "").lower()
                    if encoding == "gzip" or payload.startswith(b"\x1f\x8b"):
                        payload = gzip.decompress(payload)
                    return payload
            except Exception as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.4 * (attempt + 1))
        raise EvidenceSourceError(str(last_error))


def parse_chinabond_curve(html: str) -> tuple[date, dict[int, tuple[float, float, float, float]]]:
    table_start = html.find('id="gjqxData"')
    table = html[table_start:] if table_start >= 0 else html
    date_match = re.search(r'<td[^>]*rowspan="?\d+"?[^>]*>\s*(\d{4}-\d{2}-\d{2})\s*</td>', table)
    if not date_match:
        raise EvidenceSourceError("无法识别中债收益率日期")
    points: dict[int, tuple[float, float, float, float]] = {}
    for term in (10, 30):
        match = re.search(rf"<td[^>]*>\s*{term}年\s*</td>(.*?)</tr>", table, re.S)
        if not match:
            continue
        cells = re.findall(r"<td[^>]*>\s*([-+]?\d+(?:\.\d+)?)\s*</td>", match.group(1), re.S)
        if len(cells) >= 4:
            points[term] = tuple(float(value) for value in cells[:4])
    return date.fromisoformat(date_match.group(1)), points


def parse_miit_column_query(html: str, index_url: str) -> tuple[str, dict[str, Any]]:
    script_match = re.search(
        r'<script[^>]+url="([^"]+)"[^>]+queryData="([^"]+)"[^>]*>',
        html,
        re.S,
    )
    if not script_match:
        raise EvidenceSourceError("工信部栏目缺少公开列表接口参数")
    api_url = urljoin(index_url, unescape(script_match.group(1)))
    raw_query = unescape(script_match.group(2))
    try:
        query = ast.literal_eval(raw_query)
    except (SyntaxError, ValueError) as exc:
        raise EvidenceSourceError("工信部栏目接口参数无法解析") from exc
    if not isinstance(query, dict) or not query:
        raise EvidenceSourceError("工信部栏目接口参数为空")
    return api_url, {str(key): value for key, value in query.items()}


def parse_miit_listing(html: str, base_url: str) -> list[tuple[date, str, str]]:
    entries: list[tuple[date, str, str]] = []
    for block in re.findall(r"<li\b[^>]*>.*?</li>", html, re.S | re.I):
        anchor = re.search(r"<a\b([^>]*)>(.*?)</a>", block, re.S | re.I)
        day = re.search(r"(20\d{2}-\d{2}-\d{2})", block)
        if not anchor or not day:
            continue
        attributes, body = anchor.groups()
        href_match = re.search(r'href="([^"]+)"', attributes, re.I)
        if not href_match:
            continue
        title_match = re.search(r'title="([^"]*)"', attributes, re.I)
        href = href_match.group(1)
        title_attr = title_match.group(1) if title_match else ""
        title = _clean_text(title_attr or re.sub(r"<[^>]+>", "", body))
        if not title:
            continue
        entries.append((date.fromisoformat(day.group(1)), title, urljoin(base_url, href)))
    entries.sort(key=lambda item: item[0], reverse=True)
    return entries


def parse_miit_publication_date(html: str) -> date | None:
    match = re.search(r"发布时间[：:]\s*(20\d{2}-\d{2}-\d{2})", _html_text(html))
    return date.fromisoformat(match.group(1)) if match else None


def parse_nev_yoy(text: str) -> tuple[float, float]:
    segment_match = re.search(r"新能源汽车产销分别完成(.{0,180}?)(?:；|。)", text)
    if not segment_match:
        raise EvidenceSourceError("工信部汽车月报缺少新能源汽车产销段落")
    change = re.search(
        r"同比(?:分别)?(增长|下降)\s*([0-9.]+)%\s*和\s*(?:(增长|下降)\s*)?([0-9.]+)%",
        segment_match.group(1),
    )
    if not change:
        raise EvidenceSourceError("工信部汽车月报缺少新能源产销同比值")
    first_sign = 1 if change.group(1) == "增长" else -1
    second_sign = 1 if (change.group(3) or change.group(1)) == "增长" else -1
    return first_sign * float(change.group(2)), second_sign * float(change.group(4))


def parse_semiconductor_yoy(text: str) -> tuple[float, float]:
    output_match = re.search(
        r"集成电路产量[^。；]{0,60}?同比(增长|下降)\s*([0-9.]+)%",
        text,
    )
    value_match = re.search(
        r"规模以上电子信息制造业增加值同比(增长|下降)\s*([0-9.]+)%",
        text,
    )
    if not output_match or not value_match:
        raise EvidenceSourceError("工信部电子信息月报缺少集成电路产量或行业增加值同比值")

    def signed(match) -> float:
        return (1 if match.group(1) == "增长" else -1) * float(match.group(2))

    return signed(output_match), signed(value_match)


def parse_optical_communications_yoy(text: str) -> tuple[float, float, float]:
    revenue_match = re.search(
        r"电信业务收入累计完成[^。；]{0,100}?同比(增长|下降)\s*([0-9.]+)%",
        text,
    )
    volume_match = re.search(
        r"电信业务总量同比(增长|下降)\s*([0-9.]+)%",
        text,
    )
    fiber_match = re.search(
        r"全国光缆线路总长度[^。；]{0,100}?同比(增长|下降)\s*([0-9.]+)%",
        text,
    )
    if not revenue_match or not volume_match or not fiber_match:
        raise EvidenceSourceError("工信部通信业月报缺少收入、业务总量或光缆线路同比值")

    def signed(match) -> float:
        return (1 if match.group(1) == "增长" else -1) * float(match.group(2))

    return signed(revenue_match), signed(volume_match), signed(fiber_match)


def classify_announcement_title(
    title: str,
    critical_keywords: tuple[str, ...] = DEFAULT_CRITICAL_KEYWORDS,
    caution_keywords: tuple[str, ...] = DEFAULT_CAUTION_KEYWORDS,
) -> tuple[str, int, int]:
    # 标题关键词只作保守风险闸门；明确否定表述不能被机械地识别成利空。
    negated = tuple(
        f"{prefix}{keyword}"
        for keyword in critical_keywords + caution_keywords
        for prefix in ("不存在", "未发生", "不涉及", "无")
    )
    if any(phrase in title for phrase in negated):
        return "none", 0, 0
    if any(keyword in title for keyword in critical_keywords):
        return "critical", -1, 2
    if any(keyword in title for keyword in caution_keywords):
        return "caution", -1, 1
    return "none", 0, 0


def classify_corporate_action_title(title: str) -> tuple[str, str] | None:
    """Classify deliberate capital-allocation/alignment actions by lifecycle.

    This is intentionally narrower than a generic positive-keyword classifier.
    Administrative repurchases (restricted-stock cancellation, pledge repo, bond
    redemption) are excluded because they do not represent open-market support.
    """
    clean = _clean_text(title)
    administrative = (
        "限制性股票回购注销", "回购注销部分限制性股票", "质押式回购",
        "债券回购", "可转换公司债券赎回", "业绩补偿股份回购注销",
    )
    if any(keyword in clean for keyword in administrative):
        return None

    action_type: str | None = None
    if "回购" in clean and any(keyword in clean for keyword in ("股份", "公司股票")):
        action_type = "share_repurchase"
    elif "增持" in clean and any(keyword in clean for keyword in ("股东", "董事", "高管", "实际控制人")):
        action_type = "insider_increase"
    elif any(keyword in clean for keyword in ("现金分红", "权益分派", "利润分配")):
        action_type = "cash_distribution"
    elif "员工持股计划" in clean:
        action_type = "employee_ownership"
    elif "股权激励" in clean or "股票期权激励" in clean:
        action_type = "equity_incentive"
    if action_type is None:
        return None

    if any(keyword in clean for keyword in ("终止", "不实施", "未获通过")):
        stage = "terminated"
    elif any(keyword in clean for keyword in ("实施结果", "实施完成", "回购完成", "增持完成")):
        stage = "completed"
    elif any(keyword in clean for keyword in ("首次回购", "首次实施回购", "首次增持")):
        stage = "first_execution"
    elif any(keyword in clean for keyword in ("进展", "累计回购", "累计增持")):
        stage = "progress"
    elif any(keyword in clean for keyword in ("审议通过", "股东大会决议")):
        stage = "approved"
    elif any(keyword in clean for keyword in ("预案", "方案", "报告书", "草案", "实施公告")):
        stage = "plan"
    elif "提议" in clean:
        stage = "proposal"
    else:
        # Vague action titles are visible in the raw announcement ledger but are
        # not promoted into the positive-action layer.
        return None
    return action_type, stage


def parse_corporate_action_terms(
    text: str, action_type: str, stage: str
) -> dict[str, Any]:
    clean = _clean_text(text).replace(",", "")
    terms: dict[str, Any] = {}
    amounts = _extract_amounts_near(
        clean,
        (
            "回购资金总额", "预计回购金额", "回购金额",
            "增持金额", "增持资金", "分红金额", "现金分红",
        ),
    )
    if amounts:
        terms["amount_min"] = min(amounts)
        terms["amount_max"] = max(amounts)
    actual_amounts = _extract_amounts_near(
        clean,
        ("已支付的总金额", "支付的资金总额", "成交总金额", "累计增持金额"),
    )
    if actual_amounts:
        terms["actual_amount"] = max(actual_amounts)

    price_match = re.search(
        r"(?:回购|增持)?价格(?:上限)?(?:不超过|不高于|为)?(?:人民币)?"
        r"([0-9]+(?:\.[0-9]+)?)元/?股",
        clean,
    )
    if price_match:
        terms["price_cap"] = float(price_match.group(1))

    ratio_matches = re.findall(
        r"占(?:公司)?(?:当前)?总股本(?:的)?(?:比例)?(?:约为|为|不低于|不超过)?"
        r"([0-9]+(?:\.[0-9]+)?)%",
        clean,
    )
    if ratio_matches:
        terms["share_ratio_pct"] = max(float(value) for value in ratio_matches)

    share_matches = re.findall(
        r"(?:累计)?(?:已)?(?:回购|增持)(?:公司)?股份(?:数量)?(?:为|共计|合计)?"
        r"([0-9]+(?:\.[0-9]+)?)(万|亿)?股",
        clean,
    )
    if share_matches:
        multiplier = {"": 1.0, "万": 10_000.0, "亿": 100_000_000.0}
        terms["shares"] = max(
            float(value) * multiplier.get(unit or "", 1.0)
            for value, unit in share_matches
        )

    terms["purpose"] = _corporate_action_purpose(clean)
    terms["action_type"] = action_type
    terms["stage"] = stage
    return terms


def assess_corporate_action(
    action_type: str, stage: str, terms: dict[str, Any]
) -> tuple[int, int]:
    """Score verified actions; strength two is needed for strategy confirmation."""
    if stage == "terminated":
        return 0, 0
    if action_type in {"cash_distribution", "employee_ownership", "equity_incentive"}:
        return 1, 1
    if action_type not in {"share_repurchase", "insider_increase"}:
        return 0, 0

    has_plan_scale = bool(
        (terms.get("amount_max") or 0) > 0
        or (terms.get("share_ratio_pct") or 0) > 0
        or (terms.get("shares") or 0) > 0
    )
    has_execution = bool(
        (terms.get("actual_amount") or 0) > 0 or (terms.get("shares") or 0) > 0
    )
    if stage == "proposal":
        return (1, 1) if has_plan_scale else (0, 0)
    if stage in {"plan", "approved"}:
        strength = 2 if has_plan_scale else 1
    elif stage in {"first_execution", "progress"}:
        strength = 2 if has_execution else 1
    elif stage == "completed":
        strength = 3 if has_execution else 1
    else:
        strength = 0
    if terms.get("purpose") == "employee_incentive":
        strength = min(strength, 1)
    return (1, strength) if strength > 0 else (0, 0)


def corporate_action_type_label(action_type: str) -> str:
    return {
        "share_repurchase": "股份回购",
        "insider_increase": "股东/高管增持",
        "cash_distribution": "现金分红",
        "employee_ownership": "员工持股计划",
        "equity_incentive": "股权激励",
    }.get(action_type, action_type)


def format_corporate_action_summary(
    action_label: str,
    stage_label: str,
    terms: dict[str, Any],
    strength: int,
) -> str:
    details: list[str] = []
    amount_min = terms.get("amount_min")
    amount_max = terms.get("amount_max")
    actual_amount = terms.get("actual_amount")
    if amount_min and amount_max:
        details.append(
            f"计划金额{_format_yuan(amount_min)}—{_format_yuan(amount_max)}"
            if amount_min != amount_max else f"计划金额{_format_yuan(amount_max)}"
        )
    if actual_amount:
        details.append(f"已实施金额{_format_yuan(actual_amount)}")
    if terms.get("share_ratio_pct"):
        details.append(f"约占总股本{float(terms['share_ratio_pct']):.2f}%")
    if terms.get("price_cap"):
        details.append(f"价格上限{float(terms['price_cap']):.2f}元/股")
    purpose = {
        "cancel": "用途为注销",
        "employee_incentive": "用途为员工持股/激励",
        "value_support": "目的含维护公司价值",
    }.get(str(terms.get("purpose")), "")
    if purpose:
        details.append(purpose)
    suffix = "；".join(details) if details else "正文条款已核验"
    level = {1: "弱", 2: "中", 3: "强"}.get(strength, "中性")
    return f"正向公司行动：{action_label}{stage_label}；{suffix}；证据强度{level}"


def _extract_amounts_near(text: str, anchors: tuple[str, ...]) -> list[float]:
    values: list[float] = []
    for anchor in anchors:
        for match in re.finditer(re.escape(anchor), text):
            segment = text[match.start(): match.start() + 180]
            for raw, unit in re.findall(
                r"([0-9]+(?:\.[0-9]+)?)(亿元|万元|元)", segment
            ):
                multiplier = {"亿元": 100_000_000.0, "万元": 10_000.0, "元": 1.0}[unit]
                value = float(raw) * multiplier
                # This layer concerns capital-allocation amounts; small "元/股"
                # prices found in the same paragraph are parsed separately.
                if value >= 10_000:
                    values.append(value)
    return values


def _corporate_action_purpose(text: str) -> str:
    # Exchange templates list several mutually exclusive purposes. A keyword scan
    # over the full PDF would therefore read unchecked options as facts. Prefer a
    # checked box, then restrict inference to the local purpose paragraph.
    selected_patterns = (
        ("employee_incentive", r"[√☑■]\s*用于员工持股计划或股权激励"),
        ("cancel", r"[√☑■]\s*(?:减少注册资本|用于注销)"),
        ("value_support", r"[√☑■]\s*(?:为维护公司价值|维护广大投资者利益)"),
    )
    for name, pattern in selected_patterns:
        if re.search(pattern, text):
            return name

    segments: list[str] = []
    for anchor in ("回购用途", "回购股份的目的", "回购目的"):
        match = re.search(re.escape(anchor), text)
        if match:
            segments.append(text[match.start(): match.start() + 450])
    scope = " ".join(segments) if segments else (text if len(text) <= 800 else "")
    purpose_map = (
        ("employee_incentive", ("用于员工持股计划", "用于股权激励计划")),
        ("cancel", ("注销并减少注册资本", "用于注销", "依法注销")),
        ("value_support", ("维护公司价值", "维护广大投资者利益", "增强投资者信心")),
    )
    return next(
        (name for name, keywords in purpose_map if any(keyword in scope for keyword in keywords)),
        "unspecified",
    )


def _format_yuan(value: float) -> str:
    if value >= 100_000_000:
        return f"{value / 100_000_000:.2f}亿元"
    if value >= 10_000:
        return f"{value / 10_000:.0f}万元"
    return f"{value:.0f}元"


def parse_shfe_copper_warrant(payload: dict[str, Any]) -> float | None:
    rows = payload.get("o_cursor") or payload.get("o_warrant") or payload.get("data") or []
    candidates: list[tuple[bool, float]] = []
    for row in rows:
        product = str(
            row.get("VARNAME") or row.get("PRODUCTNAME") or row.get("PRODUCTID") or ""
        ).strip().lower()
        if not ("铜" in product or product in {"cu", "copper"}):
            continue
        raw = (
            row.get("WRTWGHTS") or row.get("WARRANTS") or row.get("WRTWGHT")
            or row.get("WARRANTQTY")
        )
        try:
            value = float(str(raw).replace(",", ""))
        except (TypeError, ValueError):
            continue
        label = " ".join(str(row.get(key) or "") for key in ("REGNAME", "WHABBRNAME", "WHNAME"))
        candidates.append(("总计" in label or "合计" in label, value))
    totals = [value for is_total, value in candidates if is_total]
    if totals:
        return max(totals)
    values = [value for _, value in candidates]
    return sum(values) if values else None


def parse_shfe_option_contracts(
    payload: dict[str, Any], product_group: str = "cu"
) -> list[OptionContractSnapshot]:
    result: list[OptionContractSnapshot] = []
    for row in payload.get("o_curinstrument", []) or []:
        product = str(row.get("PRODUCTGROUPID") or "").strip().lower()
        product_id = str(row.get("PRODUCTID") or "").strip().lower()
        if product != product_group.lower() and product_id != f"{product_group.lower()}_o":
            continue
        instrument_id = str(row.get("INSTRUMENTID") or "").strip()
        underlying_id = str(row.get("UNDERLYINGINSTRID") or "").strip().lower()
        raw_type = str(row.get("OPTIONSTYPE") or "").strip().upper()
        option_type = "call" if raw_type in {"1", "C", "CALL"} else (
            "put" if raw_type in {"2", "P", "PUT"} else ""
        )
        if not option_type:
            compact = instrument_id.replace("-", "")
            match = re.search(r"[CP]", compact.upper())
            if match:
                option_type = "call" if match.group(0) == "C" else "put"
        strike = _coerce_number(row.get("STRIKEPRICE"))
        settlement = _coerce_number(row.get("SETTLEMENTPRICE"))
        previous = _coerce_number(row.get("PRESETTLEMENTPRICE"))
        if (
            not instrument_id
            or not underlying_id
            or not option_type
            or strike is None
            or settlement is None
            or previous is None
            or strike <= 0
            or settlement <= 0
            or previous <= 0
        ):
            continue
        delta = _coerce_number(row.get("DELTA"))
        result.append(OptionContractSnapshot(
            instrument_id=instrument_id,
            underlying_id=underlying_id,
            option_type=option_type,
            strike=strike,
            settlement=settlement,
            previous_settlement=previous,
            volume=max(_coerce_number(row.get("VOLUME")) or 0.0, 0.0),
            open_interest=max(_coerce_number(row.get("OPENINTEREST")) or 0.0, 0.0),
            open_interest_change=_coerce_number(row.get("OPENINTERESTCHG")) or 0.0,
            delta=delta,
        ))
    return result


def parse_shfe_option_expiries(payload: dict[str, Any]) -> dict[str, date]:
    expiries: dict[str, date] = {}
    for row in payload.get("OptionContractBaseInfo", []) or []:
        instrument_id = str(row.get("INSTRUMENTID") or "").strip()
        raw_expiry = str(row.get("EXPIREDATE") or "").strip()
        if not instrument_id or not re.fullmatch(r"\d{8}", raw_expiry):
            continue
        try:
            expiries[instrument_id] = datetime.strptime(raw_expiry, "%Y%m%d").date()
        except ValueError:
            continue
    return expiries


def analyze_shfe_option_chain(
    *,
    option_payload: dict[str, Any],
    future_payload: dict[str, Any],
    contract_payload: dict[str, Any],
    source_date: date,
    as_of: datetime,
    settings: dict[str, Any],
    source_url: str = "",
    futures_product: str = "cu",
    commodity_label: str = "沪铜",
) -> CommodityOptionEvidence:
    product = futures_product.lower()
    contracts = parse_shfe_option_contracts(option_payload, product)
    if not contracts:
        raise EvidenceSourceError(f"上期所期权日行情缺少有效{commodity_label}合约")
    expiries = parse_shfe_option_expiries(contract_payload)
    futures = _shfe_future_settlements(future_payload, product)
    if not futures:
        raise EvidenceSourceError(f"上期所日行情缺少{commodity_label}标的期货结算价")

    min_days = int(settings.get("min_days_to_expiry", 7))
    max_days = int(settings.get("max_days_to_expiry", 90))
    max_moneyness = float(settings.get("max_moneyness_ratio", 0.12))
    min_volume = float(settings.get("min_volume", 5))
    min_open_interest = float(settings.get("min_open_interest", 50))
    minimum_pairs = int(settings.get("minimum_paired_strikes", 3))
    rate = float(settings.get("risk_free_rate", 0.015))

    grouped: dict[str, list[OptionContractSnapshot]] = {}
    for contract in contracts:
        grouped.setdefault(contract.underlying_id, []).append(contract)
    candidates: list[dict[str, Any]] = []
    partial_counts: list[tuple[str, int]] = []
    for underlying_id, rows in grouped.items():
        future = futures.get(underlying_id)
        if not future:
            continue
        expiry_values = [expiries.get(row.instrument_id) for row in rows]
        expiry = next((value for value in expiry_values if value is not None), None)
        if expiry is None:
            expiry = _fallback_shfe_option_expiry(underlying_id, product)
        if expiry is None:
            continue
        days_to_expiry = (expiry - source_date).days
        if days_to_expiry < min_days or days_to_expiry > max_days:
            continue
        future_settlement, previous_future = future
        liquid = [
            row for row in rows
            if abs(row.strike / future_settlement - 1) <= max_moneyness
            and row.volume >= min_volume
            and row.open_interest >= min_open_interest
        ]
        by_strike: dict[float, dict[str, OptionContractSnapshot]] = {}
        for row in liquid:
            by_strike.setdefault(row.strike, {})[row.option_type] = row
        pairs = [
            (strike, pair["call"], pair["put"])
            for strike, pair in by_strike.items()
            if "call" in pair and "put" in pair
        ]
        partial_counts.append((underlying_id, len(pairs)))
        if len(pairs) < minimum_pairs:
            continue
        candidates.append({
            "underlying_id": underlying_id,
            "future": future_settlement,
            "previous_future": previous_future,
            "expiry": expiry,
            "days_to_expiry": days_to_expiry,
            "pairs": sorted(pairs),
            "liquid": liquid,
        })

    if not candidates:
        best_underlying, best_count = max(partial_counts, key=lambda value: value[1], default=("CU", 0))
        summary = (
            f"{commodity_label}期权链不完整：{best_underlying.upper()}仅{best_count}组近月近ATM双边合约满足流动性；"
            f"仅作数据提示，不影响{commodity_label}期货产业门控"
        )
        return CommodityOptionEvidence(
            status="partial",
            view="unavailable",
            summary=summary,
            metrics={"underlying": best_underlying.upper(), "paired_strikes": best_count},
            item=EvidenceItem(
                key=f"shfe-{product}-options-{source_date.isoformat()}-partial",
                label=f"上期所{commodity_label}期权链辅助",
                source="上海期货交易所",
                source_url=source_url,
                observed_at=datetime.combine(source_date, datetime.min.time(), as_of.tzinfo).replace(hour=16),
                direction=0,
                strength=0,
                summary=summary,
                freshness="fresh",
                fact_type="commodity_option_context",
            ),
        )

    candidates.sort(key=lambda value: (value["days_to_expiry"], value["underlying_id"]))
    selected = candidates[0]
    maturity_metrics = _option_maturity_metrics(selected, source_date, rate)
    next_metrics = (
        _option_maturity_metrics(candidates[1], source_date, rate)
        if len(candidates) > 1 else {}
    )
    term_structure = None
    if maturity_metrics.get("atm_iv") is not None and next_metrics.get("atm_iv") is not None:
        term_structure = float(next_metrics["atm_iv"]) - float(maturity_metrics["atm_iv"])
    metrics = {
        **maturity_metrics,
        "source_date": source_date.isoformat(),
        "underlying": str(selected["underlying_id"]).upper(),
        "expiry": selected["expiry"].isoformat(),
        "days_to_expiry": selected["days_to_expiry"],
        "paired_strikes": len(selected["pairs"]),
        "term_structure": term_structure,
        "next_underlying": (
            str(candidates[1]["underlying_id"]).upper() if len(candidates) > 1 else None
        ),
        "iv_method": "Black-76 proxy",
        "raw_premium_is_underlying_return": False,
    }
    view = _commodity_option_view(metrics, settings)
    metrics["view"] = view
    age = (as_of.date() - source_date).days
    max_age = int(settings.get("max_age_calendar_days", 4))
    status = "fresh" if 0 <= age <= max_age else "stale"
    summary = _commodity_option_summary(metrics, view, commodity_label)
    item = EvidenceItem(
        key=f"shfe-{product}-options-{source_date.isoformat()}-{selected['underlying_id']}",
        label=f"上期所{commodity_label}期权链辅助",
        source="上海期货交易所",
        source_url=source_url,
        observed_at=datetime.combine(source_date, datetime.min.time(), as_of.tzinfo).replace(hour=16),
        direction=0,
        strength=0,
        summary=summary,
        freshness=status,
        fact_type="commodity_option_context",
    )
    return CommodityOptionEvidence(
        status=status,
        view=view,
        summary=summary,
        metrics=metrics,
        item=item,
    )


def _shfe_future_settlements(
    payload: dict[str, Any], product_group: str
) -> dict[str, tuple[float, float]]:
    result: dict[str, tuple[float, float]] = {}
    for row in payload.get("o_curinstrument", []) or []:
        if str(row.get("PRODUCTGROUPID") or "").lower() != product_group.lower():
            continue
        month = str(row.get("DELIVERYMONTH") or "").strip()
        settlement = _coerce_number(row.get("SETTLEMENTPRICE"))
        previous = _coerce_number(row.get("PRESETTLEMENTPRICE"))
        if month.isdigit() and settlement and previous and settlement > 0 and previous > 0:
            result[f"{product_group.lower()}{month}"] = (settlement, previous)
    return result


def _fallback_shfe_option_expiry(underlying_id: str, product_group: str = "cu") -> date | None:
    prefix = product_group.lower()
    match = re.fullmatch(rf"{re.escape(prefix)}(\d{{2}})(\d{{2}})", underlying_id.lower())
    if not match:
        return None
    year = 2000 + int(match.group(1))
    month = int(match.group(2)) - 1
    if month == 0:
        year -= 1
        month = 12
    day = calendar.monthrange(year, month)[1]
    candidate = date(year, month, day)
    remaining = 5
    while remaining:
        if candidate.weekday() < 5:
            remaining -= 1
            if remaining == 0:
                return candidate
        candidate -= timedelta(days=1)
    return None


def _option_maturity_metrics(
    candidate: dict[str, Any], source_date: date, rate: float
) -> dict[str, Any]:
    future = float(candidate["future"])
    previous_future = float(candidate["previous_future"])
    expiry = candidate["expiry"]
    time_to_expiry = max((expiry - source_date).days, 1) / 365.0
    previous_time = time_to_expiry + 1 / 365.0
    pairs = candidate["pairs"]
    atm_strike, atm_call, atm_put = min(pairs, key=lambda row: abs(row[0] - future))
    call_iv = _black76_implied_vol(
        atm_call.settlement, future, atm_strike, time_to_expiry, rate, "call"
    )
    put_iv = _black76_implied_vol(
        atm_put.settlement, future, atm_strike, time_to_expiry, rate, "put"
    )
    previous_call_iv = _black76_implied_vol(
        atm_call.previous_settlement,
        previous_future,
        atm_strike,
        previous_time,
        rate,
        "call",
    )
    previous_put_iv = _black76_implied_vol(
        atm_put.previous_settlement,
        previous_future,
        atm_strike,
        previous_time,
        rate,
        "put",
    )
    atm_iv = _mean_available(call_iv, put_iv)
    previous_atm_iv = _mean_available(previous_call_iv, previous_put_iv)
    atm_iv_change = (
        atm_iv - previous_atm_iv
        if atm_iv is not None and previous_atm_iv is not None else None
    )
    liquid = candidate["liquid"]
    calls = [row for row in liquid if row.option_type == "call"]
    puts = [row for row in liquid if row.option_type == "put"]
    call_volume = sum(row.volume for row in calls)
    put_volume = sum(row.volume for row in puts)
    call_oi = sum(row.open_interest for row in calls)
    put_oi = sum(row.open_interest for row in puts)
    call_oi_change = sum(row.open_interest_change for row in calls)
    put_oi_change = sum(row.open_interest_change for row in puts)
    call_premium_change = _weighted_option_change(calls)
    put_premium_change = _weighted_option_change(puts)

    call_25 = min(
        (row for row in calls if row.delta is not None),
        key=lambda row: abs(abs(float(row.delta)) - 0.25),
        default=None,
    )
    put_25 = min(
        (row for row in puts if row.delta is not None),
        key=lambda row: abs(abs(float(row.delta)) - 0.25),
        default=None,
    )
    call_25_iv = (
        _black76_implied_vol(
            call_25.settlement, future, call_25.strike, time_to_expiry, rate, "call"
        ) if call_25 else None
    )
    put_25_iv = (
        _black76_implied_vol(
            put_25.settlement, future, put_25.strike, time_to_expiry, rate, "put"
        ) if put_25 else None
    )
    skew_25d = (
        put_25_iv - call_25_iv
        if put_25_iv is not None and call_25_iv is not None else None
    )
    return {
        "future_settlement": future,
        "future_change": future / previous_future - 1,
        "atm_strike": atm_strike,
        "atm_iv": atm_iv,
        "previous_atm_iv": previous_atm_iv,
        "atm_iv_change": atm_iv_change,
        "skew_25d": skew_25d,
        "put_call_volume_ratio": put_volume / call_volume if call_volume > 0 else None,
        "put_call_open_interest_ratio": put_oi / call_oi if call_oi > 0 else None,
        "call_premium_change": call_premium_change,
        "put_premium_change": put_premium_change,
        "call_open_interest_change_ratio": (
            call_oi_change / max(call_oi - call_oi_change, 1.0)
        ),
        "put_open_interest_change_ratio": (
            put_oi_change / max(put_oi - put_oi_change, 1.0)
        ),
        "call_price_open_interest_sync": bool(
            call_premium_change is not None
            and call_premium_change > 0
            and call_oi_change > 0
        ),
        "put_price_open_interest_sync": bool(
            put_premium_change is not None
            and put_premium_change > 0
            and put_oi_change > 0
        ),
        "call_volume": call_volume,
        "put_volume": put_volume,
        "call_open_interest": call_oi,
        "put_open_interest": put_oi,
    }


def _black76_implied_vol(
    price: float,
    future: float,
    strike: float,
    time_to_expiry: float,
    rate: float,
    option_type: str,
) -> float | None:
    if min(price, future, strike, time_to_expiry) <= 0:
        return None
    discount = math.exp(-rate * time_to_expiry)
    intrinsic = discount * max(
        future - strike if option_type == "call" else strike - future,
        0.0,
    )
    upper = discount * (future if option_type == "call" else strike)
    if price < intrinsic - 1e-8 or price >= upper:
        return None
    normal = NormalDist()

    def model(volatility: float) -> float:
        sigma_root = volatility * math.sqrt(time_to_expiry)
        if sigma_root <= 0:
            return intrinsic
        d1 = (math.log(future / strike) + 0.5 * volatility * volatility * time_to_expiry) / sigma_root
        d2 = d1 - sigma_root
        if option_type == "call":
            return discount * (future * normal.cdf(d1) - strike * normal.cdf(d2))
        return discount * (strike * normal.cdf(-d2) - future * normal.cdf(-d1))

    low, high = 0.0001, 5.0
    if model(high) < price:
        return None
    for _ in range(80):
        mid = (low + high) / 2
        if model(mid) < price:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def _weighted_option_change(rows: list[OptionContractSnapshot]) -> float | None:
    weighted = [
        (row.settlement / row.previous_settlement - 1, max(row.open_interest, row.volume, 1.0))
        for row in rows
        if row.previous_settlement > 0
    ]
    total_weight = sum(weight for _, weight in weighted)
    return (
        sum(change * weight for change, weight in weighted) / total_weight
        if total_weight > 0 else None
    )


def _mean_available(*values: float | None) -> float | None:
    present = [float(value) for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _commodity_option_view(metrics: dict[str, Any], settings: dict[str, Any]) -> str:
    iv_change = metrics.get("atm_iv_change")
    call_premium = metrics.get("call_premium_change")
    put_premium = metrics.get("put_premium_change")
    call_oi = metrics.get("call_open_interest_change_ratio")
    put_oi = metrics.get("put_open_interest_change_ratio")
    pcr_oi = metrics.get("put_call_open_interest_ratio")
    skew = metrics.get("skew_25d")
    future_change = metrics.get("future_change")
    vol_threshold = float(settings.get("volatility_expansion_threshold", 0.03))
    pcr_low = float(settings.get("put_call_low", 0.70))
    pcr_high = float(settings.get("put_call_high", 1.30))
    call_sync = bool(call_premium is not None and call_premium > 0.10 and call_oi > 0)
    put_sync = bool(put_premium is not None and put_premium > 0.10 and put_oi > 0)
    if iv_change is not None and iv_change >= vol_threshold and call_sync and put_sync:
        return "volatility_expansion"
    if (
        future_change is not None and future_change > 0
        and call_sync
        and ((pcr_oi is not None and pcr_oi <= pcr_low) or (skew is not None and skew < 0))
    ):
        return "upside_demand"
    if put_sync and (
        (pcr_oi is not None and pcr_oi >= pcr_high) or (skew is not None and skew > 0)
    ):
        return "downside_hedging"
    if call_sync or put_sync or (iv_change is not None and abs(iv_change) >= vol_threshold):
        return "mixed"
    return "balanced"


def _commodity_option_summary(
    metrics: dict[str, Any], view: str, commodity_label: str = "沪铜"
) -> str:
    labels = {
        "balanced": "期权结构均衡",
        "upside_demand": "认购需求与标的期货同向",
        "downside_hedging": "认沽保护需求偏强",
        "volatility_expansion": "双边波动率扩张",
        "mixed": "期权结构信号混合",
    }
    iv = metrics.get("atm_iv")
    iv_change = metrics.get("atm_iv_change")
    skew = metrics.get("skew_25d")
    pcr_volume = metrics.get("put_call_volume_ratio")
    pcr_oi = metrics.get("put_call_open_interest_ratio")
    parts = [
        f"上期所{metrics.get('underlying', commodity_label.upper())}期权（{metrics.get('source_date', '日期暂缺')}日终）",
        f"剩余{metrics.get('days_to_expiry', '?')}天",
        f"近ATM双边{metrics.get('paired_strikes', 0)}组",
    ]
    if iv is not None:
        change_text = f"、较前日{iv_change * 100:+.1f}波动率点" if iv_change is not None else ""
        parts.append(f"ATM代理IV {iv:.1%}{change_text}")
    if skew is not None:
        parts.append(f"25Δ认沽-认购偏度{skew * 100:+.1f}波动率点")
    if pcr_volume is not None or pcr_oi is not None:
        volume_text = f"{pcr_volume:.2f}" if pcr_volume is not None else "—"
        oi_text = f"{pcr_oi:.2f}" if pcr_oi is not None else "—"
        parts.append(f"Put/Call成交量比{volume_text}、持仓量比{oi_text}")
    term_structure = metrics.get("term_structure")
    if term_structure is not None:
        term_label = "远月隐波高于近月" if term_structure >= 0 else "近月隐波高于远月"
        parts.append(f"期限结构：{term_label}{abs(term_structure) * 100:.1f}波动率点")
    sync_labels = []
    if metrics.get("call_price_open_interest_sync"):
        sync_labels.append("认购价格-持仓同步增强")
    if metrics.get("put_price_open_interest_sync"):
        sync_labels.append("认沽价格-持仓同步增强")
    if sync_labels:
        parts.append("、".join(sync_labels))
    parts.append(labels.get(view, "期权辅助不可判定"))
    parts.append("权利金涨跌不等同商品期货或个股涨跌，仅作辅助观察")
    return "；".join(parts)


analyze_shfe_copper_option_chain = analyze_shfe_option_chain


def parse_margin_observations(
    payload: Any,
) -> list[tuple[date, float, float, float]]:
    """Normalize SSE/SZSE margin-detail JSON into dated observations.

    Exchange response envelopes and field names have changed historically, so the
    parser deliberately accepts only a small set of known aliases while walking
    nested response containers. Rows without a valid date or financing balance are
    discarded rather than imputed.
    """
    date_keys = ("opDate", "jyrq", "交易日期", "tradeDate", "TRD_DT", "rq")
    balance_keys = ("rzye", "融资余额", "FIN_VAL", "RZYE")
    buy_keys = ("rzmre", "融资买入额", "融资买入金额", "FIN_BUY_VAL", "RZMRE")
    repay_keys = ("rzche", "融资偿还额", "融资偿还金额", "FIN_RPAY_VAL", "RZCHE")

    rows: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if any(key in value for key in date_keys) and any(
                key in value for key in balance_keys
            ):
                rows.append(value)
            for child in value.values():
                if isinstance(child, (dict, list, tuple)):
                    walk(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child)

    def pick(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in row and row[key] not in (None, "", "--", "-"):
                return row[key]
        return None

    def parse_day(raw: Any) -> date | None:
        text = str(raw or "").strip()[:10]
        for format_ in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text, format_).date()
            except ValueError:
                continue
        return None

    def number(raw: Any, default: float | None = None) -> float | None:
        if raw in (None, "", "--", "-"):
            return default
        try:
            return float(str(raw).replace(",", "").strip())
        except (TypeError, ValueError):
            return default

    walk(payload)
    by_day: dict[date, tuple[date, float, float, float]] = {}
    for row in rows:
        observed = parse_day(pick(row, date_keys))
        balance = number(pick(row, balance_keys))
        if observed is None or balance is None or balance < 0:
            continue
        buy = number(pick(row, buy_keys), 0.0) or 0.0
        repay = number(pick(row, repay_keys), 0.0) or 0.0
        by_day[observed] = (observed, balance, max(buy, 0.0), max(repay, 0.0))
    return [by_day[key] for key in sorted(by_day)]


def _nested_dicts(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if isinstance(value, dict):
        result.append(value)
        for child in value.values():
            if isinstance(child, (dict, list, tuple)):
                result.extend(_nested_dicts(child))
    elif isinstance(value, (list, tuple)):
        for child in value:
            result.extend(_nested_dicts(child))
    return result


def _coerce_number(value: Any) -> float | None:
    if value in (None, "", "--", "-"):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def classify_operating_text(text: str, threshold_pct: float = 3.0) -> tuple[int | None, str]:
    clean = _clean_text(text)
    if not clean:
        return None, ""
    positive_metrics = (
        "营业收入", "保费收入", "收入", "净利润", "利润", "新业务价值",
        "内含价值", "经营现金流", "现金流",
    )
    negative_metrics = (
        "营业成本", "赔付支出", "综合成本率", "成本", "费用", "负债",
        "不良贷款", "不良率", "减值损失",
    )
    pattern = re.compile(
        r"([\u4e00-\u9fffA-Za-z0-9（）()、]{1,24}?)"
        r"同比\s*(增长|增加|提升|上升|下降|减少|下滑|降低)\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*%"
    )
    improvements: list[tuple[str, float]] = []
    deteriorations: list[tuple[str, float]] = []
    for context, verb, raw_value in pattern.findall(clean):
        value = float(raw_value)
        if value < threshold_pct:
            continue
        positive_metric = next(
            (metric for metric in positive_metrics if metric in context), None
        )
        negative_metric = next(
            (metric for metric in negative_metrics if metric in context), None
        )
        if positive_metric is None and negative_metric is None:
            continue
        increased = verb in {"增长", "增加", "提升", "上升"}
        improvement = increased if positive_metric else not increased
        target = improvements if improvement else deteriorations
        target.append((positive_metric or negative_metric or "经营指标", value))
    if improvements and not deteriorations:
        metric, value = max(improvements, key=lambda item: item[1])
        return 1, f"公司经营公告正文识别到{metric}同比改善{value:.2f}%"
    if deteriorations and not improvements:
        metric, value = max(deteriorations, key=lambda item: item[1])
        return -1, f"公司经营公告正文识别到{metric}同比走弱{value:.2f}%"
    if improvements or deteriorations:
        return 0, "公司经营公告正文同时存在改善与走弱指标，方向混合"
    return 0, "公司经营公告正文未识别出可解释且达到阈值的同比变化"


def _parse_announcement_datetime(row: dict[str, Any], tz: ZoneInfo) -> datetime:
    raw = str(row.get("ADDDATE") or row.get("publishTime") or row.get("publishDate") or "").strip()
    for format_ in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, format_).replace(tzinfo=tz)
        except ValueError:
            continue
    raw_date = str(row.get("SSEDATE") or row.get("SSEDate") or row.get("publishDate") or "").strip()
    try:
        return datetime.strptime(raw_date, "%Y-%m-%d").replace(tzinfo=tz)
    except ValueError as exc:
        raise EvidenceSourceError(f"公告时间无法识别: {raw or raw_date}") from exc


# Backwards-compatible private helper used by existing integrations/tests.
_parse_sse_datetime = _parse_announcement_datetime


def _highest_announcement_risk(items: list[EvidenceItem]) -> str:
    labels = {item.label.rsplit("/", 1)[-1] for item in items}
    if "critical" in labels:
        return "critical"
    if "caution" in labels:
        return "caution"
    return "none"


def _announcement_summary(items: list[EvidenceItem], status: str) -> str:
    if status == "not_applicable":
        return "指数ETF不适用个股经营公告门控"
    if status != "fresh":
        return "公告查询不可用"
    risk = _highest_announcement_risk(items)
    if risk == "critical":
        title = next(item.summary for item in items if item.label.endswith("/critical"))
        return f"公告标题高风险：{title}"
    if risk == "caution":
        title = next(item.summary for item in items if item.label.endswith("/caution"))
        return f"公告标题需复核：{title}"
    company_items = [item for item in items if item.fact_type == "company_operating_metric"]
    if company_items:
        return "近期公告标题未识别出风险词，已解析可识别经营公告正文"
    return "近期公告标题未识别出风险词；经营公告正文暂无可结构化结论"


def _corporate_action_item_state(item: EvidenceItem) -> tuple[str | None, str]:
    parts = item.label.split("/")
    if len(parts) >= 4 and parts[0] == "正向公司行动":
        return parts[2], parts[3]
    return None, "missing"


def _corporate_action_summary(
    item: EvidenceItem | None, announcement_status: str, is_etf: bool
) -> str:
    if is_etf:
        return "指数ETF不适用个股正向公司行动证据"
    if announcement_status != "fresh":
        return "正向公司行动公告查询不可用"
    if item is None:
        return "近期未识别出可结构化的正向公司行动"
    return item.summary


def _aggregate_direction(items: list[EvidenceItem]) -> int | None:
    if not items:
        return None
    directions = [item.direction for item in items]
    if any(direction < 0 for direction in directions):
        return -1
    if any(direction > 0 for direction in directions):
        return 1
    return 0


def _company_summary(items: list[EvidenceItem]) -> str:
    if not items:
        return "公司经营指标正文暂无可结构化结论"
    return "公司经营：" + "；".join(item.summary for item in items[:2])


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _is_cash_dividend_implementation_title(title: str) -> bool:
    clean = _clean_text(title)
    if "实施" not in clean:
        return False
    if any(word in clean for word in ("预案", "提议", "董事会", "股东大会", "终止")):
        return False
    return any(word in clean for word in ("权益分派", "利润分配", "现金红利", "分红派息"))


def _cash_dividend_key(event: dict[str, Any]) -> str:
    return "|".join((
        str(event.get("record_date", "")),
        str(event.get("ex_date", "")),
        f"{float(event.get('cash_per_share', 0) or 0):.6f}",
    ))


def _parse_cn_date(value: str) -> date | None:
    clean = _clean_text(value).replace("年", "-").replace("月", "-").replace("日", "")
    match = re.search(r"(20\d{2})\s*[-/]\s*(\d{1,2})\s*[-/]\s*(\d{1,2})", clean)
    if not match:
        return None
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError:
        return None


def _dividend_date_after(text: str, labels: tuple[str, ...]) -> date | None:
    for label in labels:
        match = re.search(
            rf"{re.escape(label)}\s*(?:为|是|：|:)?\s*"
            r"(20\d{2}\s*(?:年|[-/])\s*\d{1,2}\s*(?:月|[-/])\s*\d{1,2}\s*日?)",
            text,
        )
        if match:
            parsed = _parse_cn_date(match.group(1))
            if parsed is not None:
                return parsed
    return None


def parse_cash_dividend_implementation(text: str) -> dict[str, Any]:
    """Parse settlement terms from an exchange-hosted implementation notice.

    The parser is intentionally strict: all three dates and the per-share cash
    term have to be explicit.  This is a bookkeeping parser, not a prediction
    model, so ambiguous documents simply yield no event.
    """
    clean = _clean_text(text).replace("（", "(").replace("）", ")")
    record_date = _dividend_date_after(clean, ("股权登记日",))
    ex_date = _dividend_date_after(clean, ("除权(息)日", "除权除息日", "除息日", "除权日"))
    payment_date = _dividend_date_after(
        clean,
        ("现金红利发放日", "现金股利发放日", "红利发放日", "派息日"),
    )
    cash_per_share: float | None = None
    per_ten = re.search(
        r"每\s*10\s*股(?:派发|派送|派现|分配)\s*(?:现金红利|现金股利|现金)?(?:人民币)?\s*"
        r"([0-9]+(?:\.[0-9]+)?)\s*元",
        clean,
    )
    if per_ten:
        cash_per_share = float(per_ten.group(1)) / 10
    else:
        per_share = re.search(
            r"每\s*股(?:派发|派送|派现|分配)?\s*(?:现金红利|现金股利|现金)?(?:人民币)?\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*元",
            clean,
        )
        if per_share:
            cash_per_share = float(per_share.group(1))
    if (
        record_date is None
        or ex_date is None
        or payment_date is None
        or cash_per_share is None
        or cash_per_share <= 0
        or ex_date < record_date
        or payment_date < ex_date
    ):
        raise EvidenceSourceError("分红实施公告缺少可核验的日期或每股现金额")
    return {
        "record_date": record_date.isoformat(),
        "ex_date": ex_date.isoformat(),
        "payment_date": payment_date.isoformat(),
        "cash_per_share": round(cash_per_share, 8),
    }


def _html_text(value: str) -> str:
    clean = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", value, flags=re.S | re.I)
    clean = re.sub(r"<[^>]+>", " ", clean)
    return _clean_text(clean)


def _is_etf(position: Position) -> bool:
    code = position.symbol.split(".")[0]
    return "ETF" in position.name.upper() or code.startswith(("15", "51", "56", "58"))
