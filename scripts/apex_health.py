#!/usr/bin/env python3
"""
APEX Layer 3 — Trade Health Monitor ("am I still in the move?").

Every cycle, each open position gets a health score (0-100) recomputed from today's live
intraday 5-min bars. Inputs follow the design doc (Layer 3):
  - Thesis still valid?   holding above the entry / breakout level
  - Key levels held?      VWAP, short-EMA (5-min EMA9) ribbon proxy
  - Momentum sustaining?  last-3-bar slope, higher-high structure

Actions, per the "never sell without a reason" principle:
  - health < EXIT_HEALTH  -> proactive exit NOW (don't wait for the hard -stop). This is the
                             edge: cut HITI/SPCB-style fades early instead of riding to -5%.
  - near the close        -> CONDITIONAL EOD: carry a healthy, trending runner overnight, so a
                             $2 → $80 mover isn't force-closed on day one. Positions GRADUATE
                             intraday → swing → multi-week as long as health stays strong; only
                             scratch / thesis-broken trades are force-closed at the bell.

The score weights here are a transparent v1 baseline (like composite_score in the entry
engine). Phase 3 data work / Layer 4 will tune EXIT_HEALTH, CARRY_HEALTH and the graduation
tiers from the journal (logs/apex-journal.json).

See docs/STRATEGY_V2_DESIGN.md (Layer 3).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

import apex_config as cfg
from apex_rationale import (send_telegram, telegram_exit_message,
                            telegram_carry_message, telegram_health_message)

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("apex.health")


# ── health score ───────────────────────────────────────────────────────────────
def _vwap(g: pd.DataFrame) -> pd.Series:
    tp = (g["high"] + g["low"] + g["close"]) / 3
    return (tp * g["volume"]).cumsum() / g["volume"].cumsum()


def compute_health(position: dict, bars: pd.DataFrame, live_price: float | None = None) -> dict:
    """
    Recompute a position's health (0-100) from today's accumulating regular-session 5-min
    bars (same frame the entry engine uses: open/high/low/close/volume + 'tod').
    Returns {} if there isn't enough data yet.

    Hybrid feed: VWAP / EMA9 / momentum / structure come from `bars`; the CURRENT price (thesis,
    VWAP/EMA checks, gain%) uses `live_price` (TradingView real-time) when given, so health and
    the proactive-exit decision react to the live price, not a 15-min-old bar close.
    """
    if bars is None or bars.empty:
        return {}
    g = bars.sort_values("tod").reset_index(drop=True)
    g = g.assign(vwap=_vwap(g).values)
    last = g.iloc[-1]
    price = float(live_price) if live_price is not None else float(last["close"])
    vwap = float(last["vwap"])
    entry = float(position["entry"])
    ema9 = float(g["close"].ewm(span=9, adjust=False).mean().iloc[-1])
    gain_pct = (price - entry) / entry * 100 if entry else 0.0

    health = 100.0
    reasons = []

    # 1. Thesis — holding above the entry / breakout level
    if price < entry:
        pen = min(30.0, (entry - price) / entry * 100 * 6)
        health -= pen
        reasons.append(f"below entry (-{pen:.0f})")

    # 2. Key level — VWAP
    if price < vwap:
        health -= 20.0
        reasons.append("below VWAP (-20)")

    # 3. Ribbon-proxy support — 5-min EMA9
    if price < ema9:
        health -= 10.0
        reasons.append("below EMA9 (-10)")

    # 4. Momentum — last 3 closes monotonically declining
    if len(g) >= 3:
        c = g["close"].iloc[-3:].values
        if c[0] > c[1] > c[2]:
            health -= 20.0
            reasons.append("momentum fading (-20)")

    # 5. Structure — recent 20m high below the prior 20m high (lower highs)
    if len(g) >= 8:
        recent_hi = float(g["high"].iloc[-4:].max())
        prior_hi = float(g["high"].iloc[-8:-4].max())
        if recent_hi < prior_hi:
            health -= 10.0
            reasons.append("lower highs (-10)")

    health = max(0.0, min(100.0, health))
    return {"health": int(round(health)), "price": price, "vwap": vwap,
            "ema9": ema9, "gain_pct": round(gain_pct, 2), "reasons": reasons}


# ── exit plumbing ────────────────────────────────────────────────────────────────
def _trading_client():
    key, sec = cfg.os.getenv("ALPACA_API_KEY"), cfg.os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec:
        return None
    try:
        from alpaca.trading.client import TradingClient
        paper = cfg.os.getenv("ALPACA_PAPER", "true").lower() == "true"
        return TradingClient(key, sec, paper=paper)
    except ImportError:
        logger.error("alpaca-py not installed")
        return None


def _load_journal() -> list:
    if cfg.APEX_JOURNAL.exists():
        try:
            return json.loads(cfg.APEX_JOURNAL.read_text())
        except Exception:
            pass
    return []


def _journal_exit(record: dict) -> None:
    cfg.APEX_JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    rows = _load_journal()
    rows.append(record)
    cfg.APEX_JOURNAL.write_text(json.dumps(rows, indent=2))


def _liquidate(sym: str) -> None:
    """Live close: cancel the symbol's open (protective stop) orders first, then market-sell."""
    client = _trading_client()
    if client is None:
        logger.error(f"[{sym}] no Alpaca client — cannot close")
        return
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym])):
            try:
                client.cancel_order_by_id(o.id)
            except Exception as e:
                logger.debug(f"[{sym}] cancel {o.id} failed: {e}")
        client.close_position(sym)
    except Exception as e:
        logger.error(f"[{sym}] liquidate failed: {e}")


