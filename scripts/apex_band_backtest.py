#!/usr/bin/env python3
"""
APEX — price-band backtest (standalone).

Question this answers: within the APEX RS-leader universe, does focusing entries on
lower-priced names ($2-30) actually compound the account faster than taking the whole
price range? It buckets the SAME live VWAP_PB trigger by entry price band and reports
quality (win%, avg R) AND realized $/trade under the REAL whole-share sizer — so you can
see both the edge and the compounding impact (where the forced-1-share sizing on high-
priced names quietly hurts).

It reuses the live machinery, not a reinvention:
  - entry trigger : VWAP_PB, identical detection to apex_phase2_entry_backtest.simulate
  - stop          : min(ATR_STOP_MULT*ATR14, MAX_STOP_PCT*entry)  (apex_execute.py:113)
  - sizing        : qty = max(1, min(floor(risk$/stop_dist), floor(equity/price)))  (apex_execute.py:115-118)
  - risk$, equity, params : pulled from apex_config so the test tracks live settings

It also reports the proposed sizer fix (skip when one share busts the risk cap, i.e.
raw_qty < 1) side-by-side, so you can see exactly which trades that change removes and
what it does to each band's P&L.

Exit model: intraday: if the post-entry low pierces the stop -> stopped at the stop (-1R);
otherwise exit at the same-day close. (Layer-3 health exits are path-dependent and not
modeled here; same-day-close + hard stop is a conservative, sizing-faithful proxy.)

Usage:
  python -W ignore scripts/apex_band_backtest.py [--days 30] [--top 60]
                                                 [--equity N] [--min-dvol 0]
"""

from __future__ import annotations

import argparse
import json
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

import os
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ET = "America/New_York"

SESS_OPEN  = dtime(9, 30)
SESS_CLOSE = dtime(16, 0)
ORB_END    = dtime(9, 45)

# ── live params (defaults from apex_config; overridable via CLI) ───────────────
try:
    from scripts import apex_config as cfg  # noqa
except Exception:
    try:
        import apex_config as cfg  # type: ignore
        sys.path.insert(0, str(WORKSPACE / "scripts"))
    except Exception:
        cfg = None

def _cfg(name, default):
    return getattr(cfg, name, default) if cfg else default

DEF_EQUITY        = _cfg("ACCOUNT_EQUITY", 5000.0)
DEF_RISK_PCT      = _cfg("RISK_PCT_TUNABLE", 1.0)
DEF_MAX_RISK      = _cfg("MAX_RISK_GUARD", 150.0)
DEF_ATR_STOP_MULT = _cfg("ATR_STOP_MULT", 2.0)
DEF_MAX_STOP_PCT  = _cfg("MAX_STOP_PCT", 0.10)
ATR_LEN           = _cfg("ATR_LEN", 14)

# price bands (lower edge inclusive)
BANDS = [(0, 2), (2, 10), (10, 30), (30, 100), (100, 500), (500, 1e9)]
BAND_LABELS = ["<$2", "$2-10", "$10-30", "$30-100", "$100-500", ">$500"]
# liquidity tiers on entry-price * avg daily volume ($/day)
DVOL_TIERS = [(0, 5e6), (5e6, 50e6), (50e6, 1e12)]
DVOL_LABELS = ["<$5M/d", "$5-50M/d", ">$50M/d"]


def _band(p):
    for i, (lo, hi) in enumerate(BANDS):
        if lo <= p < hi:
            return BAND_LABELS[i]
    return BAND_LABELS[-1]


def _dvol_tier(dv):
    for i, (lo, hi) in enumerate(DVOL_TIERS):
        if lo <= dv < hi:
            return DVOL_LABELS[i]
    return DVOL_LABELS[-1]


def _data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)


def load_leaders(top):
    path = SHARED / "apex-leaders.json"
    if not path.exists():
        print("  no apex-leaders.json — run apex_daily_filter.py first")
        return []
    data = json.loads(path.read_text())
    return [l["symbol"] for l in data.get("leaders", [])[:top]]


def fetch_5min(symbols, days):
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


def fetch_daily(symbols, days):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    dc = _data_client()
    start = datetime.now() - timedelta(days=days + 40)  # extra for ATR warmup
    req = StockBarsRequest(symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
                           start=start, feed="sip", adjustment="all")
    return dc.get_stock_bars(req).df


def regular_session(intra):
    idx = intra.index.get_level_values("timestamp").tz_convert(ET)
    intra = intra.copy()
    intra["et"] = idx
    intra["day"] = idx.date
    intra["tod"] = idx.time
    return intra[(intra["tod"] >= SESS_OPEN) & (intra["tod"] < SESS_CLOSE)]


