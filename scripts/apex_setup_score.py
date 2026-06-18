#!/usr/bin/env python3
"""
APEX setup scorer (build #2 of the setup-detection upgrade — see docs/APEX_SETUP_DETECTION.md).

Takes the radar shortlist (apex_radar.py → shared/apex-radar.json) and scores each candidate on
the COMMON precursor signature that precedes a move — so we can promote names that are *setting up*
into the real-time warm set BEFORE they trigger, instead of reacting late.

Precursors (each normalized 0-1, weighted into a composite):
  RVOL      today's volume vs its 20-day average      (the #1 precursor)
  MOM       how far it's already moving today          (% change)
  LEVEL     reclaiming / breaking the prior-day high
  COMPRESS  range coiled then releasing (ATR5 / ATR20)
  RS        relative-strength context (leader rs_pct)
  NEWS      fresh catalyst in the last ~2 days

Also drops the radar noise: warrants/units (.WS / W / U / WWW suffixes) and names with no usable
volume. Pure read — daily history (SIP, through yesterday so no delayed-SIP rejection) + one news
call. No orders, no streaming, no poller. Writes shared/apex-setups.json.

Usage: python -W ignore scripts/apex_setup_score.py [--min-score 0.45] [--limit 20]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path.home() / "tri-city-inator"
SHARED = WORKSPACE / "shared"

try:
    from dotenv import load_dotenv
    _env = WORKSPACE / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

import numpy as np
import pandas as pd

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET = os.getenv("ALPACA_SECRET_KEY")

# weights for the composite (sum = 1.0)
W = {"rvol": 0.25, "mom": 0.20, "level": 0.20, "compress": 0.15, "rs": 0.10, "news": 0.10}
WARRANT_RE = re.compile(r"(\.WS$|\.U$|W{2,}$|[.\-]?WS$)", re.I)


def _clamp(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def load_candidates() -> list:
    p = SHARED / "apex-radar.json"
    if not p.exists():
        print("  no apex-radar.json — run apex_radar.py first")
        return []
    return json.loads(p.read_text()).get("candidates", [])


def daily_bars(symbols: list[str], days: int = 35) -> pd.DataFrame:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    if not symbols:
        return pd.DataFrame()
    dc = StockHistoricalDataClient(API_KEY, SECRET)
    end = datetime.now() - timedelta(minutes=20)        # stay out of the delayed-SIP window
    start = end - timedelta(days=days + 25)
    try:
        return dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
            start=start, end=end, feed="sip", adjustment="all")).df
    except Exception as e:
        print(f"  daily bars warn: {e}")
        return pd.DataFrame()


def recent_news_symbols(symbols: list[str], days: int = 2) -> set:
    """Set of symbols with at least one news item in the last `days`."""
    try:
        from alpaca.data.historical.news import NewsClient
        from alpaca.data.requests import NewsRequest
        nc = NewsClient(API_KEY, SECRET)
        start = datetime.now() - timedelta(days=days)
        out = set()
        news = nc.get_news(NewsRequest(symbols=",".join(symbols), start=start, limit=50))
        for a in getattr(news, "news", []) or []:
            for s in getattr(a, "symbols", []) or []:
                out.add(s)
        return out
    except Exception as e:
        print(f"  news warn: {e}")
        return set()


def _atr(d: pd.DataFrame, n: int) -> float:
    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()],
                   axis=1).max(axis=1)
    return float(tr.rolling(n).mean().iloc[-1])


def score_one(c: dict, d: pd.DataFrame, has_news: bool) -> dict | None:
    price = c.get("price")
    if not price or d is None or d.empty or len(d) < 20:
        return None
    d = d.sort_index()
    avg20_vol = float(d["volume"].tail(20).mean())
    pdh = float(d["high"].iloc[-1])             # prior-day high (last closed daily bar)
    atr5, atr20 = _atr(d, 5), _atr(d, 20)

    # precursor sub-scores (0-1)
    rvol = (c["volume"] / avg20_vol) if (c.get("volume") and avg20_vol) else None
    s_rvol = _clamp(rvol / 3.0) if rvol is not None else 0.0           # 3x avg = full
    s_mom = _clamp(c.get("pct_change", 0.0) / 20.0)                    # +20% = full
    s_level = 1.0 if price >= pdh else _clamp(1 - (pdh - price) / pdh * 10)  # within 10% partial
    s_comp = _clamp((0.9 - (atr5 / atr20)) / 0.4) if atr20 else 0.0    # coiled (atr5<<atr20)
    s_rs = (c["rs_pct"] / 100.0) if c.get("rs_pct") is not None else 0.4
    s_news = 1.0 if has_news else 0.0

    composite = (W["rvol"] * s_rvol + W["mom"] * s_mom + W["level"] * s_level
                 + W["compress"] * s_comp + W["rs"] * s_rs + W["news"] * s_news)
    return {
        "symbol": c["symbol"], "price": price, "pct_change": c.get("pct_change"),
        "rvol": round(rvol, 1) if rvol is not None else None,
        "score": round(composite, 3),
        "sub": {"rvol": round(s_rvol, 2), "mom": round(s_mom, 2), "level": round(s_level, 2),
                "compress": round(s_comp, 2), "rs": round(s_rs, 2), "news": round(s_news, 2)},
        "flags": c.get("flags"), "above_pdh": price >= pdh,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-score", type=float, default=0.45)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    if not API_KEY:
        print("ERROR: ALPACA_API_KEY not set")
        sys.exit(1)

    cands = load_candidates()
    # drop warrant/unit tickers + names with no usable volume signal
    cands = [c for c in cands if not WARRANT_RE.search(c["symbol"])]
    if not cands:
        print("no candidates after noise filter")
        sys.exit(0)

    syms = [c["symbol"] for c in cands]
    print(f"APEX setup scorer | {len(syms)} candidates | fetching daily history + news…")
    bars = daily_bars(syms)
    news_syms = recent_news_symbols(syms)

    scored = []
    for c in cands:
        try:
            d = bars.xs(c["symbol"], level="symbol")
        except (KeyError, Exception):
            d = None
        r = score_one(c, d, c["symbol"] in news_syms)
        if r:
            scored.append(r)
    scored.sort(key=lambda r: r["score"], reverse=True)
    keep = [r for r in scored if r["score"] >= args.min_score]

    SHARED.mkdir(parents=True, exist_ok=True)
    (SHARED / "apex-setups.json").write_text(json.dumps(
        {"generated_at": datetime.now().isoformat(timespec="seconds"),
         "min_score": args.min_score, "n_scored": len(scored), "n_setups": len(keep),
         "setups": keep}, indent=2))

    print(f"  scored {len(scored)} | {len(keep)} ≥ {args.min_score}  "
          f"(sub-scores: RVOL MOM LVL CMP RS NEWS)")
    print(f"  {'SYM':7} {'PRICE':>8} {'CHG%':>6} {'RVOL':>5} {'SCORE':>6}  {'R  M  L  C  S  N':>17}  PDH")
    print("  " + "-" * 66)
    for r in scored[:args.limit]:
        s = r["sub"]
        sub = f"{s['rvol']:.1f} {s['mom']:.1f} {s['level']:.1f} {s['compress']:.1f} {s['rs']:.1f} {s['news']:.1f}"
        rv = f"{r['rvol']:.1f}" if r["rvol"] is not None else "—"
        print(f"  {r['symbol']:7} {r['price']:>8.2f} {r['pct_change']:>+5.1f}% {rv:>5} "
              f"{r['score']:>6.3f}  {sub:>17}  {'↑' if r['above_pdh'] else ' '}")
    print(f"\n  wrote {SHARED/'apex-setups.json'}")


if __name__ == "__main__":
    main()
