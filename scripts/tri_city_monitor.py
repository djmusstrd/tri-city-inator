#!/usr/bin/env python3
"""
TRI-CITY MONITOR — Intraday signal scanner and execution loop.

Runs every 3 minutes via Claude cron during market hours (9:30 AM – 4:00 PM CT).
Fetches real-time data from Alpaca, evaluates each watchlist symbol through the
active strategy, and calls tri_city_execute.py for qualifying signals.

STRATEGY SELECTION
──────────────────
Default (no config needed):
    Uses strategies/tri_city_strategy.py — the Tri-City Inator ENHANCED logic.

Custom strategy:
    Set in .env:
        CUSTOM_STRATEGY=true
        STRATEGY_FILE=strategies/my_orb_strategy.py
    Then drop your strategy file at that path. See strategies/custom_template.py.

Usage:
    python -W ignore scripts/tri_city_monitor.py
    python -W ignore scripts/tri_city_monitor.py --dry-run
"""

from __future__ import annotations

import importlib.util
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

MIN_RVOL          = float(os.getenv("MIN_RVOL",          "1.5"))
MIN_GAP_PCT       = float(os.getenv("MIN_GAP_PCT",       "3.0"))
MIN_FROM_OPEN_PCT = float(os.getenv("MIN_FROM_OPEN_PCT", "2.0"))
MAX_52W_DIST      = float(os.getenv("MAX_52W_DIST",      "30.0"))
SETUP_RVOL        = float(os.getenv("SETUP_RVOL",        "1.3"))
RVOL_LOOKBACK     = int(os.getenv("RVOL_LOOKBACK",       "20"))

CUSTOM_STRATEGY   = os.getenv("CUSTOM_STRATEGY", "false").lower() == "true"
STRATEGY_FILE     = os.getenv("STRATEGY_FILE", "strategies/tri_city_strategy.py")

# Market hours guard
MARKET_OPEN_HOUR    = 9
MARKET_OPEN_MINUTE  = 30
MARKET_CLOSE_HOUR   = 16
MARKET_CLOSE_MINUTE = 0

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_cache: dict = {}
CACHE_TTL_MIN = 15


# ── Strategy loader ────────────────────────────────────────────────────────────

def load_strategy():
    """
    Load the active strategy module.
    Default: strategies/tri_city_strategy.py
    Custom:  path set via STRATEGY_FILE in .env
    """
    if CUSTOM_STRATEGY:
        strategy_path = WORKSPACE / STRATEGY_FILE
        if not strategy_path.exists():
            print(f"ERROR: STRATEGY_FILE not found: {strategy_path}")
            print("Falling back to default Tri-City strategy.")
            strategy_path = WORKSPACE / "strategies" / "tri_city_strategy.py"
    else:
        strategy_path = WORKSPACE / "strategies" / "tri_city_strategy.py"

    spec   = importlib.util.spec_from_file_location("strategy", strategy_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ── Indicator calculations ─────────────────────────────────────────────────────

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
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)


def calc_ema(closes: list[float], period: int = 20) -> float | None:
    if len(closes) < period:
        return None
    k   = 2 / (period + 1)
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
        snaps = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbols))
        return {sym: snaps[sym] for sym in symbols if sym in snaps}
    except Exception as e:
        logger.error(f"fetch_snapshots: {e}")
        return {}


def fetch_intraday_bars(symbol: str, minutes_back: int = 120) -> list[float]:
    """Fetch 5-min intraday closes using yfinance (free, no SIP subscription needed)."""
    try:
        import yfinance as yf
        now = datetime.now(CT)
        start = (now - timedelta(minutes=minutes_back)).strftime("%Y-%m-%d %H:%M:%S")
        df = yf.download(
            symbol,
            start=start,
            interval="5m",
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            return []
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        return list(df["close"].dropna())
    except Exception as e:
        logger.warning(f"fetch_intraday_bars {symbol}: {e}")
        return []


def fetch_daily_closes(symbol: str, days: int = 60) -> list[float]:
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
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=now - timedelta(days=days + 10),
            end=now,
            limit=days,
        ))
        df = bars.df
        closes = list(df["close"]) if not df.empty else []
        _cache[cache_key] = {"data": closes, "ts": datetime.now(CT)}
        return closes
    except Exception as e:
        logger.warning(f"fetch_daily_closes {symbol}: {e}")
        return []


