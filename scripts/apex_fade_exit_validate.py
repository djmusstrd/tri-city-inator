#!/usr/bin/env python
"""
APEX — does the REAL fade detector (compute_health) protect profit on graduated winners
WITHOUT capping runners?  Analysis only; touches nothing live.

Tests the operator's actual idea (not the distance-trail that failed): the rollover is a
detectable multi-bar STATE, and APEX already scores it via compute_health (below VWAP / below
EMA9 / 3 declining closes / lower highs). Today that exit is switched OFF for `swing` positions
(apex_health.py:499) — which is why RXT round-tripped 7.22->6.76 intraday unmanaged.

Compares, on a large historical ORB15-proxy trade set:
  (a) BASELINE  — no intraday fade exit: hold to EOD, hard stop only   [= current swing behavior]
  (b) FADE-EXIT — exit when compute_health < EXIT_H, once ARMED (had a real run-up)   [proposed]
Reports head-to-head P&L, win%, peak-profit retention, + block-bootstrap Monte Carlo.
"""
import argparse, datetime, random
import pandas as pd
import apex_poller as ap
import apex_config as cfg
from apex_health import compute_health

def col(df, *n):
    low = {c.lower(): c for c in df.columns}
    for x in n:
        if x in df.columns: return x
        if x.lower() in low: return low[x.lower()]
    return None

def session_dates(days):
    out, d = [], datetime.date.today()
    while len(out) < days:
        d -= datetime.timedelta(days=1)
        if d.weekday() < 5: out.append(d)
    return sorted(out)

def norm(df):
    """Return a clean frame with high/low/close/volume/tod."""
    h, l, c, v = col(df, "high"), col(df, "low"), col(df, "close"), col(df, "volume")
    if not all([h, l, c, v]): return None
    g = pd.DataFrame({"high": df[h].astype(float), "low": df[l].astype(float),
                      "close": df[c].astype(float), "volume": df[v].astype(float)})
    g["tod"] = range(len(g))
    return g.reset_index(drop=True)

def gen_trades(universe, dates, orb_min, atr_stop_mult):
    trades = []
    for d in dates:
        try: day = ap.fetch_intraday(universe, d)
        except Exception: continue
        for s in universe:
            raw = day.get(s)
            if raw is None or getattr(raw, "empty", True) or len(raw) < 12: continue
            g = norm(raw.sort_index())
            if g is None or len(g) < 12: continue
            orb = g.iloc[:orb_min]
            orh = float(orb["high"].max())
            post = g.iloc[orb_min:].reset_index(drop=True)
            ei = next((i for i in range(len(post)) if post["high"].iloc[i] >= orh), None)
            if ei is None: continue
            path = post.iloc[ei:].reset_index(drop=True)
            if len(path) < 3: continue
            atr_i = float((path["high"] - path["low"]).mean())
            if atr_i <= 0: continue
            trades.append(dict(sym=s, date=str(d), entry=orh, path=path,
                               stop=orh - atr_stop_mult * atr_i))
    return trades

def _qty(entry): return max(1.0, round(1000.0 / entry))

def run_trade(t, fade, exit_h, arm_pct):
    """Return (pnl, peak_gain_dollars). fade=False -> baseline (EOD/stop)."""
    entry = t["entry"]; q = _qty(entry); path = t["path"]; pos = {"entry": entry}
    peak = entry; armed = False
    for i in range(len(path)):
        bar = path.iloc[i]
        peak = max(peak, float(bar["high"]))
        if float(bar["low"]) <= t["stop"]:                      # hard stop first (both policies)
            return (t["stop"] - entry) * q, (peak - entry) * q
        if not fade: continue
        if peak >= entry * (1 + arm_pct): armed = True
        if not armed: continue
        hh = compute_health(pos, path.iloc[:i+1], live_price=float(bar["close"]))
        if hh and hh["health"] < exit_h:
            return (float(bar["close"]) - entry) * q, (peak - entry) * q
    return (float(path["close"].iloc[-1]) - entry) * q, (peak - entry) * q

def summarize(trades, fade, exit_h, arm_pct, label):
    pnls, rets = [], []
    for t in trades:
        pnl, peak = run_trade(t, fade, exit_h, arm_pct)
        pnls.append(pnl)
        if peak > 0: rets.append(max(0.0, pnl) / peak)
    n = len(pnls); wins = sum(1 for x in pnls if x > 0)
    print(f"  {label:34} P&L ${sum(pnls):+10.2f}  win {100*wins/n:.0f}%  "
          f"avg peak-profit retained {100*sum(rets)/max(len(rets),1):.0f}%")
    return pnls

def mc_block(trades, fade, exit_h, arm_pct, iters, seed=7):
    random.seed(seed)
    by_day = {}
    for t in trades: by_day.setdefault(t["date"], []).append(t)
    daypnl = {d: sum(run_trade(t, fade, exit_h, arm_pct)[0] for t in ts) for d, ts in by_day.items()}
    days = list(daypnl); tot, dd = [], []
    for _ in range(iters):
        seq = [daypnl[random.choice(days)] for _ in range(len(days))]
        tot.append(sum(seq)); eq = pk = mx = 0.0
        for x in seq:
            eq += x; pk = max(pk, eq); mx = min(mx, eq - pk)
        dd.append(mx)
    p = lambda a, q: round(sorted(a)[int(q*len(a))], 0)
    return p(tot,.05), p(tot,.50), p(tot,.95), p(dd,.50)

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--days", type=int, default=40)
    a.add_argument("--exit_h", type=int, default=40)
    a.add_argument("--arm", type=float, default=0.02)
    a.add_argument("--mc", type=int, default=1500)
    a.add_argument("--orb", type=int, default=cfg.ORB_MINUTES)
    a.add_argument("--atrstop", type=float, default=1.5)
    A = a.parse_args()
    import json
    universe = [l["symbol"] for l in json.load(open(cfg.SHARED/"apex-leaders.json")).get("leaders",[])][:60]
    dates = session_dates(A.days)
    trades = gen_trades(universe, dates, A.orb, A.atrstop)
    print(f"[fade-test] {len(trades)} trades, {len(universe)} symbols, {dates[0]}..{dates[-1]}, "
          f"exit_h={A.exit_h}, arm=+{A.arm*100:.0f}%\n")
    if len(trades) < 30: print("too few trades"); return

    print("HEAD-TO-HEAD (full sample):")
    summarize(trades, False, A.exit_h, A.arm, "(a) baseline (hold EOD/stop)")
    summarize(trades, True,  A.exit_h, A.arm, f"(b) fade-exit (health<{A.exit_h}, armed)")
    # sensitivity on the one real knob
    for eh in (35, 50):
        summarize(trades, True, eh, A.arm, f"    fade-exit health<{eh} (sens.)")

    print(f"\nMONTE CARLO (block-bootstrap by day, {A.mc} iters):")
    for fade, lbl in [(False,"(a) baseline "),(True,"(b) fade-exit")]:
        p5,p50,p95,ddm = mc_block(trades, fade, A.exit_h, A.arm, A.mc)
        print(f"  {lbl}  P&L p5/p50/p95 ${p5}/{p50}/{p95}   maxDD p50 ${ddm}")
    print("\n[note] ORB15-proxy entry + fixed universe + one window. Structural test of the "
          "fade-exit-on-winners idea vs unmanaged hold; not a live go-ahead by itself.")

if __name__ == "__main__":
    main()
