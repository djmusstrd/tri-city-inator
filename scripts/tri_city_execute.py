#!/usr/bin/env python3
"""
TRI-CITY EXECUTE — Execute Tri-City Inator signals via Alpaca.

Called by tri_city_monitor.py when a qualifying ENTER or CONV signal is detected.

Entry gate (7 guards, in order):
  1. already_executed_today   — no duplicate setups per symbol per day
  2. already_in_position      — no duplicate symbols
  3. max_positions             — max concurrent open trades (default: 3)
  4. daily_loss_limit          — circuit breaker if down too much (default: -$300)
  5. time_window               — no new entries after cutoff (default: 1:00 PM CT)
  6. market_regime             — block if SPY down > threshold (default: -1.5%)
  7. rvol                      — minimum relative volume required (default: 1.5x)

Position structure (50-25-25 scale-out):
  T1 bracket: 50% of shares, take_profit at +10%, stop at -5%
  T2 bracket: 25% of shares, take_profit at +20%, stop at -5%
  T3 bracket: 25% of shares, take_profit at +30%, stop at -5% (trailed by position manager)

Usage:
    python -W ignore scripts/tri_city_execute.py \\
        --symbol NVDA --price 142.50 --rsi 62 --rvol 2.1 \\
        --signal ENTER --setup ENTER

    python -W ignore scripts/tri_city_execute.py \\
        --symbol NVDA --price 142.50 --rsi 62 --rvol 2.1 \\
        --signal ENTER --setup ENTER --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
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

from managers.trade_executor import execute_tri_city_trade, get_open_positions

CT       = ZoneInfo("America/Chicago")
LOG_FILE = WORKSPACE / "logs" / "tri-city-executions.json"

# ── Guard parameters (all overridable via .env) ───────────────────────────────
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS",      "3"))
MAX_DAILY_LOSS     = float(os.getenv("MAX_DAILY_LOSS",   "-300"))
MIN_RVOL           = float(os.getenv("MIN_RVOL",         "1.5"))
SPY_BEAR_THRESHOLD = float(os.getenv("SPY_BEAR_THRESHOLD", "-1.5"))
_no_entry_hour     = int(os.getenv("NO_ENTRY_HOUR",      "13"))   # 1:00 PM CT
_no_entry_minute   = int(os.getenv("NO_ENTRY_MINUTE",    "0"))
NO_ENTRY_AFTER     = (_no_entry_hour, _no_entry_minute)
RVOL_LOOKBACK      = int(os.getenv("RVOL_LOOKBACK",      "20"))

# ── Position sizing (percentage-based) ────────────────────────────────────────
RISK_PCT  = float(os.getenv("RISK_PCT",  "2.0"))   # % of account equity to risk per trade
STOP_PCT  = float(os.getenv("STOP_PCT",  "5.0"))   # % below entry for stop loss
T1_PCT    = float(os.getenv("T1_PCT",   "10.0"))   # first take-profit target
T2_PCT    = float(os.getenv("T2_PCT",   "20.0"))   # second take-profit target
T3_PCT    = float(os.getenv("T3_PCT",   "30.0"))   # third take-profit target (trailed)

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Execution log ──────────────────────────────────────────────────────────────

def load_log() -> list:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            pass
    return []


def save_log(entries: list):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(json.dumps(entries, indent=2, default=str))


# ── Guard 1 & 2: duplicate checks ─────────────────────────────────────────────

def already_executed_today(symbol: str, setup: str) -> bool:
    today = datetime.now(CT).strftime("%Y-%m-%d")
    for e in load_log():
        if (e.get("symbol") == symbol and e.get("setup") == setup
                and e.get("date") == today):
            logger.info(f"Already executed {setup} on {symbol} today.")
            return True
    return False


def already_in_position(symbol: str) -> bool:
    positions = get_open_positions()
    if symbol in [p["ticker"] for p in positions]:
        logger.info(f"Already holding {symbol}.")
        return True
    return False


# ── Guard 3: max positions ─────────────────────────────────────────────────────

def check_max_positions() -> bool:
    positions = get_open_positions()
    if len(positions) >= MAX_POSITIONS:
        logger.info(f"Max positions: {len(positions)}/{MAX_POSITIONS} open.")
        return True
    return False


# ── Guard 4: daily loss limit ──────────────────────────────────────────────────

def check_daily_loss_limit(today: str) -> bool:
    try:
        from managers.trade_journal import get_all_trades
        trades = get_all_trades()
        today_pnl = sum(
            t["realized_pnl"] for t in trades
            if t.get("date") == today and t.get("status") == "closed"
        )
        if today_pnl <= MAX_DAILY_LOSS:
            logger.warning(
                f"Daily loss limit hit: ${today_pnl:.2f} "
                f"(limit ${MAX_DAILY_LOSS:.0f}). No new entries."
            )
            return True
    except Exception as e:
        logger.warning(f"check_daily_loss_limit: {e}")
    return False


# ── Guard 5: time window ───────────────────────────────────────────────────────

def check_time_window(now: datetime) -> bool:
    cutoff = now.replace(
        hour=NO_ENTRY_AFTER[0], minute=NO_ENTRY_AFTER[1],
        second=0, microsecond=0
    )
    if now >= cutoff:
        logger.info(f"Past entry cutoff {NO_ENTRY_AFTER[0]:02d}:{NO_ENTRY_AFTER[1]:02d} CT.")
        return True
    return False


# ── Guard 6: SPY market regime ─────────────────────────────────────────────────

def get_spy_regime() -> tuple[str, float | None]:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return "UNKNOWN", None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols="SPY"))
        s = snap.get("SPY")
        if not s or not s.daily_bar or not s.prev_daily_bar:
            return "UNKNOWN", None
        prev   = float(s.prev_daily_bar.close)
        curr   = float(s.daily_bar.close)
        change = round((curr - prev) / prev * 100, 2)
        if change <= SPY_BEAR_THRESHOLD:
            return "BEAR", change
        elif change >= 0.5:
            return "BULL", change
        return "NEUTRAL", change
    except Exception as e:
        logger.warning(f"get_spy_regime: {e}")
        return "UNKNOWN", None


# ── Guard 7: relative volume ───────────────────────────────────────────────────

def verify_rvol(symbol: str, rvol_from_monitor: float | None) -> float | None:
    """Use rvol passed from monitor (already calculated). Recompute if missing."""
    if rvol_from_monitor is not None:
        return rvol_from_monitor
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest, StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol))
        s = snap.get(symbol)
        if not s or not s.daily_bar:
            return None
        today_vol = float(s.daily_bar.volume)
        now = datetime.now(CT)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=now - timedelta(days=RVOL_LOOKBACK * 2),
            end=now.replace(hour=0, minute=0, second=0),
            limit=RVOL_LOOKBACK,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            return None
        avg_vol = sum(list(df["volume"])[-RVOL_LOOKBACK:]) / RVOL_LOOKBACK
        open_ct  = now.replace(hour=8, minute=30, second=0, microsecond=0)
        close_ct = now.replace(hour=15, minute=0, second=0, microsecond=0)
        fraction = min(1.0, max(60, (now - open_ct).total_seconds()) /
                       (close_ct - open_ct).total_seconds())
        expected = avg_vol * fraction
        return round(today_vol / expected, 2) if expected > 0 else None
    except Exception as e:
        logger.warning(f"verify_rvol {symbol}: {e}")
        return None


# ── Account equity & position sizing ──────────────────────────────────────────

def get_account_equity() -> float | None:
    """Fetch current account equity from Alpaca."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        account = client.get_account()
        return float(account.equity)
    except Exception as e:
        logger.warning(f"get_account_equity: {e}")
        return None


