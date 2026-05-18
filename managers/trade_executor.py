#!/usr/bin/env python3
"""
TRADE EXECUTOR — Execute Tri-City Inator trades via Alpaca.

Handles market orders, stop-loss placement, and three-target (50-25-25)
bracket structure for T1, T2, and T3 take-profit levels.

Env vars: ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

try:
    from dotenv import load_dotenv
    _env = Path(__file__).parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"


def _get_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        logger.warning("Alpaca credentials not set — running in mock mode")
        return None
    try:
        from alpaca.trading.client import TradingClient
        return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    except ImportError:
        logger.error("alpaca-py not installed. Run: pip install alpaca-py")
        return None


@dataclass
class ExecutionResult:
    success: bool
    order_id: Optional[str] = None
    error: Optional[str] = None
    shares_filled: int = 0
    avg_fill_price: float = 0.0


def execute_tri_city_trade(agent_name: str, signal_data: dict) -> ExecutionResult:
    """
    Execute a Tri-City trade with 50-25-25 bracket structure.

    Places three separate bracket orders:
      T1 bracket: 50% of shares — stop at stop_loss, take_profit at target_1 (+10%)
      T2 bracket: 25% of shares — stop at stop_loss, take_profit at target_2 (+20%)
      T3 bracket: 25% of shares — stop at stop_loss, take_profit at target_3 (+30%)

    Position manager trails T3 after T2 is hit.

    signal_data keys:
        ticker         str    symbol e.g. "NVDA"
        entry_price    float  expected fill (for display)
        position_size  int    total number of shares
        stop_loss      float  stop price for all lots
        target_1       float  +10% take-profit (T1)
        target_2       float  +20% take-profit (T2)
        target_3       float  +30% take-profit (T3)
        direction      str    "BULLISH" (default)
    """
    ticker        = signal_data.get("ticker", "UNKNOWN")
    entry_price   = signal_data.get("entry_price", 0)
    position_size = signal_data.get("position_size", 0)
    stop_loss     = signal_data.get("stop_loss", 0)
    target_1      = signal_data.get("target_1", 0)
    target_2      = signal_data.get("target_2", 0)
    target_3      = signal_data.get("target_3", 0)
    direction     = signal_data.get("direction", "BULLISH").upper()

    logger.info(
        f"[{agent_name.upper()}] {direction} {ticker} x{position_size} "
        f"@ ~${entry_price:.2f} | stop ${stop_loss:.2f} | "
        f"T1 ${target_1:.2f} | T2 ${target_2:.2f} | T3 ${target_3:.2f}"
    )

    if not ticker or position_size <= 0 or entry_price <= 0:
        return ExecutionResult(
            success=False,
            error=f"Invalid params: ticker={ticker}, size={position_size}, entry={entry_price}"
        )

    # ── Share split: 50 / 25 / 25 ─────────────────────────────────────────────
    t1_shares = max(1, position_size // 2)
    t2_shares = max(1, (position_size - t1_shares) // 2)
    t3_shares = max(1, position_size - t1_shares - t2_shares)

    client = _get_client()
    if client is None:
        # Mock mode
        mock_id = f"MOCK-{agent_name.upper()}-{ticker}-{position_size}"
        logger.info(f"✅ MOCK order: {mock_id}")
        return ExecutionResult(
            success=True,
            order_id=mock_id,
            shares_filled=position_size,
            avg_fill_price=entry_price
        )

    try:
        from alpaca.trading.requests import (
            MarketOrderRequest, StopLossRequest, TakeProfitRequest
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass

        side = OrderSide.BUY if direction == "BULLISH" else OrderSide.SELL

        def place_bracket(qty: int, take_profit: float, label: str) -> str | None:
            kwargs = dict(
                symbol=ticker,
                qty=qty,
                side=side,
                time_in_force=TimeInForce.DAY,
                order_class=OrderClass.BRACKET,
            )
            if stop_loss > 0:
                kwargs["stop_loss"] = StopLossRequest(stop_price=round(stop_loss, 2))
            if take_profit > 0:
                kwargs["take_profit"] = TakeProfitRequest(limit_price=round(take_profit, 2))
            order = client.submit_order(MarketOrderRequest(**kwargs))
            oid = str(order.id)
            logger.info(f"  ✅ {label} bracket: {oid} ({qty} shares @ T {take_profit:.2f})")
            return oid

        # T1 bracket (50%)
        order_id = place_bracket(t1_shares, target_1, "T1")

        # T2 bracket (25%)
        place_bracket(t2_shares, target_2, "T2")

        # T3 bracket (25%) — position manager will trail this
        place_bracket(t3_shares, target_3, "T3")

        return ExecutionResult(
            success=True,
            order_id=order_id,
            shares_filled=position_size,
            avg_fill_price=entry_price
        )

    except Exception as e:
        logger.error(f"execute_tri_city_trade error: {e}", exc_info=True)
        return ExecutionResult(success=False, error=str(e))


def set_stop_loss(order_id: str, ticker: str, stop_price: float,
                  shares: int = 0, direction: str = "BULLISH") -> bool:
    """Place a GTC stop-loss order. Used by position manager for breakeven moves."""
    logger.info(f"Setting stop: {ticker} @ ${stop_price:.2f} ({shares} shares)")
    client = _get_client()
    if client is None:
        logger.info(f"MOCK: stop {ticker} @ ${stop_price:.2f}")
        return True
    try:
        from alpaca.trading.requests import StopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        side = OrderSide.SELL if direction.upper() == "BULLISH" else OrderSide.BUY
        req = StopOrderRequest(
            symbol=ticker,
            qty=shares,
            side=side,
            stop_price=round(stop_price, 2),
            time_in_force=TimeInForce.GTC
        )
        client.submit_order(req)
        logger.info(f"✅ Stop placed: {ticker} @ ${stop_price:.2f}")
        return True
    except Exception as e:
        logger.error(f"set_stop_loss error: {e}")
        return False


def set_profit_target(order_id: str, ticker: str, target_price: float,
                      quantity: int, direction: str = "BULLISH") -> bool:
    """Place a GTC limit order for a profit target."""
    logger.info(f"Setting target: {ticker} x{quantity} @ ${target_price:.2f}")
    client = _get_client()
    if client is None:
        logger.info(f"MOCK: target {ticker} @ ${target_price:.2f}")
        return True
    try:
        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        side = OrderSide.SELL if direction.upper() == "BULLISH" else OrderSide.BUY
        req = LimitOrderRequest(
            symbol=ticker,
            qty=quantity,
            side=side,
            limit_price=round(target_price, 2),
            time_in_force=TimeInForce.GTC
        )
        client.submit_order(req)
        logger.info(f"✅ Target placed: {ticker} x{quantity} @ ${target_price:.2f}")
        return True
    except Exception as e:
        logger.error(f"set_profit_target error: {e}")
        return False


def close_position(ticker: str, reason: str = "Manual close",
                   setup: str | None = None) -> bool:
    """Close all shares of a position at market. Auto-logs exit to trade journal."""
    import time as _time
    from datetime import datetime
    from zoneinfo import ZoneInfo
    CT = ZoneInfo("America/Chicago")

    logger.info(f"Closing position: {ticker} — reason: {reason}")
    client = _get_client()
    if client is None:
        logger.info(f"MOCK: closed {ticker}")
        return True
    try:
        positions = client.get_all_positions()
        pos = next((p for p in positions if p.symbol == ticker), None)
        shares     = int(pos.qty)             if pos else 0
        curr_price = float(pos.current_price) if pos else 0.0

        client.close_position(ticker)
        logger.info(f"✅ Closed: {ticker}")
        _time.sleep(1.5)

        try:
            from managers.trade_journal import fetch_exit_price, log_exit, get_open_entries
            today      = datetime.now(CT).strftime("%Y-%m-%d")
            exit_price = fetch_exit_price(ticker) or curr_price
            open_entries = get_open_entries(today)
            matched = [e for e in open_entries if e["symbol"] == ticker]
            if not matched and setup:
                matched = [{"setup": setup}]
            for entry in matched:
                log_exit(
                    symbol=ticker,
                    setup=entry.get("setup", setup or "UNKNOWN"),
                    date=today,
                    exit_price=exit_price,
                    exit_reason=reason,
                    shares=shares,
                )
        except Exception as je:
            logger.warning(f"Journal log skipped for {ticker}: {je}")

        return True
    except Exception as e:
        logger.error(f"close_position error for {ticker}: {e}")
        return False


def get_open_positions() -> list:
    """Return list of currently open positions from Alpaca."""
    client = _get_client()
    if client is None:
        return []
    try:
        positions = client.get_all_positions()
        return [
            {
                "ticker":         p.symbol,
                "shares":         int(p.qty),
                "entry_price":    float(p.avg_entry_price),
                "current_price":  float(p.current_price),
                "unrealized_pnl": float(p.unrealized_pl),
                "side":           p.side.value,
            }
            for p in positions
        ]
    except Exception as e:
        logger.error(f"get_open_positions error: {e}")
        return []


def sell_shares_at_market(ticker: str, shares: int) -> bool:
    """Sell a specific number of shares at market price (DAY order)."""
    logger.info(f"Market sell: {ticker} x{shares}")
    client = _get_client()
    if client is None:
        logger.info(f"MOCK: sell {ticker} x{shares}")
        return True
    try:
        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        req = MarketOrderRequest(
            symbol=ticker,
            qty=shares,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        client.submit_order(req)
        logger.info(f"✅ Market sell submitted: {ticker} x{shares}")
        return True
    except Exception as e:
        logger.error(f"sell_shares_at_market error: {e}")
        return False


def place_trailing_stop(ticker: str, shares: int, trail_pct: float,
                        direction: str = "BULLISH") -> bool:
    """Place a trailing stop sell order. trail_pct is the % drawdown from peak."""
    logger.info(f"Trailing stop: {ticker} x{shares} @ {trail_pct}% trail")
    client = _get_client()
    if client is None:
        logger.info(f"MOCK: trailing stop {ticker} x{shares} @ {trail_pct}%")
        return True
    try:
        from alpaca.trading.requests import TrailingStopOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce
        side = OrderSide.SELL if direction.upper() == "BULLISH" else OrderSide.BUY
        req = TrailingStopOrderRequest(
            symbol=ticker,
            qty=shares,
            side=side,
            trail_percent=round(trail_pct, 2),
            time_in_force=TimeInForce.DAY,
        )
        client.submit_order(req)
        logger.info(f"✅ Trailing stop placed: {ticker} x{shares} @ {trail_pct}% trail")
        return True
    except Exception as e:
        logger.error(f"place_trailing_stop error: {e}")
        return False


def cancel_all_orders(ticker: str) -> bool:
    """Cancel all open orders for a symbol."""
    client = _get_client()
    if client is None:
        return True
    try:
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        orders = client.get_orders(GetOrdersRequest(
            symbol=ticker,
            status=QueryOrderStatus.OPEN
        ))
        for order in orders:
            client.cancel_order_by_id(order.id)
        logger.info(f"Cancelled {len(orders)} orders for {ticker}")
        return True
    except Exception as e:
        logger.error(f"cancel_all_orders error: {e}")
        return False


if __name__ == "__main__":
    print("=" * 60)
    print("TRADE EXECUTOR — TEST")
    print(f"Mode: {'PAPER' if ALPACA_PAPER else 'LIVE'}")
    print(f"Keys set: {bool(ALPACA_API_KEY and ALPACA_SECRET_KEY)}")
    print("=" * 60)

    test_signal = {
        "ticker":        "NVDA",
        "entry_price":   142.50,
        "position_size": 10,
        "stop_loss":     135.38,   # -5%
        "target_1":      156.75,   # +10%
        "target_2":      171.00,   # +20%
        "target_3":      185.25,   # +30%
        "direction":     "BULLISH",
    }

    print("\n[Tri-City] NVDA ENTER test (dry run — mock mode):")
    result = execute_tri_city_trade("test", test_signal)
    print(f"  Success:    {result.success}")
    print(f"  Order ID:   {result.order_id}")
    print(f"  Shares:     {result.shares_filled}")
    if result.error:
        print(f"  Error:      {result.error}")
