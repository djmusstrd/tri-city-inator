#!/usr/bin/env python3
"""
APEX radar (build #1 of the setup-detection upgrade — see docs/APEX_SETUP_DETECTION.md).

The cheap, broad FIND layer: one-call market-wide mover/most-active pulls (no streaming),
intersected with today's RS leaders, band-filtered, and emitted as a setup-candidate shortlist.
This is what feeds the (separate) setup scorer + promotion-to-real-time step — so the system can
position on emergent movers *before* they trigger, instead of after.

Pure read: pulls data, writes shared/apex-radar.json, prints a table. No orders, no streaming,
no poller interaction. Safe to run anytime the market's open.

Usage: python -W ignore scripts/apex_radar.py [--top 50] [--pmin 2] [--pmax 100]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
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

API_KEY = os.getenv("ALPACA_API_KEY")
SECRET = os.getenv("ALPACA_SECRET_KEY")
PRICE_MIN = float(os.getenv("APEX_PRICE_MIN", "2"))
PRICE_MAX = float(os.getenv("APEX_PRICE_MAX", "100"))


def _screener():
    from alpaca.data.historical.screener import ScreenerClient
    return ScreenerClient(API_KEY, SECRET)


def load_leaders() -> dict:
    p = SHARED / "apex-leaders.json"
    if not p.exists():
        return {}
    d = json.loads(p.read_text())
    return {l["symbol"]: l for l in d.get("leaders", [])}


def get_movers(top: int) -> dict:
    """Top gainers as {symbol: {pct, price}} — the movers payload carries price (no SIP snapshot)."""
    from alpaca.data.requests import MarketMoversRequest
    m = _screener().get_market_movers(MarketMoversRequest(top=top))
    return {g.symbol: {"pct": float(g.percent_change), "price": float(g.price)} for g in m.gainers}


def get_actives(top: int) -> dict:
    from alpaca.data.requests import MostActivesRequest
    a = _screener().get_most_actives(MostActivesRequest(top=top))
    return {s.symbol: int(s.volume) for s in a.most_actives}


def latest_prices(symbols: list[str]) -> dict:
    if not symbols:
        return {}
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockSnapshotRequest
    dc = StockHistoricalDataClient(API_KEY, SECRET)
    out = {}
    try:
        # IEX feed — the basic plan rejects recent SIP snapshots (see project_apex_delayed_sip_data)
        snaps = dc.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols, feed="iex"))
        for s, snap in snaps.items():
            p = None
            if snap and snap.latest_trade:
                p = snap.latest_trade.price
            elif snap and snap.minute_bar:
                p = snap.minute_bar.close
            elif snap and snap.daily_bar:
                p = snap.daily_bar.close
            if p:
                out[s] = float(p)
    except Exception as e:
        print(f"  snapshot fetch warn: {e}")
    return out


def build(top: int, pmin: float, pmax: float) -> dict:
    leaders = load_leaders()
    gainers = get_movers(top)
    actives = get_actives(top)

    syms = set(gainers) | set(actives)
    # price comes from the movers payload for gainers; IEX snapshot fills the active-only names
    snap_prices = latest_prices(sorted(s for s in syms if s not in gainers))

    cands = []
    for s in syms:
        price = (gainers.get(s, {}).get("price") if s in gainers else None) \
            or snap_prices.get(s) or leaders.get(s, {}).get("price")
        if price is None or not (pmin <= price <= pmax):
            continue   # honor the locked $2-100 band (capital efficiency)
        is_leader = s in leaders
        is_gainer = s in gainers
        is_active = s in actives
        pct = gainers.get(s, {}).get("pct", 0.0)
        # priority: RS-leader-that's-moving is the sweet spot; then movers; then raw actives
        score = (2.0 if is_leader else 0.0) + (1.0 if is_gainer else 0.0) \
            + (1.0 if is_active else 0.0) + min(pct, 30.0) / 30.0
        cands.append({
            "symbol": s, "price": round(price, 2),
            "pct_change": round(pct, 2),
            "volume": actives.get(s),
            "rs_pct": leaders.get(s, {}).get("rs_pct"),
            "flags": "".join(["L" if is_leader else "-", "G" if is_gainer else "-",
                              "A" if is_active else "-"]),
            "priority": round(score, 3),
        })
    cands.sort(key=lambda c: c["priority"], reverse=True)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "top": top, "band": [pmin, pmax],
        "n_gainers": len(gainers), "n_actives": len(actives),
        "n_leaders": len(leaders), "n_candidates": len(cands),
        "candidates": cands,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=50)
    ap.add_argument("--pmin", type=float, default=PRICE_MIN)
    ap.add_argument("--pmax", type=float, default=PRICE_MAX)
    ap.add_argument("--limit", type=int, default=25, help="rows to print")
    args = ap.parse_args()
    if not API_KEY:
        print("ERROR: ALPACA_API_KEY not set")
        sys.exit(1)

    out = build(args.top, args.pmin, args.pmax)
    SHARED.mkdir(parents=True, exist_ok=True)
    (SHARED / "apex-radar.json").write_text(json.dumps(out, indent=2))

    print(f"APEX radar | top{args.top} movers/actives ∩ leaders | band ${args.pmin:.0f}-${args.pmax:.0f}")
    print(f"  gainers {out['n_gainers']} | actives {out['n_actives']} | leaders {out['n_leaders']} "
          f"→ {out['n_candidates']} in-band candidates  (flags: L=leader G=gainer A=active)")
    print(f"  {'SYM':6} {'PRICE':>8} {'CHG%':>7} {'VOLUME':>13} {'RS':>5} {'FLAGS':>6} {'PRI':>5}")
    print("  " + "-" * 60)
    for c in out["candidates"][:args.limit]:
        vol = f"{c['volume']:,}" if c["volume"] else "—"
        rs = f"{c['rs_pct']:.0f}" if c["rs_pct"] is not None else "—"
        print(f"  {c['symbol']:6} {c['price']:>8.2f} {c['pct_change']:>+6.1f}% {vol:>13} "
              f"{rs:>5} {c['flags']:>6} {c['priority']:>5.2f}")
    print(f"\n  wrote {SHARED/'apex-radar.json'}")


if __name__ == "__main__":
    main()
