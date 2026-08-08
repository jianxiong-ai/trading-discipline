from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import _expand
from .portfolio_store import PortfolioStore, PortfolioStoreError


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
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


def _context(request: Request, raw: dict[str, Any], store: PortfolioStore, **extra: Any) -> dict[str, Any]:
    snapshot = store.snapshot(raw)
    positions = snapshot["positions"]
    holdings = [item for item in positions if item.get("role") == "holding"]
    watchlist = [item for item in positions if item.get("role") == "watchlist"]
    context = {
        "request": request,
        "title": "A股持仓纪律",
        "today": date.today().isoformat(),
        "cash": snapshot["cash"],
        "positions": positions,
        "holdings": holdings,
        "watchlist": watchlist,
        "supported_sectors": snapshot["supported_sectors"],
        "notice": request.query_params.get("notice"),
        "error": request.query_params.get("error"),
    }
    context.update(extra)
    return context


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
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        _context(request, raw, store, latest_summary=latest, recent_actions=recent_actions),
    )


@app.get("/positions")
def positions(request: Request):
    raw = _raw_config()
    store = _store(raw)
    return templates.TemplateResponse(
        request,
        "positions.html",
        _context(request, raw, store, transactions=store.transactions()),
    )


@app.post("/trades")
def create_trade(
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
    symbol: str = Form(...),
    amount: float = Form(...),
    executed_at: str = Form(...),
    note: str = Form(""),
):
    raw = _raw_config()
    store = _store(raw)
    try:
        store.record_dividend(symbol=symbol, amount=amount, executed_at=executed_at, note=note)
    except PortfolioStoreError as exc:
        return _redirect("/positions", error=str(exc))
    return _redirect("/positions", notice="分红到账已记录，现金与经济投入已同步更新")


@app.get("/watchlist")
def watchlist(request: Request):
    raw = _raw_config()
    store = _store(raw)
    records = _audit_records(raw)
    return templates.TemplateResponse(
        request,
        "watchlist.html",
        _context(request, raw, store, audit_records=records, latest_summary=_latest_summary(records)),
    )


@app.post("/watchlist")
def add_watchlist(
    symbol: str = Form(...),
    name: str = Form(...),
    sector: str = Form("generic"),
    peers: str = Form(""),
):
    raw = _raw_config()
    store = _store(raw)
    try:
        store.add_watchlist(symbol=symbol, name=name, sector=sector, peers=peers)
    except PortfolioStoreError as exc:
        return _redirect("/watchlist", error=str(exc))
    return _redirect("/watchlist", notice="观察标的已加入；产业证据覆盖范围会在日内检查中自动校验")


@app.post("/watchlist/{symbol}/remove")
def remove_watchlist(symbol: str):
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
