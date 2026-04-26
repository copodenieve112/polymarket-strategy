# data_feed.py

import warnings
warnings.filterwarnings("ignore")

import json
import math
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import List, Optional

from clock import now_utc
from config import POLYMARKET_EVENTS_URL, REQUEST_TIMEOUT, SERIES, DEBUG_MODE
from models import Market

# Métricas de conexión accesibles desde el dashboard
last_fetch_latency_ms: float = 0.0   # latencia del último ciclo completo
last_fetch_errors:     int   = 0     # errores en el último ciclo
last_fetch_ts:         float = 0.0   # timestamp unix del último fetch


def fetch_markets() -> List[Market]:
    """Fetches the nearest future market for every (coin, window) in SERIES."""
    global last_fetch_latency_ms, last_fetch_errors, last_fetch_ts

    markets: List[Market] = []
    errors = 0
    t0 = time.perf_counter()

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(_fetch_series, slug, ticker, window_min): (ticker, window_min)
            for slug, ticker, window_min in SERIES
        }
        for future in as_completed(futures):
            try:
                result = future.result()
                if result is not None:
                    markets.append(result)
            except Exception:
                errors += 1

    last_fetch_latency_ms = (time.perf_counter() - t0) * 1000
    last_fetch_errors     = errors
    last_fetch_ts         = time.time()

    markets.sort(key=lambda m: m.time_left_seconds)
    return markets


def _next_expected_close(now: datetime, window_min: int) -> datetime:
    """Return the next scheduled close time for a regular-interval market (UTC)."""
    total_min = now.hour * 60 + now.minute + now.second / 60
    next_mark = math.ceil(total_min / window_min) * window_min
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    next_dt = base + timedelta(minutes=next_mark)
    if next_dt <= now:
        next_dt += timedelta(minutes=window_min)
    return next_dt


def _fetch_series(slug: str, ticker: str, window_min: int) -> Optional[Market]:
    """
    Fetches the nearest upcoming market for a series.

    Strategy waterfall:
      1. end_date_min=now  → nearest future events first (correct, bypasses clock issues)
      2. closed=false      → fallback if (1) returns nothing
      3. active=true       → fallback for older API behaviour
      4. ascending=false + large limit → last resort, scan from far-future end
    """
    now = now_utc()
    now_str = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    headers = {"Cache-Control": "no-cache", "Pragma": "no-cache"}

    attempts = [
        # Primary: events with endDate >= now, sorted ascending → guaranteed near-term
        {"series_slug": slug, "limit": 10, "order": "endDate", "ascending": "true",
         "end_date_min": now_str},
        # Fallback: nearest non-closed events
        {"series_slug": slug, "limit": 20, "order": "endDate", "ascending": "true", "closed": "false"},
        # Fallback: active-only filter
        {"series_slug": slug, "limit": 20, "order": "endDate", "ascending": "true", "active": "true"},
        # Last resort: scan 2000 from far-future end (API caps at 500)
        {"series_slug": slug, "limit": 2000, "order": "endDate", "ascending": "false"},
    ]

    for params in attempts:
        try:
            r = requests.get(POLYMARKET_EVENTS_URL, params=params,
                             timeout=REQUEST_TIMEOUT, headers=headers)
            r.raise_for_status()
            events = r.json()
        except Exception as e:
            if DEBUG_MODE:
                print(f"[ERROR] {slug}: {e}")
            continue

        future_events = []
        for event in events:
            end_time = _parse_end_time(event.get("endDate"))
            if end_time is not None and end_time > now:
                future_events.append((end_time, event))
        future_events.sort(key=lambda x: x[0])  # nearest first

        for end_time, event in future_events:
            for raw_market in event.get("markets", []):
                market = _parse_market(raw_market, ticker, window_min, end_time)
                if market is not None:
                    if DEBUG_MODE:
                        _debug_print(slug, raw_market, market)
                    return market

        # If we found future events but no valid/tradeable market among them, stop.
        if future_events:
            break

        if DEBUG_MODE and events:
            print(f"[WARN] {slug}: {len(events)} events returned, 0 future — trying next strategy")

    return None


