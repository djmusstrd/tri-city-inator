#!/usr/bin/env python3
"""
SANDBOX EXECUTE — Guards + bracket order placement for sandbox signals.

Guards (in order):
  1. Market hours: 9:35 AM – 2:30 PM ET
  2. Max 2 concurrent sandbox positions
  3. Symbol not already executed today
  4. Symbol not in stopped_today list
  5. Daily loss lock: -$150 total sandbox P&L

Position sizing:
  risk_dollars = SANDBOX_CAPITAL * RISK_PCT  (default 0.5%)
  atr_stop     = ATR(14, 5-min) * ATR_STOP_MULT  (default 2.0)
  qty          = max(1, floor(risk_dollars / atr_stop))
  qty          = min(qty, floor(SANDBOX_CAPITAL / price))  (cash cap)

Exit targets:
  Super Scalper → stop: entry - ATR*2, target: entry + ATR*5
  Supertrend    → stop: entry - ATR*2, target: entry + ATR*4
"""

from __future__ import annotations

import json
import logging
import math
import os
import sys
from datetime import datetime, timezone, timedelta
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

ET = ZoneInfo("America/New_York")
CT = ZoneInfo("America/Chicago")

SANDBOX_CAPITAL  = float(os.getenv("SANDBOX_CAPITAL", "5000"))
RISK_PCT         = float(os.getenv("SANDBOX_RISK_PCT", "0.005"))     # 0.5%
ATR_STOP_MULT    = float(os.getenv("SANDBOX_ATR_STOP", "2.0"))
ATR_TARGET_SS    = float(os.getenv("SANDBOX_ATR_TARGET_SS", "5.0"))  # Super Scalper
ATR_TARGET_ST    = float(os.getenv("SANDBOX_ATR_TARGET_ST", "4.0"))  # Supertrend
MAX_POSITIONS    = int(os.getenv("SANDBOX_MAX_POSITIONS", "2"))
DAILY_LOSS_LOCK  = float(os.getenv("SANDBOX_DAILY_LOSS", "-150"))
ATR_PERIOD       = 14

EXEC_LOG   = WORKSPACE / "logs" / "sandbox-executions.json"
STATE_FILE = WORKSPACE / "shared" / "sandbox-state.json"

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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


def _load_exec_log() -> list:
    if EXEC_LOG.exists():
        try:
            return json.loads(EXEC_LOG.read_text())
        except Exception:
            pass
    return []


def _save_exec_log(entries: list) -> None:
    EXEC_LOG.parent.mkdir(parents=True, exist_ok=True)
    EXEC_LOG.write_text(json.dumps(entries, indent=2))


def _get_alpaca_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.trading.client import TradingClient
        return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    except ImportError:
        logger.error("alpaca-py not installed")
        return None


