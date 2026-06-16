#!/usr/bin/env python3
"""
APEX Phase 0 — concept backtest (RS proxy + EMA ribbon, long-only) on daily bars.

Cheap go/no-go gate for the APEX strategy (see docs/STRATEGY_V2_DESIGN.md). Mirrors the
Pine logic in pine/apex_phase0.pine but runs across a BASKET so we get aggregate expectancy
instead of a single-symbol read. The fake single-symbol RS here is a placeholder; Phase 1
replaces it with a true universe-percentile RS.

Reports R-multiple expectancy (sizing-independent): trades, win rate, avg R, profit factor,
expectancy per trade, and a per-symbol breakdown.

Usage:
  python -W ignore scripts/apex_phase0_backtest.py [--years 3] [--symbols NVDA PLTR ...]
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path.home() / "tri-city-inator"
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

# Default basket: liquid, varied trend/chop behavior across sectors + high-beta names.
DEFAULT_BASKET = [
    "NVDA", "PLTR", "TSLA", "AAPL", "MSFT", "AMD", "META", "AMZN", "GOOGL", "NFLX",
    "AVGO", "MSTR", "COIN", "SMCI", "CRWD", "SHOP", "UBER", "ABNB", "SOFI", "RIVN",
]
BENCHMARK = "SPY"

# Strategy params (match pine/apex_phase0.pine defaults)
E1, E2, E3, E4, E5 = 8, 13, 21, 34, 55
ATR_LEN       = 14
ATR_STOP_MULT = 2.0
TP_RATIO      = 2.5
ENTRY_THRESH  = 65.0
RS_MIN        = 55.0
RS_WEIGHT     = 50.0
EMA_WEIGHT    = 50.0
Q1, Q2, Q3, Q4 = 63, 126, 189, 252

USE_TP = True  # set False via --no-tp to test "let it run" (exit only on stop/trend break)


def fetch_daily(symbols: list[str], years: int) -> dict[str, pd.DataFrame]:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    start = datetime.now() - timedelta(days=int(years * 365 + Q4 + 60))
    out: dict[str, pd.DataFrame] = {}
    for feed in ("sip", "iex"):
        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                start=start, feed=feed, adjustment="all",
            )
            df = client.get_stock_bars(req).df
            if df.empty:
                continue
            for sym in symbols:
                try:
                    sub = df.xs(sym, level="symbol").sort_index()
                    if len(sub) > Q4:
                        out[sym] = sub
                except (KeyError, Exception):
                    pass
            if out:
                print(f"  (data feed: {feed})")
                return out
        except Exception as e:
            print(f"  feed={feed} failed: {e}")
    return out


def ema(series: pd.Series, length: int) -> pd.Series:
    return series.ewm(span=length, adjust=False).mean()


def atr(df: pd.DataFrame, length: int) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(length).mean()


def perf(series: pd.Series, n: int) -> pd.Series:
    return (series / series.shift(n) - 1) * 100


def compute_signals(df: pd.DataFrame, bench_close: pd.Series) -> pd.DataFrame:
    c = df["close"]
    # RS proxy (IBD quarter weighting vs benchmark)
    stock_w = perf(c, Q1) * 0.4 + perf(c, Q2) * 0.2 + perf(c, Q3) * 0.2 + perf(c, Q4) * 0.2
    bench_w = (perf(bench_close, Q1) * 0.4 + perf(bench_close, Q2) * 0.2
               + perf(bench_close, Q3) * 0.2 + perf(bench_close, Q4) * 0.2)
    bench_w = bench_w.reindex(c.index).ffill()
    rs_diff = stock_w - bench_w
    rs_score = (50 + rs_diff).clip(0, 100)

    # EMA ribbon
    m1, m2, m3, m4, m5 = ema(c, E1), ema(c, E2), ema(c, E3), ema(c, E4), ema(c, E5)
    bull = (c > m1) & (m1 > m2) & (m2 > m3) & (m3 > m4) & (m4 > m5)
    width = (m1 - m5) / m3 * 100
    expanding = width > width.shift(1)
    above = (c > m1) & (c > m2) & (c > m3)

    ema_score = pd.Series(50.0, index=c.index)
    ema_score = ema_score.mask(above, 50)
    ema_score = ema_score.mask(above & expanding, 65)
    ema_score = ema_score.mask(bull & above, 80)
    ema_score = ema_score.mask(bull & expanding & above, 100)
    ema_score = ema_score.mask(~above, 25)

    tw = RS_WEIGHT + EMA_WEIGHT
    comp = rs_score * (RS_WEIGHT / tw) + ema_score * (EMA_WEIGHT / tw)

    out = pd.DataFrame(index=c.index)
    out["close"] = c
    out["ema21"] = m3
    out["atr"] = atr(df, ATR_LEN)
    out["rs"] = rs_score
    out["comp"] = comp
    out["entry"] = (comp > ENTRY_THRESH) & (rs_score > RS_MIN)
    return out


def simulate(sig: pd.DataFrame) -> list[dict]:
    """Long-only, one position at a time. Returns list of trades with R-multiples."""
    trades = []
    in_pos = False
    entry_px = stop_px = tp_px = risk = 0.0
    entry_date = None
    idx = sig.index
    for i in range(Q4, len(sig)):
        row = sig.iloc[i]
        if np.isnan(row["atr"]) or row["atr"] <= 0:
            continue
        if not in_pos:
            if row["entry"]:
                entry_px = row["close"]
                stop_dist = row["atr"] * ATR_STOP_MULT
                stop_px = entry_px - stop_dist
                tp_px = entry_px + stop_dist * TP_RATIO
                risk = stop_dist
                entry_date = idx[i]
                in_pos = True
        else:
            hi, lo, cl = sig.iloc[i]["close"], sig.iloc[i]["close"], row["close"]
            exit_px = None
            reason = None
            # intrabar approximation on daily close: check stop, then target, then trend break
            if cl <= stop_px:
                exit_px, reason = stop_px, "stop"
            elif USE_TP and cl >= tp_px:
                exit_px, reason = tp_px, "target"
            elif cl < row["ema21"]:
                exit_px, reason = cl, "trend_break"
            if exit_px is not None:
                r_mult = (exit_px - entry_px) / risk if risk > 0 else 0.0
                trades.append({
                    "entry_date": str(entry_date.date()), "exit_date": str(idx[i].date()),
                    "entry": round(entry_px, 2), "exit": round(exit_px, 2),
                    "reason": reason, "r": round(r_mult, 3),
                    "bars": i - idx.get_loc(entry_date),
                })
                in_pos = False
    return trades


def report(all_trades: dict[str, list[dict]]) -> None:
    flat = [t for ts in all_trades.values() for t in ts]
    if not flat:
        print("\nNo trades generated.")
        return
    rs = np.array([t["r"] for t in flat])
    wins = rs[rs > 0]
    losses = rs[rs <= 0]
    win_rate = len(wins) / len(rs) * 100
    avg_r = rs.mean()
    expectancy = avg_r  # per-trade R expectancy
    gross_win = wins.sum()
    gross_loss = abs(losses.sum())
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_bars = np.mean([t["bars"] for t in flat])

    print("\n" + "=" * 64)
    print("  APEX PHASE 0 — CONCEPT BACKTEST RESULTS")
    print("=" * 64)
    print(f"  Trades:          {len(rs)}")
    print(f"  Win rate:        {win_rate:.1f}%")
    print(f"  Avg R/trade:     {avg_r:+.3f}R   (expectancy)")
    print(f"  Profit factor:   {pf:.2f}")
    print(f"  Best / Worst:    {rs.max():+.2f}R / {rs.min():+.2f}R")
    print(f"  Avg hold (bars): {avg_bars:.1f} days")
    print(f"  Total R:         {rs.sum():+.1f}R")
    print("\n  BY SYMBOL:")
    print(f"  {'SYM':6s} {'N':>3s} {'WIN%':>6s} {'AVG R':>7s} {'TOT R':>7s}")
    print("  " + "-" * 34)
    for sym, ts in sorted(all_trades.items(), key=lambda kv: -sum(t["r"] for t in kv[1])):
        if not ts:
            continue
        r = np.array([t["r"] for t in ts])
        wr = (r > 0).mean() * 100
        print(f"  {sym:6s} {len(r):>3d} {wr:>5.0f}% {r.mean():>+6.2f}R {r.sum():>+6.1f}R")
    print("=" * 64)

    # Verdict
    print("\n  VERDICT:")
    if expectancy > 0.1 and pf > 1.3:
        print(f"  PROMISING — +{expectancy:.3f}R/trade, PF {pf:.2f}. Proceed to Phase 1")
        print("  (true universe-percentile RS should improve on this).")
    elif expectancy > 0:
        print(f"  MARGINAL — +{expectancy:.3f}R/trade, PF {pf:.2f}. Edge is thin; Phase 1's")
        print("  real RS + better entry timing must carry it. Worth a careful Phase 1.")
    else:
        print(f"  NEGATIVE — {expectancy:.3f}R/trade, PF {pf:.2f}. Concept as-is does not work;")
        print("  rethink filters/exits before investing in the Python build.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=3.0)
    ap.add_argument("--symbols", nargs="*", default=DEFAULT_BASKET)
    ap.add_argument("--no-tp", action="store_true", help="disable take-profit (let winners run)")
    args = ap.parse_args()

    global USE_TP
    if args.no_tp:
        USE_TP = False
        print("[let-it-run mode: take-profit disabled, exit only on stop/trend break]")

    if not ALPACA_API_KEY:
        print("ERROR: ALPACA_API_KEY not set in .env")
        sys.exit(1)

    syms = list(args.symbols)
    print(f"APEX Phase 0 backtest | {len(syms)} symbols | {args.years}y daily bars")
    data = fetch_daily(syms + [BENCHMARK], args.years)
    if BENCHMARK not in data:
        print(f"ERROR: could not fetch benchmark {BENCHMARK}")
        sys.exit(1)
    bench_close = data[BENCHMARK]["close"]

    all_trades: dict[str, list[dict]] = {}
    for sym in syms:
        if sym not in data:
            print(f"  skip {sym}: no data")
            continue
        sig = compute_signals(data[sym], bench_close)
        all_trades[sym] = simulate(sig)

    report(all_trades)


if __name__ == "__main__":
    main()