def atr_series(daily_df):
    """ATR14 per day on daily bars, shifted one day (use prior-day ATR -> no lookahead)."""
    d = daily_df.sort_index()
    prev_close = d["close"].shift(1)
    tr = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(ATR_LEN).mean().shift(1)  # as-of prior session
    avg_vol = d["volume"].rolling(20).mean().shift(1)
    out = pd.DataFrame({"atr": atr, "avg_vol": avg_vol})
    out.index = [ts.date() for ts in out.index]
    return out


def simulate_symbol(sym, intra, atr_df, p):
    """One row per VWAP_PB entry, with stop/exit/R and live + fixed-sizer P&L."""
    out = []
    for day, g in intra.groupby("day"):
        g = g.sort_values("et")
        if len(g) < 6:
            continue
        o = g.iloc[0]["open"]
        tp = (g["high"] + g["low"] + g["close"]) / 3
        vwap = (tp * g["volume"]).cumsum() / g["volume"].cumsum()
        g = g.assign(vwap=vwap.values)

        # VWAP_PB: after an early >1% push, first bar that dips to/below vwap and closes above
        pushed = False
        entry_px = None
        entry_et = None
        for _, bar in g.iterrows():
            if bar["high"] > o * 1.01:
                pushed = True
            if pushed and bar["low"] <= bar["vwap"] and bar["close"] > bar["vwap"]:
                entry_px = float(bar["close"])
                entry_et = bar["et"]
                break
        if entry_px is None or entry_px <= 0:
            continue

        a = atr_df.loc[day] if day in atr_df.index else None
        if a is None or pd.isna(a["atr"]) or a["atr"] <= 0:
            continue
        atr = float(a["atr"])
        avg_vol = float(a["avg_vol"]) if not pd.isna(a["avg_vol"]) else 0.0

        stop_dist = min(atr * p["atr_stop_mult"], entry_px * p["max_stop_pct"])
        if stop_dist <= 0:
            continue
        stop_px = entry_px - stop_dist

        after = g[g["et"] > entry_et]
        post_low = float(after["low"].min()) if not after.empty else entry_px
        close_px = float(g.iloc[-1]["close"])
        exit_px = stop_px if post_low <= stop_px else close_px
        r_mult = (exit_px - entry_px) / stop_dist

        # live sizer (apex_execute.py:115-118)
        risk_d = min(p["equity"] * (p["risk_pct"] / 100.0), p["max_risk"])
        raw_qty = int(np.floor(risk_d / stop_dist))
        cash_qty = int(np.floor(p["equity"] / entry_px))
        qty_live = max(1, min(raw_qty, cash_qty))
        qty_fixed = min(raw_qty, cash_qty)           # proposed fix: 0 -> skip
        forced = raw_qty < 1                          # one share busts the risk cap

        dvol = entry_px * avg_vol
        out.append({
            "symbol": sym, "day": str(day), "entry": round(entry_px, 2),
            "band": _band(entry_px), "dvol": dvol, "dtier": _dvol_tier(dvol),
            "r": r_mult, "win": exit_px > entry_px,
            "pnl_live": (exit_px - entry_px) * qty_live,
            "pnl_fixed": (exit_px - entry_px) * qty_fixed,
            "qty_live": qty_live, "forced": forced, "stop_dist": stop_dist,
        })
    return out


def _agg(df, key, labels):
    print(f"  {key.upper():10s} {'N':>4s} {'WIN%':>6s} {'AVG_R':>7s} {'EXP_R':>7s} "
          f"{'$/T·live':>9s} {'$/T·fix':>9s} {'SKIP':>5s} {'TOT$·live':>10s}")
    print("  " + "-" * 76)
    for lab in labels:
        sub = df[df[key] == lab]
        if sub.empty:
            continue
        n = len(sub)
        win = sub["win"].mean() * 100
        avg_r = sub["r"].mean()
        # expectancy R uses the fixed sizer view (skipped trades excluded from count)
        kept = sub[~sub["forced"]]
        exp_r = kept["r"].mean() if not kept.empty else float("nan")
        ppt_live = sub["pnl_live"].mean()
        ppt_fix = kept["pnl_fixed"].mean() if not kept.empty else 0.0
        skip = int(sub["forced"].sum())
        tot_live = sub["pnl_live"].sum()
        print(f"  {lab:10s} {n:>4d} {win:>5.0f}% {avg_r:>+6.2f} {exp_r:>+6.2f} "
              f"{ppt_live:>+8.2f} {ppt_fix:>+8.2f} {skip:>5d} {tot_live:>+9.0f}")
    print()


