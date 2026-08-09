"""Official exchange-first A-share trading calendar with a local cache.

The YAML holiday list remains an emergency/local override.  Normal operation
refreshes the Shanghai Stock Exchange's official annual closure page and merges
the parsed closure dates into the application calendar.  The parser is kept
dependency-free so the host scheduler can use it before Docker starts.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen


OFFICIAL_CALENDAR_URL = "https://www.sse.com.cn/disclosure/dealinstruc/closed/"
_DATE_RE = re.compile(r"(?:(\d{4})年)?\s*(\d{1,2})月\s*(\d{1,2})日")


def resolve_holidays(raw: dict, config_path: str | Path) -> set[str]:
    """Merge local overrides with cached/fresh official closure dates."""
    manual = {str(item) for item in raw.get("holidays", []) if _is_iso_date(str(item))}
    settings = raw.get("trading_calendar", {}) or {}
    if not bool(settings.get("enabled", False)):
        return manual
    cache_value = settings.get("cache_path", "data/trading_calendar.json")
    cache_path = Path(str(cache_value))
    if not cache_path.is_absolute():
        cache_path = Path(config_path).resolve().parent / cache_path
    refresh_hours = max(float(settings.get("refresh_hours", 24)), 1.0)
    cached = _read_cache(cache_path)
    now = datetime.now().astimezone()
    official = set(cached.get("holidays", [])) if cached else set()
    fetched_at = _parse_datetime(cached.get("fetched_at")) if cached else None
    stale = fetched_at is None or now - fetched_at >= timedelta(hours=refresh_hours)
    if stale:
        try:
            body = _fetch(
                str(settings.get("official_url", OFFICIAL_CALENDAR_URL)),
                int(settings.get("timeout_seconds", 5)),
            )
            official = parse_official_closures(body)
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(
                    {"fetched_at": now.isoformat(), "source_url": str(settings.get("official_url", OFFICIAL_CALENDAR_URL)), "holidays": sorted(official)},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            # Existing cache/manual dates remain authoritative when the exchange
            # page is temporarily unavailable; no guessed dates are introduced.
            pass
    return manual | {item for item in official if _is_iso_date(item)}


def parse_official_closures(body: str) -> set[str]:
    """Parse the SSE annual closure page's holiday ranges and extra closures."""
    text = html.unescape(body)
    text = re.sub(r"<\s*(?:br|/p|/li|/h[1-6]|/tr)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    result: set[str] = set()
    current_year: int | None = None
    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        year_match = re.search(r"(20\d{2})年休市安排", line)
        if year_match:
            current_year = int(year_match.group(1))
            continue
        if "休市" not in line or current_year is None:
            continue
        line_year = re.search(r"(20\d{2})年", line)
        year = int(line_year.group(1)) if line_year else current_year
        # Open-market dates after “照常开市” are not closures.  Keep the
        # preceding holiday range and explicit “另外…周末休市” fragments.
        chunks = re.split(r"照常开市", line)
        for index, chunk in enumerate(chunks):
            if index > 0 and "另外" not in chunk:
                continue
            if index == 0:
                _add_date_tokens(result, chunk, year)
            elif "另外" in chunk and "休市" in chunk:
                _add_date_tokens(result, chunk.split("另外", 1)[1], year)
    return result


def is_market_day(config_path: str | Path, today: date | None = None) -> bool:
    day = today or date.today()
    if day.weekday() >= 5:
        return False
    raw_text = Path(config_path).read_text(encoding="utf-8")
    # Only the top-level ``holidays`` override belongs here.  Dates elsewhere
    # in the YAML (corporate events, watchlist entry dates, evidence windows)
    # must not accidentally turn into exchange holidays.
    match = re.search(
        r"(?ms)^holidays:\s*(.*?)(?=^[A-Za-z_][A-Za-z0-9_]*:\s*|\Z)",
        raw_text,
    )
    manual = set(re.findall(r"20\d{2}-\d{2}-\d{2}", match.group(1) if match else ""))
    raw = {
        "holidays": sorted(manual),
        "trading_calendar": {"enabled": True, "cache_path": str(Path(config_path).parent / "data/trading_calendar.json")},
    }
    return day.isoformat() not in resolve_holidays(raw, config_path)


def _add_date_tokens(result: set[str], chunk: str, year: int) -> None:
    tokens = [(int(month), int(day)) for _, month, day in _DATE_RE.findall(chunk)]
    if not tokens:
        return
    if len(tokens) >= 2 and ("至" in chunk or "-" in chunk):
        start_month, start_day = tokens[0]
        end_month, end_day = tokens[1]
        try:
            cursor = date(year, start_month, start_day)
            end = date(year, end_month, end_day)
            while cursor <= end:
                result.add(cursor.isoformat())
                cursor += timedelta(days=1)
            return
        except ValueError:
            pass
    for month, day in tokens:
        try:
            result.add(date(year, month, day).isoformat())
        except ValueError:
            continue


def _fetch(url: str, timeout: int) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 astock-discipline-bot/0.1"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _read_cache(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _parse_datetime(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _is_iso_date(value: str) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except ValueError:
        return False
