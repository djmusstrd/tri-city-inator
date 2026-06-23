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
            "ema9": ema9, "gain_pct": round(gain_pct, 2), "reasons": reasons,
            "stale": live_price is None}   # price came from the delayed bar, not a live tick


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


# ── fault alerting (the keystone: every system fault must REACH the operator) ──────
_FAULT_LAST: dict = {}


def alert_fault(key: str, msg: str, throttle: float = 600.0) -> None:
    """Loud system-fault alert: ERROR log + Telegram, throttled per key so a persistent
    fault doesn't spam every cycle (throttle=0 forces send). Without this the 'fail loud'
    guards never reach the human — exactly how the regime-stuck bug ran silently for sessions."""
    import time as _t
    logger.error(msg)
    now = _t.time()
    if throttle and (now - _FAULT_LAST.get(key, 0.0)) < throttle:
        return
    _FAULT_LAST[key] = now
    try:
        send_telegram(msg)
    except Exception as e:
        logger.debug(f"fault telegram failed: {e}")


# ── broker reconciliation (heal local state vs the broker's ACTUAL truth) ──────────
def _last_exit_fill(client, sym: str):
    """Best-effort exit price: the most recent FILLED sell order for the symbol."""
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        for o in client.get_orders(GetOrdersRequest(
                status=QueryOrderStatus.CLOSED, symbols=[sym], limit=10)):
            if "sell" in str(getattr(o, "side", "")).lower() and getattr(o, "filled_avg_price", None):
                return float(o.filled_avg_price)
    except Exception as e:
        logger.debug(f"[{sym}] exit-fill lookup failed: {e}")
    return None


def reconcile_with_broker(state: dict, dry_run: bool) -> int:
    """Heal divergence between local state and the broker's actual positions/orders — the
    fix for the dangerous silent failures the audit found:
      - phantom / external close (incl. a hard STOP-OUT the manager never saw): in state,
        gone at broker -> journal the exit + debit daily_pnl so the loss limit SEES stop-outs.
      - orphan: held at broker, missing from state -> adopt so it gets managed + EOD-closed.
      - qty drift (partial fill / partial close) -> sync to broker truth.
      - unprotected: held at broker with NO open stop order -> alert loudly (manual re-place).
    Live-only (dry-run has no broker truth). Returns the number of corrections made."""
    if dry_run:
        return 0
    client = _trading_client()
    if client is None:
        return 0
    try:
        broker_pos = {p.symbol: p for p in client.get_all_positions()}
    except Exception as e:
        alert_fault("reconcile_fetch", f"⚠️ APEX reconcile could not read broker positions: {e}")
        return 0

    fixes = 0
    # 1. Phantom / external close — most importantly a hard stop-out the manager never journaled
    for sym in list(state.get("positions", {})):
        if sym in broker_pos:
            continue
        p = state["positions"][sym]
        entry = float(p["entry"])
        exit_px = float(_last_exit_fill(client, sym) or p.get("last_price") or p.get("stop") or entry)
        pnl = round((exit_px - entry) * int(p["qty"]), 2)
        _journal_exit({
            "timestamp": datetime.now(ET).isoformat(), "symbol": sym,
            "trigger": p.get("trigger"), "entry": round(entry, 4), "exit": round(exit_px, 4),
            "qty": int(p["qty"]), "pnl": pnl,
            "gain_pct": round((exit_px - entry) / entry * 100, 2) if entry else 0.0,
            "health_at_exit": p.get("health"),
            "reason": "broker-side close (stop-out/external) — reconciled",
            "dry_run": False, "entry_time": p.get("entry_time"), "order_id": p.get("order_id"),
        })
        state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + pnl, 2)
        state["positions"].pop(sym, None)
        fixes += 1
        alert_fault(f"reconcile_phantom_{sym}",
                    f"🩹 APEX reconciled <b>{sym}</b>: closed at broker (exit ${exit_px:.2f}, "
                    f"pnl ${pnl}). daily_pnl now ${state['daily_pnl']}.", throttle=0)

    # 2. Orphan — at broker, missing from state -> adopt so it's managed + EOD-protected
    for sym, bp in broker_pos.items():
        if sym in state.get("positions", {}):
            continue
        try:
            entry = float(getattr(bp, "avg_entry_price", 0) or 0)
            qty = int(float(getattr(bp, "qty", 0) or 0))
        except Exception:
            continue
        if entry <= 0 or qty <= 0:
            continue
        stop = round(entry * (1 - cfg.effective()["max_stop_pct"]), 2)
        state.setdefault("positions", {})[sym] = {
            "entry": entry, "stop": stop, "qty": qty, "trigger": "RECONCILED",
            "entry_time": datetime.now(ET).isoformat(), "order_id": "RECONCILED",
            "health": 100, "entry_window": "normal", "adopted": True,
        }
        # An adopted orphan is a position we're keeping — give it a real GTC stop now, not just
        # an alert (it may be naked, as the unprotected check below would otherwise only flag).
        protected = _ensure_gtc_stop(sym, state["positions"][sym], dry_run)
        fixes += 1
        alert_fault(f"reconcile_orphan_{sym}",
                    f"⚠️ APEX adopted ORPHAN <b>{sym}</b> ({qty}@${entry:.2f}) — held at broker but "
                    f"missing from state. {'GTC stop placed' if protected else 'STOP PLACE FAILED — protect manually'} "
                    f"@ ${stop:.2f}; now managed.", throttle=0)

    # 3. qty drift (partial fill / partial close) -> sync to broker truth
    for sym, p in state.get("positions", {}).items():
        if sym not in broker_pos:
            continue
        try:
            bqty = int(float(getattr(broker_pos[sym], "qty", p["qty"])))
        except Exception:
            continue
        if bqty > 0 and bqty != int(p["qty"]):
            alert_fault(f"reconcile_qty_{sym}",
                        f"⚠️ APEX <b>{sym}</b> qty drift: state {p['qty']} → broker {bqty}; syncing.",
                        throttle=0)
            p["qty"] = bqty
            fixes += 1

    # 4. Unprotected — held at broker with NO open stop order. Alert-only in v1: auto-replacing
    #    risks a double-stop (oversell) if the OTO leg is merely momentarily absent. Operator acts.
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        protected = {o.symbol for o in client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN))}
        for sym, p in state.get("positions", {}).items():
            if sym in broker_pos and sym not in protected:
                alert_fault(f"reconcile_unprot_{sym}",
                            f"🚨 APEX <b>{sym}</b> is UNPROTECTED — held at broker with no open stop. "
                            f"Place a stop @ ${float(p['stop']):.2f} ({int(p['qty'])} sh) NOW.", throttle=300)
    except Exception as e:
        logger.debug(f"reconcile: open-order check failed: {e}")
    return fixes


