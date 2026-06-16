#!/usr/bin/env python3
"""
SANDBOX POSITION MANAGER — Manages open sandbox positions.

Called each poller cycle after signal reading.

Actions:
  1. Exit signal → close position immediately (Supertrend flip / Super Scalper Sell)
  2. EOD close   → force-flat all at 2:45 PM ET
  3. Sync state  → remove positions closed by bracket (filled stop/target)

Logs exits to logs/sandbox-journal.json.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path.home() / "tri-city-inator"
sys.path.insert(0, str(WORKSPACE))

try:
    from dotenv import load_dotenv
    _env = WORKSPACE / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

ET         = ZoneInfo("America/New_York")
CT         = ZoneInfo("America/Chicago")
STATE_FILE = WORKSPACE / "shared" / "sandbox-state.json"
JOURNAL    = WORKSPACE / "logs" / "sandbox-journal.json"

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

EOD_HOUR   = int(os.getenv("SANDBOX_EOD_HOUR",   "14"))
EOD_MINUTE = int(os.getenv("SANDBOX_EOD_MINUTE", "45"))

logger = logging.getLogger(__name__)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"date": "", "last_bar_idx": -1, "daily_pnl": 0.0,
            "positions": {}, "executed_today": [], "stopped_today": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _load_journal() -> list:
    if JOURNAL.exists():
        try:
            return json.loads(JOURNAL.read_text())
        except Exception:
            pass
    return []


def _save_journal(entries: list) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL.write_text(json.dumps(entries, indent=2))


def _get_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.trading.client import TradingClient
        return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    except ImportError:
        return None


def _get_open_alpaca_positions(client) -> dict[str, dict]:
    """Return {symbol: {qty, current_price, unrealized_pl}} from Alpaca."""
    if not client:
        return {}
    try:
        positions = client.get_all_positions()
        return {
            p.symbol: {
                "qty":           int(float(p.qty)),
                "current_price": float(p.current_price),
                "unrealized_pl": float(p.unrealized_pl),
                "entry_price":   float(p.avg_entry_price),
            }
            for p in positions
            if float(p.qty) > 0
        }
    except Exception as e:
        logger.warning(f"Alpaca position fetch failed: {e}")
        return {}


def _close_position(client, symbol: str) -> bool:
    """Market close all shares of a symbol."""
    if not client:
        return False
    try:
        client.close_position(symbol)
        logger.info(f"[{symbol}] CLOSED via market order")
        return True
    except Exception as e:
        logger.error(f"[{symbol}] Close failed: {e}")
        return False


def _cancel_all_orders(client, symbol: str) -> None:
    if not client:
        return
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, symbols=[symbol])
        orders = client.get_orders(req)
        for o in orders:
            try:
                client.cancel_order_by_id(o.id)
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Cancel orders for {symbol} failed: {e}")


def _log_exit(symbol: str, position: dict, exit_price: float,
              exit_reason: str, now: datetime) -> float:
    """Append exit record to journal. Returns realized P&L."""
    entry_price = position.get("entry", 0.0)
    qty         = position.get("qty", 0)
    pnl         = (exit_price - entry_price) * qty
    r_multiple  = 0.0
    stop        = position.get("stop", entry_price)
    risk_ps     = abs(entry_price - stop)
    if risk_ps > 0:
        r_multiple = round((exit_price - entry_price) / risk_ps, 2)

    record = {
        "date":        now.strftime("%Y-%m-%d"),
        "symbol":      symbol,
        "strategy":    position.get("strategy", ""),
        "entry":       entry_price,
        "exit":        round(exit_price, 2),
        "qty":         qty,
        "pnl":         round(pnl, 2),
        "r_multiple":  r_multiple,
        "exit_reason": exit_reason,
        "entry_time":  position.get("entry_time", ""),
        "exit_time":   now.isoformat(),
    }
    journal = _load_journal()
    journal.append(record)
    _save_journal(journal)
    logger.info(f"[{symbol}] EXIT {exit_reason}: pnl={pnl:.2f} R={r_multiple}")
    _notify(f"SANDBOX {symbol} EXIT", f"{exit_reason} P&L={pnl:+.2f} R={r_multiple}")
    return pnl


def _notify(title: str, msg: str) -> None:
    try:
        import subprocess
        subprocess.run(
            ["osascript", "-e", f'display notification "{msg}" with title "{title}"'],
            check=False, capture_output=True,
        )
    except Exception:
        pass


def manage(exit_signals: list, state: dict, dry_run: bool = False) -> dict:
    """
    Main entry point for position management. Called each poller cycle.

    exit_signals: list of SandboxSignal with direction="exit"
    Returns updated state dict.
    """
    client   = _get_client()
    now_et   = datetime.now(ET)
    is_eod   = (now_et.hour == EOD_HOUR and now_et.minute >= EOD_MINUTE) or now_et.hour > EOD_HOUR
    positions = dict(state.get("positions", {}))

    if not positions:
        return state

    # Sync with Alpaca — remove positions already closed by bracket orders
    alpaca_positions = _get_open_alpaca_positions(client)
    for symbol in list(positions.keys()):
        if symbol not in alpaca_positions:
            # Position was closed by bracket (stop hit or target hit)
            pos = positions.pop(symbol)
            # We don't know exact exit price — log approximate
            approx_exit = pos.get("entry", 0.0)  # conservative; bracket already logged via Alpaca
            # Add to stopped_today if likely a stop (negative direction)
            state.setdefault("stopped_today", []).append(symbol)
            logger.info(f"[{symbol}] Bracket closed (stop/target hit) — removed from state")

    # Process exit signals from TV
    exit_symbols = {s.symbol: s for s in (exit_signals or []) if s.direction == "exit"}
    for symbol, signal in exit_symbols.items():
        if symbol not in positions:
            continue
        if dry_run:
            logger.info(f"[{symbol}] DRY-RUN exit signal: {signal.label_text}")
            continue
        # Cancel bracket legs first, then close
        _cancel_all_orders(client, symbol)
        success = _close_position(client, symbol)
        if success:
            pos = positions.pop(symbol)
            alpaca_price = alpaca_positions.get(symbol, {}).get("current_price", pos.get("entry"))
            pnl = _log_exit(symbol, pos, alpaca_price, f"TV_EXIT:{signal.label_text}", now_et)
            state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + pnl, 2)
            state.setdefault("stopped_today", []).append(symbol)

    # EOD force-flat
    if is_eod and positions:
        logger.info(f"EOD close: {list(positions.keys())}")
        for symbol in list(positions.keys()):
            if dry_run:
                logger.info(f"[{symbol}] DRY-RUN EOD close")
                continue
            _cancel_all_orders(client, symbol)
            success = _close_position(client, symbol)
            if success:
                pos = positions.pop(symbol)
                alpaca_price = alpaca_positions.get(symbol, {}).get("current_price", pos.get("entry"))
                pnl = _log_exit(symbol, pos, alpaca_price, "EOD_CLOSE", now_et)
                state["daily_pnl"] = round(state.get("daily_pnl", 0.0) + pnl, 2)

    state["positions"] = positions
    _save_state(state)
    return state
