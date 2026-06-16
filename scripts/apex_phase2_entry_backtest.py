#!/usr/bin/env python3
"""
APEX Phase 2 — intraday entry-timing backtest.

Given daily RS leaders (the "what to trade" from Layer 1), this answers the "when to enter"
question: compare candidate intraday entry triggers on the SAME set of leaders so the
comparison is bias-free (leader-selection bias cancels out — like the Phase 0 let-it-run test).

Triggers compared (regular session, ET):
  OPEN      buy the 09:30 open
  ORB15     buy on first break above the 09:30-09:45 opening-range high (with volume)
  VWAP_PB   buy first pullback that touches VWAP and closes back above it after an early push

Metrics per trigger (forward return measured on daily closes):
  entries, fill rate, avg same-day-close return, avg +2-day return, avg MAE (heat before close),
  and a simple edge ratio (avg fwd return / avg MAE).

Usage:
  python -W ignore scripts/apex_phase2_entry_backtest.py [--days 30] [--top 40]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, time as dtime
from pathlib import Path

WORKSPACE = Path.home() / "tri-city-inator"
SHARED = WORKSPACE / "shared"
sys.path.insert(0, str(WORKSPACE))

try:
    from dotenv import load_dotenv
    _env = WORKSPACE / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

import numpy as np
import pandas as pd

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ET = "America/New_York"

SESS_OPEN  = dtime(9, 30)
SESS_CLOSE = dtime(16, 0)
ORB_END    = dtime(9, 45)


def _data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def load_leaders(top: int) -> list[str]:
    path = SHARED / "apex-leaders.json"
    if not path.exists():
        print("  no apex-leaders.json — run apex_daily_filter.py first")
        return []
    data = json.loads(path.read_text())
    return [l["symbol"] for l in data.get("leaders", [])[:top]]


def fetch_5min(symbols: list[str], days: int) -> pd.DataFrame:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    dc = _data_client()
    start = datetime.now() - timedelta(days=days + 5)
    frames = []
    for i in range(0, len(symbols), 200):
        batch = symbols[i:i + 200]
        try:
            req = StockBarsRequest(symbol_or_symbols=batch,
                                   timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                                   start=start, feed="sip", adjustment="all")
            df = dc.get_stock_bars(req).df
            if not df.empty:
                frames.append(df)
        except Exception as e:
            print(f"    5min batch {i} failed: {e}")
    return pd.concat(frames) if frames else pd.DataFrame()


def fetch_daily(symbols: list[str], days: int) -> pd.DataFrame:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    dc = _data_client()
    start = datetime.now() - timedelta(days=days + 15)
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                           start=start, feed="sip", adjustment="all")
    return dc.get_stock_bars(req).df


def regular_session(intra: pd.DataFrame) -> pd.DataFrame:
    """Index is UTC tz-aware; return only regular-session bars with an ET 'day' + 'tod'."""
    idx = intra.index.get_level_values("timestamp").tz_convert(ET)
    intra = intra.copy()
    intra["et"] = idx
    intra["day"] = idx.date
    intra["tod"] = idx.time
    return intra[(intra["tod"] >= SESS_OPEN) & (intra["tod"] < SESS_CLOSE)]


def simulate_symbol(sym: str, intra: pd.DataFrame, daily: pd.Series) -> list[dict]:
    """Detect each trigger's entry per day; compute forward returns on daily closes."""
    out = []
    daily = daily.sort_index()
    daily_dates = [d.date() for d in daily.index]

    for day, g in intra.groupby("day"):
        g = g.sort_values("et")
        if len(g) < 6:
            continue
        o = g.iloc[0]["open"]
        orb = g[g["tod"] < ORB_END]
        if orb.empty:
            continue
        orb_high = orb["high"].max()
        orb_vol = orb["volume"].mean()
        after = g[g["tod"] >= ORB_END]

        # VWAP series across the day
        tp = (g["high"] + g["low"] + g["close"]) / 3
        vwap = (tp * g["volume"]).cumsum() / g["volume"].cumsum()
        g = g.assign(vwap=vwap.values)

        entries = {}
        entries["OPEN"] = o

        # ORB15: first bar after 09:45 whose high breaks orb_high with above-avg volume
        brk = after[(after["high"] > orb_high) & (after["volume"] > orb_vol)]
        if not brk.empty:
            entries["ORB15"] = orb_high  # fill at breakout level

        # VWAP_PB: after an early push above open, first bar that dips to/below vwap then
        # closes back above it
        pushed = False
        for _, bar in g.iterrows():
            if bar["high"] > o * 1.01:
                pushed = True
            if pushed and bar["low"] <= bar["vwap"] and bar["close"] > bar["vwap"]:
                entries["VWAP_PB"] = bar["close"]
                break

        # forward outcomes from each entry
        try:
            di = daily_dates.index(day)
        except ValueError:
            continue
        close_today = daily.iloc[di]
        close_p2 = daily.iloc[di + 2] if di + 2 < len(daily) else np.nan
        day_low = g["low"].min()

        for trig, px in entries.items():
            if px is None or px <= 0:
                continue
            mae = (day_low / px - 1) * 100  # worst heat intraday after/around entry
            out.append({
                "symbol": sym, "day": str(day), "trigger": trig, "entry": round(float(px), 2),
                "ret_close": (close_today / px - 1) * 100,
                "ret_p2": (close_p2 / px - 1) * 100 if not np.isnan(close_p2) else np.nan,
                "mae": mae,
            })
    return out


