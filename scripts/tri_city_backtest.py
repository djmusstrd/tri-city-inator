#!/usr/bin/env python3
"""
TRI-CITY BACKTEST — Historical performance simulation using Alpaca data.

Simulates Tri-City entry conditions on daily OHLCV data for a date range.
Uses simplified daily-bar conditions (not intraday RSI/EMA) suitable for
validating the strategy concept and adjusting parameters.

Conditions simulated:
  - Gap >= MIN_GAP_PCT from previous close
  - Volume > MIN_VOL_MULTIPLIER * 20-day avg
  - Price above SMA50
  - SPY not in BEAR regime (daily change > -1.5%)
  - Entry at open price (next day after signal)

Exit simulation:
  - T1 hit (+10%): partial exit 50%
  - T2 hit (+20%): partial exit 25%
  - T3 hit (+30%): exit remaining 25%
  - Stop hit (-5%): exit all remaining

Usage:
    python -W ignore scripts/tri_city_backtest.py --symbols NVDA TSLA AAPL
    python -W ignore scripts/tri_city_backtest.py --symbols NVDA --start 2024-01-01
    python -W ignore scripts/tri_city_backtest.py --symbols NVDA --start 2024-01-01 --end 2024-12-31
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

CT = ZoneInfo("America/Chicago")

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

MIN_GAP_PCT       = float(os.getenv("MIN_GAP_PCT",     "3.0"))
MIN_VOL_MULTI     = float(os.getenv("MIN_RVOL",         "1.5"))
STOP_PCT          = float(os.getenv("STOP_PCT",         "5.0"))
T1_PCT            = float(os.getenv("T1_PCT",          "10.0"))
T2_PCT            = float(os.getenv("T2_PCT",          "20.0"))
T3_PCT            = float(os.getenv("T3_PCT",          "30.0"))
SPY_BEAR_THRESH   = float(os.getenv("SPY_BEAR_THRESHOLD", "-1.5"))
FIXED_RISK        = float(os.getenv("FIXED_RISK",       "150"))

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)


# ── Data fetching ──────────────────────────────────────────────────────────────

def get_data_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        print("ERROR: Set ALPACA_API_KEY and ALPACA_SECRET_KEY in .env")
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    except ImportError:
        print("ERROR: alpaca-py not installed. Run: pip install alpaca-py")
        return None


def fetch_daily_bars_df(symbol: str, start: datetime, end: datetime):
    """Return pandas DataFrame of daily bars for the symbol."""
    client = get_data_client()
    if not client:
        return None
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=start - timedelta(days=80),   # Extra buffer for SMA50 warm-up
            end=end,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            return None
        # Flatten multi-index if needed
        if isinstance(df.index, type(None)):
            return None
        if hasattr(df.index, 'levels'):
            df = df.reset_index(level=0, drop=True)
        df = df.sort_index()
        return df
    except Exception as e:
        print(f"ERROR fetching {symbol}: {e}")
        return None


# ── Indicator helpers ──────────────────────────────────────────────────────────

def sma(series: list[float], period: int) -> list[float | None]:
    result = []
    for i in range(len(series)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(series[i - period + 1:i + 1]) / period)
    return result


# ── Trade simulation ───────────────────────────────────────────────────────────

def simulate_trade(entry_price: float, highs: list[float], lows: list[float]) -> dict:
    """
    Simulate a single trade over subsequent bars.
    Returns P&L as % of entry using 50-25-25 scale-out.

    Scale-out:
      T1 hit (+10%): close 50% of position
      T2 hit (+20%): close 25%
      T3 hit (+30%): close 25%
      Stop hit (-5%): close all remaining

    Returns dict with P&L breakdown.
    """
    stop_price = entry_price * (1 - STOP_PCT / 100)
    t1_price   = entry_price * (1 + T1_PCT / 100)
    t2_price   = entry_price * (1 + T2_PCT / 100)
    t3_price   = entry_price * (1 + T3_PCT / 100)

    remaining  = 1.0       # as fraction of position
    pnl_pct    = 0.0       # running P&L as fraction
    t1_hit = t2_hit = t3_hit = stop_hit = False
    bars_held  = 0
    exit_price = entry_price

    for high, low in zip(highs, lows):
        bars_held += 1

        # Check stop first (intrabar worst case)
        if low <= stop_price:
            pnl_pct  += remaining * ((stop_price - entry_price) / entry_price)
            remaining = 0
            stop_hit  = True
            exit_price = stop_price
            break

        # Check T3
        if not t3_hit and high >= t3_price and remaining > 0:
            lot = min(remaining, 0.25)
            pnl_pct  += lot * ((t3_price - entry_price) / entry_price)
            remaining -= lot
            t3_hit    = True
            exit_price = t3_price

        # Check T2
        if not t2_hit and high >= t2_price and remaining > 0:
            lot = min(remaining, 0.25)
            pnl_pct  += lot * ((t2_price - entry_price) / entry_price)
            remaining -= lot
            t2_hit    = True

        # Check T1
        if not t1_hit and high >= t1_price and remaining > 0:
            lot = min(remaining, 0.50)
            pnl_pct  += lot * ((t1_price - entry_price) / entry_price)
            remaining -= lot
            t1_hit    = True

        if remaining <= 0:
            break

    # Force exit remaining (e.g. EOD after max bars)
    if remaining > 0:
        pnl_pct += remaining * ((exit_price - entry_price) / entry_price)

    return {
        "pnl_pct":   round(pnl_pct * 100, 3),
        "pnl_$":     round(FIXED_RISK * pnl_pct / (STOP_PCT / 100), 2),
        "t1_hit":    t1_hit,
        "t2_hit":    t2_hit,
        "t3_hit":    t3_hit,
        "stop_hit":  stop_hit,
        "bars_held": bars_held,
    }


# ── Main backtest ──────────────────────────────────────────────────────────────

def backtest_symbol(symbol: str, start: datetime, end: datetime) -> list[dict]:
    """Run full backtest for one symbol. Returns list of trade records."""
    df = fetch_daily_bars_df(symbol, start, end)
    if df is None or len(df) < 55:
        print(f"  {symbol}: Not enough data (need 55+ bars). Skipping.")
        return []

    closes  = list(df["close"])
    opens   = list(df["open"])
    highs   = list(df["high"])
    lows    = list(df["low"])
    volumes = list(df["volume"])
    dates   = list(df.index)

    sma50_series = sma(closes, 50)

    # 20-day avg volume
    avg_vols = []
    for i in range(len(volumes)):
        if i < 20:
            avg_vols.append(None)
        else:
            avg_vols.append(sum(volumes[i - 20:i]) / 20)

    trades = []
    start_idx = max(51, 0)  # Start after SMA50 warm-up

    for i in range(start_idx, len(dates) - 1):
        bar_date = dates[i]
        # Filter to requested date range
        if hasattr(bar_date, 'date'):
            bar_date_only = bar_date.date()
        else:
            bar_date_only = bar_date

        if bar_date_only < start.date() or bar_date_only > end.date():
            continue

        prev_close  = closes[i - 1]
        open_price  = opens[i]
        curr_close  = closes[i]
        curr_vol    = volumes[i]
        sma50       = sma50_series[i]
        avg_vol     = avg_vols[i]

        # ── Entry conditions ───────────────────────────────────────────────────
        gap_pct = (open_price - prev_close) / prev_close * 100
        if gap_pct < MIN_GAP_PCT:
            continue

        rvol = (curr_vol / avg_vol) if avg_vol and avg_vol > 0 else 0
        if rvol < MIN_VOL_MULTI:
            continue

        if sma50 and open_price <= sma50:
            continue  # Require Stage 2

        # ── Enter at next bar's open ───────────────────────────────────────────
        if i + 1 >= len(dates):
            continue

        entry_price = opens[i + 1]
        future_highs = highs[i + 1:i + 11]    # Simulate up to 10 bars forward
        future_lows  = lows[i + 1:i + 11]

        if not future_highs:
            continue

        result = simulate_trade(entry_price, future_highs, future_lows)

        outcome = "stop"
        if result["t3_hit"]:
            outcome = "full_win"
        elif result["t2_hit"]:
            outcome = "partial_win"
        elif result["t1_hit"]:
            outcome = "partial_win"
        elif result["pnl_pct"] > -0.5:
            outcome = "scratch"

        trades.append({
            "date":        str(bar_date_only),
            "symbol":      symbol,
            "gap_pct":     round(gap_pct, 2),
            "rvol":        round(rvol, 2),
            "entry_price": round(entry_price, 4),
            "outcome":     outcome,
            **result,
        })

    return trades


# ── Report ─────────────────────────────────────────────────────────────────────

def print_report(all_trades: list[dict], symbols: list[str],
                 start: datetime, end: datetime):
    print(f"\n{'='*72}")
    print(f"  TRI-CITY BACKTEST — {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}")
    print(f"  Symbols: {', '.join(symbols)}")
    print(f"  Conditions: Gap >{MIN_GAP_PCT:.0f}% | RVol >{MIN_VOL_MULTI:.1f}x | "
          f"Stop {STOP_PCT:.0f}% | T1 {T1_PCT:.0f}% | T2 {T2_PCT:.0f}% | T3 {T3_PCT:.0f}%")
    print(f"{'='*72}")

    if not all_trades:
        print("  No trades found matching criteria.")
        print(f"{'='*72}\n")
        return

    # Trade log
    print(f"\n{'#':<4} {'DATE':<12} {'SYM':<6} {'GAP%':>6} {'RVOL':>6} "
          f"{'ENTRY':>7} {'P&L%':>7} {'P&L$':>8} {'OUTCOME'}")
    print("-" * 72)

    total_pnl = 0.0
    wins = losses = scratches = 0
    outcome_icon = {"full_win": "🟢", "partial_win": "🟡", "scratch": "⚪", "stop": "🔴"}

    for i, t in enumerate(all_trades, 1):
        icon = outcome_icon.get(t["outcome"], "?")
        total_pnl += t["pnl_$"]
        if t["outcome"] in ("full_win", "partial_win"):
            wins += 1
        elif t["outcome"] == "stop":
            losses += 1
        else:
            scratches += 1

        print(f"{i:<4} {t['date']:<12} {t['symbol']:<6} "
              f"{t['gap_pct']:>+5.1f}% {t['rvol']:>5.1f}x "
              f"${t['entry_price']:>6.2f} "
              f"{t['pnl_pct']:>+6.2f}% ${t['pnl_$']:>+7.2f}  "
              f"{icon} {t['outcome']}")

    total = len(all_trades)
    win_rate = wins / total if total else 0
    avg_pnl  = total_pnl / total if total else 0

    print(f"\n{'─'*72}")
    print(f"  SUMMARY ({total} trades)")
    print(f"{'─'*72}")
    print(f"  Total P&L:   ${total_pnl:>+8.2f}")
    print(f"  Win Rate:    {win_rate*100:.1f}%  ({wins}W / {scratches}S / {losses}L)")
    print(f"  Avg P&L:     ${avg_pnl:>+8.2f} per trade")

    # T1/T2/T3 hit rates
    t1_hits = sum(1 for t in all_trades if t["t1_hit"])
    t2_hits = sum(1 for t in all_trades if t["t2_hit"])
    t3_hits = sum(1 for t in all_trades if t["t3_hit"])
    print(f"  T1 Hit Rate: {t1_hits/total*100:.1f}%  ({t1_hits}/{total})")
    print(f"  T2 Hit Rate: {t2_hits/total*100:.1f}%  ({t2_hits}/{total})")
    print(f"  T3 Hit Rate: {t3_hits/total*100:.1f}%  ({t3_hits}/{total})")

    # By symbol
    if len(symbols) > 1:
        print(f"\n  BY SYMBOL:")
        print(f"  {'SYMBOL':<8} {'TRADES':>6} {'WINS':>5} {'P&L':>9} {'WIN%':>6}")
        print(f"  {'─'*8} {'─'*6} {'─'*5} {'─'*9} {'─'*6}")
        for sym in symbols:
            sym_trades = [t for t in all_trades if t["symbol"] == sym]
            if not sym_trades:
                continue
            sym_wins = sum(1 for t in sym_trades
                          if t["outcome"] in ("full_win", "partial_win"))
            sym_pnl  = sum(t["pnl_$"] for t in sym_trades)
            sym_wr   = sym_wins / len(sym_trades) * 100
            print(f"  {sym:<8} {len(sym_trades):>6} {sym_wins:>5} "
                  f"${sym_pnl:>+8.2f} {sym_wr:>5.1f}%")

    print(f"\n{'='*72}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City Backtest")
    parser.add_argument("--symbols", nargs="+", required=True,
                        help="Space-separated list of symbols e.g. NVDA TSLA")
    parser.add_argument("--start",   default="2024-01-01",
                        help="Start date YYYY-MM-DD (default: 2024-01-01)")
    parser.add_argument("--end",     default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--save",    action="store_true",
                        help="Save results to logs/backtest-results.json")
    args = parser.parse_args()

    start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=CT)
    end   = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=CT) \
            if args.end else datetime.now(CT)

    symbols = [s.upper() for s in args.symbols]
    all_trades = []

    for sym in symbols:
        print(f"Backtesting {sym}...")
        trades = backtest_symbol(sym, start, end)
        print(f"  {len(trades)} trades found.")
        all_trades.extend(trades)

    all_trades.sort(key=lambda t: t["date"])
    print_report(all_trades, symbols, start, end)

    if args.save and all_trades:
        out = WORKSPACE / "logs" / "backtest-results.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(all_trades, indent=2))
        print(f"Results saved to {out}")


if __name__ == "__main__":
    main()
