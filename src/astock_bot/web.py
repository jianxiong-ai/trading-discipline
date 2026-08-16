from __future__ import annotations

import json
import hashlib
import logging
import os
import secrets
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

import yaml
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import _expand, load_config
from .notifier import FeishuNotifier
from .onboarding import StockOnboardingService, sector_options
from .portfolio_store import PortfolioStore, PortfolioStoreError
from .workspace import WorkspaceError, WorkspaceRegistry


APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["asset_version"] = hashlib.sha256(
    (APP_DIR / "static" / "styles.css").read_bytes()
    + (APP_DIR / "static" / "workspace.css").read_bytes()
).hexdigest()[:12]
app = FastAPI(title="A股持仓纪律", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
logger = logging.getLogger(__name__)


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


def _workspace_config(workspace_id: str):
    try:
        config = load_config(_config_path(), workspace_id)
    except WorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return config, _store(config.raw)


def _workspace_registry() -> WorkspaceRegistry:
    return WorkspaceRegistry.from_config_path(_config_path())


def _workspace(workspace_id: str):
    try:
        return _workspace_registry().get(workspace_id)
    except WorkspaceError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _workspace_is_authorized(request: Request, workspace_id: str) -> bool:
    workspace = _workspace(workspace_id)
    return _workspace_registry().has_access(
        workspace, request.cookies.get("astock_workspace_access")
    )


def _require_workspace_access(request: Request, workspace_id: str):
    workspace = _workspace(workspace_id)
    if not _workspace_registry().has_access(
        workspace, request.cookies.get("astock_workspace_access")
    ):
        raise HTTPException(status_code=403, detail="请先输入工作区访问密码")
    return workspace


def _access_context(request: Request, workspace, *, error: str | None = None) -> dict[str, Any]:
    return {
        "request": request,
        "title": "输入工作区密码",
        "workspace": workspace,
        "workspace_base": _workspace_base(workspace.id),
        "csrf_token": _csrf_token(request),
        "error": error,
    }


def _workspace_base(workspace_id: str) -> str:
    return f"/u/{workspace_id}"


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


def _context(
    request: Request, raw: dict[str, Any], store: PortfolioStore, *, workspace_id: str, **extra: Any,
) -> dict[str, Any]:
    snapshot = store.snapshot(raw)
    positions = snapshot["positions"]
    global_sizing = raw.get("position_sizing", {})
    global_target = float(global_sizing.get("target_main_weight", 0.20))
    global_single_cap = float(global_sizing.get("max_single_position_weight", 0.30))
    portfolio_single_cap = _portfolio_single_position_cap(raw)
    for item in positions:
        sizing = dict(item.get("sizing") or {})
        target_override = sizing.get("target_main_weight")
        single_cap_override = sizing.get("max_single_position_weight")
        target = global_target if target_override is None else float(target_override)
        single_cap = (
            global_single_cap
            if single_cap_override is None
            else float(single_cap_override)
        )
        item["target_main_weight_pct"] = round(target * 100, 4)
        item["target_main_weight_is_override"] = target_override is not None
        item["max_single_position_weight_pct"] = round(single_cap * 100, 4)
        item["max_single_position_weight_is_override"] = single_cap_override is not None
        item["portfolio_single_position_cap_pct"] = round(portfolio_single_cap * 100, 4)
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
        "sector_options": sector_options(),
        "notice": request.query_params.get("notice"),
        "error": request.query_params.get("error"),
        "csrf_token": _csrf_token(request),
        "workspace_id": workspace_id,
        "workspace_base": _workspace_base(workspace_id),
        "can_create_workspace": bool(raw.get("_workspace_seed_static", False)),
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
    # Avoid leaking workspace IDs in a Referer header or retaining a private
    # page in a shared browser cache.
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _render_access(request: Request, workspace, *, error: str | None = None):
    return _render(request, "workspace_access.html", _access_context(request, workspace, error=error))


def _origin_matches_request_host(origin: str, host: str) -> bool:
    """Accept the exact origin and equivalent local loopback addresses.

    Docker Desktop can expose this local-only service as either ``localhost``
    or ``127.0.0.1`` depending on the browser and launch path.  Comparing the
    raw strings made a safe, local form submission fail when those aliases
    differed.  The port must still match, and non-local origins must match the
    request host exactly.
    """
    try:
        origin_url = urlsplit(origin)
        request_url = urlsplit(f"//{host}")
    except ValueError:
        return False
    # Sandboxed and in-app browser surfaces can serialize an otherwise
    # same-site form origin as the literal string "null".  This also happens
    # for some mobile clients viewing a public-IP deployment.  There is no
    # usable origin to compare in that case, so rely on the independent
    # synchronizer-token check in _verify_form below: a cross-site page cannot
    # read this application's HttpOnly CSRF cookie or its rendered form token.
    local_hosts = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}
    if origin.strip().lower() == "null":
        return True
    if origin_url.scheme not in {"http", "https"} or not origin_url.hostname:
        return False
    if origin_url.netloc == host:
        return True
    # Docker Desktop may preserve 0.0.0.0 as the Host header even though the
    # compose port is published only on the local machine.
    try:
        return (
            origin_url.hostname in local_hosts
            and request_url.hostname in local_hosts
            and origin_url.port == request_url.port
        )
    except ValueError:
        return False