def _parse_market(raw: dict, ticker: str, window_min: int, end_time: datetime) -> Optional[Market]:
    """Build a Market from a raw API dict. Returns None if data is insufficient or oracle-resolved."""
    try:
        market_id = str(raw.get("conditionId") or raw.get("id") or "")
        question  = str(raw.get("question") or "").strip()
        if not market_id or not question:
            return None

        bid  = _safe_float(raw.get("bestBid") if raw.get("bestBid") is not None else raw.get("bid"))
        ask  = _safe_float(raw.get("bestAsk") if raw.get("bestAsk") is not None else raw.get("ask"))
        last = _safe_float(raw.get("lastTradePrice"))

        # outcomePrices: canonical Polymarket display price (exact match to web UI)
        op_raw = raw.get("outcomePrices", [])
        if isinstance(op_raw, str):
            try:
                op_raw = json.loads(op_raw)
            except Exception:
                op_raw = []
        op_yes = _safe_float(op_raw[0]) if len(op_raw) >= 1 else 0.0
        op_no  = _safe_float(op_raw[1]) if len(op_raw) >= 2 else 0.0

        # Active order book: both sides present, spread < 50%
        has_book = bid > 0 and ask > 0 and 0 < ask < 1 and (ask - bid) < 0.50

        # Oracle-resolved: outcomePrices confirmed at 0 or 1 → skip entirely.
        # Polymarket already shows the NEXT open market; we must do the same.
        if op_yes >= 0.99 or op_no >= 0.99:
            return None

        # Price hierarchy — exact match to Polymarket's displayed probability:
        # 1. outcomePrices[0] when active [0.01–0.99] — canonical mid from CLOB
        # 2. bid/ask mid from live order book (fallback if outcomePrices absent)
        # 3. lastTradePrice (last executed price)
        # 4. default 0.5 (no data at all)
        if 0.01 < op_yes < 0.99 and abs(op_yes + op_no - 1.0) < 0.02:
            price_yes = op_yes
        elif has_book:
            price_yes = (bid + ask) / 2
        elif 0 < last < 1:
            price_yes = last
        else:
            price_yes = 0.5

        # has_real_price: active order book with real liquidity
        has_real_price = has_book

        price_no = 1.0 - price_yes

        if not (0 < price_yes < 1):
            return None

        volume = _safe_float(raw.get("volumeNum") or raw.get("volume") or 0)

        # API update timestamp for staleness detection in dashboard
        upd_str = raw.get("updatedAt", "")
        try:
            updated_at_ms = int(
                datetime.fromisoformat(upd_str.replace("Z", "+00:00"))
                .replace(tzinfo=None).timestamp() * 1000
            )
        except Exception:
            updated_at_ms = int(now_utc().timestamp() * 1000)

        market = Market(
            id=market_id,
            question=question,
            coin=ticker,
            window_minutes=window_min,
            price_yes=price_yes,
            price_no=price_no,
            volume=volume,
            end_time=end_time,
            has_real_price=has_real_price,
            last_fetched_ms=int(now_utc().timestamp() * 1000),
            updated_at_ms=updated_at_ms,
            bid=bid,
            ask=ask,
            last_trade_price=last,
        )

        if abs(price_yes - 0.5) < 0.001:
            print(f"[WARN] {ticker} {market.window_label}: precio 0.500 (sin actividad)")

        return market

    except Exception:
        return None


def _debug_print(slug: str, raw: dict, market: Market):
    op = raw.get("outcomePrices", "?")
    print(f"\n[DEBUG] {slug}")
    print(f"  question: {market.question[:70]}")
    print(f"  RAW  → bid={raw.get('bestBid') or raw.get('bid')}  "
          f"ask={raw.get('bestAsk') or raw.get('ask')}  "
          f"last={raw.get('lastTradePrice')}  outcomePrices={op}")
    print(f"  CALC → YES={market.price_yes:.4f}  NO={market.price_no:.4f}  "
          f"real={market.has_real_price}  time_left={market.time_left}")
    print(f"  updatedAt={raw.get('updatedAt', '?')}")


def _safe_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _parse_end_time(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None
