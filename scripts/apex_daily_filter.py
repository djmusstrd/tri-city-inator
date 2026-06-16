#!/usr/bin/env python3
"""
APEX Layer 1 — Daily Filter (the "what to trade" engine).

Screens the full liquid US-equity universe daily, ranks EVERY name by a true
universe-percentile Relative Strength, applies an EMA-ribbon trend gate, and writes a
ranked pre-open leader watchlist. See docs/STRATEGY_V2_DESIGN.md.

Two-stage fetch for efficiency:
  1. cheap recent window for ALL symbols -> liquidity filter (avg $ volume)
  2. full history only for liquid survivors -> RS + ribbon

Modes:
  (default)    build today's leader watchlist -> shared/apex-leaders.json
  --validate   walk-forward test: do top-decile leaders out-perform SPY forward?

Usage:
  python -W ignore scripts/apex_daily_filter.py [--min-dollar-vol 10000000] [--top-pct 90]
  python -W ignore scripts/apex_daily_filter.py --validate [--horizon 20] [--step 21]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
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

BENCHMARK = "SPY"
E1, E2, E3, E4, E5 = 8, 13, 21, 34, 55
Q1, Q2, Q3, Q4 = 63, 126, 189, 252
LIQ_LOOKBACK = 20          # days for avg dollar volume
BATCH = 500


def _trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=True)


def _data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def get_universe() -> list[str]:
    from alpaca.trading.requests import GetAssetsRequest
    from alpaca.trading.enums import AssetClass, AssetStatus
    tc = _trading_client()
    assets = tc.get_all_assets(GetAssetsRequest(asset_class=AssetClass.US_EQUITY,
                                                 status=AssetStatus.ACTIVE))
    syms = [a.symbol for a in assets
            if a.tradable and "OTC" not in str(a.exchange) and a.symbol.isalpha()]
    return sorted(set(syms))


def fetch_bars(symbols: list[str], days: int, label: str = "") -> pd.DataFrame:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    dc = _data_client()
    start = datetime.now() - timedelta(days=days)
    frames = []
    n = len(symbols)
    for i in range(0, n, BATCH):
        batch = symbols[i:i + BATCH]
        for feed in ("sip", "iex"):
            try:
                req = StockBarsRequest(symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                                       start=start, feed=feed, adjustment="all")
                df = dc.get_stock_bars(req).df
                if not df.empty:
                    frames.append(df)
                break
            except Exception as e:
                if feed == "iex":
                    print(f"    batch {i//BATCH} failed: {e}")
        print(f"  [{label}] {min(i+BATCH, n)}/{n} symbols", end="\r", flush=True)
    print()
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames)


def liquidity_filter(symbols: list[str], min_dollar_vol: float) -> list[str]:
    """Stage 1: cheap recent fetch -> keep names above the dollar-volume floor."""
    df = fetch_bars(symbols, days=LIQ_LOOKBACK + 15, label="liquidity")
    if df.empty:
        return []
    survivors = []
    for sym in df.index.get_level_values(0).unique():
        sub = df.xs(sym, level="symbol")
        if len(sub) < 5:
            continue
        recent = sub.tail(LIQ_LOOKBACK)
        dollar_vol = (recent["close"] * recent["volume"]).mean()
        if dollar_vol >= min_dollar_vol:
            survivors.append(sym)
    return survivors


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def rs_raw(close: pd.Series, asof: int | None = None) -> float:
    """IBD quarter-weighted return, evaluated at index `asof` (default last)."""
    c = close if asof is None else close.iloc[:asof + 1]
    if len(c) <= Q4:
        return np.nan
    def perf(n):
        return (c.iloc[-1] / c.iloc[-1 - n] - 1) * 100
    return perf(Q1) * 0.4 + perf(Q2) * 0.2 + perf(Q3) * 0.2 + perf(Q4) * 0.2


def ribbon_bull(close: pd.Series) -> bool:
    if len(close) < E5 + 2:
        return False
    m1, m2, m3, m4, m5 = (ema(close, e).iloc[-1] for e in (E1, E2, E3, E4, E5))
    c = close.iloc[-1]
    return bool(c > m1 > m2 > m3 > m4 > m5)


def build_leaders(min_dollar_vol: float, top_pct: float) -> dict:
    print(f"APEX Daily Filter | min $vol ${min_dollar_vol:,.0f} | top {top_pct:.0f}th pct")
    universe = get_universe()
    print(f"  universe: {len(universe)} tradable non-OTC names")

    liquid = liquidity_filter(universe, min_dollar_vol)
    print(f"  liquid (≥${min_dollar_vol/1e6:.0f}M/day): {len(liquid)} names")
    if not liquid:
        return {"error": "no liquid names"}

    bars = fetch_bars(liquid + [BENCHMARK], days=int(Q4 * 1.6 + 30), label="history")

    rows = []
    for sym in liquid:
        try:
            c = bars.xs(sym, level="symbol")["close"].sort_index()
        except KeyError:
            continue
        if len(c) <= Q4:
            continue
        r = rs_raw(c)
        if np.isnan(r):
            continue
        rows.append({"symbol": sym, "rs_raw": r,
                     "price": round(float(c.iloc[-1]), 2),
                     "ribbon_bull": ribbon_bull(c)})

    if not rows:
        return {"error": "no rankable names"}

    df = pd.DataFrame(rows)
    df["rs_pct"] = (df["rs_raw"].rank(pct=True) * 100).round(1)
    df = df.sort_values("rs_pct", ascending=False)

    leaders = df[(df["rs_pct"] >= top_pct) & (df["ribbon_bull"])]
    print(f"  ranked {len(df)} names | leaders (RS≥{top_pct} & ribbon bull): {len(leaders)}")

    out = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "min_dollar_vol": min_dollar_vol,
        "top_pct": top_pct,
        "universe_size": len(universe),
        "liquid_size": len(liquid),
        "ranked_size": len(df),
        "leaders": [
            {"symbol": r.symbol, "rs_pct": r.rs_pct, "price": r.price}
            for r in leaders.itertuples()
        ],
    }
    return out


def _perf_series(c: pd.Series, n: int) -> pd.Series:
    return (c / c.shift(n) - 1) * 100


def validate(min_dollar_vol: float, top_pct: float, horizon: int, step: int) -> None:
    """Walk-forward: at each as-of date, do top-decile leaders out-perform SPY forward?

    Vectorized: precompute per-symbol RS, ribbon-bull, and close panels aligned to the SPY
    trading calendar, then do cross-sectional ranking per date. No per-window recomputation.
    """
    print(f"APEX walk-forward validation | horizon {horizon}d | step {step}d")
    universe = get_universe()
    liquid = liquidity_filter(universe, min_dollar_vol)
    print(f"  liquid universe: {len(liquid)}")
    bars = fetch_bars(liquid + [BENCHMARK], days=int(365 * 3.5), label="history")

    closes = {}
    for sym in liquid + [BENCHMARK]:
        try:
            closes[sym] = bars.xs(sym, level="symbol")["close"].sort_index()
        except KeyError:
            pass
    spy = closes.get(BENCHMARK)
    if spy is None:
        print("  ERROR: no SPY")
        return
    cal = spy.index  # master trading calendar

    print(f"  precomputing RS / ribbon panels for {len(closes)-1} names...")
    rs_cols, bull_cols, close_cols = {}, {}, {}
    for sym, c in closes.items():
        if sym == BENCHMARK or len(c) <= Q4:
            continue
        c = c[~c.index.duplicated()].reindex(cal)
        rs_s = (_perf_series(c, Q1) * 0.4 + _perf_series(c, Q2) * 0.2
                + _perf_series(c, Q3) * 0.2 + _perf_series(c, Q4) * 0.2)
        m1, m2, m3, m4, m5 = (ema(c, e) for e in (E1, E2, E3, E4, E5))
        bull = (c > m1) & (m1 > m2) & (m2 > m3) & (m3 > m4) & (m4 > m5)
        rs_cols[sym] = rs_s
        bull_cols[sym] = bull
        close_cols[sym] = c

    rs_panel = pd.DataFrame(rs_cols)
    bull_panel = pd.DataFrame(bull_cols)
    close_panel = pd.DataFrame(close_cols)
    fwd_panel = close_panel.shift(-horizon) / close_panel - 1.0  # forward return per name
    spy_fwd_series = spy.reindex(cal).shift(-horizon) / spy.reindex(cal) - 1.0

    results = []
    start_i = Q4 + 5
    for t in range(start_i, len(cal) - horizon, step):
        asof = cal[t]
        rs_row = rs_panel.iloc[t].dropna()
        if len(rs_row) < 50:
            continue
        pct = rs_row.rank(pct=True) * 100
        bull_row = bull_panel.iloc[t]
        leader_syms = pct[(pct >= top_pct) & (bull_row.reindex(pct.index).fillna(False))].index
        if len(leader_syms) == 0:
            continue
        fwd_row = fwd_panel.iloc[t].reindex(leader_syms).dropna()
        if fwd_row.empty:
            continue
        spy_fwd = spy_fwd_series.iloc[t]
        if pd.isna(spy_fwd):
            continue
        results.append({
            "date": str(asof.date()), "n_leaders": int(len(fwd_row)),
            "leader_ret": float(fwd_row.mean() * 100), "spy_ret": float(spy_fwd * 100),
            "excess": float(fwd_row.mean() * 100 - spy_fwd * 100),
            "hit": float((fwd_row > spy_fwd).mean()),
        })

    if not results:
        print("  no validation windows produced")
        return
    rdf = pd.DataFrame(results)
    print("\n" + "=" * 64)
    print(f"  WALK-FORWARD: top-{100-top_pct:.0f}% RS leaders vs SPY, {horizon}d forward")
    print("=" * 64)
    print(f"  Windows:               {len(rdf)}")
    print(f"  Avg leader fwd return: {rdf['leader_ret'].mean():+.2f}%")
    print(f"  Avg SPY fwd return:    {rdf['spy_ret'].mean():+.2f}%")
    print(f"  Avg EXCESS vs SPY:     {rdf['excess'].mean():+.2f}%")
    print(f"  Windows beating SPY:   {(rdf['excess'] > 0).mean()*100:.0f}%")
    print(f"  Avg per-name hit rate: {rdf['hit'].mean()*100:.0f}%")
    print("=" * 64)
    if rdf["excess"].mean() > 0 and (rdf["excess"] > 0).mean() > 0.5:
        print("  VERDICT: RS leaders prospectively out-perform SPY. Layer 1 edge confirmed.")
    else:
        print("  VERDICT: no consistent prospective edge — rethink RS/ribbon before building on it.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-dollar-vol", type=float, default=10_000_000)
    ap.add_argument("--top-pct", type=float, default=90.0)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--step", type=int, default=21)
    args = ap.parse_args()

    if not ALPACA_API_KEY:
        print("ERROR: ALPACA_API_KEY not set")
        sys.exit(1)

    if args.validate:
        validate(args.min_dollar_vol, args.top_pct, args.horizon, args.step)
        return

    out = build_leaders(args.min_dollar_vol, args.top_pct)
    if out.get("error"):
        print(f"ERROR: {out['error']}")
        sys.exit(1)
    SHARED.mkdir(parents=True, exist_ok=True)
    path = SHARED / "apex-leaders.json"
    path.write_text(json.dumps(out, indent=2))
    print(f"\n  Top 15 leaders:")
    for ld in out["leaders"][:15]:
        print(f"    {ld['symbol']:6s} RS {ld['rs_pct']:>5.1f}  ${ld['price']}")
    print(f"\n  Wrote {len(out['leaders'])} leaders -> {path}")


if __name__ == "__main__":
    main()