def _verify_form(request: Request, csrf_token: str) -> None:
    origin = request.headers.get("origin")
    host = request.headers.get("host", "")
    if origin and not _origin_matches_request_host(origin, host):
        # Only log routing metadata; never log cookies, form values or tokens.
        logger.warning(
            "Rejected form origin: path=%s origin=%r host=%r",
            request.url.path,
            origin,
            host,
        )
        raise HTTPException(status_code=403, detail="来源校验失败")
    cookie = request.cookies.get("astock_csrf")
    if not cookie or not secrets.compare_digest(cookie, csrf_token):
        raise HTTPException(status_code=403, detail="表单已过期，请刷新页面后重试")


def _redirect(workspace_id: str, path: str = "", *, notice: str | None = None, error: str | None = None) -> RedirectResponse:
    query = urlencode({key: value for key, value in {"notice": notice, "error": error}.items() if value})
    target = f"{_workspace_base(workspace_id)}{path}"
    return RedirectResponse(f"{target}?{query}" if query else target, status_code=303)


@app.get("/health")
def health() -> JSONResponse:
    config = load_config(_config_path())
    store = _store(config.raw)
    snapshot = store.snapshot(config.raw)
    return JSONResponse({
        "ok": True, "positions": len(snapshot["positions"]), "cash": snapshot["cash"],
        "workspaces": len(WorkspaceRegistry.from_config_path(_config_path()).list()),
    })


@app.get("/")
def root() -> RedirectResponse:
    workspace = WorkspaceRegistry.from_config_path(_config_path()).default()
    return RedirectResponse(_workspace_base(workspace.id), status_code=303)


@app.post("/workspaces")
def create_workspace(request: Request, csrf_token: str = Form(...)):
    _verify_form(request, csrf_token)
    registry = _workspace_registry()
    default = registry.default()
    if not registry.has_access(default, request.cookies.get("astock_workspace_access")):
        raise HTTPException(status_code=403, detail="只有默认工作区可以新建工作区")
    workspace = registry.create()
    return _render(
        request,
        "workspace_created.html",
        {
            "request": request,
            "title": "工作区已创建",
            "workspace": workspace,
            "workspace_base": _workspace_base(workspace.id),
            "csrf_token": _csrf_token(request),
        },
    )


@app.post("/u/{workspace_id}/access")
def access_workspace(
    workspace_id: str,
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(...),
):
    _verify_form(request, csrf_token)
    workspace = _workspace(workspace_id)
    registry = _workspace_registry()
    if not registry.verify_password(workspace, password):
        return _render_access(request, workspace, error="访问密码不正确")
    token = registry.issue_access_token(workspace)
    response = RedirectResponse(_workspace_base(workspace.id), status_code=303)
    response.set_cookie(
        "astock_workspace_access",
        token,
        httponly=True,
        samesite="strict",
        secure=False,
        path="/",
    )
    return response


