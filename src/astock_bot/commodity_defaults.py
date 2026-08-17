from __future__ import annotations

from typing import Any


COMMODITY_EXPOSURE_CATALOG: dict[str, dict[str, Any]] = {
    "copper": {
        "commodity": "copper",
        "commodity_label": "铜",
        "exchange": "SHFE",
        "futures_product": "cu",
        "option_product": "cu_o",
        "industry_sector": "copper",
        "keywords": ("铜矿", "铜冶炼", "阴极铜", "铜加工", "铜业", "铜、", "铜，", "铜和"),
        "mining": ("铜矿", "矿山", "采矿", "铜精矿", "矿产资源"),
        "smelting": ("铜冶炼", "冶炼", "阴极铜", "电解铜"),
        "processing": ("铜加工", "铜杆", "铜箔", "铜板", "铜带", "线缆", "电缆"),
        "related_derivative": {
            "kind": "commodity_option",
            "exchange": "SHFE",
            "product": "CU",
            "underlying": "CU期货",
            "label": "沪铜期权",
            "role": "auxiliary",
        },
    },
    "gold": {
        "commodity": "gold",
        "commodity_label": "黄金",
        "exchange": "SHFE",
        "futures_product": "au",
        "option_product": "au_o",
        "industry_sector": "gold",
        "keywords": ("黄金", "金矿", "金冶炼", "金精矿", "产金"),
        "mining": ("金矿", "黄金开采", "黄金矿山", "金精矿"),
        "smelting": ("金冶炼", "黄金冶炼", "电解金"),
        "processing": ("黄金加工", "金饰", "珠宝"),
        "related_derivative": {
            "kind": "commodity_option",
            "exchange": "SHFE",
            "product": "AU",
            "underlying": "AU期货",
            "label": "沪金期权",
            "role": "auxiliary",
        },
    },
    "silver": {
        "commodity": "silver",
        "commodity_label": "白银",
        "exchange": "SHFE",
        "futures_product": "ag",
        "option_product": "ag_o",
        "industry_sector": "silver",
        "keywords": ("白银", "银矿", "银冶炼", "银精矿"),
        "mining": ("银矿", "白银开采", "银精矿"),
        "smelting": ("银冶炼", "白银冶炼", "电解银"),
        "processing": ("白银加工", "银饰"),
        "related_derivative": {
            "kind": "commodity_option",
            "exchange": "SHFE",
            "product": "AG",
            "underlying": "AG期货",
            "label": "沪银期权",
            "role": "auxiliary",
        },
    },
}


def default_legacy_copper_exposure() -> dict[str, Any]:
    item = COMMODITY_EXPOSURE_CATALOG["copper"]
    return {
        "commodity": "copper",
        "commodity_label": item["commodity_label"],
        "exchange": item["exchange"],
        "futures_product": item["futures_product"],
        "option_product": item["option_product"],
        "exposure_types": ["integrated_or_unknown"],
        "hedge_disclosed": False,
        "sensitivity": "历史标的需补充分部业务与套保资料后再细分铜价敏感性",
    }


def default_copper_related_derivatives() -> list[dict[str, Any]]:
    return [dict(COMMODITY_EXPOSURE_CATALOG["copper"]["related_derivative"])]


def related_derivatives_for_exposures(exposures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for exposure in exposures:
        commodity = str(exposure.get("commodity") or "")
        catalog = COMMODITY_EXPOSURE_CATALOG.get(commodity)
        if not catalog or commodity in seen:
            continue
        seen.add(commodity)
        result.append(dict(catalog["related_derivative"]))
    return result


def detect_commodity_exposures(
    text: str,
    *,
    sector: str = "",
) -> list[dict[str, Any]]:
    """Map company text to commodity exposures, independent of industry sector."""
    hedge_disclosed = any(keyword in text for keyword in ("套期保值", "套保", "衍生品交易"))
    exposures: list[dict[str, Any]] = []
    for commodity, catalog in COMMODITY_EXPOSURE_CATALOG.items():
        if not any(keyword in text for keyword in catalog["keywords"]):
            if catalog.get("industry_sector") != sector:
                continue
        exposure_types: list[str] = []
        for exposure_type in ("mining", "smelting", "processing"):
            keywords = tuple(catalog.get(exposure_type) or ())
            if any(keyword in text for keyword in keywords):
                exposure_types.append(exposure_type)
        if not exposure_types:
            exposure_types.append("integrated_or_unknown")
        exposures.append({
            "commodity": commodity,
            "commodity_label": catalog["commodity_label"],
            "exchange": catalog["exchange"],
            "futures_product": catalog["futures_product"],
            "option_product": catalog["option_product"],
            "exposure_types": exposure_types,
            "hedge_disclosed": hedge_disclosed,
            "sensitivity": _exposure_sensitivity(commodity, exposure_types),
        })
    return exposures


def _exposure_sensitivity(commodity: str, exposure_types: list[str]) -> str:
    label = COMMODITY_EXPOSURE_CATALOG[commodity]["commodity_label"]
    if exposure_types == ["mining"]:
        return f"公司偏资源端：期权/期货多头更贴近{label}价格方向，但仍需核对产量与成本"
    if exposure_types == ["smelting"]:
        if commodity == "copper":
            return "公司偏冶炼环节：期权多头需结合加工费，不宜等同铜价上涨"
        return f"公司偏冶炼环节：{label}价格上涨不能直接等同冶炼利润上涨"
    if exposure_types == ["processing"]:
        return f"公司偏加工环节：{label}价格与利润传导不同步，期权信号仅作辅助"
    return f"业务可能跨多个{label}产业链环节，需按分部盈利与套保拆解价格敏感性"
