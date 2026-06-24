#!/usr/bin/env python
"""
APEX dynamic give-back exit — VALIDATION HARNESS (B).  Analysis only; touches nothing live.

Decision-grade validation the 67-trade sanity check (apex_giveback_backtest.py) cannot provide:
  1. HISTORICAL REPLAY — generate a large, less-correlated trade set by replaying a self-contained
     ORB15 entry proxy over a date window across the leader universe (exit policy is what's under
     test, so entry is a deterministic proxy with a known entry bar).
  2. WALK-FORWARD — rolling train/test: grid-search give-back params on each train window, score on
     the *next* (out-of-sample by construction); accumulate OOS results. No look-ahead.
  3. MONTE CARLO — BLOCK-bootstrap by trading day (resample whole sessions, not individual trades)
     to respect intraday correlation -> P&L and max-drawdown confidence bands.

Baseline exit (the thing give-back must beat) = intraday ATR hard-stop OR EOD close, whichever first.
Give-back exit = dynamic %-band policy (tier-scaled, day/swing mult, confirmation gate).

Documented assumptions (v1):
  - Universe is FIXED (current leaders file), not reconstructed per historical day.
  - Entry = ORB15 break proxy; this validates the EXIT layer, not entry selection.
  - %-bands here; ATR-normalized bands are a follow-up once this plumbing is trusted.
Run:  python apex_giveback_validate.py --days 60 --train 20 --test 5 --mc 2000
"""
import argparse, datetime, statistics, random
import pandas as pd
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
import apex_poller as ap
import apex_config as cfg

def col(df, *names):
    low = {c.lower(): c for c in df.columns}
    for n in names:
        if n in df.columns: return n
        if n.lower() in low: return low[n.lower()]
    return None

def session_dates(days):
    out, d = [], datetime.date.today()
    while len(out) < days:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5: out.append(d)
    return sorted(out)

# ---- 1. HISTORICAL REPLAY: ORB15 entry proxy + per-trade bar path ----
def gen_trades(universe, dates, orb_min, atr_stop_mult):
    trades = []
    for d in dates:
        try: day = ap.fetch_intraday(universe, d)
        except Exception: continue
        for s in universe:
            df = day.get(s)
            if df is None or getattr(df, "empty", True) or len(df) < 10: continue
            h = col(df, "high"); l = col(df, "low"); c = col(df, "close")
            df = df.sort_index()
            orb = df.iloc[:orb_min] if len(df) > orb_min else df
            orh = float(orb[h].max())
            rng = float(orb[h].max() - orb[l].min())
            if rng <= 0: continue
            post = df.iloc[orb_min:]
            entry_i = None
            for i in range(len(post)):
                if float(post.iloc[i][h]) >= orh:
                    entry_i = i; break
            if entry_i is None: continue
            entry = orh
            path = post.iloc[entry_i:]
            if len(path) < 2: continue
            atr_intraday = float((path[h] - path[l]).mean())  # intraday vol unit
            if atr_intraday <= 0: continue
            trades.append(dict(sym=s, date=str(d), entry=entry, atr=atr_intraday,
                               highs=list(path[h].astype(float)),
                               lows=list(path[l].astype(float)),
                               closes=list(path[c].astype(float)),
                               stop=entry - atr_stop_mult * atr_intraday))
    return trades

# ---- exit sims (per trade, qty normalized to $1000 notional for comparability) ----
def _qty(entry): return max(1.0, round(1000.0 / entry))

def baseline_exit(t):
    q = _qty(t["entry"])
    for lo, cl in zip(t["lows"], t["closes"]):
        if lo <= t["stop"]:
            return (t["stop"] - t["entry"]) * q
    return (t["closes"][-1] - t["entry"]) * q  # EOD

def tier_band(peak_pct, P):
    if peak_pct <= 5: return P["b1"]
    if peak_pct <= 10: return P["b2"]
    return P["b3"]

def giveback_exit(t, P):
    entry = t["entry"]; q = _qty(entry); peak = entry; armed = False; below = 0
    arm = entry * (1 + P["arm_pct"])
    for hi, lo, cl in zip(t["highs"], t["lows"], t["closes"]):
        peak = max(peak, hi)
        if peak >= arm: armed = True
        if lo <= t["stop"]:                       # hard stop always first
            return (t["stop"] - entry) * q
        if not armed: continue
        band = tier_band((peak - entry) / entry * 100, P) * P["day_mult"]
        stop = peak * (1 - band / 100)
        if P["confirm"]:
            below = below + 1 if cl <= stop else 0
            if below >= 2: return (cl - entry) * q
        else:
            if lo <= stop: return (stop - entry) * q
    return (t["closes"][-1] - entry) * q

# ---- 2. WALK-FORWARD ----
GRID = [dict(arm_pct=a, b1=b1, b2=b2, b3=b3, day_mult=dm, confirm=cf)
        for a in (0.02, 0.03, 0.05)
        for (b1, b2, b3) in ((3.0, 2.5, 2.0), (2.5, 2.0, 1.5), (4.0, 3.0, 2.5))
        for dm in (0.8, 1.0)
        for cf in (True, False)]

