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


def execute(signal, rs_pct: float, atr: float, regime: str, state: dict,
            dry_run: bool = False) -> bool:
    """Run guards → size → place entry+stop → log execution + rationale → Telegram."""
    c = cfg.effective()
    sym = signal.symbol

    # ── Guards ──
    if signal.trigger in c["disabled_setups"]:
        logger.info(f"[{sym}] BLOCKED: {signal.trigger} disabled by Layer 4")
        return False
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
    if score < c["entry_thresh"]:
        logger.info(f"[{sym}] BLOCKED: score {score} < threshold {c['entry_thresh']}")
        return False

    # ── Sizing (risk-based, no leverage) ──
    entry_price = float(signal.price)
    # ATR stop, capped so volatile/high-priced leaders don't get absurd (e.g. 60%) stops
    stop_dist = min(atr * c["atr_stop_mult"], entry_price * c["max_stop_pct"])
    stop_price = entry_price - stop_dist
    risk_dollars = min(c["equity"] * (c["risk_pct"] / 100.0), c["max_risk"])
    raw_qty = math.floor(risk_dollars / stop_dist) if stop_dist > 0 else 0
    cash_qty = math.floor(c["equity"] / entry_price)
    qty = max(1, min(raw_qty, cash_qty))

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
        except Exception as e:
            logger.error(f"[{sym}] order failed: {e}")
            return False

    # ── Rationale snapshot (Layer 6) ──
    rationale = build_rationale(signal, rs_pct, score, regime, entry_price,
                                stop_price, atr, qty)
    rationale["order_id"] = order_id
    rationale["dry_run"] = dry_run
    log_rationale(rationale)

    # ── Execution log ──
    rows = _load_exec_log()
    rows.append({
        "timestamp": datetime.now(ET).isoformat(), "symbol": sym,
        "trigger": signal.trigger, "score": score, "rs_pct": round(rs_pct, 1),
        "entry": round(entry_price, 4), "stop": round(stop_price, 4),
        "qty": qty, "atr": round(atr, 4), "regime": regime,
        "order_id": order_id, "dry_run": dry_run,
    })
    _save_exec_log(rows)

    # ── State ──
    state.setdefault("positions", {})[sym] = {
        "entry": entry_price, "stop": stop_price, "qty": qty,
        "trigger": signal.trigger, "entry_time": datetime.now(ET).isoformat(),
        "order_id": order_id, "health": 100,
    }
    state.setdefault("executed_today", []).append(sym)

    _send_telegram(telegram_entry_message(rationale))
    logger.info(f"[{sym}] ORDER PLACED qty={qty} stop={stop_price:.2f} order={order_id}")
    return True