def fetch_avg_volume(symbol: str) -> float | None:
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
        bars = client.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Day,
            start=now - timedelta(days=RVOL_LOOKBACK * 2),
            end=now.replace(hour=0, minute=0, second=0),
            limit=RVOL_LOOKBACK,
        ))
        df = bars.df
        if df.empty:
            return None
        avg = sum(list(df["volume"])[-RVOL_LOOKBACK:]) / RVOL_LOOKBACK
        _cache[cache_key] = {"data": avg, "ts": datetime.now(CT)}
        return avg
    except Exception as e:
        logger.warning(f"fetch_avg_volume {symbol}: {e}")
        return None


def get_spy_regime() -> tuple[str, float | None]:
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
        if not s or not s.daily_bar or not s.previous_daily_bar:
            return "UNKNOWN", None
        prev = float(s.previous_daily_bar.close)
        curr = float(s.daily_bar.close)
        chg  = round((curr - prev) / prev * 100, 2)
        regime = "BEAR" if chg <= -1.5 else ("BULL" if chg >= 0.5 else "NEUTRAL")
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
    client = get_data_client()
    if not client:
        return None
    try:
        from alpaca.data.requests import StockSnapshotRequest
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols="SPY"))
        s = snap.get("SPY")
        if not s or not s.previous_daily_bar:
            return None
        prev = float(s.previous_daily_bar.close)
        curr = float(s.latest_trade.price)
        return round((curr - prev) / prev * 100, 2)
    except Exception:
        return None


def calc_rvol(symbol: str, today_vol: float) -> float | None:
    avg_vol = fetch_avg_volume(symbol)
    if not avg_vol or avg_vol == 0:
        return None
    now      = datetime.now(CT)
    open_ct  = now.replace(hour=8, minute=30, second=0, microsecond=0)
    close_ct = now.replace(hour=15, minute=0, second=0, microsecond=0)
    fraction = min(1.0, max(60, (now - open_ct).total_seconds()) /
                   (close_ct - open_ct).total_seconds())
    expected = avg_vol * fraction
    return round(today_vol / expected, 2) if expected > 0 else None


# ── Symbol data builder ────────────────────────────────────────────────────────

def build_symbol_data(sym: str, snap, spy_chg: float | None) -> dict | None:
    """
    Fetch all data for one symbol and return a standardized dict
    for the strategy to evaluate. Returns None if data is insufficient.
    """
    try:
        curr_price = float(snap.latest_trade.price)
        prev_close = float(snap.prev_daily_bar.close)
        open_price = float(snap.daily_bar.open)
        vwap       = float(snap.daily_bar.vwap)
        today_vol  = float(snap.daily_bar.volume)
    except (AttributeError, TypeError):
        return None

    gap_pct     = round((open_price - prev_close) / prev_close * 100, 2)
    from_open   = round((curr_price - open_price) / open_price * 100, 2)
    momentum_ok = gap_pct >= MIN_GAP_PCT or from_open >= MIN_FROM_OPEN_PCT
    above_vwap  = curr_price > vwap

    rvol    = calc_rvol(sym, today_vol)
    rvol_ok = rvol is not None and rvol >= MIN_RVOL

    # Skip bar fetch if hard fails already present
    if not (momentum_ok and above_vwap):
        return None

    bars  = fetch_intraday_bars(sym, minutes_back=150)
    if not bars:
        return None

    rsi   = calc_rsi(bars, 14)
    ema20 = calc_ema(bars, 20)
    sma50 = calc_sma(bars, min(50, len(bars)))

    above_ema20 = ema20 is not None and curr_price > ema20
    conv_gap    = abs(curr_price - (sma50 or curr_price)) / curr_price * 100 if sma50 else 999
    is_conv     = sma50 is not None and conv_gap < 2.0 and curr_price > sma50

    daily_closes = fetch_daily_closes(sym, days=260)
    high_52w     = (max(daily_closes[-252:]) if len(daily_closes) >= 252
                    else (max(daily_closes) if daily_closes else curr_price))
    dist_52w     = round((high_52w - curr_price) / high_52w * 100, 2)
    near_high    = dist_52w <= MAX_52W_DIST

    stock_chg = round((curr_price - prev_close) / prev_close * 100, 2)
    rs_vs_spy  = spy_chg is not None and stock_chg > spy_chg

    return {
        "symbol":      sym,
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
        "above_vwap":  above_vwap,
        "above_ema20": above_ema20,
        "momentum_ok": momentum_ok,
        "near_high":   near_high,
        "rvol_ok":     rvol_ok,
    }


# ── Watchlist loader ───────────────────────────────────────────────────────────