def walk_forward(trades, train_n, test_n):
    by_day = {}
    for t in trades: by_day.setdefault(t["date"], []).append(t)
    days = sorted(by_day)
    oos_gb, oos_base = [], []
    chosen = []
    i = 0
    while i + train_n + test_n <= len(days):
        train = [t for d in days[i:i+train_n] for t in by_day[d]]
        test  = [t for d in days[i+train_n:i+train_n+test_n] for t in by_day[d]]
        if train and test:
            best = max(GRID, key=lambda P: sum(giveback_exit(t, P) for t in train))
            chosen.append(best)
            oos_gb   += [giveback_exit(t, best) for t in test]
            oos_base += [baseline_exit(t) for t in test]
        i += test_n
    return oos_gb, oos_base, chosen, days

# ---- 3. MONTE CARLO: block-bootstrap by day ----
def mc_block(trades, exitfn, iters, seed=7):
    random.seed(seed)
    by_day = {}
    for t in trades: by_day.setdefault(t["date"], []).append(t)
    day_pnls = {d: sum(exitfn(t) for t in ts) for d, ts in by_day.items()}
    days = list(day_pnls); n = len(days)
    totals, maxdds = [], []
    for _ in range(iters):
        seq = [day_pnls[random.choice(days)] for _ in range(n)]
        totals.append(sum(seq))
        eq = 0.0; peak = 0.0; dd = 0.0
        for x in seq:
            eq += x; peak = max(peak, eq); dd = min(dd, eq - peak)
        maxdds.append(dd)
    p = lambda a, q: round(sorted(a)[int(q*len(a))], 2)
    return dict(pnl_p5=p(totals,.05), pnl_p50=p(totals,.50), pnl_p95=p(totals,.95),
                dd_p5=p(maxdds,.05), dd_p50=p(maxdds,.50))

def main():
    ap_ = argparse.ArgumentParser()
    ap_.add_argument("--days", type=int, default=60)
    ap_.add_argument("--train", type=int, default=20)
    ap_.add_argument("--test", type=int, default=5)
    ap_.add_argument("--mc", type=int, default=2000)
    ap_.add_argument("--orb", type=int, default=cfg.ORB_MINUTES)
    ap_.add_argument("--atrstop", type=float, default=1.5)
    A = ap_.parse_args()

    import json
    lf = cfg.SHARED / "apex-leaders.json"
    universe = [l["symbol"] for l in json.load(open(lf)).get("leaders", [])][:60]
    dates = session_dates(A.days)
    print(f"[B] replay {len(universe)} symbols x {len(dates)} sessions "
          f"({dates[0]}..{dates[-1]}), ORB{A.orb}, atr-stop {A.atrstop}x")
    trades = gen_trades(universe, dates, A.orb, A.atrstop)
    print(f"[B] generated {len(trades)} trades across {len(set(t['date'] for t in trades))} active days\n")
    if len(trades) < 30:
        print("[B] too few trades — widen --days/universe."); return

    oos_gb, oos_base, chosen, days = walk_forward(trades, A.train, A.test)
    if oos_gb:
        wr = lambda a: 100*sum(1 for x in a if x>0)/len(a)
        print(f"WALK-FORWARD (out-of-sample, {len(oos_gb)} test trades over "
              f"{(len(days)-A.train)//A.test} folds):")
        print(f"  give-back OOS  P&L ${sum(oos_gb):+9.2f}  win {wr(oos_gb):.0f}%")
        print(f"  baseline  OOS  P&L ${sum(oos_base):+9.2f}  win {wr(oos_base):.0f}%")
        print(f"  OOS delta      ${sum(oos_gb)-sum(oos_base):+9.2f}")
        confs = sum(1 for c in chosen if c['confirm'])
        print(f"  confirm=ON chosen in {confs}/{len(chosen)} train windows\n")
    else:
        print("WALK-FORWARD: not enough days for the chosen train/test split\n")

    print(f"MONTE CARLO (block-bootstrap by day, {A.mc} iters):")
    mg = mc_block(trades, lambda t: giveback_exit(t, GRID[0]), A.mc)
    mb = mc_block(trades, baseline_exit, A.mc)
    print(f"  give-back  P&L p5/p50/p95 ${mg['pnl_p5']}/{mg['pnl_p50']}/{mg['pnl_p95']}  "
          f"maxDD p50 ${mg['dd_p50']}")
    print(f"  baseline   P&L p5/p50/p95 ${mb['pnl_p5']}/{mb['pnl_p50']}/{mb['pnl_p95']}  "
          f"maxDD p50 ${mb['dd_p50']}")
    print("\n[B] NOTE: %-bands + ORB15-proxy entry + fixed universe. Treat as structural validation; "
          "re-run with ATR-bands + longer window before any live decision.")

if __name__ == "__main__":
    main()
