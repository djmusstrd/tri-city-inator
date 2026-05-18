#!/usr/bin/env python3
"""
TRI-CITY POSITION MANAGER — Post-entry management for open Tri-City positions.

Called every 3 minutes as part of the tri_city_monitor.py loop.

Actions (in order):
  1. T1 CHECK:    If price >= T1 → move stop to breakeven for remaining shares
  2. T2 CHECK:    If price >= T2 → move stop to T2 level (lock T2 gains on T3)
  3. TRAILING:    If in normal mode and price < EMA20 AND < VWAP → close T3 lot
  4. EOD CLOSE:   If time >= 3:45 PM CT → cancel all orders → close all positions

Usage:
    python -W ignore scripts/tri_city_position_manager.py
    python -W ignore scripts/tri_city_position_manager.py --eod
    python -W ignore scripts/tri_city_position_manager.py --status
"""

from __future__ import annotations

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

from managers.trade_executor import (
    get_open_positions, close_position, set_stop_loss, cancel_all_orders,
    sell_shares_at_market, place_trailing_stop
)

CT                  = ZoneInfo("America/Chicago")
EOD_HOUR            = int(os.getenv("EOD_HOUR",             "15"))
EOD_MINUTE          = int(os.getenv("EOD_MINUTE",           "45"))
FREE_RIDE_PCT       = float(os.getenv("FREE_RIDE_PCT",       "3.0"))
T3_TRAIL_PCT        = float(os.getenv("T3_TRAIL_PCT",        "5.0"))
PULLBACK_TIMEOUT    = int(os.getenv("PULLBACK_TIMEOUT_MIN",  "25"))   # Fix A
EXEC_LOG            = WORKSPACE / "logs" / "tri-city-executions.json"

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_executions() -> list:
    if not EXEC_LOG.exists():
        return []
    try:
        return json.loads(EXEC_LOG.read_text())
    except Exception:
        return []


def save_executions(entries: list):
    EXEC_LOG.write_text(json.dumps(entries, indent=2, default=str))