def calculate_signal(symbol: str, price: float) -> dict:
    """
    Build trade signal with auto-calculated position size and percentage targets.
    Uses account equity from Alpaca. Falls back to FIXED_RISK if equity unavailable.
    """
    equity       = get_account_equity()
    fallback_risk = float(os.getenv("FIXED_RISK", "150"))

    if equity and equity > 0:
        max_risk_dollars = equity * (RISK_PCT / 100)
    else:
        max_risk_dollars = fallback_risk

    stop_price   = round(price * (1 - STOP_PCT / 100), 2)
    risk_per_share = round(price - stop_price, 4)
    if risk_per_share < 0.05:
        risk_per_share = 0.05

    position_size = max(1, int(max_risk_dollars / risk_per_share))

    target_1 = round(price * (1 + T1_PCT / 100), 2)
    target_2 = round(price * (1 + T2_PCT / 100), 2)
    target_3 = round(price * (1 + T3_PCT / 100), 2)

    return {
        "ticker":        symbol,
        "entry_price":   price,
        "position_size": position_size,
        "stop_loss":     stop_price,
        "target_1":      target_1,
        "target_2":      target_2,
        "target_3":      target_3,
        "direction":     "BULLISH",
        "confidence":    1.0,
    }


# ── Execution log ──────────────────────────────────────────────────────────────

