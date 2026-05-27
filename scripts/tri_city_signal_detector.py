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

WORKSPACE  = Path.home() / "tri-city-inator"
SHARED     = WORKSPACE / "shared"
TABLE_FILE = SHARED / "tri-city-table.json"
FLAGS_FILE = SHARED / "tri-city-flags.json"
RVOL_FILE  = SHARED / "tri-city-rvol-state.json"
SIG_FILE   = SHARED / "tri-city-signals.json"

CT = ZoneInfo("America/Chicago")

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Setup thresholds (mirror CLAUDE.md) ──────────────────────────────────────

PULLBACK_EMA_MAX  = 0.8   # EMA Dev% upper bound for PULLBACK
CONT_EMA_MAX      = 1.0   # EMA Dev% upper bound for CONTINUATION
PULLBACK_RSI_MIN  = 38
PULLBACK_RSI_MAX  = 55

# MACD valid after this many minutes into session (26 × 5-min bars = 130 min)
MACD_VALID_AFTER_MIN = 130   # ~10:30 AM CT
BREAKOUT_RSI_MIN  = 50

RVOL_SPIKE_THRESH     = 0.50   # ≥50% increase triggers alert
RVOL_SPIKE_MIN        = 2.0    # must be ≥2.0x after spike


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

    # Only valid after 130 min into session
    session_open = now.replace(hour=8, minute=30, second=0, microsecond=0)
    elapsed_min = (now - session_open).total_seconds() / 60
    if elapsed_min < MACD_VALID_AFTER_MIN:
        return None  # too early — skip filter

    try:
        import yfinance as yf
        df = yf.download(symbol, period="1d", interval="5m", progress=False, auto_adjust=True)
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
    Return None if data unavailable (never blocks or forces a signal).
    """
    if symbol in _ribbon_cache:
        fetched_at, cached = _ribbon_cache[symbol]
        if (now - fetched_at).total_seconds() < _MACD_CACHE_TTL:
            return cached

    try:
        import yfinance as yf
        df = yf.download(symbol, period="2d", interval="5m", progress=False, auto_adjust=True)
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


# ── Setup detection ───────────────────────────────────────────────────────────

def detect_setup(row: dict, now: datetime | None = None) -> str | None:
    """
    Apply the three setup rules in priority order.
    Returns "BREAKOUT" | "CONTINUATION" | "PULLBACK" | None.

    CONTINUATION: requires MACD line > Signal line (Ch 10, Papp).
    BREAKOUT:     EMA ribbon expanding → RSI threshold lowered by 5 pts (more permissive).
    CONTINUATION: EMA ribbon compressing → EMA dev window tightened to 0.5% (stricter).
    Ribbon/MACD filters are skipped (never block) when data is unavailable.
    """
    sig     = row["signal"]
    price   = row["price"]
    orh     = row["orh"]
    rsi     = row["rsi"]
    ema_dev = row["ema_dev"]
    now     = now or datetime.now(CT)

    above_orh = orh > 0 and price > orh
    ribbon    = ema_ribbon_trend(row["symbol"], now)

    # SETUP 1: BREAKOUT
    # EMA ribbon expanding → momentum confirmed → allow RSI as low as 45 (vs 50 default)
    breakout_rsi_min = BREAKOUT_RSI_MIN - 5 if ribbon == "EXPANDING" else BREAKOUT_RSI_MIN
    if (sig == "BREAKOUT"
            and above_orh
            and rsi > breakout_rsi_min
            and ema_dev > 0):
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

    # ── Detect signals ──────────────────────────────────────────────────────
    signals: list[dict] = []
    for row in rows:
        setup = detect_setup(row, now_ct)
        if setup is None:
            continue
        sym = row["symbol"]
        signals.append({
            "symbol":     sym,
            "setup":      setup,
            "price":      row["price"],
            "orh":        row["orh"],
            "orl":        row["orl"],
            "rsi":        row["rsi"],
            "ema_dev":    row["ema_dev"],
            "rvol":       row["rvol"],
            "cup":        row["cup"],
            "htf":        sym in htf_set,
            "resistance": sym in resistance_set,
            "bb_squeeze": sym in bb_squeeze_set,
        })

    # ── Detect RVOL spikes ──────────────────────────────────────────────────
    rvol_spikes = detect_rvol_spikes(rows, prev_rvol)

    # ── Update RVOL state ───────────────────────────────────────────────────
    new_rvol_state = {row["symbol"]: row["rvol"] for row in rows}
    if not args.dry_run:
        RVOL_FILE.write_text(json.dumps(new_rvol_state))

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
            cup_tag = " CUP" if s["cup"] else ""
            htf_tag = " HTF" if s["htf"] else ""
            res_tag = " ⚠RES" if s["resistance"] else ""
            bb_tag  = " BB✓" if s.get("bb_squeeze") else ""
            print(f"SIGNAL: {s['setup']} {s['symbol']} ${s['price']} "
                  f"ORH=${s['orh']} ORL=${s['orl']} "
                  f"RSI={s['rsi']} EMA={s['ema_dev']:+.2f}% "
                  f"RVOL={s['rvol']:.1f}x{cup_tag}{htf_tag}{res_tag}{bb_tag}")

    for spike in rvol_spikes:
        print(f"RVOL_SPIKE: {spike['symbol']} {spike['prev']:.1f}x → {spike['now']:.1f}x")


def _write_empty(now_ct: datetime, dry_run: bool):
    output = {"timestamp": now_ct.strftime("%H:%M CT"), "signals": [], "rvol_spikes": []}
    if not dry_run:
        SHARED.mkdir(parents=True, exist_ok=True)
        SIG_FILE.write_text(json.dumps(output))


if __name__ == "__main__":
    main()
