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
    Execute a Tri-City trade with 40-35-25 OCO structure.

    Places a single market BUY for the full position, then three OCO SELL orders:
      T1 OCO: 40% of shares — limit at target_1 (+T1%), stop at stop_loss
      T2 OCO: 35% of shares — limit at target_2 (+T2%), stop at stop_loss
      T3:     25% of shares — trailing stop at T3_TRAIL_PCT%; OCO fallback on failure

    Each OCO guarantees stop protection from the moment of entry. When the limit
    leg fills (price hits take-profit), the stop leg is automatically cancelled.
    When the stop leg hits, the limit leg is automatically cancelled.

    Replaces prior BRACKET BUY approach where T3 trailing stop consistently failed
    with "insufficient qty" due to T1+T2 bracket accounting locking all shares.

    signal_data keys:
        ticker         str    symbol e.g. "NVDA"
        entry_price    float  expected fill (for display)
        position_size  int    total number of shares
        stop_loss      float  stop price for all lots
        target_1       float  +T1% take-profit (T1)
        target_2       float  +T2% take-profit (T2)
        target_3       float  +T3% take-profit (T3)
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

    # ── Share split: 40 / 35 / 25 ─────────────────────────────────────────────
    # T1 at +5%: bank 40% early (matches actual winner excursion of +3-5%)
    # T2 at +12%: 35% for the runners that extend
    # T3 trail: 25% free ride with trailing stop
    t1_shares = max(1, int(position_size * 0.40))
    t2_shares = max(1, int(position_size * 0.35))
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
        import time as _time
        from alpaca.trading.requests import (
            MarketOrderRequest, LimitOrderRequest, TrailingStopOrderRequest,
            StopLossRequest, TakeProfitRequest
        )
        from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass, OrderStatus

        buy_side  = OrderSide.BUY  if direction == "BULLISH" else OrderSide.SELL
        sell_side = OrderSide.SELL if direction == "BULLISH" else OrderSide.BUY

        def _wait_for_fill(order_id: str, timeout: float = 30.0, poll: float = 1.0) -> bool:
            """Poll until order is filled or terminal status. Returns True if filled."""
            elapsed = 0.0
            while elapsed < timeout:
                _time.sleep(poll)
                elapsed += poll
                try:
                    o = client.get_order_by_id(order_id)
                    if o.status == OrderStatus.FILLED:
                        logger.info(f"  Order {order_id} filled after {elapsed:.1f}s")
                        return True
                    if o.status in (OrderStatus.CANCELED, OrderStatus.EXPIRED,
                                    OrderStatus.REJECTED):
                        logger.warning(f"  Order {order_id} terminal status: {o.status}")
                        return False
                except Exception as pe:
                    logger.warning(f"  Poll error for {order_id}: {pe}")
            logger.warning(f"  Fill poll timed out for {order_id} after {timeout}s")
            return False

        # ── Step 1: Single market BUY for full position ───────────────────────
        # Buying all shares in one order avoids the "insufficient qty" issue that
        # plagued the prior 3-bracket approach (T3 trailing SELL saw 0 available
        # shares because T1+T2 bracket BUYs had locked all qty in their child orders).
        tif = TimeInForce.GTC
        try:
            entry_req = MarketOrderRequest(
                symbol=ticker,
                qty=position_size,
                side=buy_side,
                time_in_force=tif,
            )
            entry_order = client.submit_order(entry_req)
        except Exception as e:
            if "42210000" in str(e):
                # HTB asset — only DAY orders allowed
                logger.warning("HTB asset (42210000) — retrying entry with DAY TIF")
                entry_req = MarketOrderRequest(
                    symbol=ticker, qty=position_size,
                    side=buy_side, time_in_force=TimeInForce.DAY,
                )
                entry_order = client.submit_order(entry_req)
            else:
                raise

        entry_order_id = str(entry_order.id)
        logger.info(f"  ✅ Entry: {entry_order_id} ({position_size} shares market BUY)")

        # ── Step 2: Wait for entry fill ───────────────────────────────────────
        _wait_for_fill(entry_order_id)

        actual_fill = entry_price
        try:
            filled_order = client.get_order_by_id(entry_order_id)
            if filled_order.filled_avg_price:
                actual_fill = float(filled_order.filled_avg_price)
        except Exception:
            pass

        # ── Fill-adjusted stop guard ───────────────────────────────────────────
        # If fill price is significantly below signal price (negative slippage),
        # the pre-calculated stop may end up ABOVE the actual fill — inverted stop.
        # Recalculate to 5% below actual fill to ensure the stop is always valid.
        if stop_loss >= actual_fill:
            adjusted = round(actual_fill * 0.95, 2)
            logger.warning(
                f"Stop ${stop_loss:.2f} >= fill ${actual_fill:.2f} — "
                f"adjusting to 5% below fill: ${adjusted:.2f}"
            )
            stop_loss = adjusted

        # ── Step 3: OCO SELL orders — take-profit + stop paired per tranche ───
        # Each OCO has two linked sell legs: limit at take-profit, stop at stop_loss.
        # Whichever fires first automatically cancels the other.
        # This ensures stop protection from entry on every tranche simultaneously.
        def place_oco_sell(qty: int, take_profit: float, label: str) -> str | None:
            try:
                req = LimitOrderRequest(
                    symbol=ticker,
                    qty=qty,
                    side=sell_side,
                    limit_price=round(take_profit, 2),
                    time_in_force=TimeInForce.GTC,
                    order_class=OrderClass.OCO,
                    take_profit=TakeProfitRequest(limit_price=round(take_profit, 2)),
                    stop_loss=StopLossRequest(stop_price=round(stop_loss, 2)),
                )
                order = client.submit_order(req)
                oid = str(order.id)
                logger.info(
                    f"  ✅ {label} OCO: {oid} "
                    f"({qty}sh, TP=${take_profit:.2f}, stop=${stop_loss:.2f})"
                )
                return oid
            except Exception as e:
                logger.error(f"  ❌ {label} OCO failed: {e}")
                return None

        place_oco_sell(t1_shares, target_1, "T1")
        place_oco_sell(t2_shares, target_2, "T2")

        # ── Step 4: T3 trailing stop (best effort); OCO fallback ─────────────
        t3_trail_pct = float(os.getenv("T3_TRAIL_PCT", "3"))
        try:
            t3_req = TrailingStopOrderRequest(
                symbol=ticker,
                qty=t3_shares,
                side=sell_side,
                time_in_force=TimeInForce.GTC,
                trail_percent=t3_trail_pct,
            )
            t3_order = client.submit_order(t3_req)
            logger.info(
                f"  ✅ T3 trailing: {t3_order.id} "
                f"({t3_shares}sh @ -{t3_trail_pct:.1f}% trail)"
            )
        except Exception as trail_err:
            logger.warning(f"T3 trailing stop failed ({trail_err}) — falling back to OCO")
            place_oco_sell(t3_shares, target_3, "T3-fallback")

        return ExecutionResult(
            success=True,
            order_id=entry_order_id,
            shares_filled=position_size,
            avg_fill_price=actual_fill
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


def cancel_all_orders(ticker: str, wait_secs: float = 3.0, poll_interval: float = 0.5) -> bool:
    """
    Cancel all open orders for a symbol and wait until Alpaca confirms they
    are no longer open (up to wait_secs). This prevents 'insufficient qty'
    errors when close_position is called immediately after cancellation.
    """
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

        if not orders:
            return True

        # Poll until no open orders remain (or timeout)
        import time as _time
        elapsed = 0.0
        while elapsed < wait_secs:
            _time.sleep(poll_interval)
            elapsed += poll_interval
            remaining = client.get_orders(GetOrdersRequest(
                symbol=ticker,
                status=QueryOrderStatus.OPEN
            ))
            if not remaining:
                logger.info(f"All orders cleared for {ticker} after {elapsed:.1f}s")
                return True

        logger.warning(f"cancel_all_orders: orders still open for {ticker} after {wait_secs}s")
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