def _close(sym: str, p: dict, h: dict, reason: str, state: dict, dry_run: bool) -> None:
    """Exit a position: (live) liquidate, then journal + state P&L + Telegram, regardless of mode."""
    price = h["price"]
    pnl = round((price - p["entry"]) * p["qty"], 2)
    if not dry_run:
        _liquidate(sym)
    record = {
        "timestamp": datetime.now(ET).isoformat(), "symbol": sym,
        "trigger": p.get("trigger"), "entry": round(float(p["entry"]), 4),
        "exit": round(price, 4), "qty": p["qty"], "pnl": pnl,
        "gain_pct": h["gain_pct"], "health_at_exit": h["health"],
        "peak_gain": round(p.get("peak_gain", h["gain_pct"]), 2),
        "status_at_exit": p.get("status", "intraday"), "days_held": p.get("days_held", 0),
        "entry_window": p.get("entry_window", "normal"),
        "reason": reason, "dry_run": dry_run, "entry_time": p.get("entry_time"),
        "order_id": p.get("order_id"),
    }
    _journal_exit(record)
    state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + pnl, 2)
    state.get("positions", {}).pop(sym, None)
    send_telegram(telegram_exit_message(record))
    logger.info(f"[{sym}] EXIT {reason} px={price:.2f} pnl=${pnl} health={h['health']} "
                f"{'(DRY)' if dry_run else ''}")


# ── per-cycle management ─────────────────────────────────────────────────────────
def _eod_now() -> bool:
    try:
        hh, mm = (int(x) for x in cfg.EOD_CLOSE_ET.split(":"))
    except Exception:
        hh, mm = 15, 45
    return datetime.now(ET).time() >= dtime(hh, mm)


