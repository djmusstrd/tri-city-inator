"""APEX A/B shadow logger (additive, read-only — does NOT touch the live trading path).

Compares, per real APEX entry:
  A = ACTUAL realized result (from logs/apex-journal.json — the live Layer-3 exits)
  B = SCALP counterfactual (full exit at a quick target, same stop), simulated from historical bars

Entries come from logs/apex-rationale.json; actual exits from logs/apex-journal.json; bars from Alpaca.
Re-run after each session (or daily cron) — it re-derives the whole comparison from the persistent
logs the live system already writes, so the sample grows automatically as APEX trades. Output:
logs/apex-ab-shadow.json (+ printed summary). Wire into EOD later if desired; not auto-wired here.

R = (exit-entry)/(entry-stop), using the entry's actual recorded stop, so A and B share one risk unit.
B variants: +3%, +5%, and +1.5R fixed targets; stop = recorded stop; 15-min time-stop; else EOD close.

CAVEATS: B uses 5-min bars (coarse intrabar; conservative stop-before-target), no slippage/commission;
A excludes positions still open and flags operator-trim contamination. Directional, not precise P&L.
"""
import os, sys, json
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(__file__))
import apex_config as cfg
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

ET = ZoneInfo("America/New_York"); UTC = ZoneInfo("UTC")
dc = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
TIME_STOP_MIN = 15
EOD = (15, 55)

entries = [e for e in json.load(open("logs/apex-rationale.json")) if not e.get("dry_run")]
journal = json.load(open("logs/apex-journal.json"))

# index actual exits by (symbol, date): list of rows
jx = defaultdict(list)
for j in journal:
    d = str(j.get("entry_time") or j.get("timestamp"))[:10]
    jx[(j["symbol"], d)].append(j)

# bars cache: one request per date for all symbols entered that date
need = defaultdict(set)
for e in entries:
    need[str(e["timestamp"])[:10]].add(e["symbol"])
bars = {}
for d, syms in need.items():
    day = datetime.fromisoformat(d).date()
    start = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
    end = datetime.combine(day, datetime.min.time(), ET).replace(hour=16, minute=0)
    try:
        df = dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=sorted(syms), timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=start.astimezone(UTC), end=end.astimezone(UTC),
            feed=cfg.DATA_FEED, adjustment="raw")).df
        if df is not None and not df.empty:
            df = df.reset_index(); df["et"] = df["timestamp"].dt.tz_convert(ET)
        bars[d] = df
    except Exception as ex:
        bars[d] = None

def scalp_exit(seg, entry, stop, target_px):
    """Return (exit_price, reason) for a full-position scalp."""
    risk = entry - stop
    t0 = seg["et"].iloc[0]
    for _, r in seg.iterrows():
        if float(r["low"]) <= stop:
            return stop, "stop"
        if float(r["high"]) >= target_px:
            return target_px, "target"
        if (r["et"] - t0) >= timedelta(minutes=TIME_STOP_MIN):
            return float(r["close"]), "time"
    return float(seg.iloc[-1]["close"]), "eod"

rows = []
for e in entries:
    sym = e["symbol"]; d = str(e["timestamp"])[:10]
    entry = float(e["entry"]); stop = float(e.get("stop") or 0)
    risk = entry - stop
    if risk <= 0:
        continue
    # ---- A: actual realized R ----
    cands = [j for j in jx.get((sym, d), []) if abs(float(j.get("entry", 0)) - entry) / entry < 0.01]
    manual = any("manual trim" in (j.get("reason", "")) for j in cands)
    realized = sum(float(j.get("pnl", 0)) for j in cands)
    qty = float(e.get("qty") or sum(float(j.get("qty", 0)) for j in cands) or 0)
    risk_dollars = float(e.get("risk_dollars") or (risk * qty))
    closed = bool(cands)
    R_A = realized / risk_dollars if (closed and risk_dollars) else None
    # ---- B: scalp counterfactual ----
    df = bars.get(d); R_B = {}
    if df is not None and not df.empty:
        g = df[df["symbol"] == sym].sort_values("et")
        et0 = datetime.fromisoformat(e["timestamp"])
        seg = g[g["et"] >= et0]
        if not seg.empty:
            for tag, tpx in (("+3%", entry * 1.03), ("+5%", entry * 1.05), ("1.5R", entry + 1.5 * risk)):
                xp, why = scalp_exit(seg, entry, stop, tpx)
                R_B[tag] = round((xp - entry) / risk, 3)
    rows.append({"date": d, "sym": sym, "trigger": e.get("trigger"), "entry": entry, "stop": stop,
                 "closed": closed, "manual_trim": manual, "R_A": round(R_A, 3) if R_A is not None else None,
                 "R_B": R_B})

# ---- summary over CLEAN closed trades (closed, no operator-trim contamination, B computed) ----
clean = [r for r in rows if r["closed"] and not r["manual_trim"] and r["R_B"]]
def avg(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(xs) / len(xs), 3) if xs else None
def winrate(xs):
    xs = [x for x in xs if x is not None]
    return round(sum(1 for x in xs if x > 0) / len(xs) * 100, 0) if xs else None

summary = {
    "generated": datetime.now(ET).isoformat(), "sessions": sorted(need),
    "n_entries": len(rows), "n_clean_closed": len(clean),
    "A_actual": {"avgR": avg([r["R_A"] for r in clean]), "winrate%": winrate([r["R_A"] for r in clean]),
                 "totalR": round(sum(r["R_A"] for r in clean if r["R_A"] is not None), 2)},
    "B_scalp": {tag: {"avgR": avg([r["R_B"].get(tag) for r in clean]),
                      "winrate%": winrate([r["R_B"].get(tag) for r in clean]),
                      "totalR": round(sum(r["R_B"].get(tag, 0) for r in clean), 2)}
                for tag in ("+3%", "+5%", "1.5R")},
}
out = {"summary": summary, "trades": rows}
json.dump(out, open("logs/apex-ab-shadow.json", "w"), indent=2)

print(f"A/B SHADOW — {len(rows)} entries, {len(clean)} clean closed (excl. open + operator-trim)")
print(f"sessions: {', '.join(s[5:] for s in sorted(need))}\n")
a = summary["A_actual"]
print(f"  A (actual live exits):  avgR {a['avgR']}  win {a['winrate%']}%  totalR {a['totalR']}")
for tag in ("+3%", "+5%", "1.5R"):
    b = summary["B_scalp"][tag]
    print(f"  B scalp {tag:<5}:          avgR {b['avgR']}  win {b['winrate%']}%  totalR {b['totalR']}")
print("\nwrote logs/apex-ab-shadow.json")
