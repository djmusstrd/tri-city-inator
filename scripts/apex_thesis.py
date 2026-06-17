#!/usr/bin/env python3
"""
APEX — entry-time thesis enrichment (Layer 6 / Trade Journal foundation).

At the moment of entry we capture a point-in-time thesis the dashboard renders as a full trade
card AND the swing manager later enforces:
  - support / resistance levels (ORB, prior-day H/L, recent swing H/L, 52-week high)
  - catalyst (latest Alpaca news headline)
  - planned exit framing + invalidation
These are captured ONCE, at entry, so they're accurate to that moment and durable — a swing's
invalidation/support becomes its swing stop. All best-effort: missing data degrades gracefully.
"""

from __future__ import annotations

import logging
import os

import pandas as pd

logger = logging.getLogger("apex.thesis")


def _round(v, n=2):
    try:
        return round(float(v), n)
    except Exception:
        return None


def support_resistance(signal, daily: pd.DataFrame | None) -> tuple[list, list, dict]:
    """
    Return (support_levels, resistance_levels, extras) from the ORB levels + the symbol's daily
    bars. Each level is {"label": str, "price": float}, nearest-first. `daily` is the symbol's
    daily OHLC (most recent row = today's forming bar).
    """
    support, resistance, extras = [], [], {}
    px = float(signal.price)

    # ORB levels (always present, from the signal)
    support.append({"label": "ORB low", "price": _round(signal.orb_low)})
    resistance.append({"label": "ORB high", "price": _round(signal.orb_high)})

    if daily is not None and not daily.empty:
        g = daily.sort_index()
        try:
            prev = g.iloc[-2]  # prior completed daily bar (─1 is today, forming)
            support.append({"label": "prior-day low", "price": _round(prev["low"])})
            resistance.append({"label": "prior-day high", "price": _round(prev["high"])})
        except Exception:
            pass
        try:
            win = g.iloc[-11:-1]  # last 10 completed days
            support.append({"label": "10d swing low", "price": _round(win["low"].min())})
            resistance.append({"label": "10d swing high", "price": _round(win["high"].max())})
        except Exception:
            pass
        try:
            yr = g.iloc[-252:]
            hi52 = float(yr["high"].max())
            resistance.append({"label": "52wk high", "price": _round(hi52)})
            extras["near_52wk_high"] = px >= hi52 * 0.97
        except Exception:
            pass

    # de-dup + keep meaningful: support below price, resistance above price, nearest first
    support = [l for l in support if l["price"] is not None and l["price"] <= px * 1.001]
    resistance = [l for l in resistance if l["price"] is not None and l["price"] >= px * 0.999]
    support = sorted({l["price"]: l for l in support}.values(), key=lambda l: -l["price"])
    resistance = sorted({l["price"]: l for l in resistance}.values(), key=lambda l: l["price"])
    return support, resistance, extras


_vix_cache: dict = {"date": None, "value": None}
_float_cache: dict = {}


def get_vix() -> float | None:
    """Current VIX (cached daily, best-effort). None if unavailable."""
    import datetime as _dt
    today = _dt.date.today().isoformat()
    if _vix_cache["date"] == today and _vix_cache["value"] is not None:
        return _vix_cache["value"]
    try:
        import yfinance as yf
        h = yf.Ticker("^VIX").history(period="1d")
        v = float(h["Close"].iloc[-1]) if not h.empty else None
        _vix_cache.update(date=today, value=v)
        return v
    except Exception:
        return None


def get_float(symbol: str) -> float | None:
    """Float shares for a symbol (cached, best-effort). None if unavailable."""
    if symbol in _float_cache:
        return _float_cache[symbol]
    val = None
    try:
        import yfinance as yf
        info = yf.Ticker(symbol).get_info()
        val = info.get("floatShares") or info.get("sharesOutstanding")
    except Exception:
        val = None
    _float_cache[symbol] = val
    return val


def catalyst(symbol: str, limit: int = 3) -> list:
    """Latest news headlines for `symbol` (best-effort, short timeout). [] on any failure."""
    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec:
        return []
    try:
        from alpaca.data.historical import NewsClient
        from alpaca.data.requests import NewsRequest
        nc = NewsClient(key, sec)
        news = nc.get_news(NewsRequest(symbols=symbol, limit=limit))
        items = news.data.get("news", []) if hasattr(news, "data") else []
        return [{"headline": a.headline, "date": str(a.created_at.date()),
                 "source": getattr(a, "source", ""), "url": getattr(a, "url", "")}
                for a in items[:limit]]
    except Exception as e:
        logger.debug(f"catalyst({symbol}) failed: {e}")
        return []


def build_thesis(signal, daily: pd.DataFrame | None, stop: float) -> dict:
    """Assemble the entry thesis: setup, direction, S/R, catalyst, planned exit + invalidation."""
    support, resistance, extras = support_resistance(signal, daily)
    cat = catalyst(signal.symbol)
    entry = float(signal.price)
    invalidation = support[0]["price"] if support else _round(stop)
    return {
        "direction": "long",
        "setup": signal.trigger,
        "support": support,
        "resistance": resistance,
        "catalyst": cat,
        "near_52wk_high": extras.get("near_52wk_high", False),
        "planned_exit": (
            "Layer 3 health-managed: hold while healthy, proactive exit < 40, carry overnight "
            "if ≥ 70 & green & above VWAP. No fixed take-profit (let winners run)."
        ),
        "invalidation": invalidation,
        "thesis": (
            f"RS leader reclaiming {'ORB high' if signal.trigger == 'ORB15' else 'VWAP'} on a "
            f"{signal.trigger} trigger; long above {entry:.2f}, wrong below "
            f"{invalidation:.2f}." if invalidation else
            f"RS leader, {signal.trigger} trigger; long above {entry:.2f}."
        ),
    }
