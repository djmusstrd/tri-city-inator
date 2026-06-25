"""One-off: quantify the 16-min-delay entry-latency cost.
For each journaled entry, compute the opening-range high (9:30-9:45 ET), how far above ORH we
actually filled (extension % = the chasing cost), entry time-of-day, and the counterfactual gain
had we filled at ORH instead. Read-only; no orders, no state writes."""
import os, sys, json, collections
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
sys.path.insert(0, os.path.dirname(__file__))
import apex_config as cfg
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

ET = ZoneInfo("America/New_York")
dc = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))

rows = json.load(open("logs/apex-journal.json"))
# unique entries (dedupe partial-trim rows that share symbol+entry_time+entry)
seen = set(); trades = []
for r in rows:
    et = r.get("entry_time")
    if not et or r.get("reason", "").startswith("manual trim"):
        continue
    key = (r["symbol"], et, r.get("entry"))
    if key in seen:
        continue
    seen.add(key)
    trades.append(r)

def orh_for(sym, day):
    """Opening-range high = max high of 9:30-9:45 ET 5-min bars."""
    start = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=30)
    end = start + timedelta(minutes=60)
    df = dc.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=[sym], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
        start=start.astimezone(ZoneInfo("UTC")), end=end.astimezone(ZoneInfo("UTC")),
        feed=cfg.DATA_FEED, adjustment="raw")).df
    if df is None or df.empty:
        return None, None
    df = df.reset_index()
    df["et"] = df["timestamp"].dt.tz_convert(ET)
    orb = df[(df["et"].dt.hour == 9) & (df["et"].dt.minute < 45) & (df["et"].dt.minute >= 30)]
    if orb.empty:
        return None, None
    return float(orb["high"].max()), float(orb["low"].min())

results = []
for t in trades:
    sym = t["symbol"]; et = datetime.fromisoformat(t["entry_time"]).astimezone(ET)
    entry = float(t["entry"]); exitp = float(t.get("exit", entry))
    gain = float(t.get("gain_pct", 0.0)); trig = t.get("trigger", "?")
    try:
        orh, orl = orh_for(sym, et.date())
    except Exception as e:
        orh = None
    if not orh:
        continue
    ext = (entry - orh) / orh * 100          # how far above ORH we filled
    cf_gain = (exitp - orh) / orh * 100        # gain had we filled at ORH
    results.append({"sym": sym, "date": str(et.date()), "t": et.strftime("%H:%M"),
                    "trig": trig, "orh": round(orh, 2), "entry": entry, "exit": exitp,
                    "ext": round(ext, 2), "gain": round(gain, 2), "cf_gain": round(cf_gain, 2),
                    "hour": et.hour})

def summarize(rs, label):
    if not rs:
        print(f"\n== {label}: no trades =="); return
    ext = [r["ext"] for r in rs]; gain = [r["gain"] for r in rs]; cf = [r["cf_gain"] for r in rs]
    print(f"\n== {label}: n={len(rs)} ==")
    print(f"  entry extension above ORH:  avg {sum(ext)/len(ext):+.2f}%  "
          f"median {sorted(ext)[len(ext)//2]:+.2f}%  max {max(ext):+.2f}%  min {min(ext):+.2f}%")
    print(f"  actual avg gain/trade:      {sum(gain)/len(gain):+.2f}%")
    print(f"  counterfactual (fill@ORH):  {sum(cf)/len(cf):+.2f}%   "
          f"(uplift {sum(cf)/len(cf)-sum(gain)/len(gain):+.2f}%/trade)")

print(f"Analyzed {len(results)} entries with resolvable ORH (of {len(trades)} journaled entries)\n")
print(f"{'SYM':<6}{'DATE':<12}{'ET':<7}{'TRIG':<9}{'ORH':>8}{'ENTRY':>8}{'EXT%':>7}{'GAIN%':>7}{'CF%':>7}")
for r in sorted(results, key=lambda x: (x["date"], x["t"])):
    print(f"{r['sym']:<6}{r['date']:<12}{r['t']:<7}{r['trig']:<9}{r['orh']:>8}{r['entry']:>8}"
          f"{r['ext']:>7.2f}{r['gain']:>7.2f}{r['cf_gain']:>7.2f}")

summarize(results, "ALL entries")
summarize([r for r in results if r["trig"] == "ORB15"], "ORB15 only (cleanest signal)")
summarize([r for r in results if r["hour"] < 11], "Morning entries (before 11:00 ET)")
summarize([r for r in results if r["hour"] >= 11], "Midday+ entries (11:00 ET on)")
