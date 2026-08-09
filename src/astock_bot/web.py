from __future__ import annotations

import json
import hashlib
import os
import secrets
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import _expand
from .onboarding import StockOnboardingService, sector_options
from .portfolio_store import PortfolioStore, PortfolioStoreError


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["asset_version"] = hashlib.sha256(
    (APP_DIR / "static" / "styles.css").read_bytes()
).hexdigest()[:12]
app = FastAPI(title="A股持仓纪律", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


def _config_path() -> Path:
    return Path(os.getenv("ASTOCK_CONFIG", "config.yaml")).resolve()


def _raw_config() -> dict[str, Any]:
    path = _config_path()
    with path.open("r", encoding="utf-8") as handle:
        return _expand(yaml.safe_load(handle) or {})


def _store(raw: dict[str, Any]) -> PortfolioStore:
    store = PortfolioStore.from_config(_config_path(), raw)
    if store is None:
        raise RuntimeError("请先在portfolio下配置 database_path")
    return store


def _audit_records(raw: dict[str, Any], limit: int = 120) -> list[dict[str, Any]]:
    value = str(raw.get("log_file", "data/events.jsonl"))
    path = Path(value)
    if not path.is_absolute():
        path = _config_path().parent / path
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(records))


def _latest_summary(records: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in records:
        if record.get("node") == "15:30" and record.get("summaries"):
            return record
    return None


def _review_records(
    records: list[dict[str, Any]],
    positions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Normalize machine-readable node records for the review UI.

    Formal nodes store ``symbol/status/price/levels`` summaries, while the
    15:30 record stores formatted ``recommendation/reason`` rows.  The old
    template assumed the latter shape for every record, so ordinary nodes
    rendered empty bordered rows even though their data was present.
    """
    name_by_symbol = {
        str(position.get("symbol", "")): str(position.get("name") or position.get("symbol", ""))
        for position in positions
    }
    status_labels = {
        "BASELINE": "基线已更新",
        "NO_ALERT": "观察：未触发完整动作",
        "ALERT": "本节点有纪律建议",
        "DATA_MISSING": "数据缺失：暂不操作",
        "STALE": "行情延迟：暂不操作",
        "STALE_TECH": "技术数据不足：暂不操作",
    }

    def fmt_change(value: Any) -> str:
        try:
            return f"日内涨跌 {float(value):+.2f}%"
        except (TypeError, ValueError):
            return "日内涨跌暂无"

    def compact_reason(row: dict[str, Any]) -> str:
        parts: list[str] = []
        if row.get("price") is not None:
            try:
                parts.append(f"现价 {float(row['price']):.2f}")
            except (TypeError, ValueError):
                pass
        parts.append(fmt_change(row.get("change_pct")))
        levels: list[str] = []
        for label, key in (("支撑", "support"), ("压力", "resistance"), ("VWAP", "vwap")):
            value = row.get(key)
            if value is not None:
                try:
                    levels.append(f"{label} {float(value):.2f}")
                except (TypeError, ValueError):
                    continue
        if levels:
            parts.append("/".join(levels))
        for label, key in (("同行均值", "peer_change_pct"), ("市场均值", "market_change_pct")):
            value = row.get(key)
            if value is not None:
                try:
                    parts.append(f"{label} {float(value):+.2f}%")
                except (TypeError, ValueError):
                    continue
        status = str(row.get("status", ""))
        if status in {"DATA_MISSING", "STALE", "STALE_TECH"}:
            parts.append(status_labels.get(status, "关键数据不足"))
        elif status in {"NO_ALERT", "BASELINE"}:
            parts.append("技术、量能与外部证据尚未同时形成可执行动作")
        return "；".join(parts)

    normalized: list[dict[str, Any]] = []
    for original in records:
        record = dict(original)
        signals = [item for item in record.get("signals", []) if isinstance(item, dict)]
        signal_by_symbol: dict[str, dict[str, Any]] = {}
        for signal in signals:
            symbol = str(signal.get("symbol", ""))
            if symbol:
                signal_by_symbol[symbol] = signal
        rows: list[dict[str, Any]] = []
        for raw_row in record.get("summaries", []) or []:
            if not isinstance(raw_row, dict):
                continue
            row = dict(raw_row)
            symbol = str(row.get("symbol", ""))
            signal = signal_by_symbol.get(symbol)
            name = str(row.get("name") or name_by_symbol.get(symbol) or symbol or "未知标的")
            if row.get("recommendation") or row.get("reason"):
                recommendation = str(row.get("recommendation") or status_labels.get(str(row.get("status", "")), "复盘记录"))
                reason = str(row.get("reason") or compact_reason(row))
            elif signal:
                shares = int(signal.get("shares", 0) or 0)
                recommendation = str(signal.get("action") or "纪律建议")
                if shares:
                    recommendation += f" {shares}股"
                reason = str(signal.get("reason") or compact_reason(row))
            else:
                recommendation = status_labels.get(str(row.get("status", "")), "复盘记录")
                reason = compact_reason(row)
            rows.append({
                "name": name,
                "symbol": symbol,
                "recommendation": recommendation,
                "reason": reason,
                "status": str(row.get("status", "")),
                "price": row.get("price"),
                "change_pct": row.get("change_pct"),
            })
        if not rows:
            for signal in signals:
                shares = int(signal.get("shares", 0) or 0)
                action = str(signal.get("action") or "纪律建议")
                if shares:
                    action += f" {shares}股"
                rows.append({
                    "name": str(signal.get("name") or name_by_symbol.get(str(signal.get("symbol", ""))) or signal.get("symbol", "")),
                    "symbol": str(signal.get("symbol", "")),
                    "recommendation": action,
                    "reason": str(signal.get("reason") or ""),
                    "status": "ALERT",
                    "price": signal.get("price"),
                    "change_pct": (signal.get("details") or {}).get("change_pct"),
                })
        record["review_rows"] = rows
        record["review_count"] = len(rows)
        record["signal_count"] = len(signals)
        normalized.append(record)
    return normalized


def _context(request: Request, raw: dict[str, Any], store: PortfolioStore, **extra: Any) -> dict[str, Any]:
    snapshot = store.snapshot(raw)
    positions = snapshot["positions"]
    holdings = [item for item in positions if item.get("role") == "holding"]
    dividend_events = [
        {"symbol": item["symbol"], "name": item["name"], **event}
        for item in holdings
        for event in item.get("corporate_events", [])
        if str(event.get("type", "cash_dividend")) == "cash_dividend"
    ]
    watchlist = [item for item in positions if item.get("role") == "watchlist"]
    context = {
        "request": request,
        "title": "A股持仓纪律",
        "today": date.today().isoformat(),
        "cash": snapshot["cash"],
        "positions": positions,
        "holdings": holdings,
        "dividend_events": dividend_events,
        "watchlist": watchlist,
        "supported_sectors": snapshot["supported_sectors"],
        "sector_options": sector_options(),
        "notice": request.query_params.get("notice"),
        "error": request.query_params.get("error"),
        "csrf_token": _csrf_token(request),
    }
    context.update(extra)
    if "audit_records" in context:
        context["audit_records"] = _review_records(context["audit_records"], positions)
    if context.get("latest_summary"):
        context["latest_summary"] = _review_records([context["latest_summary"]], positions)[0]
    return context


def _csrf_token(request: Request) -> str:
    """Return a per-browser token; the cookie is deliberately host-only."""
    return request.cookies.get("astock_csrf") or secrets.token_urlsafe(32)


def _render(request: Request, template: str, context: dict[str, Any]):
    response = templates.TemplateResponse(request, template, context)
    if "astock_csrf" not in request.cookies:
        response.set_cookie("astock_csrf", context["csrf_token"], httponly=True, samesite="strict")
    return response


def _verify_form(request: Request, csrf_token: str) -> None:
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin and not origin.endswith(f"//{host}"):
        raise HTTPException(status_code=403, detail="来源校验失败")
    cookie = request.cookies.get("astock_csrf")
    if not cookie or not secrets.compare_digest(cookie, csrf_token):
        raise HTTPException(status_code=403, detail="表单已过期，请刷新页面后重试")


def _redirect(path: str, *, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    query = urlencode({key: value for key, value in {"notice": notice, "error": error}.items() if value})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


@app.get("/health")
def health() -> JSONResponse:
    raw = _raw_config()
    store = _store(raw)
    snapshot = store.snapshot(raw)
    return JSONResponse({"ok": True, "positions": len(snapshot["positions"]), "cash": snapshot["cash"]})


@app.get("/")
def dashboard(request: Request):
    raw = _raw_config()
    store = _store(raw)
    records = _audit_records(raw)
    latest = _latest_summary(records)
    recent_actions = [item for item in records if item.get("signals")][:5]
    return _render(request, "dashboard.html", _context(request, raw, store, latest_summary=latest, recent_actions=recent_actions))


@app.get("/positions")
def positions(request: Request):
    raw = _raw_config()
    store = _store(raw)
    return _render(request, "positions.html", _context(request, raw, store, transactions=store.transactions()))


@app.post("/trades")
def create_trade(
    request: Request,
    csrf_token: str = Form(...),
    symbol: str = Form(...),
    bucket: str = Form(...),
    side: str = Form(...),
    shares: int = Form(...),
    price: float = Form(...),
    fee: float = Form(0),
    executed_at: str = Form(...),
    note: str = Form(""),
    entry_support: str = Form(""),
    target_price: str = Form(""),
    stop_price: str = Form(""),
):
    _verify_form(request, csrf_token)
    raw = _raw_config()
    store = _store(raw)
    try:
        store.record_trade(
            symbol=symbol,
            bucket=bucket,
            side=side,
            shares=shares,
            price=price,
            fee=fee,
            executed_at=executed_at,
            note=note,
            entry_support=_optional_number(entry_support),
            target_price=_optional_number(target_price),
            stop_price=_optional_number(stop_price),
        )
    except PortfolioStoreError as exc:
        return _redirect("/positions", error=str(exc))
    return _redirect("/positions", notice="成交已记录，持仓、现金与经济投入已同步更新")


@app.post("/dividends")
def create_dividend(
    request: Request,
    csrf_token: str = Form(...),
    symbol: str = Form(...),
    amount: float = Form(...),
    executed_at: str = Form(...),
    note: str = Form(""),
    event_id: str = Form(""),
):
    _verify_form(request, csrf_token)
    raw = _raw_config()
    store = _store(raw)
    static_events = next(
        (
            list(item.get("corporate_events", []) or [])
            for item in raw.get("portfolio", {}).get("positions", [])
            if str(item.get("symbol", "")).upper() == symbol.strip().upper()
        ),
        [],
    )
    try:
        mode = store.record_dividend(
            symbol=symbol,
            amount=amount,
            executed_at=executed_at,
            note=note,
            event_id=event_id or None,
            corporate_events=static_events,
        )
    except PortfolioStoreError as exc:
        return _redirect("/positions", error=str(exc))
    notice = "分红到账已记录；经济投入已按该分红同步更新"
    if mode == "pre_adjusted":
        notice = "分红到账已记录；该公告已在配置中预先调整经济投入，本次仅增加现金"
    return _redirect("/positions", notice=notice)


@app.post("/transactions/{transaction_id}/reverse")
def reverse_transaction(
    transaction_id: int,
    request: Request,
    csrf_token: str = Form(...),
):
    _verify_form(request, csrf_token)
    raw = _raw_config()
    try:
        _store(raw).reverse_transaction(transaction_id)
    except PortfolioStoreError as exc:
        return _redirect("/positions", error=str(exc))
    return _redirect("/positions", notice="已追加冲销记录，原始流水仍保留")


@app.get("/watchlist")
def watchlist(request: Request):
    raw = _raw_config()
    store = _store(raw)
    records = _audit_records(raw)
    onboarding = StockOnboardingService(raw.get("onboarding", {}))
    return _render(request, "watchlist.html", _context(
            request,
            raw,
            store,
            audit_records=records,
            latest_summary=_latest_summary(records),
            llm_available=onboarding.llm_available,
            llm_model=onboarding.llm_model,
        ))


@app.post("/watchlist")
def add_watchlist(
    request: Request,
    csrf_token: str = Form(...),
    symbol: str = Form(...),
    use_llm: str = Form(""),
    name: str = Form(""),
    sector: str = Form(""),
    peers: str = Form(""),
):
    _verify_form(request, csrf_token)
    raw = _raw_config()
    store = _store(raw)
    try:
        result = StockOnboardingService(raw.get("onboarding", {})).onboard(
            symbol,
            use_llm=use_llm == "on",
            manual_name=name,
            manual_sector=sector,
            manual_peers=peers,
        )
        store.add_watchlist(
            symbol=result.symbol,
            name=result.name,
            sector=result.sector,
            peers=result.peers,
            analysis_profile=result.analysis_profile,
        )
    except (PortfolioStoreError, OSError, ValueError) as exc:
        return _redirect("/watchlist", error=str(exc))
    coverage = result.analysis_profile.get("coverage_label", "已建立跟踪")
    return _redirect("/watchlist", notice=f"{result.name} 已识别并加入股票池 · {coverage}")


@app.post("/watchlist/{symbol}/remove")
def remove_watchlist(symbol: str, request: Request, csrf_token: str = Form(...)):
    _verify_form(request, csrf_token)
    raw = _raw_config()
    store = _store(raw)
    try:
        store.remove_watchlist(symbol)
    except PortfolioStoreError as exc:
        return _redirect("/watchlist", error=str(exc))
    return _redirect("/watchlist", notice="观察标的已移除")


def _optional_number(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise PortfolioStoreError("价格字段必须是有效数字") from exc
