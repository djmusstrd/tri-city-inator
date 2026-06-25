"""Missed-breakout / latency survivorship scan.
Q: does the ~16-min data delay (ORB15 closes 9:45 ET, system can't act until ~10:01 ET) cost us on
early breakouts that ran during the blind window?

Method: universe = today's RS-leaders (proxy; exact historical watchlists aren't stored). Over each
recent past session, find names that broke their opening-range high (9:30-9:45 ET) with volume after
9:45, and compare two entries held to the SAME exit (stop = ORL, else EOD close):
  IDEAL   = enter at ORH at the break (what a real-time feed could do ~9:45-9:46)
  DELAYED = enter at the price when the 9:45 bar finally lands (~10:01 ET) — what APEX can actually do
The IDEAL-minus-DELAYED gap on blind-window breakouts is the latency cost. Read-only.

Caveats: proxy universe (today's leaders, not the real per-day lists); simplified ORL-stop/EOD exit;
ignores APEX's actual guards/sizing; clean historical bars (no delay) used for the counterfactual.
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

def day_bars(day):
    start = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
    end = datetime.combine(day, datetime.min.time(), ET).replace(hour=16, minute=0)
    df = dc.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=universe, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start.astimezone(UTC), end=end.astimezone(UTC),
        feed=cfg.DATA_FEED, adjustment="raw")).df
    return df

breakouts = []           # all post-9:45 ORH breaks
for day in DAYS:
    try:
        df = day_bars(day)
    except Exception as e:
        print(f"  {day}: fetch failed {e}"); continue
    if df is None or df.empty:
        continue
    df = df.reset_index()
    df["et"] = df["timestamp"].dt.tz_convert(ET)
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("et").reset_index(drop=True)
        orb = g[(g["et"].dt.hour == 9) & (g["et"].dt.minute >= 30) & (g["et"].dt.minute < 45)]
        if len(orb) < 2:
            continue
        orh = float(orb["high"].max()); orl = float(orb["low"].min())
        orvol = float(orb["volume"].mean())
        after = g[g["et"] >= g["et"].iloc[0].replace(hour=9, minute=45)]
        brk = after[(after["high"] > orh) & (after["volume"] > orvol)]
        if brk.empty:
            continue
        b = brk.iloc[0]; btime = b["et"]
        cutoff = btime.replace(hour=10, minute=1, second=0)
        # delayed entry price: first bar at/after 10:01 ET
        post = g[g["et"] >= cutoff]
        if post.empty:
            continue
        delayed_px = float(post.iloc[0]["open"])
        blind = btime < cutoff                      # broke during the 9:45-10:01 blind window
        # simulate to EOD with stop = ORL, from each entry's start bar
        def sim(entry_px, start_time):
            seg = g[g["et"] >= start_time]
            for _, row in seg.iterrows():
                if float(row["low"]) <= orl:
                    return (orl - entry_px) / entry_px * 100, "stop"
            return (float(seg.iloc[-1]["close"]) - entry_px) / entry_px * 100, "eod"
        r_ideal, _ = sim(orh, btime)
        r_delayed, _ = sim(delayed_px, cutoff)
        breakouts.append({
            "day": str(day), "sym": sym, "brk": btime.strftime("%H:%M"), "blind": blind,
            "orh": round(orh, 2), "delayed_px": round(delayed_px, 2),
            "runup": round((delayed_px - orh) / orh * 100, 2),   # delay's entry-price penalty
            "r_ideal": round(r_ideal, 2), "r_delayed": round(r_delayed, 2),
            "cost": round(r_ideal - r_delayed, 2),
        })

bw = [b for b in breakouts if b["blind"]]
def agg(rs, label):
    if not rs:
        print(f"\n== {label}: none =="); return
    n = len(rs)
    runup = sum(r["runup"] for r in rs) / n
    ri = sum(r["r_ideal"] for r in rs) / n
    rd = sum(r["r_delayed"] for r in rs) / n
    reverted = sum(1 for r in rs if r["delayed_px"] < r["orh"])   # back below ORH by 10:01
    print(f"\n== {label}: n={n} ==")
    print(f"  entry-price penalty from delay (px@10:01 vs ORH):  avg {runup:+.2f}%")
    print(f"  already back BELOW ORH by 10:01 (failed break):    {reverted}/{n} ({reverted/n*100:.0f}%)")
    print(f"  return to exit — IDEAL (enter@ORH @ break):        {ri:+.2f}%/name")
    print(f"  return to exit — DELAYED (enter@10:01 price):      {rd:+.2f}%/name")
    print(f"  latency cost (ideal - delayed):                    {ri-rd:+.2f}%/name")

print(f"Universe {len(universe)} names × {len(DAYS)} sessions. "
      f"Post-9:45 ORH breakouts found: {len(breakouts)}; in 9:45-10:01 blind window: {len(bw)}")
print("\nTop 12 blind-window breakouts by latency cost (ideal vs delayed):")
print(f"{'DAY':<12}{'SYM':<6}{'BRK':<6}{'ORH':>9}{'PX@10:01':>10}{'RUNUP%':>8}{'IDEAL%':>8}{'DELAY%':>8}{'COST%':>8}")
for b in sorted(bw, key=lambda x: -x["cost"])[:12]:
    print(f"{b['day']:<12}{b['sym']:<6}{b['brk']:<6}{b['orh']:>9}{b['delayed_px']:>10}"
          f"{b['runup']:>8.2f}{b['r_ideal']:>8.2f}{b['r_delayed']:>8.2f}{b['cost']:>8.2f}")

agg(breakouts, "ALL post-9:45 breakouts")
agg(bw, "BLIND-WINDOW breakouts (9:45-10:01 ET) — the ones the delay affects")
