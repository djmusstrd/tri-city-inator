#!/usr/bin/env python
"""
APEX dynamic give-back exit — BACKTEST (analysis only; does NOT touch the live system).

Replays each journaled trade over its actual intraday path (entry_time -> exit time, across
session days) and simulates the dynamic give-back policy from PRD_APEX_DYNAMIC_EXIT.md:
  - ARM only after peak gain >= arm_atr * ATR  (don't trail noise)
  - allowed give-back band = tier(peak_gain_in_ATR) * ATR, scaled by day/swing tier
  - confirmation gate (tunable): off = intrabar low touches band; on = close confirms (2 bars)
Compares total P&L / win-rate / avg give-back vs the actual (baseline) journal results.

Day-tier vs swing-tier (resolved §8.1: day-trades-that-graduate): a trade is day-tier if it
exited the same session it entered; swing-tier if it carried overnight (graduated).
"""
import json, datetime, sys
import pandas as pd
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")
import apex_poller as ap
import apex_config as cfg
from apex_poller import atr_from_daily

JOURNAL = cfg.SHARED.parent / "logs" / "apex-journal.json"

def col(df, *names):
    low = {c.lower(): c for c in df.columns}
    for n in names:
        if n in df.columns: return n
        if n.lower() in low: return low[n.lower()]
    return None

# ---- load trades ----
J = json.load(open(JOURNAL))
trades = [e for e in J if e.get("entry") and e.get("exit") and e.get("qty") and e.get("entry_time")]
syms = sorted(set(e["symbol"] for e in trades))

# ---- daily ATR (price) per symbol ----
daily = ap.fetch_daily(syms)
atr_px = {}
for s in syms:
    try:
        dsub = daily.xs(s, level="symbol")
        a = atr_from_daily(dsub, cfg.ATR_LEN)
        atr_px[s] = float(a) if a and a > 0 else None
    except Exception:
        atr_px[s] = None

# ---- intraday bars cached per session date ----
_daycache = {}
def get_day(d):
    if d not in _daycache:
        try: _daycache[d] = ap.fetch_intraday(syms, d)
        except Exception: _daycache[d] = {}
    return _daycache[d]

def trade_path(e):
    """Concatenated intraday bars for the trade's symbol from entry_time to exit time."""
    et = pd.Timestamp(e["entry_time"]); xt = pd.Timestamp(e["timestamp"])
    frames = []
    for d in pd.date_range(et.date(), xt.date(), freq="D"):
        dd = d.date()
        if dd.weekday() >= 5: continue
        df = get_day(dd).get(e["symbol"])
        if df is None or getattr(df, "empty", True): continue
        frames.append(df)
    if not frames: return None
    allb = pd.concat(frames).sort_index()
    idx = allb.index
    try: idx = idx.tz_convert(ET)
    except Exception:
        try: idx = idx.tz_localize(ET)
        except Exception: pass
    et = et.tz_convert(ET) if et.tzinfo else et.tz_localize(ET)
    xt = xt.tz_convert(ET) if xt.tzinfo else xt.tz_localize(ET)
    mask = (idx >= et) & (idx <= xt)
    return allb[mask]

def tier_band(peak_pct, P):
    # bigger gain -> protect MORE -> smaller give-back allowed (b1 >= b2 >= b3)
    if peak_pct <= 5: return P["b1"]
    if peak_pct <= 10: return P["b2"]
    return P["b3"]

