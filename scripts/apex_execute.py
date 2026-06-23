#!/usr/bin/env python3
"""
APEX execution — guards + sizing + order placement for an entry signal.

Long-only. Risk-based sizing, no leverage (cash-capped). ATR stop. Per the "never sell
without a reason" design, NO hard take-profit is placed at entry — the exit is owned by the
Layer 3 health monitor (TODO). For the skeleton we place entry + protective stop only.

Logs every entry to logs/apex-executions.json and persists a Layer 6 rationale snapshot.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import apex_config as cfg
from apex_entry_engine import composite_score
from apex_rationale import build_rationale, log_rationale, telegram_entry_message
from apex_thesis import build_thesis

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("apex.execute")


def _alpaca_client():
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


def _send_telegram(msg: str) -> None:
    if not cfg.TELEGRAM_BOT_TOKEN or not cfg.TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.parse, urllib.request as _ur
        url = f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": cfg.TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
        ).encode()
        _ur.urlopen(_ur.Request(url, data=data), timeout=5)
    except Exception as e:
        logger.debug(f"telegram failed: {e}")


def _load_exec_log() -> list:
    if cfg.EXEC_LOG.exists():
        try:
            return json.loads(cfg.EXEC_LOG.read_text())
        except Exception:
            pass
    return []


def _save_exec_log(rows: list) -> None:
    cfg.EXEC_LOG.parent.mkdir(parents=True, exist_ok=True)
    cfg.EXEC_LOG.write_text(json.dumps(rows, indent=2))


def _confirm_order(client, order_id, tries: int = 12, delay: float = 0.4):
    """Poll the order until TERMINAL: return (filled_avg_price|None, filled_qty|None, status).
    Confirms the entry actually filled BEFORE we record a position — a rejected/expired order must
    not write a phantom full-size position into state, and a partial fill records the REAL qty. The
    fill price (not the signal/quote price) also feeds Layer 3 health so a stale TV quote can't read
    a phantom loss and churn the position out seconds after entering.

    We poll to a terminal status (do NOT break on the first partial: a market order fills in
    several prints, and breaking on the first one under-recorded the size — e.g. ARMG booked 2 of
    10 shares, 2026-06-23). Only if it's still mid-fill after the whole window do we accept the
    partial we've seen so far.
    """
    import time as _t
    status, fap, fqty = "", None, None
    for _ in range(tries):
        try:
            o = client.get_order_by_id(order_id)
            status = str(getattr(o, "status", "")).lower()
            fap = getattr(o, "filled_avg_price", None)
            fqty = getattr(o, "filled_qty", None)
            if status in ("filled", "rejected", "canceled", "cancelled", "expired"):
                break
        except Exception:
            pass
        _t.sleep(delay)
    try:
        fqty = int(float(fqty)) if fqty not in (None, "") else None
    except Exception:
        fqty = None
    return (float(fap) if fap else None), fqty, status


def execute(signal, rs_pct: float, atr: float, regime: str, state: dict,
            dry_run: bool = False, daily_bars=None, prioritized: bool = False) -> bool:
    """Run guards → size → place entry+stop → log execution + rationale → Telegram."""
    c = cfg.effective()
    sym = signal.symbol

    # ── Guards ──
    if signal.trigger in c["disabled_setups"]:
        logger.info(f"[{sym}] BLOCKED: {signal.trigger} disabled by Layer 4")
        return False
    try:
        from apex_flags import load_flags
        if sym in load_flags().get("avoid", {}):
            logger.info(f"[{sym}] BLOCKED: flagged AVOID by operator")
            return False
    except Exception:
        pass
    if sym in state.get("positions", {}):
        logger.info(f"[{sym}] BLOCKED: already in position")
        return False
    if sym in state.get("executed_today", []):
        logger.info(f"[{sym}] BLOCKED: already executed today")
        return False
    if len(state.get("positions", {})) >= c["max_positions"]:
        logger.info(f"[{sym}] BLOCKED: max positions {c['max_positions']}")
        return False
    if state.get("daily_pnl", 0.0) <= c["daily_loss"]:
        logger.info(f"[{sym}] BLOCKED: daily loss circuit breaker")
        return False
    if not atr or atr <= 0:
        logger.info(f"[{sym}] BLOCKED: invalid ATR (data quality)")
        return False

    score = composite_score(signal, rs_pct)
    thresh = c["entry_thresh"] - (cfg.PRIORITIZE_RELAX if prioritized else 0)
    if score < thresh:
        logger.info(f"[{sym}] BLOCKED: score {score} < threshold {thresh}"
                    f"{' (prioritized)' if prioritized else ''}")
        return False

    # ── Sizing (risk-based, no leverage) ──
    entry_price = float(signal.price)
    # Price band: bias the book to the compoundable $2-30 zone and skip dead / un-sizable
    # high-priced names (band backtest: $100+ ≈ zero edge / forced-1-share). Tunable via
    # APEX_PRICE_MIN/MAX. Existing positions are unaffected (guarded out above).
    if not (c["price_min"] <= entry_price <= c["price_max"]):
        logger.info(f"[{sym}] BLOCKED: price ${entry_price:.2f} outside band "
                    f"${c['price_min']:.0f}-${c['price_max']:.0f}")
        return False
    # ATR stop, capped so volatile/high-priced leaders don't get absurd (e.g. 60%) stops
    stop_dist = min(atr * c["atr_stop_mult"], entry_price * c["max_stop_pct"])
    stop_price = entry_price - stop_dist
    risk_dollars = min(c["equity"] * (c["risk_pct"] / 100.0), c["max_risk"])
    raw_qty = math.floor(risk_dollars / stop_dist) if stop_dist > 0 else 0
    cash_qty = math.floor(c["equity"] / entry_price)
    qty = min(raw_qty, cash_qty)
    # Never force a share over budget: if one share's stop distance already busts the risk
    # cap, skip the trade (the old max(1,…) silently over-risked high-priced names).
    if qty < 1:
        logger.info(f"[{sym}] BLOCKED: 1 share risks ${stop_dist:.2f} > cap ${risk_dollars:.0f} "
                    f"(price ${entry_price:.2f} too high to size within risk)")
        return False

    logger.info(f"[{sym}] ENTRY {signal.trigger} score={score} px={entry_price:.2f} "
                f"stop={stop_price:.2f} qty={qty} {'(DRY)' if dry_run else ''}")

    order_id = "DRY-RUN"
    if not dry_run:
        client = _alpaca_client()
        if client is None:
            logger.error(f"[{sym}] no Alpaca client")
            return False
        try:
            from alpaca.trading.requests import MarketOrderRequest, StopLossRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
            # entry + protective stop (no TP cap — Layer 3 owns the exit)
            order = MarketOrderRequest(
                symbol=sym, qty=qty, side=OrderSide.BUY,
                time_in_force=TimeInForce.DAY, order_class=OrderClass.OTO,
                stop_loss=StopLossRequest(stop_price=round(stop_price, 2)),
            )
            resp = client.submit_order(order)
            order_id = str(resp.id)
            # Confirm the order actually filled BEFORE recording a position.
            fill_price, filled_qty, status = _confirm_order(client, resp.id)
            if status in ("rejected", "canceled", "cancelled", "expired"):
                from apex_health import alert_fault
                alert_fault(f"order_{sym}",
                            f"🚨 APEX <b>{sym}</b> entry order {status} — no position recorded.")
                return False
            # Reconcile entry to the ACTUAL fill (not the signal/quote price) so Layer 3 health
            # compares against what we really paid — kills the phantom-loss churn.
            if fill_price and fill_price > 0:
                if abs(fill_price - entry_price) / entry_price > 0.005:
                    logger.info(f"[{sym}] entry reconciled: signal ${entry_price:.2f} → "
                                f"fill ${fill_price:.2f}")
                entry_price = fill_price
            # Honor a partial fill: record the real position size, not the requested qty.
            if filled_qty and 0 < filled_qty < qty:
                logger.info(f"[{sym}] PARTIAL fill {filled_qty}/{qty} — recording actual qty")
                qty = filled_qty
        except Exception as e:
            logger.error(f"[{sym}] order failed: {e}")
            return False

    # ── Rationale snapshot (Layer 6) ──
    rationale = build_rationale(signal, rs_pct, score, regime, entry_price,
                                stop_price, atr, qty)
    rationale["order_id"] = order_id
    rationale["dry_run"] = dry_run
    if prioritized:
        rationale["flag"] = "⭐ prioritized"
    # Tag the entry window (last hour before the close = "late") for performance evaluation.
    try:
        _hh, _mm = (int(x) for x in cfg.LATE_ENTRY_ET.split(":"))
        now_et = datetime.now(ET).time()
        rationale["entry_window"] = "late" if now_et >= __import__("datetime").time(_hh, _mm) else "normal"
    except Exception:
        rationale["entry_window"] = "normal"
    try:
        rationale["thesis"] = build_thesis(signal, daily_bars, stop_price)
    except Exception as e:
        logger.debug(f"[{sym}] thesis build failed: {e}")
    log_rationale(rationale)

    # ── Execution log ──
    rows = _load_exec_log()
    rows.append({
        "timestamp": datetime.now(ET).isoformat(), "symbol": sym,
        "trigger": signal.trigger, "score": score, "rs_pct": round(rs_pct, 1),
        "entry": round(entry_price, 4), "stop": round(stop_price, 4),
        "qty": qty, "atr": round(atr, 4), "regime": regime,
        "entry_window": rationale.get("entry_window", "normal"),
        "order_id": order_id, "dry_run": dry_run,
    })
    _save_exec_log(rows)

    # ── State ──
    state.setdefault("positions", {})[sym] = {
        "entry": entry_price, "stop": stop_price, "qty": qty,
        "trigger": signal.trigger, "entry_time": datetime.now(ET).isoformat(),
        "order_id": order_id, "health": 100,
        "entry_window": rationale.get("entry_window", "normal"),
    }
    state.setdefault("executed_today", []).append(sym)

    _send_telegram(telegram_entry_message(rationale))
    logger.info(f"[{sym}] ORDER PLACED qty={qty} stop={stop_price:.2f} order={order_id}")

    # On a real buy, add the symbol to the APEX TradingView watchlist (best-effort).
    if not dry_run:
        try:
            from apex_tv_control import watchlist_add
            watchlist_add(sym)
        except Exception as e:
            logger.debug(f"[{sym}] watchlist_add skipped: {e}")
    return True
