#!/usr/bin/env python3
"""
APEX manual-override ACTIONS — the one shared close primitive used by BOTH the Telegram buttons and
the dashboard.  Broker-only by design:
  - FULL close  -> reuse the proven _liquidate(); the poller's reconcile_with_broker journals it
                   (one entry — verified 2026-06-24 on 7 manual closes). Journaling here too would
                   double-journal (the ORKA bug), so we DON'T.
  - PARTIAL trim -> reconcile only syncs qty (no journal), so we journal the realized partial HERE
                   and re-stop the remainder so the runner is never left naked.

Safe-by-default: callers gate on APEX_MANUAL_OVERRIDE; this module only executes a validated broker
action when explicitly asked. Design: docs/PRD_APEX_MANUAL_OVERRIDE.md.

Known v1 limitation: a partial trim's realized P&L lands in the journal but is NOT added to the
live state's daily_pnl counter (state is owned by the poller; reconcile syncs qty only). Full
closes are fully consistent via reconcile. Tracked in the PRD open items.
"""
from __future__ import annotations
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import apex_config as cfg
import apex_flags
from apex_health import (_trading_client, _liquidate, _confirm_exit_fill,
                         _journal_exit, _reprotect)

ET = ZoneInfo("America/New_York")
logger = logging.getLogger("apex.actions")


def tv_chart_url(symbol: str, exchange: str | None = None, interval: int | None = None) -> str:
    """TradingView chart deep-link. Exchange prefix (from Alpaca position data) resolves the right
    ticker; interval pins the trading timeframe so it opens on the chart you trade off."""
    sym = f"{exchange}:{symbol}" if exchange else symbol
    iv = interval if interval is not None else cfg.ORB_MINUTES
    return f"https://www.tradingview.com/chart/?symbol={sym}&interval={iv}"


def _state_stop(symbol: str) -> float | None:
    """Protective-stop price for a held symbol, from the poller's state (read-only)."""
    try:
        s = json.loads(cfg.STATE_FILE.read_text())
        return float(s["positions"][symbol]["stop"])
    except Exception:
        return None


def close_now(symbol: str, fraction: float = 1.0, block: bool = False) -> dict:
    """Operator-initiated close.
      fraction >= 1.0 -> FULL close (reconcile journals it).
      0 < fraction < 1 -> TRIM (journals the partial here + re-stops the remainder).
      block=True       -> add to today's avoid list so the poller can't re-enter the name.
    Returns a result dict for the caller to echo back to Telegram/dashboard. Idempotent: a name
    already flat returns status 'gone' (safe no-op), so a double-tap or a rule that beat the tap
    can't do harm."""
    symbol = symbol.upper()
    if block:
        try:
            apex_flags.set_flag("avoid", symbol, "operator manual close — no re-entry today")
        except Exception as e:
            logger.error(f"[{symbol}] avoid-flag failed: {e}")

    # ── FULL close: proven liquidate; reconcile_with_broker journals it ──
    if fraction >= 1.0:
        status, fill = _liquidate(symbol)
        logger.info(f"[{symbol}] manual FULL close → {status} fill={fill} block={block}")
        return {"symbol": symbol, "action": "close", "status": status,
                "fill": fill, "blocked": block}

    # ── PARTIAL trim ──
    client = _trading_client()
    if client is None:
        return {"symbol": symbol, "action": "trim", "status": "fail", "reason": "no client"}
    try:
        pos = client.get_open_position(symbol)
    except Exception:
        return {"symbol": symbol, "action": "trim", "status": "gone"}   # already flat — safe no-op

    qty = int(float(pos.qty)); entry = float(pos.avg_entry_price)
    sell = int(qty * fraction); remaining = qty - sell
    if sell < 1 or remaining < 1:                       # can't split cleanly → full close
        status, fill = _liquidate(symbol)
        return {"symbol": symbol, "action": "close", "status": status, "fill": fill,
                "reason": "trim too small → full close", "blocked": block}

    try:
        from alpaca.trading.requests import (GetOrdersRequest, ClosePositionRequest,
                                             StopOrderRequest)
        from alpaca.trading.enums import QueryOrderStatus, OrderSide, TimeInForce
        import time as _t

        # 1. free the shares: cancel the open protective stop(s)
        for o in client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])):
            try:
                client.cancel_order_by_id(o.id)
            except Exception as e:
                logger.debug(f"[{symbol}] cancel {o.id} failed: {e}")
        _t.sleep(0.5)

        # 2. sell the trimmed qty
        try:
            resp = client.close_position(symbol, ClosePositionRequest(qty=str(sell)))
        except Exception as e:
            # sell failed AFTER the stop was cancelled → re-protect the FULL position, never naked
            _reprotect(symbol, {"qty": qty, "stop": _state_stop(symbol) or entry})
            logger.error(f"[{symbol}] trim sell failed, re-protected full qty: {e}")
            return {"symbol": symbol, "action": "trim", "status": "fail", "reason": str(e)}
        fill = _confirm_exit_fill(client, getattr(resp, "id", None), symbol)

        # 3. re-stop the remainder so the runner is never naked
        stop = _state_stop(symbol)
        if stop:
            _t.sleep(0.5)
            try:
                client.submit_order(StopOrderRequest(
                    symbol=symbol, qty=remaining, side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY, stop_price=round(stop, 2)))
            except Exception as e:
                logger.error(f"[{symbol}] remainder re-stop FAILED: {e}")
                stop = None

        # 4. journal the realized partial (reconcile won't — it only syncs qty)
        px = fill if (fill and fill > 0) else entry
        pnl = round((px - entry) * sell, 2)
        _journal_exit({
            "timestamp": datetime.now(ET).isoformat(), "symbol": symbol,
            "entry": round(entry, 4), "exit": round(px, 4), "qty": sell, "pnl": pnl,
            "gain_pct": round((px - entry) / entry * 100, 2) if entry else 0.0,
            "reason": f"manual trim {int(fraction*100)}% (operator)", "dry_run": False,
            "partial": True, "remaining_qty": remaining,
        })
        logger.info(f"[{symbol}] manual TRIM {sell}/{qty} px={px} pnl=${pnl}; "
                    f"{remaining} left {'(re-stopped)' if stop else '(NO STOP — verify!)'}")
        return {"symbol": symbol, "action": "trim", "status": "ok", "sold": sell,
                "remaining": remaining, "fill": px, "pnl": pnl,
                "restopped": bool(stop), "blocked": block}
    except Exception as e:
        logger.error(f"[{symbol}] trim failed: {e}")
        return {"symbol": symbol, "action": "trim", "status": "fail", "reason": str(e)}