def _compute_atr(symbol: str) -> float | None:
    """Compute 14-bar ATR from Alpaca 5-min bars."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        import pandas as pd

        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            limit=ATR_PERIOD * 3,
            feed="iex",
            adjustment="raw",
        )
        bars = client.get_stock_bars(req).df
        if bars.empty or len(bars) < ATR_PERIOD:
            return None

        bars = bars.reset_index() if "symbol" in bars.index.names else bars
        if isinstance(bars.index, pd.MultiIndex):
            bars = bars.reset_index()

        high  = bars["high"].values
        low   = bars["low"].values
        close = bars["close"].values

        tr = []
        for i in range(1, len(high)):
            tr.append(max(
                high[i] - low[i],
                abs(high[i] - close[i - 1]),
                abs(low[i]  - close[i - 1]),
            ))
        if len(tr) < ATR_PERIOD:
            return None
        return sum(tr[-ATR_PERIOD:]) / ATR_PERIOD

    except Exception as e:
        logger.warning(f"ATR compute failed for {symbol}: {e}")
        return None


def _get_quote(client, symbol: str) -> float | None:
    """Get latest ask price for entry."""
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockLatestQuoteRequest

        data_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        req = StockLatestQuoteRequest(symbol_or_symbols=symbol, feed="iex")
        quote = data_client.get_stock_latest_quote(req)
        if symbol in quote:
            q = quote[symbol]
            ask = getattr(q, "ask_price", None) or getattr(q, "ap", None)
            if ask and ask > 0:
                return float(ask)
    except Exception as e:
        logger.warning(f"Quote fetch failed for {symbol}: {e}")
    return None


def _place_bracket(client, symbol: str, qty: int, stop: float, target: float, dry_run: bool) -> dict:
    """Place a market-entry bracket order (entry + stop + limit)."""
    stop   = round(stop, 2)
    target = round(target, 2)

    if dry_run:
        return {"success": True, "order_id": "DRY-RUN", "stop": stop, "target": target, "qty": qty}

    try:
        from alpaca.trading.requests import MarketOrderRequest, TakeProfitRequest, StopLossRequest
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        order = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=target),
            stop_loss=StopLossRequest(stop_price=stop),
        )
        resp = client.submit_order(order)
        return {"success": True, "order_id": str(resp.id), "stop": stop, "target": target, "qty": qty}
    except Exception as e:
        logger.error(f"Bracket order failed for {symbol}: {e}")
        return {"success": False, "error": str(e)}


def execute_signal(signal, state: dict, dry_run: bool = False) -> bool:
    """
    Run guards → compute sizing → place bracket order → update state.
    Returns True if order was placed.
    signal must have .symbol, .strategy, .direction, .price, .bar_idx, .label_text
    """
    symbol    = signal.symbol
    strategy  = signal.strategy
    now_et    = datetime.now(ET)
    today_str = now_et.strftime("%Y-%m-%d")

    # Reset state if new day
    if state.get("date") != today_str:
        state.update({"date": today_str, "daily_pnl": 0.0,
                      "executed_today": [], "stopped_today": []})
        logger.info("New day — sandbox state reset")

    # Guard 1: market hours (9:35 AM – 2:30 PM ET)
    market_open  = now_et.replace(hour=9,  minute=35, second=0, microsecond=0)
    market_close = now_et.replace(hour=14, minute=30, second=0, microsecond=0)
    if not (market_open <= now_et <= market_close):
        logger.info(f"[{symbol}] BLOCKED: outside market hours ({now_et.strftime('%H:%M ET')})")
        return False

    # Guard 2: max positions
    open_positions = state.get("positions", {})
    if len(open_positions) >= MAX_POSITIONS:
        logger.info(f"[{symbol}] BLOCKED: max positions ({MAX_POSITIONS}) reached")
        return False

    # Guard 3: already executed today
    if symbol in state.get("executed_today", []):
        logger.info(f"[{symbol}] BLOCKED: already executed today")
        return False

    # Guard 4: stopped out today (no same-day re-entry on a loser)
    if symbol in state.get("stopped_today", []):
        logger.info(f"[{symbol}] BLOCKED: stopped out today, no re-entry")
        return False

    # Guard 5: daily loss lock
    if state.get("daily_pnl", 0.0) <= DAILY_LOSS_LOCK:
        logger.info(f"[{symbol}] BLOCKED: daily loss lock (pnl={state['daily_pnl']:.2f})")
        return False

    # Guard 6: already in position
    if symbol in open_positions:
        logger.info(f"[{symbol}] BLOCKED: already in position")
        return False

    # Compute ATR
    atr = _compute_atr(symbol)
    if not atr:
        logger.warning(f"[{symbol}] BLOCKED: ATR compute failed")
        return False

    # Get live quote
    client = _get_alpaca_client()
    entry_price = _get_quote(client, symbol) if client else signal.price
    if not entry_price or entry_price <= 0:
        entry_price = signal.price
    if not entry_price or entry_price <= 0:
        logger.warning(f"[{symbol}] BLOCKED: no valid entry price")
        return False

    # Sizing
    atr_stop_dist = atr * ATR_STOP_MULT
    risk_dollars  = SANDBOX_CAPITAL * RISK_PCT
    raw_qty       = math.floor(risk_dollars / atr_stop_dist) if atr_stop_dist > 0 else 1
    cash_cap_qty  = math.floor(SANDBOX_CAPITAL / entry_price)
    qty           = max(1, min(raw_qty, cash_cap_qty))

    stop_price   = entry_price - atr_stop_dist
    atr_mult_tgt = ATR_TARGET_SS if strategy == "super_scalper" else ATR_TARGET_ST
    target_price = entry_price + atr * atr_mult_tgt

    logger.info(
        f"[{symbol}] SIGNAL: {strategy} | price={entry_price:.2f} "
        f"atr={atr:.2f} stop={stop_price:.2f} target={target_price:.2f} qty={qty} "
        f"{'(DRY-RUN)' if dry_run else ''}"
    )

    result = _place_bracket(client, symbol, qty, stop_price, target_price, dry_run)

    if not result.get("success"):
        logger.error(f"[{symbol}] Order failed: {result.get('error')}")
        return False

    # Log execution
    entry = {
        "timestamp": now_et.isoformat(),
        "symbol":    symbol,
        "strategy":  strategy,
        "direction": signal.direction,
        "signal_id": getattr(signal, "signal_id", ""),
        "bar_tm":    getattr(signal, "bar_tm", 0),
        "entry":     entry_price,
        "stop":      result["stop"],
        "target":    result["target"],
        "qty":       qty,
        "atr":       round(atr, 4),
        "order_id":  result.get("order_id"),
        "dry_run":   dry_run,
    }
    entries = _load_exec_log()
    entries.append(entry)
    _save_exec_log(entries)

    # Update state
    state.setdefault("positions", {})[symbol] = {
        "entry": entry_price, "qty": qty, "stop": result["stop"],
        "target": result["target"], "strategy": strategy,
        "entry_time": now_et.isoformat(), "order_id": result.get("order_id"),
    }
    state.setdefault("executed_today", []).append(symbol)
    _save_state(state)

    logger.info(f"[{symbol}] ORDER PLACED: qty={qty} stop={result['stop']:.2f} target={result['target']:.2f}")
    _notify(f"SANDBOX {symbol} LONG", f"qty={qty} entry={entry_price:.2f} stop={result['stop']:.2f}")
    return True


def _notify(title: str, msg: str) -> None:
    try:
        import subprocess
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}"'],
            check=False, capture_output=True,
        )
    except Exception:
        pass