def load_watchlist() -> list[str]:
    """
    Build scan list from today's scanner candidates (primary) +
    a small permanent base list (SPY, QQQ for regime context).
    The static default-watchlist.txt is NOT used — the scanner
    generates a fresh list every morning from TradingView hotlists.
    """
    symbols = set()

    # Today's scanner candidates (primary source)
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

    # Permanent base — SPY/QQQ for regime checks always included
    for sym in ["SPY", "QQQ"]:
        symbols.add(sym)

    # Fallback: if scanner hasn't run yet today, use static list
    if len(symbols) <= 2:
        wl = WORKSPACE / "watchlists" / "default-watchlist.txt"
        if wl.exists():
            for line in wl.read_text().splitlines():
                sym = line.strip().upper()
                if sym and not sym.startswith("#"):
                    symbols.add(sym)

    return sorted(symbols)


# ── Execute signal ─────────────────────────────────────────────────────────────

def fire_execute(data: dict, signal_type: str, dry_run: bool = False) -> str:
    cmd = [
        sys.executable, "-W", "ignore",
        str(WORKSPACE / "scripts" / "tri_city_execute.py"),
        "--symbol", data["symbol"],
        "--price",  str(data["price"]),
        "--rsi",    str(data["rsi"] or 0),
        "--rvol",   str(data["rvol"] or 0),
        "--signal", signal_type,
        "--setup",  signal_type,
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


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Tri-City Signal Monitor")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print signals without placing orders")
    args = parser.parse_args()

    now = datetime.now(CT)

    # Market hours guard
    market_open  = now.replace(hour=MARKET_OPEN_HOUR,  minute=MARKET_OPEN_MINUTE,  second=0, microsecond=0)
    market_close = now.replace(hour=MARKET_CLOSE_HOUR, minute=MARKET_CLOSE_MINUTE, second=0, microsecond=0)
    if now < market_open or now >= market_close:
        return

    # Load strategy
    strategy = load_strategy()
    strategy_name = getattr(strategy, "STRATEGY_NAME", "Custom Strategy")

    # SPY regime
    spy_regime, spy_chg_daily = get_spy_regime()
    spy_chg = get_spy_change()

    if spy_regime == "BEAR":
        print(f"[{now.strftime('%H:%M CT')}] MARKET REGIME: BEAR "
              f"(SPY {spy_chg_daily:+.2f}%) — no new entries.")
        return

    symbols = load_watchlist()
    if not symbols:
        return

    snaps = fetch_snapshots(symbols)
    if not snaps:
        return

    for sym in symbols:
        snap = snaps.get(sym)
        if not snap:
            continue

        data = build_symbol_data(sym, snap, spy_chg)
        if not data:
            continue

        signal_type = strategy.classify_signal(data)
        if not signal_type:
            continue

        ts = now.strftime("%H:%M CT")

        if signal_type == "SETUP":
            print(f"\n[{ts}] ⚠️  SETUP — {sym}  [{strategy_name}]")
            print(f"   Price: ${data['price']:.2f} | RSI: {data['rsi']} | "
                  f"RVol: {data['rvol']:.1f}x | Gap: {data['gap_pct']:+.1f}%")
            print(f"   Watching for entry signal...")
        else:
            label = "💎 CONV BREAKOUT" if signal_type == "CONV" else "🚀 ENTER"
            print(f"\n[{ts}] {label} — {sym}  [{strategy_name}]")
            print(f"   Price:    ${data['price']:.2f}")
            print(f"   VWAP:     ${data['vwap']:.2f} ({'above' if data['above_vwap'] else 'below'})")
            if data["ema20"]:
                print(f"   EMA20:    ${data['ema20']:.2f}")
            print(f"   RSI:      {data['rsi']}")
            print(f"   RVol:     {data['rvol']:.1f}x")
            print(f"   Gap:      {data['gap_pct']:+.1f}%  |  From Open: {data['from_open']:+.1f}%")
            print(f"   vs SPY:   {'Outperforming' if data['rs_vs_spy'] else 'Underperforming'}")
            print(f"   52W Dist: {data['dist_52w']:.1f}%")
            print()

            output = fire_execute(data, signal_type, dry_run=args.dry_run)
            if output:
                print(output)

    # Run position manager
    try:
        pm = subprocess.run(
            [sys.executable, "-W", "ignore",
             str(WORKSPACE / "scripts" / "tri_city_position_manager.py")],
            capture_output=True, text=True, timeout=30
        )
        if pm.stdout.strip():
            print(pm.stdout.strip())
    except Exception:
        pass


if __name__ == "__main__":
    main()
