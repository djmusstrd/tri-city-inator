#!/usr/bin/env python3
"""
TRI-CITY SIGNAL DETECTOR — Parses the live Pine scanner table and outputs
qualifying BREAKOUT / CONTINUATION / PULLBACK setups + RVOL spikes.

Replaces the inline detection logic that previously lived in CLAUDE.md,
cutting ~2,500 characters from the signal-monitor cron description and
reducing per-cycle context accumulation by ~87%.

Reads:
  shared/tri-city-table.json      — Pine table rows written by Claude each cycle
  shared/tri-city-flags.json      — htf + resistance arrays (written by scanners)
  shared/tri-city-rvol-state.json — previous-cycle RVOL per symbol

Writes:
  shared/tri-city-signals.json    — qualifying signals + RVOL spikes
  shared/tri-city-rvol-state.json — updated RVOL snapshot

Output schema (shared/tri-city-signals.json):
{
  "timestamp": "HH:MM CT",
  "signals": [
    {
      "symbol":     str,    # e.g. "HTT"
      "setup":      str,    # "BREAKOUT" | "CONTINUATION" | "PULLBACK"
      "price":      float,
      "orh":        float,
      "orl":        float,
      "rsi":        float,
      "ema_dev":    float,  # e.g. 0.08  (percent, not fraction)
      "rvol":       float,
      "cup":        bool,
      "htf":        bool,
      "resistance": bool
    }
  ],
  "rvol_spikes": [
    {"symbol": str, "prev": float, "now": float}
  ]
}

Usage:
  python -W ignore scripts/tri_city_signal_detector.py
  python -W ignore scripts/tri_city_signal_detector.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE    = Path.home() / "tri-city-inator"
SHARED       = WORKSPACE / "shared"
TABLE_FILE   = SHARED / "tri-city-table.json"
FLAGS_FILE   = SHARED / "tri-city-flags.json"
RVOL_FILE    = SHARED / "tri-city-rvol-state.json"
VWAP_FILE    = SHARED / "tri-city-vwap-state.json"
SIG_FILE     = SHARED / "tri-city-signals.json"
LEVELS_FILE  = SHARED / "tri-city-levels.json"

CT = ZoneInfo("America/Chicago")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)
# Silence noisy third-party loggers that spam stderr every cycle
for _noisy in ("yfinance", "peewee", "urllib3", "requests"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

# ── Setup thresholds (mirror CLAUDE.md) ──────────────────────────────────────

PULLBACK_EMA_MAX  = 1.2   # EMA Dev% upper bound for PULLBACK (raised from 0.8 — Pine already filters)
CONT_EMA_MAX      = 1.5   # EMA Dev% upper bound for CONTINUATION (raised from 1.0)
PULLBACK_RSI_MIN  = 38
PULLBACK_RSI_MAX  = 65    # raised from 62 — looser guard, valid pullback zone is wider

# Locked-level detection thresholds (Pine-independent)
# Used when Pine shows "---" because its live ORH chases price up.
# On green SPY days, RSI is naturally elevated; locked-level checks use wider RSI bands.
LOCKED_PULLBACK_RSI_MAX = 65  # wider than Pine's 55 ceiling — valid on green-day pullbacks
LOCKED_CONT_RSI_MAX     = 68  # continuation on elevated momentum days

# MACD valid after this many minutes into session (26 × 5-min bars = 130 min)
MACD_VALID_AFTER_MIN = 130   # ~10:30 AM CT
BREAKOUT_RSI_MIN  = 50

RVOL_SPIKE_THRESH     = 0.50   # ≥50% increase triggers alert
RVOL_SPIKE_MIN        = 2.0    # must be ≥2.0x after spike

# VWAP settings (Aziz methodology)
VWAP_PROXIMITY_PCT    = 0.003  # 0.3% — price within this % of VWAP = "near VWAP"
VWAP_STOP_OFFSET      = 0.05   # 5 cents below VWAP for VWAP-reclaim stops

# ── EMA20 Pullback signal thresholds ─────────────────────────────────────────
# Fires when: stock had a significant run, pulled back to EMA20, holds above VWAP
# Based on CODX/ONDS/PONY pattern analysis — multiple entries per day available
EMA20_PB_EMA_MIN   = -0.5   # EMA Dev% lower bound (at or just below EMA20)
EMA20_PB_EMA_MAX   =  1.5   # EMA Dev% upper bound (at or just above EMA20)
EMA20_PB_RSI_MIN   =  45    # RSI cooled from overbought
EMA20_PB_RSI_MAX   =  70    # RSI not reversed (still bullish context)
EMA20_PB_MIN_RUN   =  5.0   # minimum % move from open to confirm a real run
EMA20_PB_MIN_RVOL  =  0.8   # sustained volume (bar-level, not just opening spike)
EMA20_PB_ORH_MULT  =  1.02  # price must be at least 2% above ORH (was 1.05 — missed RCAT bounce at 13.20 when locked ORH=12.85)
EMA20_PB_START_H   =  8     # earliest CT hour for this signal
EMA20_PB_START_M   = 35     # earliest CT minute (8:35 AM — 5 min after ORB with ORB_MINUTES=5)
EMA20_PB_END_H     = 11     # latest CT hour
EMA20_PB_END_M     = 30     # latest CT minute (11:30 AM, before lunch)

# ── BREAKOUT quality filters ──────────────────────────────────────────────────
BREAKOUT_MAX_ORH_ORL_SPREAD = 8.0  # % of price — wide spread = indecision, skip

# ── Symbols that should skip yfinance VWAP/data fetches ───────────────────────
# SPACs and units (e.g. QETAU, ASPCU) cause yfinance "possibly delisted" errors
# every cycle because yfinance cannot fetch price data for SPAC unit tickers.
# These symbols are still tracked by the scanner table for signal purposes, but
# we skip the yfinance data fetch silently to avoid noisy error output.
SKIP_VWAP_SYMBOLS: set[str] = {
    "QETAU", "ASPCU",
}


# ── Table row parser ──────────────────────────────────────────────────────────

def _pct(s: str) -> float:
    """Parse '1.88%' or '-0.39%' → float 1.88 / -0.39."""
    return float(s.replace("%", "").strip())


def _rvol(s: str) -> float:
    """Parse '2.7x' or '3.1x' → float."""
    return float(s.replace("x", "").strip())


def _orh_orl(s: str) -> tuple[float, float]:
    """Parse '8.92/8.79' → (8.92, 8.79). Returns (0.0, 0.0) on failure."""
    try:
        parts = s.split("/")
        return float(parts[0].strip()), float(parts[1].strip())
    except Exception:
        return 0.0, 0.0


def parse_table_rows(rows: list[str]) -> list[dict]:
    """
    Parse Pine scanner table rows into structured dicts.
    Skips the header row and any blank (---) rows.

    Expected columns (by index):
      0:SYMBOL  1:PRICE  2:RSI  3:EMA DEV%  4:RVOL  5:ORH/ORL  6:CUP  7:SMA↑  8:SIGNAL
    """
    results = []
    for row in rows:
        if "SYMBOL" in row and "PRICE" in row:
            continue  # header
        cols = [c.strip() for c in row.split("|")]
        if len(cols) < 9:
            continue
        symbol = cols[0]
        if symbol in ("---", "", "SYMBOL"):
            continue
        try:
            price   = float(cols[1])
            rsi     = float(cols[2])
            ema_dev = _pct(cols[3])
            rvol    = _rvol(cols[4])
            orh, orl = _orh_orl(cols[5])
            cup     = cols[6].upper() == "YES"
            signal  = cols[8].strip()
        except (ValueError, IndexError) as e:
            logger.debug(f"Row parse error ({symbol}): {e}")
            continue
        results.append({
            "symbol":  symbol,
            "price":   price,
            "rsi":     rsi,
            "ema_dev": ema_dev,
            "rvol":    rvol,
            "orh":     orh,
            "orl":     orl,
            "cup":     cup,
            "signal":  signal,
        })
    return results


# ── MACD confirmation (Ch 10, Listing 10.5 — Papp) ───────────────────────────
# Used to filter CONTINUATION signals: require MACD line > Signal line.
# Only valid after MACD_VALID_AFTER_MIN minutes into session (needs 26 5-min bars).
# Returns True  = MACD bullish (allow CONTINUATION)
#         False = MACD bearish (block CONTINUATION)
#         None  = data unavailable or too early (allow — never block on missing data)

_macd_cache: dict[str, tuple[datetime, bool | None]] = {}  # symbol → (fetched_at, result)
_MACD_CACHE_TTL = 180  # seconds — refresh every 3 min (matches signal monitor cycle)


def _ema_series(values: list[float], period: int) -> list[float]:
    """Compute EMA over a list of floats. Returns list same length as input."""
    k = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def macd_is_bullish(symbol: str, now: datetime) -> bool | None:
    """
    Fetch 5-min bars via yfinance and compute MACD(12,26,9).
    Returns True if MACD line > Signal line (bullish momentum).
    Returns None if before MACD_VALID_AFTER_MIN or data unavailable (no block).
    Caches result for _MACD_CACHE_TTL seconds to avoid redundant fetches.
    """
    # Check cache
    if symbol in _macd_cache:
        fetched_at, cached_result = _macd_cache[symbol]
        if (now - fetched_at).total_seconds() < _MACD_CACHE_TTL:
            return cached_result

    if symbol in SKIP_VWAP_SYMBOLS:
        return None

    # Only valid after 130 min into session
    session_open = now.replace(hour=8, minute=30, second=0, microsecond=0)
    elapsed_min = (now - session_open).total_seconds() / 60
    if elapsed_min < MACD_VALID_AFTER_MIN:
        return None  # too early — skip filter

    try:
        import yfinance as yf
        df = yf.download(symbol, period="1d", interval="2m", progress=False, auto_adjust=True)
        if df.empty or len(df) < 35:
            _macd_cache[symbol] = (now, None)
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        closes = list(df["close"].dropna())
        if len(closes) < 35:
            _macd_cache[symbol] = (now, None)
            return None

        ema12 = _ema_series(closes, 12)
        ema26 = _ema_series(closes, 26)
        macd_line = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        signal_line = _ema_series(macd_line, 9)

        bullish = macd_line[-1] > signal_line[-1]
        _macd_cache[symbol] = (now, bullish)
        return bullish
    except Exception as e:
        logger.debug(f"macd_is_bullish {symbol}: {e}")
        _macd_cache[symbol] = (now, None)
        return None


# ── EMA Ribbon Spread (Ch 10, Listing 10.3 — Papp) ──────────────────────────
# Compares EMA10/EMA20 spread on 5-min bars: today's last spread vs yesterday's.
# Expanding spread → momentum building → lower RSI threshold for BREAKOUT.
# Compressing spread → momentum fading → tighter EMA dev window for CONTINUATION.
# Returns "EXPANDING" | "COMPRESSING" | None (unavailable — no change to rules).
# Caches per symbol for _MACD_CACHE_TTL seconds (shared with MACD cycle time).

_ribbon_cache: dict[str, tuple[datetime, str | None]] = {}


def ema_ribbon_trend(symbol: str, now: datetime) -> str | None:
    """
    Fetch 2 days of 5-min bars, compute EMA10 and EMA20.
    Return "EXPANDING" if today's EMA10-EMA20 spread > yesterday's last spread.
    Return "COMPRESSING" if today's spread < yesterday's last spread.
    Return None for symbols in SKIP_VWAP_SYMBOLS.
    Return None if data unavailable (never blocks or forces a signal).
    """
    if symbol in SKIP_VWAP_SYMBOLS:
        return None

    if symbol in _ribbon_cache:
        fetched_at, cached = _ribbon_cache[symbol]
        if (now - fetched_at).total_seconds() < _MACD_CACHE_TTL:
            return cached

    try:
        import yfinance as yf
        df = yf.download(symbol, period="2d", interval="2m", progress=False, auto_adjust=True)
        if df.empty or len(df) < 22:
            _ribbon_cache[symbol] = (now, None)
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        closes = list(df["close"].dropna())
        if len(closes) < 22:
            _ribbon_cache[symbol] = (now, None)
            return None

        ema10 = _ema_series(closes, 10)
        ema20 = _ema_series(closes, 20)
        spreads = [abs(e10 - e20) for e10, e20 in zip(ema10, ema20)]

        # Need at least one yesterday bar and one today bar
        today_open = now.replace(hour=8, minute=30, second=0, microsecond=0)
        import pandas as pd
        timestamps = list(df.index)
        today_idx = [i for i, t in enumerate(timestamps)
                     if pd.Timestamp(t).tz_convert("America/Chicago") >= today_open]
        if not today_idx or today_idx[0] == 0:
            _ribbon_cache[symbol] = (now, None)
            return None

        yesterday_last = spreads[today_idx[0] - 1]
        today_last = spreads[-1]

        trend = "EXPANDING" if today_last > yesterday_last else "COMPRESSING"
        _ribbon_cache[symbol] = (now, trend)
        return trend
    except Exception as e:
        logger.debug(f"ema_ribbon_trend {symbol}: {e}")
        _ribbon_cache[symbol] = (now, None)
        return None


# ── VWAP + bar data (Aziz methodology) ───────────────────────────────────────
# VWAP = cumsum(typical_price × volume) / cumsum(volume), reset each session.
# Used to:
#   1. Block BREAKOUT/CONTINUATION when price < VWAP (no longs below VWAP)
#   2. Flag VWAP_RECLAIM when price crossed above VWAP since the previous cycle
#   3. Provide a tighter VWAP-based stop for reclaim entries (5¢ below VWAP)
#   4. Supply last-bar OHLC for candlestick quality filters (EMA20_PULLBACK)
#
# Fetched for Pine-signaled rows AND EMA20_PULLBACK basic candidates.
# Previous-cycle price+VWAP stored in tri-city-vwap-state.json for reclaim detection.

def fetch_vwap_data(symbol: str, now: datetime) -> dict | None:
    """
    Compute intraday VWAP from yfinance 5-min bars. Also returns:
      last_bar: {open, high, low, close} — most recent completed 5-min bar
      change_from_open_pct: % move from first regular-session bar open to last close

    Returns None on any failure — callers must never block on missing data.
    """
    try:
        import yfinance as yf
        import pandas as pd

        df = yf.download(symbol, period="1d", interval="2m", progress=False, auto_adjust=True)
        if df.empty:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

        df.index = pd.to_datetime(df.index)
        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        ct_index = df.index.tz_convert("America/Chicago")

        # Today's bars (all hours including premarket)
        today_str = now.strftime("%Y-%m-%d")
        df_today = df[ct_index.strftime("%Y-%m-%d") == today_str]
        if df_today.empty:
            df_today = df

        h = df_today["high"].astype(float).values
        l = df_today["low"].astype(float).values
        c = df_today["close"].astype(float).values
        v = df_today["volume"].astype(float).values
        o = df_today["open"].astype(float).values

        if v.sum() == 0:
            return None

        tp   = (h + l + c) / 3.0
        vwap = float((tp * v).cumsum()[-1] / v.cumsum()[-1])

        # Last completed bar
        last_bar = {
            "open":  round(float(o[-1]), 4),
            "high":  round(float(h[-1]), 4),
            "low":   round(float(l[-1]), 4),
            "close": round(float(c[-1]), 4),
        }

        # Change from first regular-session bar open (8:30 CT)
        session_open = now.replace(hour=8, minute=30, second=0, microsecond=0)
        ct_today = ct_index[ct_index.strftime("%Y-%m-%d") == today_str]
        session_bars = [i for i, t in enumerate(ct_today) if t >= session_open]
        if session_bars:
            first_open = float(df_today["open"].iloc[session_bars[0]])
            last_close = float(c[-1])
            change_pct = (last_close - first_open) / first_open * 100 if first_open > 0 else 0.0
        else:
            change_pct = 0.0

        return {
            "vwap":                round(vwap, 4),
            "last_bar":            last_bar,
            "change_from_open_pct": round(change_pct, 2),
        }
    except Exception as e:
        logger.debug(f"fetch_vwap_data {symbol}: {e}")
        return None


# ── Candlestick quality filters ───────────────────────────────────────────────

def is_rejection_bar(bar: dict) -> bool:
    """
    Shooting star / spike bar: long upper wick, close near the low.
    Identifies failed breakout bars (AEHL, PCLA, FUTG pattern).
    Blocks BREAKOUT signals when the most recent bar looks like distribution.
    """
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    bar_range = h - l
    if bar_range < 0.01:
        return False
    upper_wick = h - max(o, c)
    return (
        upper_wick / bar_range > 0.50          # upper wick > half the range
        and c < (l + bar_range * 0.35)         # close in bottom 35% of bar
    )


def is_bounce_bar(bar: dict) -> bool:
    """
    Hammer / dragonfly: bullish close, lower wick present, real body.
    Confirms EMA20 pullback entries (CODX/ONDS/PONY pattern).
    Price tested EMA20 support and buyers stepped in.
    """
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
    bar_range = h - l
    if bar_range < 0.01:
        return True   # no data = don't block
    lower_wick = min(o, c) - l
    body       = abs(c - o)
    return (
        c > o                                  # bullish close
        and lower_wick / bar_range > 0.20      # tested lower, rejected
        and body / bar_range > 0.25            # real body, not a doji
    )


# ── Setup detection ───────────────────────────────────────────────────────────

def detect_setup(row: dict, now: datetime | None = None,
                  vwap_data: dict | None = None,
                  locked_orh: float = 0.0, locked_orl: float = 0.0) -> str | None:
    """
    Apply setup rules in priority order.
    Returns "BREAKOUT" | "CONTINUATION" | "PULLBACK" | "EMA20_PULLBACK" | None.

    CONTINUATION:    requires MACD line > Signal line (Ch 10, Papp).
    BREAKOUT:        EMA ribbon expanding → RSI threshold lowered by 5 pts.
                     ORH/ORL spread > 8% of price → blocked (wide range = indecision).
                     Rejection bar (shooting star) on last 5-min bar → blocked.
    CONTINUATION:    EMA ribbon compressing → EMA dev window tightened to 0.5%.
    EMA20_PULLBACK:  New signal. Price at EMA20 after a run, above rising VWAP,
                     RSI cooled 45–68. Bounce bar confirms. 9:15–11:30 CT only.

    VWAP filter (Aziz): BREAKOUT and CONTINUATION blocked when price < VWAP.
    PULLBACK is not blocked (a dip to VWAP is the Aziz entry zone).
    EMA20_PULLBACK requires price > VWAP (EMA20 > VWAP = bullish ribbon).
    All VWAP/bar checks are optional — never block when data unavailable.
    """
    sig      = row["signal"]
    price    = row["price"]
    orh      = row["orh"]
    orl      = row["orl"]
    rsi      = row["rsi"]
    ema_dev  = row["ema_dev"]
    rvol     = row["rvol"]
    now      = now or datetime.now(CT)

    # Unpack VWAP data
    vwap             = vwap_data["vwap"]             if vwap_data else None
    last_bar         = vwap_data.get("last_bar")     if vwap_data else None
    change_from_open = vwap_data.get("change_from_open_pct", 0.0) if vwap_data else 0.0

    # VWAP guard: no BREAKOUT or CONTINUATION longs below VWAP (Aziz)
    if vwap is not None and vwap > 0 and price < vwap:
        if sig in ("BREAKOUT", "CONTINUATION"):
            logger.info(
                f"VWAP block: {row['symbol']} ${price:.2f} below VWAP ${vwap:.2f} "
                f"— skipping {sig}"
            )
            return None

    above_orh = orh > 0 and price > orh
    ribbon    = ema_ribbon_trend(row["symbol"], now)

    # SETUP 1: BREAKOUT
    # EMA ribbon expanding → momentum confirmed → allow RSI as low as 45 (vs 50 default)
    breakout_rsi_min = BREAKOUT_RSI_MIN - 5 if ribbon == "EXPANDING" else BREAKOUT_RSI_MIN
    if sig == "BREAKOUT" and above_orh and rsi > breakout_rsi_min and ema_dev > 0:
        # ORH/ORL spread filter: wide range = uncertain direction (FUTG pattern)
        if orh > 0 and orl > 0 and price > 0:
            spread_pct = (orh - orl) / price * 100
            if spread_pct > BREAKOUT_MAX_ORH_ORL_SPREAD:
                logger.info(
                    f"BREAKOUT {row['symbol']} blocked: ORH/ORL spread "
                    f"{spread_pct:.1f}% > {BREAKOUT_MAX_ORH_ORL_SPREAD}%"
                )
                return None
        # Rejection bar filter: spike-and-reverse pattern on last bar
        if last_bar and is_rejection_bar(last_bar):
            logger.info(f"BREAKOUT {row['symbol']} blocked: rejection/spike bar on last 5-min bar")
            return None
        return "BREAKOUT"

    # SETUP 2: CONTINUATION — MACD + ribbon checks
    # EMA ribbon compressing → tighten EMA dev window to 0–0.5% (vs 0–1.0% default)
    cont_ema_max = 0.5 if ribbon == "COMPRESSING" else CONT_EMA_MAX
    if (sig == "CONTINUATION"
            and above_orh
            and 0 <= ema_dev <= cont_ema_max):
        macd_ok = macd_is_bullish(row["symbol"], now)
        if macd_ok is False:
            logger.info(f"CONTINUATION {row['symbol']} blocked: MACD bearish")
            return None
        return "CONTINUATION"

    # SETUP 3: PULLBACK
    if (sig == "PULLBACK"
            and 0 <= ema_dev <= PULLBACK_EMA_MAX
            and PULLBACK_RSI_MIN <= rsi <= PULLBACK_RSI_MAX):
        return "PULLBACK"

    # SETUP 4: EMA20_PULLBACK — price at EMA20 after a significant run, above VWAP
    # No time window — valid throughout the day wherever the pattern appears
    if (EMA20_PB_EMA_MIN <= ema_dev <= EMA20_PB_EMA_MAX
            and vwap is not None and price > vwap
            and EMA20_PB_RSI_MIN <= rsi <= EMA20_PB_RSI_MAX
            and rvol >= EMA20_PB_MIN_RVOL
            and change_from_open >= EMA20_PB_MIN_RUN
            and (orh <= 0 or price >= orh * EMA20_PB_ORH_MULT)):
        if last_bar and not is_bounce_bar(last_bar):
            logger.info(f"EMA20_PULLBACK {row['symbol']}: no bounce bar — entering anyway")
        return "EMA20_PULLBACK"

    # ── Locked-level detection (Setups 5–7) ─────────────────────────────────
    # Pine's live ORH chases price upward throughout the session, so price is
    # never "above ORH" for BREAKOUT/CONTINUATION in Pine's own logic.
    # These checks use the ORH/ORL locked at the end of the ORB window
    # (loaded from tri-city-levels.json) to detect setups independently of
    # Pine's SIGNAL column. Only fires when sig == "---" (Pine didn't signal).

    if sig not in ("---", "") or locked_orh <= 0:
        return None

    above_locked_orh = price > locked_orh

    # SETUP 5: LOCKED-LEVEL CONTINUATION (checked BEFORE BREAKOUT)
    # Tight EMA dev means price is consolidating above locked ORH — cleaner signal
    # than a wide-EMA breakout. Order matters: tight-dev setups should be CONT, not BO.
    if (above_locked_orh
            and 0 <= ema_dev <= CONT_EMA_MAX
            and 50 <= rsi <= LOCKED_CONT_RSI_MAX):
        if vwap is not None and vwap > 0 and price < vwap:
            return None  # VWAP guard
        macd_ok = macd_is_bullish(row["symbol"], now)
        if macd_ok is False:
            logger.info(f"LOCKED CONT {row['symbol']} blocked: MACD bearish")
            return None
        return "CONTINUATION"

    # SETUP 6: LOCKED-LEVEL BREAKOUT
    # Price above locked ORH with extended EMA dev (>CONT_EMA_MAX) = fresh momentum.
    # Guards: not reversing from open (change_from_open > -5%), no rejection bar,
    # ORH/ORL spread within bounds, price above VWAP.
    if (above_locked_orh
            and ema_dev > CONT_EMA_MAX          # tight EMA dev = CONTINUATION (handled above)
            and rsi > BREAKOUT_RSI_MIN
            and change_from_open > -5.0):        # skip if stock is reversing hard from open
        if vwap is not None and vwap > 0 and price < vwap:
            return None  # VWAP guard
        if locked_orl > 0:
            spread_pct = (locked_orh - locked_orl) / price * 100
            if spread_pct > BREAKOUT_MAX_ORH_ORL_SPREAD:
                logger.info(f"LOCKED BREAKOUT {row['symbol']} blocked: spread {spread_pct:.1f}%")
                return None
        if last_bar and is_rejection_bar(last_bar):
            logger.info(f"LOCKED BREAKOUT {row['symbol']} blocked: rejection bar")
            return None
        return "BREAKOUT"

    # SETUP 7: LOCKED-LEVEL PULLBACK
    # Price inside the opening range (above ORL, below ORH), near EMA.
    # RSI ceiling raised to 65 — green SPY days naturally push RSI above Pine's 55 max.
    if (locked_orl > 0
            and locked_orl <= price <= locked_orh
            and 0 <= ema_dev <= PULLBACK_EMA_MAX
            and PULLBACK_RSI_MIN <= rsi <= LOCKED_PULLBACK_RSI_MAX):
        if change_from_open >= 25.0:
            pm_cutoff = now.replace(hour=11, minute=30, second=0, microsecond=0)
            if now >= pm_cutoff:
                return None
        return "PULLBACK"

    return None


# ── RVOL spike detection ──────────────────────────────────────────────────────

def detect_rvol_spikes(rows: list[dict], prev_state: dict) -> list[dict]:
    """
    Compare current RVOL to previous cycle snapshot.
    Alert if increase ≥ RVOL_SPIKE_THRESH AND current ≥ RVOL_SPIKE_MIN.
    """
    spikes = []
    for row in rows:
        sym  = row["symbol"]
        now  = row["rvol"]
        prev = prev_state.get(sym, 0.0)
        if prev > 0 and now >= RVOL_SPIKE_MIN:
            increase = (now - prev) / prev
            if increase >= RVOL_SPIKE_THRESH:
                spikes.append({"symbol": sym, "prev": prev, "now": now})
    return spikes


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City Signal Detector")
    parser.add_argument("--dry-run", action="store_true",
                        help="Detect and print signals without writing output files")
    args = parser.parse_args()

    now_ct = datetime.now(CT)

    # ── Load table ──────────────────────────────────────────────────────────
    if not TABLE_FILE.exists():
        print(f"DETECTOR: tri-city-table.json not found — skipping cycle", file=sys.stderr)
        _write_empty(now_ct, args.dry_run)
        return

    try:
        table_data = json.loads(TABLE_FILE.read_text())
        rows_raw: list[str] = (
            table_data if isinstance(table_data, list)
            else table_data.get("rows", [])
        )
    except Exception as e:
        print(f"DETECTOR: failed to load table: {e}", file=sys.stderr)
        _write_empty(now_ct, args.dry_run)
        return

    rows = parse_table_rows(rows_raw)
    if not rows:
        _write_empty(now_ct, args.dry_run)
        return

    # ── Load flags (htf / resistance / bb_squeeze) ──────────────────────────
    htf_set        : set[str] = set()
    resistance_set : set[str] = set()
    bb_squeeze_set : set[str] = set()
    if FLAGS_FILE.exists():
        try:
            flags          = json.loads(FLAGS_FILE.read_text())
            htf_set        = set(flags.get("htf", []))
            resistance_set = set(flags.get("resistance", []))
            bb_squeeze_set = set(flags.get("bb_squeeze", []))
        except Exception:
            pass

    # ── Load previous RVOL state ────────────────────────────────────────────
    prev_rvol: dict[str, float] = {}
    if RVOL_FILE.exists():
        try:
            prev_rvol = json.loads(RVOL_FILE.read_text())
        except Exception:
            pass

    # ── Load previous VWAP state (for reclaim detection) ───────────────────
    prev_vwap_state: dict[str, dict] = {}
    if VWAP_FILE.exists():
        try:
            prev_vwap_state = json.loads(VWAP_FILE.read_text())
        except Exception:
            pass

    # ── Load locked ORH/ORL from level lock file ────────────────────────────
    # Pine's live ORH updates as price makes new highs. The locked levels
    # represent the true opening range high/low from the first ORB_MINUTES bars.
    # Reject stale data from a prior session using the "_date" field.
    locked_levels: dict[str, dict] = {}
    if LEVELS_FILE.exists():
        try:
            raw_levels = json.loads(LEVELS_FILE.read_text())
            today_str  = now_ct.strftime("%Y-%m-%d")
            file_date  = raw_levels.get("_date", today_str)  # default: assume today if missing
            if file_date == today_str:
                locked_levels = {k: v for k, v in raw_levels.items() if k != "_date"}
            else:
                logger.warning(f"DETECTOR: tri-city-levels.json is from {file_date}, ignoring stale data")
        except Exception:
            pass

    # ── Identify VWAP fetch candidates ─────────────────────────────────────
    # 1. Pine-signaled rows (existing logic)
    # 2. EMA20_PULLBACK basic candidates: time window + EMA/RSI/RVOL pre-filter
    #    (Pine won't flag these — we detect them independently)
    # 3. Locked-level candidates: symbols with locked ORH that may have live setups
    fetch_syms: set[str] = set(
        r["symbol"] for r in rows if r["signal"] not in ("---", "")
    )
    # EMA20_PULLBACK candidates — no time window, check all rows
    for r in rows:
        if (EMA20_PB_EMA_MIN <= r["ema_dev"] <= EMA20_PB_EMA_MAX
                and EMA20_PB_RSI_MIN <= r["rsi"] <= EMA20_PB_RSI_MAX
                and r["rvol"] >= EMA20_PB_MIN_RVOL):
            fetch_syms.add(r["symbol"])
    # Fetch VWAP for any symbol with a locked level — used by locked-level detection
    for r in rows:
        if r["symbol"] in locked_levels and r["signal"] in ("---", ""):
            fetch_syms.add(r["symbol"])

    vwap_map: dict[str, dict | None] = {}
    for sym in fetch_syms:
        if sym in SKIP_VWAP_SYMBOLS:
            vwap_map[sym] = None  # skip silently — SPAC/unit tickers not in yfinance
            continue
        vwap_map[sym] = fetch_vwap_data(sym, now_ct)

    # ── Detect signals ──────────────────────────────────────────────────────
    signals: list[dict] = []
    new_vwap_state: dict[str, dict] = {}
    for row in rows:
        sym       = row["symbol"]
        vwap_data = vwap_map.get(sym)   # dict | None
        vwap      = vwap_data["vwap"] if vwap_data else None

        lvl = locked_levels.get(sym, {})
        setup = detect_setup(
            row, now_ct, vwap_data=vwap_data,
            locked_orh=lvl.get("orh", 0.0),
            locked_orl=lvl.get("orl", 0.0),
        )
        if setup is None:
            continue

        # VWAP reclaim: prev cycle price was below VWAP, now above
        prev = prev_vwap_state.get(sym, {})
        prev_price = prev.get("price")
        prev_vwap  = prev.get("vwap")
        vwap_reclaim = (
            vwap is not None and vwap > 0
            and prev_price is not None and prev_vwap is not None
            and prev_price < prev_vwap   # was below VWAP last cycle
            and row["price"] >= vwap     # now at or above VWAP
        )

        vwap_above       = (vwap is not None and vwap > 0 and row["price"] >= vwap)
        change_from_open = vwap_data.get("change_from_open_pct", 0.0) if vwap_data else 0.0

        signals.append({
            "symbol":             sym,
            "setup":              setup,
            "price":              row["price"],
            "orh":                row["orh"],
            "orl":                row["orl"],
            "rsi":                row["rsi"],
            "ema_dev":            row["ema_dev"],
            "rvol":               row["rvol"],
            "cup":                row["cup"],
            "htf":                sym in htf_set,
            "resistance":         sym in resistance_set,
            "bb_squeeze":         sym in bb_squeeze_set,
            "vwap":               vwap,
            "vwap_above":         vwap_above,
            "vwap_reclaim":       vwap_reclaim,
            "change_from_open":   change_from_open,
        })

        # Track VWAP state for next cycle's reclaim detection
        if vwap is not None:
            new_vwap_state[sym] = {"price": row["price"], "vwap": vwap}

    # ── Detect RVOL spikes ──────────────────────────────────────────────────
    rvol_spikes = detect_rvol_spikes(rows, prev_rvol)

    # ── Update RVOL state ───────────────────────────────────────────────────
    new_rvol_state = {row["symbol"]: row["rvol"] for row in rows}
    if not args.dry_run:
        RVOL_FILE.write_text(json.dumps(new_rvol_state))
        # Merge new VWAP state into existing (preserve other symbols)
        merged_vwap = {**prev_vwap_state, **new_vwap_state}
        VWAP_FILE.write_text(json.dumps(merged_vwap))

    # ── Write output ────────────────────────────────────────────────────────
    output = {
        "timestamp":   now_ct.strftime("%H:%M CT"),
        "signals":     signals,
        "rvol_spikes": rvol_spikes,
    }

    if args.dry_run:
        print(json.dumps(output, indent=2))
        return

    SHARED.mkdir(parents=True, exist_ok=True)
    SIG_FILE.write_text(json.dumps(output, indent=2))

    # Print summary for Claude context (minimal — just what matters)
    if signals:
        for s in signals:
            cup_tag     = " CUP"           if s["cup"]                    else ""
            htf_tag     = " HTF"           if s["htf"]                    else ""
            res_tag     = " ⚠RES"         if s["resistance"]              else ""
            bb_tag      = " BB✓"          if s.get("bb_squeeze")          else ""
            vwap_tag    = f" VWAP=${s['vwap']:.2f}" if s.get("vwap")      else ""
            reclaim_tag = " VWAP_RECLAIM" if s.get("vwap_reclaim")        else ""
            run_tag     = (f" +{s['change_from_open']:.1f}%fromOpen"
                           if s.get("change_from_open", 0) > 0            else "")
            print(f"SIGNAL: {s['setup']} {s['symbol']} ${s['price']} "
                  f"ORH=${s['orh']} ORL=${s['orl']} "
                  f"RSI={s['rsi']} EMA={s['ema_dev']:+.2f}% "
                  f"RVOL={s['rvol']:.1f}x{cup_tag}{htf_tag}{res_tag}{bb_tag}"
                  f"{vwap_tag}{reclaim_tag}{run_tag}")

    for spike in rvol_spikes:
        print(f"RVOL_SPIKE: {spike['symbol']} {spike['prev']:.1f}x → {spike['now']:.1f}x")


def _write_empty(now_ct: datetime, dry_run: bool):
    output = {"timestamp": now_ct.strftime("%H:%M CT"), "signals": [], "rvol_spikes": []}
    if not dry_run:
        SHARED.mkdir(parents=True, exist_ok=True)
        SIG_FILE.write_text(json.dumps(output))


if __name__ == "__main__":
    main()