def simulate(e, P):
    """Percent-based give-back (sanity-check version). Non-trailed trades exit at the ACTUAL
    journaled fill, so the only P&L delta vs baseline comes from trades the rule truly acted on."""
    entry = float(e["entry"]); qty = float(e["qty"]); actual_exit = float(e["exit"])
    df = trade_path(e)
    if df is None or df.empty:
        return actual_exit, None, "no-bars"          # == baseline, no effect
    h = col(df, "high"); l = col(df, "low"); c = col(df, "close")
    is_day = str(e["entry_time"])[:10] == str(e["timestamp"])[:10]
    tier_mult = P["day_mult"] if is_day else P["swing_mult"]
    peak = entry; armed = False; below = 0
    arm_level = entry * (1 + P["arm_pct"])
    for _, bar in df.iterrows():
        hi = float(bar[h]); lo = float(bar[l]); cl = float(bar[c])
        peak = max(peak, hi)
        if peak >= arm_level: armed = True
        if not armed: continue
        peak_pct = (peak - entry) / entry * 100
        band_pct = tier_band(peak_pct, P) * tier_mult           # % below peak
        stop = peak * (1 - band_pct / 100)
        if P["confirm"]:
            below = below + 1 if cl <= stop else 0
            if below >= 2: return round(cl, 4), round(peak, 4), "trail"
        else:
            if lo <= stop: return round(stop, 4), round(peak, 4), "trail"
    return actual_exit, round(peak, 4), "held->actual"          # no trail -> baseline exit

def run(P, label):
    base_pnl = sum(e["pnl"] for e in trades)
    base_wins = sum(1 for e in trades if e["pnl"] > 0)
    pol_pnl = 0.0; pol_wins = 0; gb_sum = 0.0; gb_n = 0; trailed = 0
    for e in trades:
        ex, peak, how = simulate(e, P)
        pnl = (ex - float(e["entry"])) * float(e["qty"])
        pol_pnl += pnl
        if pnl > 0: pol_wins += 1
        if how == "trail": trailed += 1
        if peak:
            gb_sum += (peak - ex) / peak * 100; gb_n += 1
    n = len(trades)
    print(f"--- {label}")
    print(f"    params: arm={P['arm_pct']*100:.0f}% bands(<=5%/<=10%/>10%)={P['b1']}/{P['b2']}/{P['b3']}% "
          f"day_mult={P['day_mult']} swing_mult={P['swing_mult']} confirm={P['confirm']}")
    print(f"    POLICY  P&L ${pol_pnl:+8.2f}  win {pol_wins}/{n} ({100*pol_wins/n:.0f}%)  "
          f"trailed {trailed}/{n}  avg give-back {gb_sum/max(gb_n,1):.2f}%")
    print(f"    vs BASELINE ${base_pnl:+8.2f}  win {base_wins}/{n} ({100*base_wins/n:.0f}%)   "
          f"-> delta ${pol_pnl-base_pnl:+.2f}")
    return pol_pnl - base_pnl

print(f"Backtest: {len(trades)} trades, {len(syms)} symbols, dates "
      f"{sorted(set(str(e['timestamp'])[:10] for e in trades))}")
print(f"ATR available for {sum(1 for v in atr_px.values() if v)}/{len(syms)} symbols\n")

GRID = [
  dict(arm_pct=0.03, b1=3.0, b2=2.5, b3=2.0, day_mult=0.8, swing_mult=1.6, confirm=False),
  dict(arm_pct=0.03, b1=3.0, b2=2.5, b3=2.0, day_mult=0.8, swing_mult=1.6, confirm=True),
  dict(arm_pct=0.02, b1=2.5, b2=2.0, b3=1.5, day_mult=0.7, swing_mult=1.4, confirm=False),
  dict(arm_pct=0.02, b1=2.5, b2=2.0, b3=1.5, day_mult=0.7, swing_mult=1.4, confirm=True),
  dict(arm_pct=0.05, b1=4.0, b2=3.0, b3=2.5, day_mult=1.0, swing_mult=2.0, confirm=False),
  dict(arm_pct=0.05, b1=4.0, b2=3.0, b3=2.5, day_mult=1.0, swing_mult=2.0, confirm=True),
]
for i, P in enumerate(GRID):
    tag = "tight" if P["arm_pct"] < 0.03 else "loose" if P["arm_pct"] > 0.03 else "moderate"
    run(P, f"policy {i+1} ({tag}, confirm={'ON' if P['confirm'] else 'OFF'})")
