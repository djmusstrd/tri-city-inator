"""Multi-timeframe ORB backtest (research, read-only).
Tests: "Breakout durability rises with ORB timeframe; a multi-TF book that scalps 5-min breaks and
holds 30-min breaks beats single-TF ORB15 on realized R."

For each ORB timeframe (5/15/30 min): opening range 9:30->9:30+TF ET; first post-OR breakout
(high>ORH & vol>ORvol); a realistic ~16-min data delay applied per BREAK bar (you act 16 min after
the break prints); fail rate (% back below ORH within 5/10/15 min); forward R with stop=ORL under
several exit styles. Then per-tier best exit + a composite TF-matched book vs single-TF ORB15.

R = (exit-entry)/(entry-ORL). Forward sim walks 5-min bars (uniform). A separate 1-min pass rebuilds
the 5-min tier so its OR isn't a single bar.

CAVEATS: universe = TODAY's leaders (proxy; real per-day watchlists aren't stored). Simplified
ORL-stop/target/EOD exits, intrabar stop-before-target (conservative), no slippage/commission, no
APEX guards/sizing. Directional, not precise P&L. No live files touched; nothing committed.
"""
import os, sys, json
from datetime import datetime, timedelta, date as ddate
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(__file__))
import apex_config as cfg
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

ET = ZoneInfo("America/New_York"); UTC = ZoneInfo("UTC")
dc = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
universe = [l["symbol"] for l in json.load(open("shared/apex-leaders.json"))["leaders"]]
DAYS = [ddate(2026, 6, 17), ddate(2026, 6, 18), ddate(2026, 6, 22),
        ddate(2026, 6, 23), ddate(2026, 6, 24)]
DELAY_MIN = 16
EOD = (15, 55)

_cache = {}
def fetch(day, unit_min):
    key = (day, unit_min)
    if key in _cache:
        return _cache[key]
    start = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
    end = datetime.combine(day, datetime.min.time(), ET).replace(hour=16, minute=0)
    df = dc.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=universe, timeframe=TimeFrame(unit_min, TimeFrameUnit.Minute),
        start=start.astimezone(UTC), end=end.astimezone(UTC),
        feed=cfg.DATA_FEED, adjustment="raw")).df
    if df is not None and not df.empty:
        df = df.reset_index()
        df["et"] = df["timestamp"].dt.tz_convert(ET)
    _cache[key] = df
    return df

def or_close(day, tf):
    return datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30) + timedelta(minutes=tf)

def sim(seg, entry_px, orl, style):
    """seg: per-symbol bars (et-sorted) from entry onward. Returns R."""
    risk = entry_px - orl
    if risk <= 0:
        return None
    t0 = seg["et"].iloc[0]
    for _, r in seg.iterrows():
        hi, lo, cl, t = float(r["high"]), float(r["low"]), float(r["close"]), r["et"]
        if lo <= orl:                                   # stop (conservative: before target)
            return (orl - entry_px) / risk
        if style[0] == "scalp":
            tgt = entry_px * (1 + style[1] / 100.0)
            if hi >= tgt:
                return (tgt - entry_px) / risk
            if (t - t0) >= timedelta(minutes=style[2]):  # time-stop -> exit close
                return (cl - entry_px) / risk
    return (float(seg.iloc[-1]["close"]) - entry_px) / risk    # EOD

def scan(tf, unit_min):
    """Return list of breakout dicts for this ORB timeframe using `unit_min` bars."""
    out = []
    for day in DAYS:
        df = fetch(day, unit_min)
        if df is None or df.empty:
            continue
        oc = or_close(day, tf)
        eod = datetime.combine(day, datetime.min.time(), ET).replace(hour=EOD[0], minute=EOD[1])
        for sym, g in df.groupby("symbol"):
            g = g.sort_values("et").reset_index(drop=True)
            orb = g[(g["et"] >= oc - timedelta(minutes=tf)) & (g["et"] < oc)]
            if orb.empty:
                continue
            orh, orl, orvol = float(orb["high"].max()), float(orb["low"].min()), float(orb["volume"].mean())
            if orh <= orl:
                continue
            after = g[g["et"] >= oc]
            brk = after[(after["high"] > orh) & (after["volume"] > orvol)]
            if brk.empty:
                continue
            b = brk.iloc[0]; bt = b["et"]
            # fail rate: back below ORH within N min of break
            def failed(within):
                w = g[(g["et"] > bt) & (g["et"] <= bt + timedelta(minutes=within))]
                return (not w.empty) and bool((w["low"] < orh).any())
            # delayed entry: act 16 min after the break bar prints
            dt = bt + timedelta(minutes=DELAY_MIN)
            post = g[g["et"] >= dt]
            if post.empty:
                continue
            dpx = float(post.iloc[0]["open"])
            ideal_seg = g[g["et"] >= bt]
            delay_seg = g[g["et"] >= dt]
            rec = {"day": str(day), "sym": sym, "bt": bt, "orh": orh, "orl": orl,
                   "dpx": dpx, "f5": failed(5), "f10": failed(10), "f15": failed(15),
                   "pen": (dpx - orh) / orh * 100}
            # R under each exit style, IDEAL (enter@ORH) and DELAYED (enter@dpx)
            for tag, epx, seg in (("ideal", orh, ideal_seg), ("delay", dpx, delay_seg)):
                for sty in (("hold",), ("scalp", 3, 15), ("scalp", 5, 15)):
                    rec[f"{tag}_{sty[0]}{sty[1] if len(sty)>1 else ''}"] = sim(seg, epx, orl, sty)
            out.append(rec)
    return out

def avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else float("nan")

def report_tier(tf, recs, label):
    n = len(recs)
    if n == 0:
        print(f"\n[{label}] no breakouts"); return
    f5 = sum(r["f5"] for r in recs) / n * 100
    f10 = sum(r["f10"] for r in recs) / n * 100
    f15 = sum(r["f15"] for r in recs) / n * 100
    print(f"\n[{label}]  breakouts n={n}")
    print(f"  fail back<ORH within 5/10/15min:  {f5:.0f}% / {f10:.0f}% / {f15:.0f}%")
    print(f"  entry penalty (delayed px vs ORH): {avg([r['pen'] for r in recs]):+.2f}%")
    print(f"  DELAYED R/trade  hold={avg([r['delay_hold'] for r in recs]):+.2f}  "
          f"scalp+3%={avg([r['delay_scalp3'] for r in recs]):+.2f}  "
          f"scalp+5%={avg([r['delay_scalp5'] for r in recs]):+.2f}")
    print(f"  IDEAL   R/trade  hold={avg([r['ideal_hold'] for r in recs]):+.2f}  "
          f"scalp+3%={avg([r['ideal_scalp3'] for r in recs]):+.2f}  "
          f"scalp+5%={avg([r['ideal_scalp5'] for r in recs]):+.2f}")

print("="*78)
print("MULTI-TIMEFRAME ORB BACKTEST — universe=today's 109 leaders (PROXY), sessions:",
      ", ".join(str(d)[5:] for d in DAYS))
print("="*78)

tiers = {}
for tf in (5, 15, 30):
    recs = scan(tf, 5)            # 5-min bars
    tiers[tf] = recs
    report_tier(tf, recs, f"ORB{tf} (5-min bars)")

# 1-min sensitivity for the 5-min tier
recs1 = scan(5, 1)
report_tier(5, recs1, "ORB5 (1-min bars) — granularity sensitivity")

# Durability check: fail rate by tier
print("\n" + "-"*78)
print("DURABILITY (does fail rate fall as TF widens?)  [fail<ORH within 10min]")
for tf in (5, 15, 30):
    r = tiers[tf]
    fr = sum(x["f10"] for x in r) / len(r) * 100 if r else float("nan")
    print(f"  ORB{tf:<3} n={len(r):<4} fail10={fr:.0f}%")

# Composite TF-matched book vs single-TF ORB15, capped 5 entries/day, avg R.
def matched_R(rec, tf):
    return {5: rec["delay_scalp3"], 15: rec["delay_scalp5"], 30: rec["delay_hold"]}[tf]

def book(per_day_recs):
    Rs = []
    for day in DAYS:
        cands = sorted([x for x in per_day_recs if x["day"] == str(day)], key=lambda z: z["bt"])[:5]
        Rs += [x["R"] for x in cands if x.get("R") is not None]
    return Rs

comp = []
for tf in (5, 15, 30):
    for rec in tiers[tf]:
        comp.append({"day": rec["day"], "bt": rec["bt"], "R": matched_R(rec, tf)})
base = [{"day": r["day"], "bt": r["bt"], "R": r["delay_hold"]} for r in tiers[15]]

cR, bR = book(comp), book(base)
print("\n" + "-"*78)
print("COMPOSITE (TF-matched: 5->scalp+3%, 15->scalp+5%, 30->hold) vs SINGLE-TF ORB15 hold")
print(f"  composite : trades={len(cR)}  avgR={avg(cR):+.2f}  totalR={sum(x for x in cR if x is not None):+.1f}")
print(f"  ORB15 base: trades={len(bR)}  avgR={avg(bR):+.2f}  totalR={sum(x for x in bR if x is not None):+.1f}")
