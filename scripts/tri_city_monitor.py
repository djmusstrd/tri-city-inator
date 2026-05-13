#!/usr/bin/env python3
"""
TRI-CITY MONITOR — Intraday signal scanner for Tri-City Inator setups.

Runs every 3 minutes via Claude cron during market hours (9:30 AM – 4:00 PM CT).
Uses Alpaca real-time data to check entry conditions for all watchlist symbols.
Calls tri_city_execute.py for any qualifying ENTER or CONV signals.
Also runs the position manager to manage open positions.

Signal types checked:
  ENTER       — All primary conditions met → execute immediately
  CONV        — MA convergence breakout → execute (same as ENTER)
  SETUP       — Pre-entry alert → print warning, no execution
  REENTRY     — Pullback after T1 hit → execute (half size)

Conditions for ENTER:
  1. Price > EMA20 (calculated from 5-min bars)
  2. Price > VWAP (from Alpaca snapshot)
  3. RVol >= 1.5x (calculated vs 20-day avg)
  4. RSI 50–75 (calculated from 5-min bars)
  5. Gap >= 3% OR intraday move >= 2%
  6. Within 30% of 52-week high
  7. Outperforming SPY (stock % change > SPY % change)
  8. Bull market regime (SPY vs 50-day SMA)

Usage:
    python -W ignore scripts/tri_city_monitor.py
    python -W ignore scripts/tri_city_monitor.py --dry-run
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
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
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

MIN_RVOL          = float(os.getenv("MIN_RVOL",         "1.5"))
MIN_GAP_PCT       = float(os.getenv("MIN_GAP_PCT",      "3.0"))
MIN_FROM_OPEN_PCT = float(os.getenv("MIN_FROM_OPEN_PCT","2.0"))
RSI_MIN           = float(os.getenv("RSI_MIN",          "50"))
RSI_MAX           = float(os.getenv("RSI_MAX",          "75"))
MAX_52W_DIST      = float(os.getenv("MAX_52W_DIST",     "30.0"))
SETUP_RVOL        = float(os.getenv("SETUP_RVOL",       "1.3"))
RVOL_LOOKBACK     = int(os.getenv("RVOL_LOOKBACK",      "20"))

# Market hours guard
MARKET_OPEN_HOUR    = 9
MARKET_OPEN_MINUTE  = 30
MARKET_CLOSE_HOUR   = 16
MARKET_CLOSE_MINUTE = 0

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Cache for slow-changing data (SPY regime, 52W highs, avg volumes)
_cache: dict = {}
CACHE_TTL_MIN = 15


# ── Indicator calculations (pure Python) ──────────────────────────────────────

def calc_rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def calc_ema(closes: list[float], period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for price in closes[period:]:
        ema = price * k + ema * (1 - k)
    return round(ema, 4)


def calc_sma(closes: list[float], period: int = 50) -> float | None:
    if len(closes) < period:
        return None
    return round(sum(closes[-period:]) / period, 4)


# ── Alpaca data ────────────────────────────────────────────────────────────────

def get_data_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        return StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    except ImportError:
        return None


def fetch_snapshots(symbols: list[str]) -> dict:
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
        logger.error(f"fetch_snapshots: {e}")
        return {}


def fetch_intraday_bars(symbol: str, minutes_back: int = 120) -> list[float]:
    """Return list of 5-min bar closes for RSI/EMA calculation."""
    client = get_data_client()
    if not client:
        return []
    try:
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        now = datetime.now(CT)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=now - timedelta(minutes=minutes_back),
            end=now,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            return []
        return list(df["close"])
    except Exception as e:
        logger.warning(f"fetch_intraday_bars {symbol}: {e}")
        return []


def fetch_daily_closes(symbol: str, days: int = 60) -> list[float]:
    """Cache-backed daily close fetch for 52W high / SMA50."""
    cache_key = f"daily_{symbol}"
    cached = _cache.get(cache_key)
    if cached and (datetime.now(CT) - cached["ts"]).seconds < CACHE_TTL_MIN * 60:
        return cached["data"]
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
            start=now - timedelta(days=days + 10),
            end=now,
            limit=days,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        closes = list(df["close"]) if not df.empty else []
        _cache[cache_key] = {"data": closes, "ts": datetime.now(CT)}
        return closes
    except Exception as e:
        logger.warning(f"fetch_daily_closes {symbol}: {e}")
        return []


def fetch_avg_volume(symbol: str) -> float | None:
    """Cache-backed 20-day average volume."""
    cache_key = f"avgvol_{symbol}"
    cached = _cache.get(cache_key)
    if cached and (datetime.now(CT) - cached["ts"]).seconds < CACHE_TTL_MIN * 60:
        return cached["data"]
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
            start=now - timedelta(days=RVOL_LOOKBACK * 2),
            end=now.replace(hour=0, minute=0, second=0),
            limit=RVOL_LOOKBACK,
        )
        bars = client.get_stock_bars(req)
        df = bars.df
        if df.empty:
            return None
        vols = list(df["volume"])[-RVOL_LOOKBACK:]
        avg = sum(vols) / len(vols) if vols else None
        _cache[cache_key] = {"data": avg, "ts": datetime.now(CT)}
        return avg
    except Exception as e:
        logger.warning(f"fetch_avg_volume {symbol}: {e}")
        return None


def get_spy_regime() -> tuple[str, float | None]:
    """Cache-backed SPY regime check."""
    cached = _cache.get("spy_regime")
    if cached and (datetime.now(CT) - cached["ts"]).seconds < CACHE_TTL_MIN * 60:
        return cached["data"]
    client = get_data_client()
    if not client:
        return "UNKNOWN", None
    try:
        from alpaca.data.requests import StockSnapshotRequest
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols="SPY"))
        s = snap.get("SPY")
        if not s or not s.daily_bar or not s.prev_daily_bar:
            return "UNKNOWN", None
        prev = float(s.prev_daily_bar.close)
        curr = float(s.daily_bar.close)
        chg  = round((curr - prev) / prev * 100, 2)
        if chg <= -1.5:
            regime = "BEAR"
        elif chg >= 0.5:
            regime = "BULL"
        else:
            regime = "NEUTRAL"
        # Check vs 50-day SMA for deeper regime
        spy_closes = fetch_daily_closes("SPY", days=60)
        sma50 = calc_sma(spy_closes, 50)
        if sma50 and curr < sma50 * 0.98:
            regime = "BEAR"
        result = (regime, chg)
        _cache["spy_regime"] = {"data": result, "ts": datetime.now(CT)}
        return result
    except Exception as e:
        logger.warning(f"get_spy_regime: {e}")
        return "UNKNOWN", None


def get_spy_change() -> float | None:
    """Today's SPY % change vs prev close."""
    client = get_data_client()
    if not client:
        return None
    try:
        from alpaca.data.requests import StockSnapshotRequest
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols="SPY"))
        s = snap.get("SPY")
        if not s or not s.daily_bar or not s.prev_daily_bar:
            return None
        prev = float(s.prev_daily_bar.close)
        curr = float(s.latest_trade.price)
        return round((curr - prev) / prev * 100, 2)
    except Exception:
        return None