def load_carry_decisions() -> dict:
    """Dashboard-written approve/deny for pending overnight carries (today only)."""
    today = datetime.now(ET).date().isoformat()
    if cfg.CARRY_DECISIONS.exists():
        try:
            d = json.loads(cfg.CARRY_DECISIONS.read_text())
            if d.get("date") == today:
                d.setdefault("approve", [])
                d.setdefault("deny", [])
                return d
        except Exception:
            pass
    return {"date": today, "approve": [], "deny": []}


def record_carry_decision(symbol: str, decision: str) -> None:
    """decision = 'approve' (lock as swing) | 'deny' (flatten). Written by the dashboard; the
    poller applies it on its next EOD-window pass."""
    d = load_carry_decisions()
    other = "deny" if decision == "approve" else "approve"
    if symbol not in d[decision]:
        d[decision].append(symbol)
    if symbol in d.get(other, []):
        d[other].remove(symbol)
    cfg.CARRY_DECISIONS.write_text(json.dumps(d, indent=2))


def _confirm_exit_fill(client, order_id, sym, tries: int = 8, delay: float = 0.4):
    """Poll the close (market-sell) order until it reports a realized average fill price; fall
    back to the most-recent filled sell. Returns the exit price, or None if unconfirmable in time.
    This is what lets the journal/P&L use what we ACTUALLY got, not the pre-close health snapshot."""
    import time as _t
    for _ in range(tries):
        try:
            if order_id:
                o = client.get_order_by_id(order_id)
                if getattr(o, "filled_avg_price", None):
                    return float(o.filled_avg_price)
                if str(getattr(o, "status", "")).lower() in (
                        "rejected", "canceled", "cancelled", "expired"):
                    break
        except Exception:
            pass
        _t.sleep(delay)
    return _last_exit_fill(client, sym)


def _liquidate(sym: str) -> tuple[str, float | None]:
    """Live close: cancel the symbol's open (protective stop) orders, then market-sell.
    Returns (status, fill_price): status is 'ok' (closed), 'gone' (broker had no such position —
    already flat), or 'fail'; fill_price is the realized average exit price when known."""
    client = _trading_client()
    if client is None:
        logger.error(f"[{sym}] no Alpaca client — cannot close")
        return "fail", None
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[sym])):
            try:
                client.cancel_order_by_id(o.id)
            except Exception as e:
                logger.debug(f"[{sym}] cancel {o.id} failed: {e}")
        resp = client.close_position(sym)
        fill_px = _confirm_exit_fill(client, getattr(resp, "id", None), sym)
        return "ok", fill_px
    except Exception as e:
        msg = str(e).lower()
        if any(k in msg for k in ("not exist", "no position", "404", "not found")):
            return "gone", None     # already flat at the broker — safe to mark closed
        logger.error(f"[{sym}] liquidate failed: {e}")
        return "fail", None