@app.get("/u/{workspace_id}")
def dashboard(workspace_id: str, request: Request):
    workspace = _workspace(workspace_id)
    if not _workspace_is_authorized(request, workspace_id):
        return _render_access(request, workspace)
    config, store = _workspace_config(workspace_id)
    raw = config.raw
    records = _audit_records(raw)
    latest = _latest_summary(records)
    recent_actions = [item for item in records if item.get("signals")][:5]
    return _render(request, "dashboard.html", _context(
        request, raw, store, workspace_id=workspace_id, latest_summary=latest, recent_actions=recent_actions,
    ))


@app.get("/u/{workspace_id}/positions")
def positions(workspace_id: str, request: Request):
    if not _workspace_is_authorized(request, workspace_id):
        return _render_access(request, _workspace(workspace_id))
    config, store = _workspace_config(workspace_id)
    return _render(request, "positions.html", _context(
        request, config.raw, store, workspace_id=workspace_id, transactions=store.transactions(),
    ))


@app.post("/u/{workspace_id}/positions/{symbol}/target-weight")
def set_position_target_weight(
    workspace_id: str,
    symbol: str,
    request: Request,
    csrf_token: str = Form(...),
    target_weight_pct: float = Form(...),
    max_single_position_weight_pct: float = Form(...),
):
    _verify_form(request, csrf_token)
    _require_workspace_access(request, workspace_id)
    config, store = _workspace_config(workspace_id)
    position = next((item for item in config.positions if item.symbol == symbol.upper()), None)
    if position is None or position.role != "holding":
        return _redirect(workspace_id, "/positions", error="找不到该正式持仓")
    portfolio_cap = _portfolio_single_position_cap(config.raw)
    try:
        store.set_position_weight_limits(
            symbol=symbol,
            target_main_weight=target_weight_pct / 100,
            max_single_position_weight=max_single_position_weight_pct / 100,
            portfolio_max_weight=portfolio_cap,
        )
    except PortfolioStoreError as exc:
        return _redirect(workspace_id, "/positions", error=str(exc))
    return _redirect(
        workspace_id,
        "/positions",
        notice=(
            f"{position.name}长期目标已更新为{target_weight_pct:g}%，"
            f"单股上限已更新为{max_single_position_weight_pct:g}%"
        ),
    )