def report(rows: list[dict]) -> None:
    if not rows:
        print("\nNo entries produced.")
        return
    df = pd.DataFrame(rows)
    n_days = df["day"].nunique()
    n_syms = df["symbol"].nunique()
    print("\n" + "=" * 70)
    print(f"  APEX PHASE 2 — ENTRY-TIMING BACKTEST  ({n_syms} leaders, {n_days} days)")
    print("=" * 70)
    print(f"  {'TRIGGER':9s} {'N':>4s} {'CLOSE%':>8s} {'+2D%':>8s} {'MAE%':>7s} {'EDGE':>6s} {'WIN%':>6s}")
    print("  " + "-" * 58)
    for trig in ["OPEN", "ORB15", "VWAP_PB"]:
        sub = df[df["trigger"] == trig]
        if sub.empty:
            continue
        rc = sub["ret_close"].mean()
        rp2 = sub["ret_p2"].mean()
        mae = sub["mae"].mean()
        edge = rc / abs(mae) if mae != 0 else float("nan")
        win = (sub["ret_close"] > 0).mean() * 100
        print(f"  {trig:9s} {len(sub):>4d} {rc:>+7.2f}% {rp2:>+7.2f}% {mae:>+6.2f}% {edge:>5.2f} {win:>5.0f}%")
    print("=" * 70)
    print("  CLOSE% = avg return entry→same-day close | +2D% = entry→close+2 sessions")
    print("  MAE%   = avg worst intraday heat | EDGE = CLOSE% / |MAE%| (higher = better timing)")
    print()
    # verdict
    summ = {t: df[df["trigger"] == t]["ret_p2"].mean() for t in df["trigger"].unique()}
    best = max(summ, key=lambda k: (summ[k] if not np.isnan(summ[k]) else -99))
    print(f"  Best +2D trigger: {best} ({summ[best]:+.2f}%). "
          f"OPEN baseline: {summ.get('OPEN', float('nan')):+.2f}%")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()

    if not ALPACA_API_KEY:
        print("ERROR: ALPACA_API_KEY not set")
        sys.exit(1)

    leaders = load_leaders(args.top)
    if not leaders:
        sys.exit(1)
    print(f"APEX Phase 2 entry-timing backtest | {len(leaders)} leaders | {args.days} days")

    intra_all = fetch_5min(leaders, args.days)
    daily_all = fetch_daily(leaders, args.days)
    if intra_all.empty:
        print("  no intraday data")
        sys.exit(1)
    intra_all = regular_session(intra_all)

    rows = []
    for sym in leaders:
        try:
            si = intra_all[intra_all.index.get_level_values("symbol") == sym]
            sd = daily_all.xs(sym, level="symbol")["close"]
        except (KeyError, Exception):
            continue
        if si.empty or sd.empty:
            continue
        rows += simulate_symbol(sym, si, sd)

    report(rows)


if __name__ == "__main__":
    main()