# ── Telegram/dashboard manual-override surface ─────────────────────────────────────
_PENDING_CONFIRM: dict = {}                # callback_data -> expiry ts (2-tap confirm guard)
_CONFIRM_WINDOW = 30.0
_ACTIONS = {"close": (1.0, False), "trim": (0.5, False), "closeblock": (1.0, True)}


def manual_override_enabled() -> bool:
    """True only if the env flag is on AND the live kill-switch in apex-flags.json isn't false.
    The flags check is read each call, so it can be disabled mid-session with no restart."""
    if not cfg.MANUAL_OVERRIDE:
        return False
    try:
        return bool(apex_flags.load_flags().get("manual_override", True))
    except Exception:
        return True


def override_keyboard(symbol: str, exchange: str | None = None) -> dict:
    """Inline keyboard for an alert: a chart link + the three action buttons."""
    sym = symbol.upper()
    return {"inline_keyboard": [
        [{"text": "📈 Chart", "url": tv_chart_url(symbol, exchange)}],
        [{"text": "❌ Close", "callback_data": f"close:{sym}"},
         {"text": "✂️ Trim ½", "callback_data": f"trim:{sym}"},
         {"text": "🚫 Close+block", "callback_data": f"closeblock:{sym}"}],
    ]}


def _fmt_result(r: dict) -> str:
    s, sym = r.get("status"), r.get("symbol")
    if s == "gone":
        return f"{sym}: already flat — no action."
    if s == "fail":
        return f"⚠️ {sym}: action FAILED ({r.get('reason', '?')}). Check the broker."
    if r.get("action") == "trim" and s == "ok":
        tail = " (re-stopped)" if r.get("restopped") else " (NO stop — verify!)"
        return (f"✂️ {sym}: trimmed {r.get('sold')} sh @ ${r.get('fill')}, "
                f"{r.get('remaining')} left{tail}.")
    blk = " · re-entry blocked today" if r.get("blocked") else ""
    return f"❌ {sym}: closed @ ${r.get('fill')}{blk}."


def handle_callback(data: str) -> str:
    """Dispatch a button tap ('action:SYM') to close_now, honoring the 2-tap confirm guard when
    APEX_OVERRIDE_CONFIRM is on. Returns a short human result for the Telegram confirmation."""
    if not manual_override_enabled():
        return "Manual override is OFF."
    try:
        action, sym = data.split(":", 1)
    except ValueError:
        return "Unrecognized button."
    if action not in _ACTIONS:
        return f"Unknown action: {action}."
    fraction, block = _ACTIONS[action]
    if cfg.OVERRIDE_CONFIRM:
        import time
        now = time.time()
        if now > _PENDING_CONFIRM.get(data, 0):
            _PENDING_CONFIRM[data] = now + _CONFIRM_WINDOW
            verb = {"close": "CLOSE", "trim": "TRIM ½", "closeblock": "CLOSE + BLOCK"}[action]
            return f"⚠️ Tap again within {int(_CONFIRM_WINDOW)}s to confirm {verb} {sym}."
        _PENDING_CONFIRM.pop(data, None)
    return _fmt_result(close_now(sym, fraction=fraction, block=block))


if __name__ == "__main__":   # quick manual check (safe on a flat account → 'gone')
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("tv_chart_url:", tv_chart_url("RXT", "NASDAQ"))
    import sys
    if len(sys.argv) > 1:
        print(close_now(sys.argv[1], fraction=float(sys.argv[2]) if len(sys.argv) > 2 else 1.0))