def report(rows, p, args):
    if not rows:
        print("\nNo VWAP_PB entries produced over the window.")
        return
    df = pd.DataFrame(rows)
    if args.min_dvol > 0:
        before = len(df)
        df = df[df["dvol"] >= args.min_dvol]
        print(f"\n  (liquidity filter: dropped {before - len(df)} entries below "
              f"${args.min_dvol/1e6:.0f}M/day dollar-volume)")
    n_days = df["day"].nunique()
    n_syms = df["symbol"].nunique()
    print("\n" + "=" * 80)
    print(f"  APEX PRICE-BAND BACKTEST — VWAP_PB  ({n_syms} leaders, {n_days} days, "
          f"{len(df)} entries)")
    print(f"  equity ${p['equity']:,.0f} | risk {p['risk_pct']:.1f}%/trade cap ${p['max_risk']:.0f} "
          f"| stop min({p['atr_stop_mult']}·ATR, {p['max_stop_pct']*100:.0f}%)")
    print("=" * 80)
    print("  $/T·live = avg $ P&L per trade, current max(1,..) sizer")
    print("  $/T·fix  = avg $ P&L per KEPT trade, proposed raw_qty<1 -> skip sizer")
    print("  SKIP     = trades the proposed sizer drops (1 share busts the risk cap)\n")

    _agg(df, "band", BAND_LABELS)
    _agg(df, "dtier", DVOL_LABELS)

    # sizing-bug impact
    forced = df[df["forced"]]
    print("  " + "-" * 76)
    print("  FORCED-1-SHARE IMPACT (the sizing bug):")
    if forced.empty:
        print("    none — every entry sized within the risk cap.")
    else:
        print(f"    {len(forced)} entries forced to 1 share (1 share's stop > ${p['max_risk']:.0f} cap)")
        print(f"    their P&L under current sizer : ${forced['pnl_live'].sum():+,.0f} "
              f"(win {forced['win'].mean()*100:.0f}%, avg {forced['r'].mean():+.2f}R)")
        print(f"    bands affected               : "
              f"{', '.join(sorted(forced['band'].unique(), key=BAND_LABELS.index))}")
        print(f"    proposed fix would SKIP them → removes that P&L line entirely.")
    print()

    # verdict
    print("  " + "=" * 76)
    by_band = df.groupby("band")
    rk = {b: g["r"].mean() for b, g in by_band if len(g) >= 5}
    pk = {b: g["pnl_live"].mean() for b, g in by_band if len(g) >= 5}
    if rk:
        best_r = max(rk, key=rk.get)
        best_p = max(pk, key=pk.get)
        print(f"  BEST avg-R band   : {best_r}  ({rk[best_r]:+.2f}R/trade)")
        print(f"  BEST $/trade band : {best_p}  (${pk[best_p]:+.2f}/trade, current sizer)")
        cheap = df[df["band"].isin(["$2-10", "$10-30"])]
        rest = df[~df["band"].isin(["$2-10", "$10-30", "<$2"])]
        if not cheap.empty and not rest.empty:
            print(f"  $2-30 band        : {len(cheap):>3d} entries, win {cheap['win'].mean()*100:.0f}%, "
                  f"{cheap['r'].mean():+.2f}R, ${cheap['pnl_live'].mean():+.2f}/trade")
            print(f"  >$30 names        : {len(rest):>3d} entries, win {rest['win'].mean()*100:.0f}%, "
                  f"{rest['r'].mean():+.2f}R, ${rest['pnl_live'].mean():+.2f}/trade")
    print("  " + "=" * 76)
    print("  NOTE: forward outcome = same-day close or stop hit; Layer-3 health exits not")
    print("  modeled. R is sizing-independent (quality); $/trade reflects the whole-share")
    print("  sizer (compounding). Today's-leaders applied to past days => directional, not")
    print("  a P&L promise. Re-run across more --days before committing a universe change.\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--top", type=int, default=60, help="top-N leaders to test")
    ap.add_argument("--equity", type=float, default=DEF_EQUITY)
    ap.add_argument("--risk-pct", type=float, default=DEF_RISK_PCT)
    ap.add_argument("--max-risk", type=float, default=DEF_MAX_RISK)
    ap.add_argument("--atr-stop", type=float, default=DEF_ATR_STOP_MULT)
    ap.add_argument("--max-stop-pct", type=float, default=DEF_MAX_STOP_PCT)
    ap.add_argument("--min-dvol", type=float, default=0.0,
                    help="drop entries below this $/day dollar-volume (e.g. 5e6)")
    args = ap.parse_args()

    if not ALPACA_API_KEY:
        print("ERROR: ALPACA_API_KEY not set")
        sys.exit(1)

    p = {"equity": args.equity, "risk_pct": args.risk_pct, "max_risk": args.max_risk,
         "atr_stop_mult": args.atr_stop, "max_stop_pct": args.max_stop_pct}

    leaders = load_leaders(args.top)
    if not leaders:
        sys.exit(1)
    print(f"APEX price-band backtest | {len(leaders)} leaders | {args.days} days | "
          f"equity ${args.equity:,.0f}")

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
            sd = daily_all.xs(sym, level="symbol")
        except (KeyError, Exception):
            continue
        if si.empty or sd.empty:
            continue
        rows += simulate_symbol(sym, si, atr_series(sd), p)

    report(rows, p, args)


if __name__ == "__main__":
    main()