@app.post("/u/{workspace_id}/trades")
def create_trade(
    workspace_id: str,
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
    _require_workspace_access(request, workspace_id)
    config, store = _workspace_config(workspace_id)
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
        return _redirect(workspace_id, "/positions", error=str(exc))
    return _redirect(workspace_id, "/positions", notice="成交已记录，持仓、现金与经济投入已同步更新")


@app.post("/u/{workspace_id}/cash")
def set_available_cash(
    workspace_id: str,
    request: Request,
    csrf_token: str = Form(...),
    amount: float = Form(...),
    executed_at: str = Form(...),
    note: str = Form(""),
):
    _verify_form(request, csrf_token)
    _require_workspace_access(request, workspace_id)
    _config, store = _workspace_config(workspace_id)
    try:
        store.set_available_cash(amount=amount, executed_at=executed_at, note=note)
    except PortfolioStoreError as exc:
        return _redirect(workspace_id, error=str(exc))
    return _redirect(workspace_id, notice="可用资金已校准；持仓和经济投入未改动")


@app.post("/u/{workspace_id}/transactions/{transaction_id}/reverse")
def reverse_transaction(
    workspace_id: str,
    transaction_id: int,
    request: Request,
    csrf_token: str = Form(...),
):
    _verify_form(request, csrf_token)
    _require_workspace_access(request, workspace_id)
    config, store = _workspace_config(workspace_id)
    try:
        store.reverse_transaction(transaction_id)
    except PortfolioStoreError as exc:
        return _redirect(workspace_id, "/positions", error=str(exc))
    return _redirect(workspace_id, "/positions", notice="已追加冲销记录，原始流水仍保留")


@app.get("/u/{workspace_id}/watchlist")
def watchlist(workspace_id: str, request: Request):
    if not _workspace_is_authorized(request, workspace_id):
        return _render_access(request, _workspace(workspace_id))
    config, store = _workspace_config(workspace_id)
    raw = config.raw
    records = _audit_records(raw)
    onboarding = StockOnboardingService(raw.get("onboarding", {}))
    return _render(request, "watchlist.html", _context(
            request,
            raw,
            store, workspace_id=workspace_id,
            audit_records=records,
            latest_summary=_latest_summary(records),
            llm_available=onboarding.llm_available,
            llm_model=onboarding.llm_model,
        ))


@app.post("/u/{workspace_id}/watchlist")
def add_watchlist(
    workspace_id: str,
    request: Request,
    csrf_token: str = Form(...),
    symbol: str = Form(...),
    use_llm: str = Form(""),
    name: str = Form(""),
    sector: str = Form(""),
    peers: str = Form(""),
):
    _verify_form(request, csrf_token)
    _require_workspace_access(request, workspace_id)
    config, store = _workspace_config(workspace_id)
    raw = config.raw
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
        return _redirect(workspace_id, "/watchlist", error=str(exc))
    coverage = result.analysis_profile.get("coverage_label", "已建立跟踪")
    return _redirect(workspace_id, "/watchlist", notice=f"{result.name} 已识别并加入股票池 · {coverage}")


@app.post("/u/{workspace_id}/watchlist/{symbol}/remove")
def remove_watchlist(workspace_id: str, symbol: str, request: Request, csrf_token: str = Form(...)):
    _verify_form(request, csrf_token)
    _require_workspace_access(request, workspace_id)
    _config, store = _workspace_config(workspace_id)
    try:
        store.remove_watchlist(symbol)
    except PortfolioStoreError as exc:
        return _redirect(workspace_id, "/watchlist", error=str(exc))
    return _redirect(workspace_id, "/watchlist", notice="观察标的已移除")


@app.get("/u/{workspace_id}/notifications")
def notifications(workspace_id: str, request: Request):
    if not _workspace_is_authorized(request, workspace_id):
        return _render_access(request, _workspace(workspace_id))
    config, store = _workspace_config(workspace_id)
    raw = config.raw
    context = _context(request, raw, store, workspace_id=workspace_id)
    context.update({
        "notification_settings": store.notification_settings(raw),
        "notification_key_available": store.notification_key_available(),
    })
    return _render(request, "notifications.html", context)


@app.post("/u/{workspace_id}/notifications")
def save_notification_settings(
    workspace_id: str,
    request: Request,
    csrf_token: str = Form(...),
    webhook: str = Form(""),
    min_confidence: str = Form("中"),
):
    _verify_form(request, csrf_token)
    _require_workspace_access(request, workspace_id)
    config, store = _workspace_config(workspace_id)
    try:
        store.save_notification_settings(webhook=webhook, min_confidence=min_confidence, raw=config.raw)
    except PortfolioStoreError as exc:
        return _redirect(workspace_id, "/notifications", error=str(exc))
    return _redirect(workspace_id, "/notifications", notice="消息配置已保存；Webhook 已加密保存在本机")


@app.post("/u/{workspace_id}/notifications/test")
def test_notification_settings(workspace_id: str, request: Request, csrf_token: str = Form(...)):
    _verify_form(request, csrf_token)
    _require_workspace_access(request, workspace_id)
    config, store = _workspace_config(workspace_id)
    settings = store.notification_settings(config.raw, include_secrets=True)
    if not settings.get("webhook"):
        return _redirect(workspace_id, "/notifications", error="Webhook 未配置或无法读取，请重新填写")
    try:
        FeishuNotifier(settings["webhook"], settings.get("secret", "")).send("【A股持仓纪律】测试消息：工作区消息配置正常。")
    except Exception as exc:
        return _redirect(workspace_id, "/notifications", error=f"测试发送失败：{exc}")
    return _redirect(workspace_id, "/notifications", notice="测试消息已发送")


def _optional_number(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise PortfolioStoreError("价格字段必须是有效数字") from exc


def _portfolio_single_position_cap(raw: dict[str, Any]) -> float:
    return float(raw.get("risk", {}).get("max_single_position_ratio", 0.45))