def get_intraday_ema_vwap(symbol: str) -> tuple[float | None, float | None]:
    """Return (EMA20, VWAP) for current symbol from Alpaca. Used for trailing check."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None, None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta

        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

        # VWAP from snapshot
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol))
        s = snap.get(symbol)
        vwap = float(s.daily_bar.vwap) if s and s.daily_bar else None

        # EMA20 from 1-min bars (last 30 bars)
        now = datetime.now(CT)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=now - timedelta(minutes=60),
            end=now,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df.empty or len(df) < 20:
            return None, vwap

        closes = list(df["close"])
        k = 2 / 21
        ema = sum(closes[:20]) / 20
        for price in closes[20:]:
            ema = price * k + ema * (1 - k)
        return round(ema, 4), vwap

    except Exception as e:
        logger.warning(f"get_intraday_ema_vwap {symbol}: {e}")
        return None, None


# ── Action -1: Failed pullback exit (Fix A + Fix B) ───────────────────────────

def check_failed_pullback(positions: list, today: str, now: datetime) -> list[str]:
    """
    Exit PULLBACK trades that have failed — runs first, before free-ride or targets.

    Fix B: price < entry AND price < ORH  → setup broke down immediately, cut it.
    Fix A: position open > PULLBACK_TIMEOUT min and still below ORH → no follow-through, exit.

    Both require setup == "PULLBACK" and no breakeven/T2 stop already set.
    ORH is read from the execution log (stored since today's session).
    """
    actions = []
    entries = load_executions()

    for pos in positions:
        ticker      = pos["ticker"]
        curr_price  = pos["current_price"]
        entry_price = pos["entry_price"]

        entry = None
        for e in reversed(entries):
            if e.get("symbol") == ticker and e.get("date") == today and e.get("success"):
                entry = e
                break

        if not entry:
            continue
        if entry.get("setup") != "PULLBACK":
            continue
        if entry.get("breakeven_set") or entry.get("t2_stop_set"):
            continue

        orh = entry.get("orh", 0)

        # ── Fix B: immediate breakdown — price fell below entry AND ORH ───────
        if orh > 0 and curr_price < entry_price and curr_price < orh:
            logger.info(
                f"PULLBACK FAIL B: {ticker} ${curr_price:.2f} < "
                f"entry ${entry_price:.2f} AND < ORH ${orh:.2f}"
            )
            cancel_all_orders(ticker)
            success = close_position(ticker, reason="Failed pullback — below entry & ORH")
            if success:
                actions.append(
                    f"PULLBACK FAIL: {ticker} @ ${curr_price:.2f} "
                    f"— broke below entry ${entry_price:.2f} and ORH ${orh:.2f}"
                )
            else:
                actions.append(f"PULLBACK FAIL CLOSE FAILED: {ticker} — check Alpaca")
            continue

        # ── Fix A: time-based — still below ORH after PULLBACK_TIMEOUT min ───
        entry_time_str = entry.get("time", "")
        if entry_time_str and orh > 0 and curr_price < orh:
            try:
                time_part = entry_time_str.replace(" CT", "")
                entry_dt  = datetime.strptime(f"{today} {time_part}", "%Y-%m-%d %H:%M:%S")
                entry_dt  = entry_dt.replace(tzinfo=CT)
                mins_open = (now - entry_dt).total_seconds() / 60
                if mins_open >= PULLBACK_TIMEOUT:
                    logger.info(
                        f"PULLBACK TIMEOUT A: {ticker} open {mins_open:.0f}min, "
                        f"still below ORH ${orh:.2f}"
                    )
                    cancel_all_orders(ticker)
                    success = close_position(
                        ticker,
                        reason=f"Pullback timeout — {PULLBACK_TIMEOUT}min no ORH reclaim"
                    )
                    if success:
                        actions.append(
                            f"PULLBACK TIMEOUT: {ticker} @ ${curr_price:.2f} "
                            f"— {mins_open:.0f}min open, never reclaimed ORH ${orh:.2f}"
                        )
                    else:
                        actions.append(f"PULLBACK TIMEOUT CLOSE FAILED: {ticker} — check Alpaca")
            except Exception as e:
                logger.warning(f"check_failed_pullback time parse {ticker}: {e}")

    return actions


# ── Action 0: Free-ride check ──────────────────────────────────────────────────

def check_free_ride(positions: list, today: str) -> list[str]:
    """
    FREE RIDE: When price >= entry * (1 + FREE_RIDE_PCT%), automatically:
      1. Cancel all open orders for the symbol
      2. Sell T1 lot (50%) at market
      3. Move stop to breakeven for remaining shares
      4. Place trailing stop (T3_TRAIL_PCT%) on T3 lot (25%)

    Runs before check_targets() every cycle. Disabled when FREE_RIDE_PCT <= 0.
    """
    if FREE_RIDE_PCT <= 0:
        return []

    actions  = []
    entries  = load_executions()
    modified = False

    for pos in positions:
        ticker      = pos["ticker"]
        curr_price  = pos["current_price"]
        entry_price = pos["entry_price"]
        shares      = pos["shares"]

        entry = None
        for e in reversed(entries):
            if e.get("symbol") == ticker and e.get("date") == today and e.get("success"):
                entry = e
                break

        if not entry:
            continue

        # Skip if already at breakeven or beyond
        if entry.get("breakeven_set") or entry.get("t2_stop_set"):
            continue

        trigger = entry_price * (1 + FREE_RIDE_PCT / 100)
        if curr_price < trigger:
            continue

        # Split shares: T1=50%, T2=25%, T3=25%
        t1_shares  = max(1, shares // 2)
        t3_shares  = max(1, (shares - t1_shares) // 2)
        remaining  = shares - t1_shares

        logger.info(
            f"FREE RIDE: {ticker} ${curr_price:.2f} >= trigger ${trigger:.2f} "
            f"(+{FREE_RIDE_PCT}%) — selling {t1_shares} T1 shares"
        )

        cancel_all_orders(ticker)
        time.sleep(1.5)

        sold      = sell_shares_at_market(ticker, t1_shares)
        time.sleep(1.0)
        be_set    = set_stop_loss(
            order_id=entry.get("order_id", ""),
            ticker=ticker,
            stop_price=entry_price,
            shares=remaining,
            direction="BULLISH",
        )
        trail_set = place_trailing_stop(ticker, t3_shares, T3_TRAIL_PCT)

        if sold and be_set:
            entry["breakeven_set"]       = True
            entry["breakeven_price"]     = entry_price
            entry["free_ride_triggered"] = True
            entry["free_ride_price"]     = curr_price
            modified = True
            trail_note = f", trailing stop {T3_TRAIL_PCT}% on {t3_shares} shares" if trail_set else ""
            actions.append(
                f"FREE RIDE: {ticker} @ ${curr_price:.2f} (+{FREE_RIDE_PCT}%) "
                f"— sold {t1_shares} shares, stop → entry ${entry_price:.2f}{trail_note}"
            )
        else:
            actions.append(f"FREE RIDE FAILED: {ticker} — sold={sold} be={be_set}, check Alpaca")

    if modified:
        save_executions(entries)

    return actions


# ── Action 1 & 2: Target checks ────────────────────────────────────────────────

def check_targets(positions: list, today: str) -> list[str]:
    """
    T1 hit → move stop to breakeven for all remaining shares.
    T2 hit → move stop to T2 price (lock in T2 gains on T3 lot).
    """
    actions  = []
    entries  = load_executions()
    modified = False

    for pos in positions:
        ticker        = pos["ticker"]
        curr_price    = pos["current_price"]
        entry_price   = pos["entry_price"]
        shares        = pos["shares"]

        entry = None
        for e in reversed(entries):
            if e.get("symbol") == ticker and e.get("date") == today and e.get("success"):
                entry = e
                break

        if not entry:
            continue

        t1 = entry.get("target_1", 0)
        t2 = entry.get("target_2", 0)
        t3 = entry.get("target_3", 0)

        # ── T2 check (check before T1 to catch cases where T2 hit fast) ───────
        if t2 > 0 and curr_price >= t2 and not entry.get("t2_stop_set"):
            cancel_all_orders(ticker)
            time.sleep(1.5)
            success = set_stop_loss(
                order_id=entry.get("order_id", ""),
                ticker=ticker,
                stop_price=t2,       # Lock in T2 level for T3 lot
                shares=max(1, shares // 4),
                direction="BULLISH",
            )
            if success:
                entry["t2_stop_set"]   = True
                entry["t2_stop_price"] = t2
                modified = True
                actions.append(
                    f"T2 HIT: {ticker} @ ${curr_price:.2f} >= T2 ${t2:.2f} "
                    f"— stop locked at ${t2:.2f} for T3 lot"
                )
            continue  # T2 supersedes T1 logic for this cycle

        # ── T1 check ──────────────────────────────────────────────────────────
        if t1 > 0 and curr_price >= t1 and not entry.get("breakeven_set"):
            cancel_all_orders(ticker)
            time.sleep(1.5)
            success = set_stop_loss(
                order_id=entry.get("order_id", ""),
                ticker=ticker,
                stop_price=entry_price,   # Move stop to breakeven
                shares=max(1, shares - shares // 2),
                direction="BULLISH",
            )
            if success:
                entry["breakeven_set"]   = True
                entry["breakeven_price"] = entry_price
                modified = True
                actions.append(
                    f"T1 HIT: {ticker} @ ${curr_price:.2f} >= T1 ${t1:.2f} "
                    f"— stop moved to breakeven ${entry_price:.2f}"
                )

    if modified:
        save_executions(entries)

    return actions


# ── Action 3: Trailing stop (T3 lot) ──────────────────────────────────────────

def check_trailing(positions: list, today: str) -> list[str]:
    """
    For T3 (trailing) lot: if price drops below EMA20 AND VWAP → close.
    Only applies when breakeven has been set (T1 hit) and T2 stop not yet active.
    """
    actions = []
    entries = load_executions()

    for pos in positions:
        ticker      = pos["ticker"]
        curr_price  = pos["current_price"]

        entry = None
        for e in reversed(entries):
            if e.get("symbol") == ticker and e.get("date") == today and e.get("success"):
                entry = e
                break

        if not entry:
            continue

        # Only trail after T1 has been hit (breakeven set)
        if not entry.get("breakeven_set"):
            continue
        # Don't double-manage after T2 stop is active
        if entry.get("t2_stop_set"):
            continue

        ema20, vwap = get_intraday_ema_vwap(ticker)
        if ema20 is None or vwap is None:
            continue

        if curr_price < ema20 and curr_price < vwap:
            logger.info(
                f"TRAIL EXIT: {ticker} ${curr_price:.2f} < "
                f"EMA20 ${ema20:.2f} AND VWAP ${vwap:.2f}"
            )
            cancel_all_orders(ticker)
            success = close_position(ticker, reason="Trailing stop (EMA+VWAP breach)")
            if success:
                actions.append(
                    f"TRAIL EXIT: {ticker} @ ${curr_price:.2f} "
                    f"(below EMA20 ${ema20:.2f} + VWAP ${vwap:.2f})"
                )
                try:
                    from managers.trade_journal import log_exit, fetch_exit_price
                    exit_price = fetch_exit_price(ticker) or curr_price
                    log_exit(
                        symbol=ticker,
                        setup=entry.get("setup", "UNKNOWN"),
                        date=today,
                        exit_price=exit_price,
                        exit_reason="Trailing stop (EMA+VWAP breach)",
                        shares=entry.get("position_size", 0),
                    )
                except Exception as _je:
                    logger.warning(f"journal.log_exit failed for {ticker}: {_je}")
            else:
                actions.append(f"TRAIL EXIT FAILED: {ticker} — check Alpaca manually")

    return actions


# ── Action 4: EOD close ────────────────────────────────────────────────────────

def check_eod(positions: list, now: datetime) -> list[str]:
    """Close all positions at 3:45 PM CT."""
    actions = []
    eod = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)

    if now < eod or not positions:
        return actions

    logger.info(f"EOD at {now.strftime('%H:%M CT')} — closing {len(positions)} position(s)")

    today   = now.strftime("%Y-%m-%d")
    entries = load_executions()

    for pos in positions:
        ticker = pos["ticker"]
        cancel_all_orders(ticker)
        time.sleep(1.5)
        success = close_position(ticker, reason="EOD auto-close 3:45 PM CT")
        if success:
            actions.append(f"EOD CLOSED: {ticker} (was ${pos['current_price']:.2f})")
            try:
                from managers.trade_journal import log_exit, fetch_exit_price
                exec_entry = next(
                    (e for e in reversed(entries)
                     if e.get("symbol") == ticker and e.get("date") == today
                     and e.get("success")),
                    None,
                )
                if exec_entry:
                    exit_price = fetch_exit_price(ticker) or pos["current_price"]
                    log_exit(
                        symbol=ticker,
                        setup=exec_entry.get("setup", "UNKNOWN"),
                        date=today,
                        exit_price=exit_price,
                        exit_reason="EOD auto-close",
                        shares=exec_entry.get("position_size", pos["shares"]),
                    )
            except Exception as _je:
                logger.warning(f"journal.log_exit failed for {ticker}: {_je}")
        else:
            actions.append(f"EOD CLOSE FAILED: {ticker} — close manually in Alpaca!")

    return actions


# ── Status display ─────────────────────────────────────────────────────────────

def print_status(positions: list, today: str):
    if not positions:
        print("No open positions.")
        return

    entries = load_executions()
    print(f"\n{'TICKER':<8} {'ENTRY':>7} {'CURR':>7} "
          f"{'T1':>7} {'T2':>7} {'T3':>7} {'PNL':>8} {'STATUS'}")
    print("-" * 72)

    for pos in positions:
        ticker = pos["ticker"]
        entry_price = pos["entry_price"]
        curr = pos["current_price"]
        pnl  = pos["unrealized_pnl"]

        exec_e = None
        for e in reversed(entries):
            if e.get("symbol") == ticker and e.get("date") == today and e.get("success"):
                exec_e = e
                break

        t1   = exec_e.get("target_1", 0) if exec_e else 0
        t2   = exec_e.get("target_2", 0) if exec_e else 0
        t3   = exec_e.get("target_3", 0) if exec_e else 0

        if exec_e and exec_e.get("t2_stop_set"):
            status = "T2 STOP"
        elif exec_e and exec_e.get("breakeven_set"):
            status = "BE STOP"
        else:
            status = "ENTRY"

        print(
            f"{ticker:<8} ${entry_price:>6.2f} ${curr:>6.2f} "
            f"${t1:>6.2f} ${t2:>6.2f} ${t3:>6.2f} "
            f"${pnl:>+7.2f}  {status}"
        )
    print()


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Tri-City position manager")
    parser.add_argument("--eod",    action="store_true", help="EOD close check only")
    parser.add_argument("--status", action="store_true", help="Print position status")
    args = parser.parse_args()

    now   = datetime.now(CT)
    today = now.strftime("%Y-%m-%d")

    # Market hours guard: 8:30 AM – 4:05 PM CT
    market_open  = now.replace(hour=8,  minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=5,  second=0, microsecond=0)
    if not args.status and (now < market_open or now > market_close):
        return  # Silent outside market hours

    positions = get_open_positions()

    if args.status:
        print_status(positions, today)
        return

    if not positions:
        return  # Nothing to manage — silent

    all_actions = []

    if not args.eod:
        all_actions += check_failed_pullback(positions, today, now)
        positions = get_open_positions()
        all_actions += check_free_ride(positions, today)
        positions = get_open_positions()
        all_actions += check_targets(positions, today)
        positions = get_open_positions()
        all_actions += check_trailing(positions, today)
        positions = get_open_positions()

    all_actions += check_eod(positions, now)

    for action in all_actions:
        print(action)


if __name__ == "__main__":
    main()