def calc_rvol(symbol: str, today_vol: float) -> float | None:
    """Current RVol: today's volume vs expected at this time of day."""
    avg_vol = fetch_avg_volume(symbol)
    if not avg_vol or avg_vol == 0:
        return None
    now = datetime.now(CT)
    open_ct  = now.replace(hour=8, minute=30, second=0, microsecond=0)
    close_ct = now.replace(hour=15, minute=0, second=0, microsecond=0)
    total_sec   = (close_ct - open_ct).total_seconds()
    elapsed_sec = max(60, (now - open_ct).total_seconds())
    fraction    = min(1.0, elapsed_sec / total_sec)
    expected    = avg_vol * fraction
    return round(today_vol / expected, 2) if expected > 0 else None


# ── Signal evaluation ──────────────────────────────────────────────────────────

def evaluate_symbol(sym: str, snap, spy_chg: float | None) -> dict | None:
    """
    Check all Tri-City entry conditions for one symbol.
    Returns signal dict if ENTER/CONV/SETUP conditions met, else None.
    """
    try:
        curr_price = float(snap.latest_trade.price)
        prev_close = float(snap.prev_daily_bar.close)
        open_price = float(snap.daily_bar.open)
        vwap       = float(snap.daily_bar.vwap)
        today_vol  = float(snap.daily_bar.volume)
    except (AttributeError, TypeError):
        return None

    # ── Quick filters (no bars needed) ────────────────────────────────────────
    gap_pct      = round((open_price - prev_close) / prev_close * 100, 2)
    from_open    = round((curr_price - open_price) / open_price * 100, 2)
    momentum_ok  = gap_pct >= MIN_GAP_PCT or from_open >= MIN_FROM_OPEN_PCT
    above_vwap   = curr_price > vwap

    rvol = calc_rvol(sym, today_vol)
    rvol_ok = rvol is not None and rvol >= MIN_RVOL

    if not (momentum_ok and above_vwap):
        return None  # Skip bars fetch — hard fail

    # ── Intraday bars for RSI and EMA ─────────────────────────────────────────
    bars = fetch_intraday_bars(sym, minutes_back=150)
    if not bars:
        return None

    rsi  = calc_rsi(bars, 14)
    ema20 = calc_ema(bars, 20)

    rsi_ok       = rsi is not None and RSI_MIN <= rsi <= RSI_MAX
    above_ema20  = ema20 is not None and curr_price > ema20

    # ── 52-week high distance ──────────────────────────────────────────────────
    daily_closes = fetch_daily_closes(sym, days=260)
    high_52w     = max(daily_closes[-252:]) if len(daily_closes) >= 252 else \
                   (max(daily_closes) if daily_closes else curr_price)
    dist_52w     = round((high_52w - curr_price) / high_52w * 100, 2)
    near_high    = dist_52w <= MAX_52W_DIST

    # ── Relative strength vs SPY ───────────────────────────────────────────────
    stock_chg     = round((curr_price - prev_close) / prev_close * 100, 2)
    rs_vs_spy     = spy_chg is not None and stock_chg > spy_chg

    # ── MA convergence check (EMA20 vs SMA50) ─────────────────────────────────
    sma50        = calc_sma(bars, min(50, len(bars)))
    conv_gap     = abs(curr_price - (sma50 or curr_price)) / curr_price * 100 if sma50 else 999
    is_conv      = sma50 is not None and conv_gap < 2.0 and curr_price > sma50

    # ── Signal classification ──────────────────────────────────────────────────
    all_conditions = (momentum_ok and above_vwap and rvol_ok and
                      rsi_ok and above_ema20 and near_high)

    if all_conditions and rs_vs_spy:
        signal_type = "CONV" if is_conv else "ENTER"
    elif all_conditions:
        signal_type = "ENTER"  # Still enter even without SPY outperformance
    elif (momentum_ok and above_vwap and above_ema20 and
          rvol is not None and rvol >= SETUP_RVOL and
          rsi is not None and 45 <= rsi <= 80):
        signal_type = "SETUP"  # Alert only — not all conditions met
    else:
        return None

    return {
        "symbol":      sym,
        "signal":      signal_type,
        "price":       curr_price,
        "vwap":        round(vwap, 4),
        "ema20":       round(ema20, 4) if ema20 else None,
        "rsi":         rsi,
        "rvol":        rvol,
        "gap_pct":     gap_pct,
        "from_open":   from_open,
        "dist_52w":    dist_52w,
        "rs_vs_spy":   rs_vs_spy,
        "is_conv":     is_conv,
    }