def manage_positions(intraday: dict, state: dict, regime: str, dry_run: bool,
                     live_quotes: dict | None = None) -> int:
    """
    Recompute health for every open position, then act:
      proactive exit on breakdown · conditional EOD carry/close · health-decay warning.
    `live_quotes` (from apex_tv_quotes) supplies the real-time price per symbol when available.
    Returns the number of positions that took an action (exit / carry-graduation).
    """
    c = cfg.effective()
    eod = _eod_now()
    lq = live_quotes or {}
    actions = 0
    for sym in list(state.get("positions", {})):
        p = state["positions"][sym]
        # Swing / multi-week holdings are owned by the daily swing manager (apex_swing.py) — the
        # intraday 5-min health would whipsaw them. Leave them to swing rules.
        if p.get("status", "intraday") != "intraday":
            continue
        lp = lq.get(sym, {}).get("last")
        h = compute_health(p, intraday.get(sym), live_price=lp)
        if not h:
            continue
        prev = p.get("health", 100)
        p["health"] = h["health"]
        p["last_price"] = h["price"]
        p["gain_pct"] = h["gain_pct"]
        p["peak_gain"] = round(max(p.get("peak_gain", 0.0), h["gain_pct"]), 2)

        # 1. Proactive exit — thesis/health broke down; don't ride it to the hard stop.
        if h["health"] < c["exit_health"]:
            _close(sym, p, h,
                   f"health {h['health']} < {int(c['exit_health'])} ({'; '.join(h['reasons'])})",
                   state, dry_run)
            actions += 1
            continue

        # 2. Conditional EOD — carry the healthy runner, force-close the weak.
        if eod:
            runner = (cfg.ALLOW_OVERNIGHT_CARRY and h["health"] >= c["carry_health"]
                      and h["gain_pct"] > 0 and h["price"] >= h["vwap"])
            if runner:
                status = p.get("status", "intraday")
                if status == "intraday":
                    p["status"] = "swing"
                    send_telegram(telegram_carry_message(sym, h["health"], h["gain_pct"],
                                                         "holding above VWAP"))
                    logger.info(f"[{sym}] CARRY overnight (swing) health={h['health']}")
                    actions += 1
                elif status == "swing" and p.get("days_held", 0) >= c["grad_days"]:
                    p["status"] = "position"
                    send_telegram(f"🚀 <b>APEX GRADUATE</b> — {sym} swing→multi-week position\n"
                                  f"{p.get('days_held', 0)}d held  health {h['health']}  "
                                  f"+{h['gain_pct']:.1f}%")
                    logger.info(f"[{sym}] GRADUATE to multi-week position "
                                f"({p.get('days_held', 0)}d) health={h['health']}")
                    actions += 1
                # else already swing/position and still healthy → hold, nothing to announce
            else:
                _close(sym, p, h, "EOD close — thesis weak/scratch (not a runner)",
                       state, dry_run)
                actions += 1
            continue

        # 3. Health-decay warning — crossed below 60 from a healthier reading.
        if prev >= 60 and h["health"] < 60:
            send_telegram(telegram_health_message(sym, h["health"], h["reasons"], h["gain_pct"]))
            logger.info(f"[{sym}] health decay {prev}->{h['health']} ({'; '.join(h['reasons'])})")
    return actions


# ── self-test: replay one past session, show the health trajectory + where it exits ──
def self_test(day_str: str | None) -> None:
    from apex_poller import fetch_intraday, load_leaders
    from apex_entry_engine import detect_entry

    leaders = load_leaders()
    if not leaders:
        print("No leaders file — run apex_daily_filter.py first.")
        return
    syms = [l["symbol"] for l in leaders]
    day = (datetime.strptime(day_str, "%Y-%m-%d").date() if day_str
           else (datetime.now(ET) - timedelta(days=1)).date())
    print(f"APEX health self-test | session {day} | {len(syms)} leaders")
    intraday = fetch_intraday(syms, day)
    print(f"  intraday sessions returned for {len(intraday)} leaders\n")

    shown = 0
    for ld in leaders:
        if shown >= 5:
            break
        sym = ld["symbol"]
        bars = intraday.get(sym)
        sig = detect_entry(sym, bars, orb_minutes=cfg.ORB_MINUTES) if bars is not None else None
        if sig is None:
            continue
        shown += 1
        pos = {"entry": float(sig.price), "qty": 1, "trigger": sig.trigger,
               "health": 100, "peak_gain": 0.0, "status": "intraday"}
        g = bars.sort_values("tod").reset_index(drop=True)
        start = g.index[g["tod"].astype(str) == str(sig.bar_time)]
        start = int(start[0]) if len(start) else 0
        print(f"  {sym} {sig.trigger}  entry ${sig.price:.2f} @ {sig.bar_time}")
        exited = False
        for i in range(start, len(g)):
            h = compute_health(pos, g.iloc[: i + 1])
            if not h:
                continue
            tod = str(g.iloc[i]["tod"])
            tag = "  <- PROACTIVE EXIT" if h["health"] < cfg.EXIT_HEALTH else ""
            print(f"    {tod}  health {h['health']:3d}  ${h['price']:.2f} "
                  f"({h['gain_pct']:+.1f}%)  {'; '.join(h['reasons']) or 'all good'}{tag}")
            if h["health"] < cfg.EXIT_HEALTH:
                exited = True
                break
        if not exited:
            print("    held to close — would carry overnight if a healthy runner")
        print()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--self-test", nargs="?", const="", default=None,
                    help="replay one past session (optional YYYY-MM-DD)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.self_test is not None:
        self_test(args.self_test or None)
    else:
        print("Usage: apex_health.py --self-test [YYYY-MM-DD]")
