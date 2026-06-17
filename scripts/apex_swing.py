#!/usr/bin/env python3
"""
APEX — swing-tier manager (daily-bar Layer 3), durable and independent of the intraday poller.

Once a position graduates intraday → swing (carried overnight), the intraday 5-min health no
longer touches it — it would whipsaw a multi-day hold on noise. Instead this manager evaluates
swing / multi-week holdings on the DAILY timeframe and exits only on swing reasons:

  - daily close below the trade's documented INVALIDATION (the thesis support, captured at entry)
  - daily close below the SWING_TREND_EMA (the daily trend has rolled over)

It runs once per day near the close via launchd — regardless of whether an intraday `apex`
session is running — so carried positions are never orphaned. Overnight gaps are covered by the
resting Alpaca stop. Exits reuse the Layer 3 close path (journal + Telegram + Alpaca liquidate).

Enable carries with APEX_ALLOW_OVERNIGHT_CARRY=true (gated off by default during validation).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

import pandas as pd

import apex_config as cfg
from apex_health import _close
from apex_tv_quotes import get_quotes
from apex_rationale import send_telegram, telegram_swing_message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(cfg.LOGS / "apex-swing.log"),
                              logging.StreamHandler()])
for _l in ("urllib3", "requests", "alpaca"):
    logging.getLogger(_l).setLevel(logging.CRITICAL)
logger = logging.getLogger("apex.swing")


def _load_state() -> dict:
    if cfg.STATE_FILE.exists():
        try:
            return json.loads(cfg.STATE_FILE.read_text())
        except Exception:
            pass
    return {"date": "", "daily_pnl": 0.0, "positions": {}, "executed_today": []}


def _save_state(s: dict) -> None:
    cfg.STATE_FILE.write_text(json.dumps(s, indent=2))


def _fetch_daily(symbols: list, days: int = 60) -> pd.DataFrame:
    import os
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    dc = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
    return dc.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
        start=datetime.utcnow() - timedelta(days=days + 20), feed=cfg.DATA_FEED)).df


def _thesis_levels(position: dict) -> tuple:
    """(invalidation, support_low) for a position, from its entry rationale; fallback to stop."""
    inval = position.get("stop")
    support_low = None
    try:
        rats = json.loads(cfg.RATIONALE_LOG.read_text()) if cfg.RATIONALE_LOG.exists() else []
        oid = position.get("order_id")
        match = next((r for r in reversed(rats) if r.get("order_id") == oid and oid), None)
        th = (match or {}).get("thesis") or {}
        if th.get("invalidation"):
            inval = th["invalidation"]
        sup = [s["price"] for s in th.get("support", []) if s.get("price")]
        support_low = min(sup) if sup else None
    except Exception:
        pass
    return inval, support_low


def manage_swings() -> None:
    state = _load_state()
    swings = {s: p for s, p in state.get("positions", {}).items()
              if p.get("status", "intraday") != "intraday"}
    if not swings:
        print("No swing/position-tier holdings to manage.")
        return

    daily = _fetch_daily(list(swings))
    quotes = get_quotes(list(swings))
    acted = 0
    for sym, p in list(swings.items()):
        try:
            d = daily.xs(sym, level="symbol").sort_index()
        except Exception:
            logger.warning(f"[{sym}] no daily bars — skipping")
            continue
        last = float(d.iloc[-1]["close"])
        ema = float(d["close"].ewm(span=cfg.SWING_TREND_EMA, adjust=False).mean().iloc[-1])
        inval, _ = _thesis_levels(p)

        reasons = []
        if inval and last < float(inval):
            reasons.append(f"daily close {last:.2f} < invalidation {float(inval):.2f}")
        if last < ema:
            reasons.append(f"daily close {last:.2f} < EMA{cfg.SWING_TREND_EMA} {ema:.2f} (trend break)")

        if reasons:
            price = quotes.get(sym, {}).get("last") or last
            price = float(price)
            h = {"price": price, "health": p.get("health", 0),
                 "gain_pct": round((price - p["entry"]) / p["entry"] * 100, 2),
                 "reasons": reasons}
            _close(sym, p, h, "swing exit — " + "; ".join(reasons),
                   state, dry_run=(p.get("order_id") == "DRY-RUN"))
            acted += 1
        else:
            p["days_held"] = p.get("days_held", 0)
            detail = (f"daily close ${last:.2f} > EMA{cfg.SWING_TREND_EMA} ${ema:.2f}"
                      + (f" > invalidation ${float(inval):.2f}" if inval else "")
                      + " — trend intact")
            send_telegram(telegram_swing_message(sym, "HOLD", detail))
            logger.info(f"[{sym}] swing HOLD ({p.get('status')}) — {detail}")
    _save_state(state)
    print(f"Swing manager: {len(swings)} holding(s), {acted} exited.")


if __name__ == "__main__":
    manage_swings()