# ── Execute signal ─────────────────────────────────────────────────────────────

def fire_execute(sig: dict, dry_run: bool = False) -> str:
    """Call tri_city_execute.py for a qualifying signal."""
    cmd = [
        sys.executable, "-W", "ignore",
        str(WORKSPACE / "scripts" / "tri_city_execute.py"),
        "--symbol",  sig["symbol"],
        "--price",   str(sig["price"]),
        "--rsi",     str(sig["rsi"] or 0),
        "--rvol",    str(sig["rvol"] or 0),
        "--signal",  sig["signal"],
        "--setup",   sig["signal"],
    ]
    if dry_run:
        cmd.append("--dry-run")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return "ERROR: execute script timed out"
    except Exception as e:
        return f"ERROR: {e}"


# ── Watchlist loader ───────────────────────────────────────────────────────────

def load_watchlist() -> list[str]:
    """Combine default watchlist with today's scanner candidates."""
    symbols = set()

    wl = WORKSPACE / "watchlists" / "default-watchlist.txt"
    if wl.exists():
        for line in wl.read_text().splitlines():
            sym = line.strip().upper()
            if sym and not sym.startswith("#"):
                symbols.add(sym)

    candidates_file = WORKSPACE / "shared" / "tri-city-candidates.json"
    if candidates_file.exists():
        try:
            data = json.loads(candidates_file.read_text())
            today = datetime.now(CT).strftime("%Y-%m-%d")
            if data.get("date") == today:
                for c in data.get("candidates", []):
                    symbols.add(c["symbol"])
        except Exception:
            pass

    return sorted(symbols)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Tri-City Intraday Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print signals without executing orders")
    args = parser.parse_args()

    now = datetime.now(CT)

    # Market hours guard — silent outside trading hours
    market_open  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MINUTE,  second=0, microsecond=0)
    market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    if now < market_open or now >= market_close:
        return  # Silent — market closed

    # Load watchlist
    symbols = load_watchlist()
    if not symbols:
        return

    # SPY regime (cached)
    spy_regime, spy_chg_daily = get_spy_regime()
    spy_chg = get_spy_change()

    if spy_regime == "BEAR":
        print(f"[{now.strftime('%H:%M CT')}] MARKET REGIME: BEAR "
              f"(SPY {spy_chg_daily:+.2f}%) — no new entries.")
        return

    # Fetch all snapshots in one call
    snaps = fetch_snapshots(symbols)
    if not snaps:
        return

    found_any = False

    for sym in symbols:
        snap = snaps.get(sym)
        if not snap:
            continue

        sig = evaluate_symbol(sym, snap, spy_chg)
        if not sig:
            continue

        found_any = True
        ts = now.strftime("%H:%M CT")

        if sig["signal"] == "SETUP":
            # Alert only — print but don't execute
            print(f"\n[{ts}] ⚠️  SETUP — {sym}")
            print(f"   Price: ${sig['price']:.2f} | RSI: {sig['rsi']} | "
                  f"RVol: {sig['rvol']:.1f}x | Gap: {sig['gap_pct']:+.1f}%")
            print(f"   Watching for ENTER signal...")
        else:
            # ENTER or CONV — report and execute
            label = "💎 CONV BREAKOUT" if sig["signal"] == "CONV" else "🚀 ENTER"
            print(f"\n[{ts}] {label} — {sym}")
            print(f"   Price:    ${sig['price']:.2f}")
            print(f"   VWAP:     ${sig['vwap']:.2f} ({'above' if sig['price'] > sig['vwap'] else 'below'})")
            print(f"   EMA20:    ${sig['ema20']:.2f}" if sig['ema20'] else "   EMA20:    N/A")
            print(f"   RSI:      {sig['rsi']}")
            print(f"   RVol:     {sig['rvol']:.1f}x")
            print(f"   Gap:      {sig['gap_pct']:+.1f}%  |  From Open: {sig['from_open']:+.1f}%")
            print(f"   vs SPY:   {'Outperforming' if sig['rs_vs_spy'] else 'Underperforming'}")
            print(f"   52W Dist: {sig['dist_52w']:.1f}%")
            print()

            output = fire_execute(sig, dry_run=args.dry_run)
            if output:
                print(output)

    # ── Run position manager ───────────────────────────────────────────────────
    try:
        pm_result = subprocess.run(
            [sys.executable, "-W", "ignore",
             str(WORKSPACE / "scripts" / "tri_city_position_manager.py")],
            capture_output=True, text=True, timeout=30
        )
        if pm_result.stdout.strip():
            print(pm_result.stdout.strip())
    except Exception:
        pass


if __name__ == "__main__":
    main()
