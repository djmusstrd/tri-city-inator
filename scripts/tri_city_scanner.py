#!/usr/bin/env python3
"""
TRI-CITY PREMARKET SCANNER — Morning candidate list for Tri-City Inator setups.

Run at 7:00 AM CT via Claude cron. Finds gap-up stocks meeting Tri-City entry
criteria. Saves candidates to shared/tri-city-candidates.json and prints a
ranked table to the terminal.

Scoring weights:
  Gap %          35% — size of overnight move
  Relative Vol   35% — volume conviction
  Stage 2        20% — price above SMA50 (uptrend confirmed)
  Near 52W High  10% — proximity to breakout zone

Usage:
    python -W ignore scripts/tri_city_scanner.py
    python -W ignore scripts/tri_city_scanner.py --dry-run
    python -W ignore scripts/tri_city_scanner.py --top 20
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

CT           = ZoneInfo("America/Chicago")
CANDIDATES   = WORKSPACE / "shared" / "tri-city-candidates.json"

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

MIN_GAP_PCT  = float(os.getenv("MIN_GAP_PCT",  "3.0"))
MIN_PRICE    = float(os.getenv("MIN_PRICE",    "2.0"))
MAX_PRICE    = float(os.getenv("MAX_PRICE",    "500.0"))
MIN_RVOL     = float(os.getenv("MIN_RVOL",     "1.5"))
TOP_N        = int(os.getenv("SCANNER_TOP_N", "15"))

W_GAP   = 0.35
W_RVOL  = 0.35
W_STAGE = 0.20
W_HIGH  = 0.10

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Indicator helpers (pure Python, no TA-Lib) ────────────────────────────────

def calc_sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def calc_ema(closes: list[float], period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


# ── Alpaca data helpers ────────────────────────────────────────────────────────

def get_data_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    except ImportError:
        print("ERROR: alpaca-py not installed. Run: pip install alpaca-py")
        return None


def fetch_snapshots(symbols: list[str]) -> dict:
    """Fetch real-time snapshot for multiple symbols in one API call."""
    client = get_data_client()
    if not client:
        return {}
    try:
        from alpaca.data.requests import StockSnapshotRequest
        snaps = client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=symbols)
        )
        return {sym: snaps[sym] for sym in symbols if sym in snaps}
    except Exception as e:
        logger.error(f"fetch_snapshots error: {e}")
        return {}


def fetch_daily_bars(symbol: str, lookback_days: int = 60) -> list[float]:
    """Return list of daily closes (oldest → newest) for SMA / 52W high calc."""
    client = get_data_client()
    if not client:
        return []
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        now = datetime.now(CT)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=now - timedelta(days=lookback_days + 10),
            end=now,
            limit=lookback_days,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            return []
        return list(df["close"])
    except Exception as e:
        logger.warning(f"fetch_daily_bars error for {symbol}: {e}")
        return []


def fetch_avg_daily_volume(symbol: str, lookback: int = 20) -> float | None:
    """Return 20-day average daily volume."""
    client = get_data_client()
    if not client:
        return None
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        now = datetime.now(CT)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=now - timedelta(days=lookback * 2),
            end=now.replace(hour=0, minute=0, second=0),
            limit=lookback,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            return None
        vols = list(df["volume"])[-lookback:]
        return sum(vols) / len(vols) if vols else None
    except Exception as e:
        logger.warning(f"fetch_avg_volume error for {symbol}: {e}")
        return None


# ── Candidate scoring ──────────────────────────────────────────────────────────

def score_candidate(sym: str, snap, avg_vol: float | None,
                    closes: list[float]) -> dict | None:
    """
    Score a symbol against Tri-City entry criteria.
    Returns a candidate dict or None if it fails hard filters.
    """
    try:
        prev_close  = float(snap.prev_daily_bar.close)
        open_price  = float(snap.daily_bar.open)
        curr_price  = float(snap.latest_trade.price)
        today_vol   = float(snap.daily_bar.volume)
    except (AttributeError, TypeError):
        return None

    # Hard filters
    if curr_price < MIN_PRICE or curr_price > MAX_PRICE:
        return None

    gap_pct = round((open_price - prev_close) / prev_close * 100, 2)
    if gap_pct < MIN_GAP_PCT:
        return None

    # Relative volume
    rvol = None
    if avg_vol and avg_vol > 0:
        now = datetime.now(CT)
        open_ct  = now.replace(hour=8, minute=30, second=0, microsecond=0)
        close_ct = now.replace(hour=15, minute=0, second=0, microsecond=0)
        total_sec   = (close_ct - open_ct).total_seconds()
        elapsed_sec = max(300, (now - open_ct).total_seconds())
        fraction    = min(1.0, elapsed_sec / total_sec)
        expected_vol = avg_vol * fraction
        rvol = round(today_vol / expected_vol, 2) if expected_vol > 0 else None

    if rvol is not None and rvol < MIN_RVOL:
        return None

    # Stage 2: price above SMA50
    sma50   = calc_sma(closes, 50)
    stage2  = bool(sma50 and curr_price > sma50)

    # 52-week high distance
    high_52w    = max(closes[-252:]) if len(closes) >= 252 else max(closes) if closes else curr_price
    dist_52w    = round((high_52w - curr_price) / high_52w * 100, 2)
    near_high   = dist_52w < 30

    # Score (0–100)
    gap_score   = min(1.0, gap_pct / 15.0)
    rvol_score  = min(1.0, (rvol or 1.0) / 5.0)
    stage_score = 1.0 if stage2 else 0.0
    high_score  = max(0, 1.0 - dist_52w / 30.0) if near_high else 0.0

    score = round(
        (gap_score * W_GAP + rvol_score * W_RVOL +
         stage_score * W_STAGE + high_score * W_HIGH) * 100, 1
    )

    return {
        "symbol":    sym,
        "price":     curr_price,
        "gap_pct":   gap_pct,
        "rvol":      rvol,
        "stage2":    stage2,
        "sma50":     round(sma50, 2) if sma50 else None,
        "high_52w":  round(high_52w, 2),
        "dist_52w":  dist_52w,
        "score":     score,
        "scanned_at": datetime.now(CT).strftime("%H:%M CT"),
    }


# ── Universe: US stocks with gap-up potential ─────────────────────────────────

def get_scan_universe() -> list[str]:
    """
    Build scan universe: watchlist + any additional symbols.
    Reads watchlists/default-watchlist.txt.
    """
    wl_file = WORKSPACE / "watchlists" / "default-watchlist.txt"
    symbols = []
    if wl_file.exists():
        for line in wl_file.read_text().splitlines():
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.append(sym)
    return symbols


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City Premarket Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Print results, don't save")
    parser.add_argument("--top", type=int, default=TOP_N, help="Max candidates to show")
    args = parser.parse_args()

    now = datetime.now(CT)
    print(f"\n{'='*64}")
    print(f"  TRI-CITY PREMARKET SCANNER — {now.strftime('%Y-%m-%d %H:%M CT')}")
    print(f"{'='*64}")

    universe = get_scan_universe()
    if not universe:
        print("No symbols in watchlist. Add symbols to watchlists/default-watchlist.txt")
        return

    print(f"Scanning {len(universe)} symbols...")

    # Fetch snapshots in one call
    snaps = fetch_snapshots(universe)
    if not snaps:
        print("ERROR: Could not fetch market data. Check your Alpaca API keys in .env")
        return

    candidates = []
    for sym, snap in snaps.items():
        # Fetch historical data for each (parallelization possible in future)
        closes  = fetch_daily_bars(sym, lookback_days=260)
        avg_vol = fetch_avg_daily_volume(sym, lookback=20)

        candidate = score_candidate(sym, snap, avg_vol, closes)
        if candidate:
            candidates.append(candidate)

    # Sort by score descending
    candidates.sort(key=lambda c: c["score"], reverse=True)
    top = candidates[:args.top]

    # ── Print ranked table ────────────────────────────────────────────────────
    if not top:
        print(f"\n  No candidates meeting criteria (gap >{MIN_GAP_PCT}%, RVol >{MIN_RVOL}x)")
        print(f"  This is normal in slow/down markets.\n")
        return

    print(f"\n{'#':<4} {'SYMBOL':<8} {'PRICE':>7} {'GAP%':>7} {'RVOL':>6} "
          f"{'STAGE2':>7} {'52W DIST':>9} {'SCORE':>6}")
    print("-" * 64)
    for i, c in enumerate(top, 1):
        stage = "YES" if c["stage2"] else "---"
        rvol  = f"{c['rvol']:.1f}x" if c["rvol"] else "N/A"
        print(f"{i:<4} {c['symbol']:<8} ${c['price']:>6.2f} "
              f"{c['gap_pct']:>+6.1f}% {rvol:>6} "
              f"{stage:>7} {c['dist_52w']:>7.1f}% {c['score']:>6.1f}")

    print(f"\n  {len(top)} candidate(s) found.")
    print(f"  Add top symbols to your TradingView watchlist and")
    print(f"  load the Tri-City Inator indicator on each chart.")

    if not args.dry_run:
        CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date":       now.strftime("%Y-%m-%d"),
            "scanned_at": now.strftime("%H:%M CT"),
            "candidates": top,
        }
        CANDIDATES.write_text(json.dumps(payload, indent=2))
        print(f"\n  Saved → {CANDIDATES}")

    print(f"\n{'='*64}\n")


if __name__ == "__main__":
    main()