def log_execution(symbol: str, setup: str, signal: dict, result,
                  rvol: float | None = None,
                  spy_regime: str | None = None,
                  spy_change: float | None = None):
    now = datetime.now(CT)
    risk_dollars = round(
        signal["position_size"] * (signal["entry_price"] - signal["stop_loss"]), 2
    )
    entry = {
        "date":          now.strftime("%Y-%m-%d"),
        "time":          now.strftime("%H:%M:%S CT"),
        "symbol":        symbol,
        "setup":         setup,
        "entry_price":   signal["entry_price"],
        "stop_loss":     signal["stop_loss"],
        "target_1":      signal["target_1"],
        "target_2":      signal["target_2"],
        "target_3":      signal["target_3"],
        "position_size": signal["position_size"],
        "risk_dollars":  risk_dollars,
        "rvol":          rvol,
        "spy_regime":    spy_regime,
        "spy_change":    spy_change,
        "success":       result.success,
        "order_id":      result.order_id,
        "error":         result.error,
    }
    log = load_log()
    log.append(entry)
    save_log(log)
    return entry


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City auto-executor")
    parser.add_argument("--symbol",  required=True)
    parser.add_argument("--price",   required=True, type=float)
    parser.add_argument("--rsi",     required=True, type=float)
    parser.add_argument("--rvol",    required=True, type=float)
    parser.add_argument("--signal",  required=True, help="ENTER | CONV | REENTRY")
    parser.add_argument("--setup",   required=True, help="ENTER | CONV | REENTRY")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print signal without placing orders")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    now    = datetime.now(CT)
    today  = now.strftime("%Y-%m-%d")

    print("=" * 60)
    print(f"TRI-CITY EXECUTE — {now.strftime('%Y-%m-%d %H:%M CT')}")
    print(f"Symbol: {symbol} | Setup: {args.setup} | Price: ${args.price:.2f}")
    print("=" * 60)

    # ── Guard 1: already executed ──────────────────────────────────────────────
    if already_executed_today(symbol, args.setup):
        print(f"SKIP: {args.setup} already executed for {symbol} today.")
        sys.exit(0)

    # ── Guard 2: already in position ───────────────────────────────────────────
    if already_in_position(symbol):
        print(f"SKIP: Already holding {symbol}.")
        sys.exit(0)

    # ── Guard 3: max positions ─────────────────────────────────────────────────
    if check_max_positions():
        print(f"SKIP: Max positions ({MAX_POSITIONS}) already open.")
        sys.exit(0)

    # ── Guard 4: daily loss limit ──────────────────────────────────────────────
    if check_daily_loss_limit(today):
        print(f"SKIP: Daily loss limit (${MAX_DAILY_LOSS:.0f}) reached.")
        sys.exit(0)

    # ── Guard 5: time window ───────────────────────────────────────────────────
    if not args.dry_run and check_time_window(now):
        print(f"SKIP: Past entry cutoff "
              f"({NO_ENTRY_AFTER[0]:02d}:{NO_ENTRY_AFTER[1]:02d} CT).")
        sys.exit(0)

    # ── Guard 6: market regime ─────────────────────────────────────────────────
    spy_regime, spy_change = get_spy_regime()
    spy_str = f"{spy_change:+.2f}%" if spy_change is not None else "N/A"
    print(f"SPY regime: {spy_regime} ({spy_str})")
    if spy_regime == "BEAR" and not args.dry_run:
        print(f"SKIP: SPY bearish ({spy_str}) — blocking LONG entries.")
        sys.exit(0)

    # ── Guard 7: relative volume ───────────────────────────────────────────────
    rvol = verify_rvol(symbol, args.rvol if args.rvol > 0 else None)
    rvol_str = f"{rvol:.2f}x" if rvol is not None else "N/A"
    print(f"RVol: {rvol_str} (min {MIN_RVOL:.1f}x)")
    if rvol is not None and rvol < MIN_RVOL and not args.dry_run:
        print(f"SKIP: RVol {rvol_str} below minimum {MIN_RVOL:.1f}x.")
        sys.exit(0)

    # ── Build signal ───────────────────────────────────────────────────────────
    signal = calculate_signal(symbol, args.price)
    risk_dollars = round(
        signal["position_size"] * (signal["entry_price"] - signal["stop_loss"]), 2
    )

    print(f"\nSignal:")
    print(f"  Entry:  ${signal['entry_price']:.2f}")
    print(f"  Stop:   ${signal['stop_loss']:.2f}  (-{STOP_PCT:.0f}%)")
    print(f"  T1:     ${signal['target_1']:.2f}  (+{T1_PCT:.0f}%) — sell 50%")
    print(f"  T2:     ${signal['target_2']:.2f}  (+{T2_PCT:.0f}%) — sell 25%")
    print(f"  T3:     ${signal['target_3']:.2f}  (+{T3_PCT:.0f}%) — trail 25%")
    print(f"  Size:   {signal['position_size']} shares  (50+25+25 split)")
    print(f"  Risk:   ${risk_dollars:.2f}")

    if args.dry_run:
        print("\n[DRY RUN] — no order placed.")
        return

    # ── Execute ────────────────────────────────────────────────────────────────
    print("\nPlacing orders via Alpaca...")
    result = execute_tri_city_trade("tri_city", signal)

    if result.success:
        log_execution(symbol, args.setup, signal, result,
                      rvol=rvol, spy_regime=spy_regime, spy_change=spy_change)
        print(f"\n✅ ORDERS PLACED")
        print(f"   Order ID: {result.order_id}")
        print(f"   Shares:   {result.shares_filled}")
        print(f"   Fill ~:   ${result.avg_fill_price:.2f}")
        print(f"   Logged:   {LOG_FILE}")
    else:
        print(f"\n❌ EXECUTION FAILED: {result.error}")
        log_execution(symbol, args.setup, signal, result,
                      rvol=rvol, spy_regime=spy_regime, spy_change=spy_change)
        sys.exit(1)


if __name__ == "__main__":
    main()