def _reprotect(sym: str, p: dict) -> bool:
    """Re-place a protective stop for a position whose close FAILED after its stop was cancelled,
    so it's never left naked while we wait to retry."""
    client = _trading_client()
    if client is None:
        return False
    try:
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        client.submit_order(StopOrderRequest(
            symbol=sym, qty=int(p["qty"]), side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY, stop_price=round(float(p["stop"]), 2)))
        return True
    except Exception as e:
        logger.error(f"[{sym}] re-protect failed: {e}")
        return False


def _ensure_gtc_stop(sym: str, p: dict, dry_run: bool = False) -> bool:
    """Guarantee exactly ONE GTC protective stop for a position that will be HELD past the close.
    Entry stops are DAY-tif and expire at 3pm CT, so an overnight carry would otherwise go naked
    (this bit us 2026-06-22: 5 forced carries, all unprotected). Idempotent — if a GTC stop
    already exists it's left alone; otherwise cancel any open (DAY) stops for the symbol and place
    one GTC stop at p['stop']. No-op in dry_run."""
    if dry_run:
        return True
    client = _trading_client()
    if client is None:
        return False
    try:
        from alpaca.trading.requests import GetOrdersRequest, StopOrderRequest
        from alpaca.trading.enums import QueryOrderStatus, OrderSide, TimeInForce
        import time as _t
        existing = list(client.get_orders(GetOrdersRequest(
            status=QueryOrderStatus.OPEN, symbols=[sym])))
        if any(str(getattr(o, "time_in_force", "")).lower() == "gtc"
               and "stop" in str(getattr(o, "order_type", "")).lower() for o in existing):
            return True                        # already protected overnight — leave it
        for o in existing:
            try:
                client.cancel_order_by_id(o.id)
            except Exception as e:
                logger.debug(f"[{sym}] cancel {o.id} failed: {e}")
        _t.sleep(0.5)                          # let the cancel free the held shares
        client.submit_order(StopOrderRequest(
            symbol=sym, qty=int(p["qty"]), side=OrderSide.SELL,
            time_in_force=TimeInForce.GTC, stop_price=round(float(p["stop"]), 2)))
        logger.info(f"[{sym}] carry stop → GTC @ ${float(p['stop']):.2f} ({int(p['qty'])} sh)")
        return True
    except Exception as e:
        logger.error(f"[{sym}] GTC stop placement failed: {e}")
        return False


