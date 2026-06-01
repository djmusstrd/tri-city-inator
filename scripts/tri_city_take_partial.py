#!/usr/bin/env python3
"""
TRI-CITY TAKE PARTIAL — Lock in a dollar amount of profit on an open position.

Usage:
    python tri_city_take_partial.py --symbol MDB --lock 300
    python tri_city_take_partial.py --symbol MDB --lock 300 --dry-run

Steps:
  1. Fetch current position (shares, avg entry, current price)
  2. Compute shares to sell = floor(lock_amount / (current_price - avg_entry))
  3. Cancel all open GTC stop/OCO orders for the symbol
  4. Market sell the computed shares
  5. Place a new GTC stop on the remaining shares at the original stop price

The original stop price is read from the execution log (tri-city-executions.json).
If not found, the script asks for it via --stop argument.

Why this exists:
    DELL/MDB/BRUN lesson (2026-06-01) — locking $300 of profit required 5+ manual tool
    calls (check P&L, calculate shares, cancel stop, market sell, re-place stop) taking
    several minutes during live trading. This script collapses that into one command.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
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

CT            = ZoneInfo("America/Chicago")
EXEC_LOG      = WORKSPACE / "logs" / "tri-city-executions.json"
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


def _get_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        logger.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
        sys.exit(1)
    from alpaca.trading.client import TradingClient
    return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)


def get_position(client, symbol: str) -> dict:
    try:
        pos = client.get_open_position(symbol)
        return {
            "shares":      int(float(pos.qty)),
            "avg_entry":   float(pos.avg_entry_price),
            "curr_price":  float(pos.current_price),
            "unrealized":  float(pos.unrealized_pl),
        }
    except Exception as e:
        logger.error(f"No open position for {symbol}: {e}")
        sys.exit(1)


def get_original_stop(symbol: str) -> float | None:
    """Read today's stop_loss from the execution log for this symbol."""
    if not EXEC_LOG.exists():
        return None
    try:
        entries = json.loads(EXEC_LOG.read_text())
        today = datetime.now(CT).strftime("%Y-%m-%d")
        for e in reversed(entries):
            if e.get("symbol") == symbol and e.get("date") == today and e.get("success"):
                return float(e.get("stop_loss") or 0) or None
    except Exception:
        pass
    return None


def cancel_open_orders(client, symbol: str, dry_run: bool):
    from alpaca.trading.requests import GetOrdersRequest
    from alpaca.trading.enums import QueryOrderStatus
    orders = client.get_orders(GetOrdersRequest(
        symbol=symbol,
        status=QueryOrderStatus.OPEN,
    ))
    if not orders:
        logger.info(f"No open orders for {symbol} to cancel.")
        return
    if dry_run:
        logger.info(f"[DRY RUN] Would cancel {len(orders)} open orders for {symbol}.")
        return
    for o in orders:
        client.cancel_order_by_id(o.id)
    logger.info(f"Cancelled {len(orders)} open orders for {symbol}. Waiting for settlement...")
    time.sleep(2)


def market_sell(client, symbol: str, shares: int, dry_run: bool):
    from alpaca.trading.requests import MarketOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    if dry_run:
        logger.info(f"[DRY RUN] Would market sell {shares} shares of {symbol}.")
        return
    req = MarketOrderRequest(
        symbol=symbol,
        qty=shares,
        side=OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    order = client.submit_order(req)
    logger.info(f"Market sell submitted: {symbol} x{shares} — order {order.id}")
    time.sleep(1)


def place_stop(client, symbol: str, remaining_shares: int, stop_price: float, dry_run: bool):
    from alpaca.trading.requests import StopOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    if dry_run:
        logger.info(
            f"[DRY RUN] Would place GTC stop on {remaining_shares} shares of {symbol} "
            f"at ${stop_price:.2f}."
        )
        return
    req = StopOrderRequest(
        symbol=symbol,
        qty=remaining_shares,
        side=OrderSide.SELL,
        stop_price=round(stop_price, 2),
        time_in_force=TimeInForce.GTC,
    )
    order = client.submit_order(req)
    logger.info(f"GTC stop placed: {symbol} x{remaining_shares} @ ${stop_price:.2f} — order {order.id}")


def main():
    parser = argparse.ArgumentParser(description="Lock in a dollar amount of profit")
    parser.add_argument("--symbol",  required=True, type=str.upper)
    parser.add_argument("--lock",    required=True, type=float,
                        help="Dollar amount to lock in (e.g. 300)")
    parser.add_argument("--stop",    default=None,  type=float,
                        help="Stop price for remaining shares (auto-read from execution log if omitted)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would happen without placing orders")
    args = parser.parse_args()

    client = _get_client()
    pos    = get_position(client, args.symbol)

    shares      = pos["shares"]
    avg_entry   = pos["avg_entry"]
    curr_price  = pos["curr_price"]
    unrealized  = pos["unrealized"]
    pnl_per_sh  = curr_price - avg_entry

    print(f"\n{args.symbol} position:")
    print(f"  Shares:      {shares}")
    print(f"  Avg entry:   ${avg_entry:.2f}")
    print(f"  Current:     ${curr_price:.2f}")
    print(f"  Unrealized:  ${unrealized:+.2f}  (${pnl_per_sh:+.2f}/share)")

    if pnl_per_sh <= 0:
        logger.error(f"Position is not profitable (P&L/share = ${pnl_per_sh:.2f}). Nothing to lock.")
        sys.exit(1)

    shares_to_sell = max(1, int(args.lock / pnl_per_sh))
    if shares_to_sell >= shares:
        logger.warning(
            f"Calculated shares to sell ({shares_to_sell}) >= position size ({shares}). "
            f"Capping at {shares - 1} to keep at least 1 share."
        )
        shares_to_sell = max(1, shares - 1)

    expected_proceeds = round(shares_to_sell * pnl_per_sh, 2)
    remaining_shares  = shares - shares_to_sell

    # Stop price: from execution log or --stop arg
    stop_price = args.stop or get_original_stop(args.symbol)
    if not stop_price:
        logger.error(
            "Could not find original stop price in execution log. "
            "Pass --stop <price> to specify it manually."
        )
        sys.exit(1)

    print(f"\nPlan:")
    print(f"  Sell:        {shares_to_sell} shares at ~${curr_price:.2f}")
    print(f"  Expected P&L lock: ~${expected_proceeds:.2f}")
    print(f"  Remaining:   {remaining_shares} shares")
    print(f"  New stop:    ${stop_price:.2f} (GTC)")
    if args.dry_run:
        print("\n[DRY RUN] — no orders placed.\n")

    cancel_open_orders(client, args.symbol, args.dry_run)
    market_sell(client, args.symbol, shares_to_sell, args.dry_run)
    place_stop(client, args.symbol, remaining_shares, stop_price, args.dry_run)

    if not args.dry_run:
        print(f"\nDone. Locked ~${expected_proceeds:.2f} on {args.symbol}. "
              f"Stop on {remaining_shares} shares at ${stop_price:.2f}.\n")


if __name__ == "__main__":
    main()
