#!/usr/bin/env python3
"""
APEX Layer 2 — intraday entry detector.

Mirrors the validated logic in apex_phase2_entry_backtest.py so LIVE entries match the
backtest. Operates on today's accumulating regular-session 5-min bars for a leader.

Primary trigger:   ORB15  — break above the opening-range (first ORB_MINUTES) high w/ volume
Secondary trigger: VWAP_PB — first pullback to VWAP that closes back above, after an early push

Returns an EntrySignal carrying everything the rationale snapshot (Layer 6) needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time as dtime

import numpy as np
import pandas as pd

SESS_OPEN = dtime(9, 30)


@dataclass
class EntrySignal:
    symbol:    str
    trigger:   str            # "ORB15" | "VWAP_PB"
    price:     float          # modeled fill
    orb_high:  float
    orb_low:   float
    vwap:      float
    rvol:      float          # breakout-bar volume / opening-range avg volume
    bar_time:  str
    context:   dict = field(default_factory=dict)


def _orb_window_end(open_time: dtime, orb_minutes: int) -> dtime:
    total = open_time.hour * 60 + open_time.minute + orb_minutes
    return dtime(total // 60, total % 60)


def detect_entry(symbol: str, day_bars: pd.DataFrame, orb_minutes: int = 15,
                 already_entered: bool = False, live_price: float | None = None) -> EntrySignal | None:
    """
    day_bars: today's regular-session 5-min bars (cols: open/high/low/close/volume, 'tod' time).
    Returns the first valid EntrySignal, or None. ORB15 takes priority over VWAP_PB.

    Hybrid feed: levels (ORB high/low, VWAP) come from `day_bars` (Alpaca, possibly delayed).
    If `live_price` (TradingView real-time) is given, the ORB15 trigger fires the moment the
    LIVE price is above the opening-range high — entering at the live price instead of waiting
    for a delayed bar to print the break. When `live_price` is None it falls back to the
    original bar-based breakout detection, so behavior is unchanged without TV.
    """
    if already_entered or day_bars is None or len(day_bars) < 4:
        return None

    g = day_bars.sort_values("tod").reset_index(drop=True)

    # Data-quality gate — no zero/NaN prices (the HITI/SPCB failure mode)
    if (g[["open", "high", "low", "close"]] <= 0).any().any():
        return None
    if g[["open", "high", "low", "close"]].isna().any().any():
        return None

    o = float(g.iloc[0]["open"])
    orb_end = _orb_window_end(SESS_OPEN, orb_minutes)
    orb = g[g["tod"] < orb_end]
    if orb.empty:
        return None
    orb_high = float(orb["high"].max())
    orb_low = float(orb["low"].min())
    orb_vol = float(orb["volume"].mean())
    after = g[g["tod"] >= orb_end]

    # VWAP across the session so far
    tp = (g["high"] + g["low"] + g["close"]) / 3
    vwap_series = (tp * g["volume"]).cumsum() / g["volume"].cumsum()
    g = g.assign(vwap=vwap_series.values)
    last_vwap = float(g.iloc[-1]["vwap"])

    # ── Primary: ORB15 breakout ──
    # Real-time path: live price already above the opening-range high (volume confirmed by the
    # latest bar). Enter NOW at the live price.
    if live_price is not None and not after.empty and live_price > orb_high:
        last_bar = g.iloc[-1]
        if float(last_bar["volume"]) > orb_vol:    # volume confirmation (latest bar proxy)
            rvol = float(last_bar["volume"] / orb_vol) if orb_vol > 0 else 0.0
            return EntrySignal(
                symbol=symbol, trigger="ORB15", price=float(live_price),
                orb_high=orb_high, orb_low=orb_low, vwap=last_vwap,
                rvol=round(rvol, 2), bar_time="live",
                context={"open": o, "live_price": float(live_price),
                         "orb_avg_vol": orb_vol, "feed": "tv_realtime"},
            )

    # Bar-based path (no live price, or live price not yet above ORB): original detection.
    if live_price is None and not after.empty:
        brk = after[(after["high"] > orb_high) & (after["volume"] > orb_vol)]
        if not brk.empty:
            bar = brk.iloc[0]
            rvol = float(bar["volume"] / orb_vol) if orb_vol > 0 else 0.0
            return EntrySignal(
                symbol=symbol, trigger="ORB15", price=orb_high,
                orb_high=orb_high, orb_low=orb_low, vwap=float(bar["vwap"]),
                rvol=round(rvol, 2), bar_time=str(bar["tod"]),
                context={"open": o, "breakout_vol": float(bar["volume"]),
                         "orb_avg_vol": orb_vol},
            )

    # ── Secondary: VWAP pullback after an early push ──
    pushed_early = bool((g["high"] > o * 1.01).any())

    # Real-time path: early push happened + a recent bar pulled back to/under VWAP, and the LIVE
    # price is now reclaiming above VWAP → enter at the live price (the reclaim, not a stale bar).
    if live_price is not None and pushed_early and live_price > last_vwap:
        recent = g.tail(4)
        touched_vwap = bool((recent["low"] <= recent["vwap"]).any())
        if touched_vwap:
            last_bar = g.iloc[-1]
            rvol = float(last_bar["volume"] / orb_vol) if orb_vol > 0 else 0.0
            return EntrySignal(
                symbol=symbol, trigger="VWAP_PB", price=float(live_price),
                orb_high=orb_high, orb_low=orb_low, vwap=last_vwap,
                rvol=round(rvol, 2), bar_time="live",
                context={"open": o, "live_price": float(live_price), "feed": "tv_realtime"},
            )

    # Bar-based path (no live price): original single-bar dip+reclaim detection, unchanged.
    if live_price is None:
        pushed = False
        for _, bar in g.iterrows():
            if bar["high"] > o * 1.01:
                pushed = True
            if pushed and bar["low"] <= bar["vwap"] and bar["close"] > bar["vwap"]:
                return EntrySignal(
                    symbol=symbol, trigger="VWAP_PB", price=float(bar["close"]),
                    orb_high=orb_high, orb_low=orb_low, vwap=float(bar["vwap"]),
                    rvol=round(float(bar["volume"] / orb_vol) if orb_vol > 0 else 0.0, 2),
                    bar_time=str(bar["tod"]),
                    context={"open": o},
                )

    return None


def composite_score(signal: EntrySignal, rs_pct: float) -> float:
    """
    Tunable confluence score (0-100): blends daily RS leadership with intraday trigger quality.
    Layer 4 may tune the entry threshold this is compared against. Weights are a v1 baseline.
    """
    trigger_score = 80.0 if signal.trigger == "ORB15" else 60.0
    vol_score = min(100.0, signal.rvol * 25.0)            # rvol 4x -> 100
    above_vwap = 100.0 if signal.price >= signal.vwap else 40.0
    return round(0.45 * rs_pct + 0.25 * trigger_score + 0.15 * vol_score + 0.15 * above_vwap, 1)