def _close(sym: str, p: dict, h: dict, reason: str, state: dict, dry_run: bool) -> None:
    """Exit a position: (live) liquidate, then journal + state P&L + Telegram, regardless of mode."""
    price = h["price"]
    if not dry_run:
        status, fill_px = _liquidate(sym)
        if status == "fail":
            # Close failed AFTER the protective stop was cancelled. Re-protect, KEEP the position
            # so the next pass / reconcile retries, and page the operator. Never journal a phantom
            # close that abandons a still-open (now unprotected) position.
            reprotected = _reprotect(sym, p)
            alert_fault(f"close_fail_{sym}",
                        f"🚨 APEX FAILED to close <b>{sym}</b> ({int(p['qty'])} sh). Position kept; "
                        f"stop {('re-placed @ $%.2f' % float(p['stop'])) if reprotected else 'RE-PLACE FAILED — MANUAL ACTION NOW'}. Will retry.")
            return
        # Book the REALIZED exit fill, not the pre-close health snapshot, so journal/P&L/loss-limit
        # reflect what we actually got (the snapshot understated fades, e.g. QH -37→-43).
        if fill_px and fill_px > 0:
            price = fill_px
    entry = float(p["entry"])
    pnl = round((price - entry) * p["qty"], 2)
    gain_pct = round((price - entry) / entry * 100, 2) if entry else h["gain_pct"]
    record = {
        "timestamp": datetime.now(ET).isoformat(), "symbol": sym,
        "trigger": p.get("trigger"), "entry": round(entry, 4),
        "exit": round(price, 4), "qty": p["qty"], "pnl": pnl,
        "gain_pct": gain_pct, "health_at_exit": h["health"],
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


def _position_age_sec(p: dict) -> float | None:
    """Seconds since this position was entered, or None if entry_time is missing/unparseable."""
    ts = p.get("entry_time")
    if not ts:
        return None
    try:
        et = datetime.fromisoformat(ts)
        now = datetime.now(et.tzinfo) if et.tzinfo else datetime.now()
        return (now - et).total_seconds()
    except Exception:
        return None


def manage_positions(intraday: dict, state: dict, regime: str, dry_run: bool,
                     live_quotes: dict | None = None, force_eod: bool = False) -> int:
    """
    Recompute health for every open position, then act:
      proactive exit on breakdown · conditional EOD carry/close · health-decay warning.
    `live_quotes` (from apex_tv_quotes) supplies the real-time price per symbol when available.
    Returns the number of positions that took an action (exit / carry-graduation).
    """
    c = cfg.effective()
    eod = _eod_now() or force_eod          # force_eod: real session close reached (incl. half-days)
    lq = live_quotes or {}
    dec = load_carry_decisions()
    actions = 0
    for sym in list(state.get("positions", {})):
        p = state["positions"][sym]
        # A. Pending-carry deny window: a carry was PROPOSED at EOD and carries by default — the
        #    operator can deny it on the dashboard (→ flatten) or approve it (→ lock as swing).
        if p.get("pending_deny"):
            if sym in dec.get("deny", []):
                price = lq.get(sym, {}).get("last") or p.get("last_price") or p["entry"]
                price = float(price)
                hh = {"price": price, "health": p.get("health", 0),
                      "gain_pct": round((price - p["entry"]) / p["entry"] * 100, 2),
                      "reasons": ["operator denied carry"]}
                _close(sym, p, hh, "carry DENIED by operator — flattened", state, dry_run)
                actions += 1
                continue
            if sym in dec.get("approve", []):
                p["pending_deny"] = False    # locked in as a swing
        # B. Swing / multi-week holdings are owned by the daily swing manager (apex_swing.py) —
        #    the intraday 5-min health would whipsaw them. Leave them to swing rules.
        if p.get("status", "intraday") != "intraday":
            continue
        lp = lq.get(sym, {}).get("last")
        h = compute_health(p, intraday.get(sym), live_price=lp)
        if not h:
            if force_eod:
                # Session closed with no fresh bars to score — flatten on the last known price
                # so a half-day close never silently carries a position the operator expected out.
                lastpx = float(lp or p.get("last_price") or p["entry"])
                entry = float(p["entry"])
                hh = {"price": lastpx, "health": p.get("health", 0), "vwap": lastpx,
                      "gain_pct": round((lastpx - entry) / entry * 100, 2) if entry else 0.0,
                      "reasons": ["session closed — final EOD flatten"]}
                _close(sym, p, hh, "final EOD flatten (no live bars)", state, dry_run)
                actions += 1
            continue
        prev = p.get("health", 100)
        p["health"] = h["health"]
        p["last_price"] = h["price"]
        p["gain_pct"] = h["gain_pct"]
        p["peak_gain"] = round(max(p.get("peak_gain", 0.0), h["gain_pct"]), 2)

        # 1. Proactive exit — thesis/health broke down; don't ride it to the hard stop.
        if h["health"] < c["exit_health"]:
            # Fresh-entry phantom-churn guard: a just-entered symbol isn't warm in the live quote
            # feed yet, so health may be computed off a stale (delayed) bar. Don't dump a position
            # on a price that doesn't reflect the market, nor within the entry grace window — the
            # broker hard stop still protects, and the next pass re-checks on live data.
            stale = c["exit_require_live"] and h.get("stale", False)
            age = _position_age_sec(p)
            fresh = age is not None and age < c["entry_grace_sec"]
            if stale or fresh:
                logger.info(f"[{sym}] health {h['health']} < {int(c['exit_health'])} but HELD "
                            f"({'stale price' if stale else f'fresh entry {int(age)}s'}) — "
                            f"broker hard stop still protects")
            else:
                _close(sym, p, h,
                       f"health {h['health']} < {int(c['exit_health'])} ({'; '.join(h['reasons'])})",
                       state, dry_run)
                actions += 1
            continue

        # 2. Conditional EOD — PROPOSE the healthy runner as an overnight carry (default carry,
        #    operator can deny on the dashboard); force-close the weak.
        if eod:
            runner = (cfg.ALLOW_OVERNIGHT_CARRY and h["health"] >= c["carry_health"]
                      and h["gain_pct"] > 0 and h["price"] >= h["vwap"])
            if runner:
                if not p.get("pending_deny"):     # propose once
                    p["status"] = "swing"
                    p["pending_deny"] = True       # carries by default; deniable until close
                    # The intraday DAY stop expires at the close — convert to GTC so the carry
                    # is protected overnight / into tomorrow's open.
                    _ensure_gtc_stop(sym, p, dry_run)
                    send_telegram(telegram_carry_message(sym, h["health"], h["gain_pct"],
                                                         "holding above VWAP"))
                    logger.info(f"[{sym}] CARRY PROPOSED (default carry; deny on dashboard) "
                                f"health={h['health']}")
                    actions += 1
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
