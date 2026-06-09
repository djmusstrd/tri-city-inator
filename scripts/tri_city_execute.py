#!/usr/bin/env python3
"""
TRI-CITY EXECUTE — Auto-execute Tri-City scanner signals via Alpaca.

Called by the Claude Tri-City cron when a qualifying setup is detected from
the TradingView Tri-City Inator scanner table.

Setup types:
    BREAKOUT     — above ORH, high vol, EMA stack confirmed, RSI > 50
    CONTINUATION — above ORH, EMA dev 0–1%, RSI 50–65 (pullback into ORH area)
    PULLBACK     — above EMA mid, dev -0.5–0.8%, RSI 38–55 (intraday dip entry)

Guards (in order):
  1. already_executed_today   — no duplicate setups per symbol per day
  2. already_in_position      — no duplicate symbols
     → correlation warning    — advisory: flags ≥0.75 correlation with open positions
  3. max_positions             — no more than MAX_POSITIONS open at once (default: 3)
  4. daily_loss_limit          — circuit breaker if down MAX_DAILY_LOSS (default: -$300)
  5. market_regime             — block LONGs if SPY down > SPY_BEAR_THRESHOLD (default: -1.5%)
  6. rvol                      — require RVol >= MIN_RVOL vs 20-day avg (default: 1.5x)

Position sizing layers (applied in order, each can only reduce shares):
  base    — equity × RISK_PCT% / risk_per_share
  RVOL    — boost up to RVOL_SIZE_BOOST_MAX when volume elevated
  candle  — -25% for non-hammer on PULLBACK
  pennant — -15% for loose consolidation (no CUP) on PULLBACK
  VaR     — Monte Carlo 95th-pct daily loss cap (volatility-adjusted)
  BP      — cap to 90% of available buying power

Position structure (50-25-25 scale-out):
    T1 (+T1_PCT%): sell 50% → move stop to breakeven
    T2 (+T2_PCT%): sell 25% → lock at T2
    T3 (+T3_PCT%): trail 25% → position manager trails on EMA/VWAP breach or EOD close

Usage:
    python -W ignore scripts/tri_city_execute.py \\
        --symbol NVDA --price 142.50 --orh 140.00 --orl 136.00 \\
        --rsi 62.0 --ema_dev 0.35 --signal "BREAKOUT" --setup BREAKOUT

    python -W ignore scripts/tri_city_execute.py \\
        --symbol NVDA --price 142.50 --orh 140.00 --orl 136.00 \\
        --rsi 62.0 --ema_dev 0.35 --signal "BREAKOUT" --setup BREAKOUT --dry-run
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

try:
    import numpy as np
    _NUMPY_AVAILABLE = True
except ImportError:
    _NUMPY_AVAILABLE = False

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

CT           = ZoneInfo("America/Chicago")
LOG_FILE     = WORKSPACE / "logs" / "tri-city-executions.json"
MISSED_FILE  = WORKSPACE / "logs" / "tri-city-missed-slots.json"

STOP_OFFSET      = 0.13   # minimum floor: 13 cents below ORH
STOP_OFFSET_PCT  = float(os.getenv("STOP_OFFSET_PCT", "0.8"))    # 0.8% of ORH price (scales with stock price)
VWAP_STOP_OFFSET = float(os.getenv("VWAP_STOP_OFFSET", "0.05"))  # 5 cents below VWAP (Aziz)

# ── ORB timing ────────────────────────────────────────────────────────────────
ORB_MINUTES = int(os.getenv("ORB_MINUTES", "15"))   # opening range duration from .env

# ── Guard parameters (all overridable via .env) ───────────────────────────────
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS",        "5"))
LAST_ENTRY_HOUR   = int(os.getenv("LAST_ENTRY_HOUR",      "13"))   # 1:30 PM CT = last valid new entry
LAST_ENTRY_MINUTE = int(os.getenv("LAST_ENTRY_MINUTE",    "30"))
MAX_DAILY_LOSS     = float(os.getenv("MAX_DAILY_LOSS",     "-300"))
DOLLAR_T1_TRIGGER  = float(os.getenv("DOLLAR_T1_TRIGGER",  "300"))   # min $ at T1 bank — must match position_manager
MIN_RVOL           = float(os.getenv("MIN_RVOL",           "1.5"))
PM_MIN_RVOL        = float(os.getenv("PM_MIN_RVOL",        "3.5"))   # tighter floor after 11:30 AM CT
PM_START_HOUR      = int(os.getenv("PM_START_HOUR",        "11"))    # 11:30 AM CT
PM_START_MINUTE    = int(os.getenv("PM_START_MINUTE",      "30"))    # 11:30 AM CT
SPY_BEAR_THRESHOLD = float(os.getenv("SPY_BEAR_THRESHOLD", "-1.5"))

LUNCH_NO_ENTRY       = os.getenv("LUNCH_NO_ENTRY", "false").lower() == "true"
_lunch_start_hour    = int(os.getenv("LUNCH_START_HOUR",   "11"))
_lunch_start_minute  = int(os.getenv("LUNCH_START_MINUTE", "30"))
_lunch_end_hour      = int(os.getenv("LUNCH_END_HOUR",     "12"))
_lunch_end_minute    = int(os.getenv("LUNCH_END_MINUTE",   "30"))
LUNCH_START          = (_lunch_start_hour, _lunch_start_minute)
LUNCH_END            = (_lunch_end_hour,   _lunch_end_minute)
RVOL_LOOKBACK      = int(os.getenv("RVOL_LOOKBACK",        "20"))

# Setup-specific RVOL minimums (#4) — BREAKOUT needs most conviction
BREAKOUT_MIN_RVOL      = float(os.getenv("BREAKOUT_MIN_RVOL",      "3.0"))
CONTINUATION_MIN_RVOL  = float(os.getenv("CONTINUATION_MIN_RVOL",  "2.5"))
PULLBACK_MIN_RVOL      = float(os.getenv("PULLBACK_MIN_RVOL",       "1.5"))
EMA20_PB_MIN_RVOL      = float(os.getenv("EMA20_PB_MIN_RVOL",       "0.8"))  # lower — mid-morning vol distributes
EARNINGS_GAP_RVOL_FLOOR = float(os.getenv("EARNINGS_GAP_RVOL_FLOOR", "0.4"))  # large-cap earnings gaps valid at 0.4x

# BREAKOUT extension guardrails
# Parabolic check: ORB range % (orh-orl)/orl — wide opening candle = gap exhausted at open
# EMA Dev was wrong metric (always high on gap stocks regardless of intraday behavior)
PARABOLIC_ORB_RANGE_PCT = float(os.getenv("PARABOLIC_ORB_RANGE_PCT", "8.0"))  # skip if ORB range > 8%
BREAKOUT_MAX_RSI        = float(os.getenv("BREAKOUT_MAX_RSI",        "82.0"))  # skip if RSI overbought

# Time-bucketed position slots: 3 morning + 2 afternoon (replaces single MAX_POSITIONS pool)
MORNING_SLOTS          = int(os.getenv("MORNING_SLOTS",          "3"))   # 9:35–10:15 CT
AFTERNOON_SLOTS        = int(os.getenv("AFTERNOON_SLOTS",        "2"))   # 10:15 CT onward
FADE_SLOTS             = int(os.getenv("FADE_SLOTS",             "1"))   # dedicated FADE pool (does NOT consume long slots)
MORNING_CUTOFF_HOUR   = int(os.getenv("MORNING_CUTOFF_HOUR",    "10"))
MORNING_CUTOFF_MINUTE = int(os.getenv("MORNING_CUTOFF_MINUTE",  "15"))
# Parabolic ORB check only applies within 90 min of ORB close
# After this cutoff the opening candle is stale — intraday breakouts are not ORB plays
PARABOLIC_WINDOW_HOUR   = int(os.getenv("PARABOLIC_WINDOW_HOUR",   "10"))  # 10:00 CT
PARABOLIC_WINDOW_MINUTE = int(os.getenv("PARABOLIC_WINDOW_MINUTE",  "0"))

# FADE signal parameters (gap-and-crap short play)
GAP_FADE_MIN_PCT  = float(os.getenv("GAP_FADE_MIN_PCT",  "12.0"))  # min gap% to qualify
FADE_ORB_MIN_PCT  = float(os.getenv("FADE_ORB_MIN_PCT",   "8.0"))  # min ORB range% (parabolic ORB)
FADE_T1_PCT       = float(os.getenv("FADE_T1_PCT",        "5.0"))  # short-side target: -5%
FADE_STOP_BUFFER  = float(os.getenv("FADE_STOP_BUFFER",   "0.5"))  # stop above ORH by 0.5%

# RVOL-based position size boost (#2)
RVOL_SIZE_BOOST_MAX    = float(os.getenv("RVOL_SIZE_BOOST_MAX",    "1.25"))  # max multiplier
RVOL_SIZE_BOOST_THRESH = float(os.getenv("RVOL_SIZE_BOOST_THRESH", "3.0"))   # RVOL level for max boost

# ATR-based stop calibration (Davey Ch. 9): stops scale with actual stock volatility
# ATR_STOP_MULT × ATR(14) gives stop distance from ORH; overrides fixed % when larger
# Kill switches: set to 0 in .env to disable ATR widening and fall back to fixed %
ATR_STOP_MULT          = float(os.getenv("ATR_STOP_MULT",          "1.0"))   # stop = orh - max(ATR×this, fixed%)
ATR_PULLBACK_STOP_MULT = float(os.getenv("ATR_PULLBACK_STOP_MULT", "0.75"))  # tighter anchor for PULLBACK
ATR_T1_MULT            = float(os.getenv("ATR_T1_MULT",            "1.5"))   # T1 = max(entry + ATR×this, fixed T1)
ATR_T2_MULT            = float(os.getenv("ATR_T2_MULT",            "3.0"))   # T2 = max(entry + ATR×this, fixed T2)

# Master PULLBACK kill switch — set false to pause all PULLBACK entries
PULLBACK_ENABLED = os.getenv("PULLBACK_ENABLED", "true").lower() == "true"

# Pullback trending gate (Davey Ch. 7 via Parkinson vol): only take PULLBACK when
# the stock is in a confirmed trending regime (park_trending=True in candidates.json)
PULLBACK_TRENDING_REQUIRED = os.getenv("PULLBACK_TRENDING_REQUIRED", "true").lower() == "true"

# PULLBACK ORB spread guard: tiny opening range = no meaningful level to pull back against
PULLBACK_MIN_ORB_SPREAD_PCT = float(os.getenv("PULLBACK_MIN_ORB_SPREAD_PCT", "0.25"))  # % of price

# ATR volatility circuit breaker (Davey Ch. 6): skip entries on extreme market shock days.
# Compares SPY's 5-day ATR to its 20-day ATR baseline. Fires only at extreme multiples
# (default 2.5x) so normal high-vol setup days are not filtered.
VOLATILITY_CHECK_ENABLED = os.getenv("VOLATILITY_CHECK_ENABLED", "true").lower() == "true"
VOLATILITY_ATR_CEILING   = float(os.getenv("VOLATILITY_ATR_CEILING", "2.5"))  # skip if SPY ATR5 > ATR20 × this

# ── Position sizing ───────────────────────────────────────────────────────────
RISK_PCT           = float(os.getenv("RISK_PCT",           "2.0"))   # % of equity to risk
STOP_PCT           = float(os.getenv("STOP_PCT",           "5.0"))   # fallback stop % if ORH not usable
T1_PCT             = float(os.getenv("T1_PCT",             "10.0"))  # T1 take-profit %
T2_PCT             = float(os.getenv("T2_PCT",             "20.0"))  # T2 take-profit %
T3_PCT             = float(os.getenv("T3_PCT",             "30.0"))  # T3 take-profit % (trailed)
FIXED_RISK         = float(os.getenv("FIXED_RISK",         "150"))   # fallback if equity unavailable
ADR_STOP_FLOOR_PCT = float(os.getenv("ADR_STOP_FLOOR_PCT", "0.25"))  # min stop = 25% of ADR(10)

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

_LOG_DIR = Path.home() / "tri-city-inator" / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(_LOG_DIR / "tri-city-execute.log")],
    force=True,  # override any handlers set by imported modules
)
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


# ── Missed-slot log ────────────────────────────────────────────────────────────

def log_missed_slot(symbol: str, setup: str, args, rvol, blocked_by: str) -> None:
    """
    Log a signal that was blocked by the slot cap so we can review post-session
    what was missed and sharpen the edge over time.

    Written to logs/tri-city-missed-slots.json — one entry per blocked signal.
    Fields mirror the execution log so the same dashboard can display both.
    """
    try:
        MISSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        entries: list = []
        if MISSED_FILE.exists():
            try:
                entries = json.loads(MISSED_FILE.read_text())
            except Exception:
                pass

        now = datetime.now(CT)
        entry = {
            "date":       now.strftime("%Y-%m-%d"),
            "time":       now.strftime("%H:%M:%S CT"),
            "symbol":     symbol,
            "setup":      setup,
            "price":      getattr(args, "price", None),
            "orh":        getattr(args, "orh",   None),
            "orl":        getattr(args, "orl",   None),
            "rsi":        getattr(args, "rsi",   None),
            "ema_dev":    getattr(args, "ema_dev", None),
            "rvol":       rvol,
            "gap_pct":    getattr(args, "gap",   None),
            "cup":        getattr(args, "cup",   False),
            "signal":     getattr(args, "signal", setup),
            "blocked_by": blocked_by,   # e.g. "morning_slots" | "afternoon_slots" | "fade_slots"
        }
        entries.append(entry)
        MISSED_FILE.write_text(json.dumps(entries, indent=2, default=str))
        logger.info(f"Missed slot logged: {symbol} {setup} @ ${entry['price']} — {blocked_by}")
    except Exception as e:
        logger.debug(f"log_missed_slot failed: {e}")


# ── Guard 1 & 2: duplicate checks ─────────────────────────────────────────────

def already_executed_today(symbol: str, setup: str) -> bool:
    """
    Guard 1: block duplicate setups per symbol per day.

    Finding 5 (re-entry exception): if the prior execution was exited via Fix B
    (failed pullback) and the loss was a small scratch (< RE_ENTRY_MAX_LOSS $/share),
    allow one re-entry. Bulkowski: re-entry after a scratch has 65-70% win rate.
    """
    today = datetime.now(CT).strftime("%Y-%m-%d")
    for e in reversed(load_log()):
        if (e.get("symbol") == symbol and e.get("setup") == setup
                and e.get("date") == today and e.get("success")):
            # Most recent execution found — check if it was a Fix B scratch
            if (e.get("fix_b_exit")
                    and e.get("pnl_per_share") is not None
                    and e["pnl_per_share"] >= -RE_ENTRY_MAX_LOSS):
                logger.info(
                    f"Re-entry allowed: {setup} {symbol} — Fix B scratch "
                    f"${e['pnl_per_share']:+.4f}/share within ${RE_ENTRY_MAX_LOSS:.2f} limit"
                )
                return False
            logger.info(f"Already executed {setup} on {symbol} today.")
            return True
    return False


def already_in_position(symbol: str) -> bool:
    positions = get_open_positions()
    if symbol in [p["ticker"] for p in positions]:
        logger.info(f"Already holding {symbol}.")
        return True
    return False


# ── Guard 3: time-bucketed position slots ─────────────────────────────────────
# Morning window (9:35–10:15 CT): up to MORNING_SLOTS positions
# Afternoon window (10:15 CT+):   up to AFTERNOON_SLOTS positions
# Hard cap: MAX_POSITIONS total at any time

def _morning_window(now: "datetime") -> bool:
    """True if current time is before the morning cutoff (10:15 CT by default)."""
    return now.hour < MORNING_CUTOFF_HOUR or (
        now.hour == MORNING_CUTOFF_HOUR and now.minute < MORNING_CUTOFF_MINUTE
    )


def _count_bucket_positions(is_morning_entry: bool) -> int:
    """
    Count open positions that were entered in the given time bucket today.
    Cross-references executions log (entry time) with current Alpaca positions.
    """
    today = datetime.now(CT).strftime("%Y-%m-%d")
    cutoff_str = f"{MORNING_CUTOFF_HOUR:02d}:{MORNING_CUTOFF_MINUTE:02d}"
    try:
        execs = load_log()
        entered_syms: set[str] = set()
        for e in execs:
            if e.get("date") != today or not e.get("success"):
                continue
            t = e.get("time", "99:99 CT").split(" ")[0]  # "HH:MM"
            entry_is_morning = t < cutoff_str
            if entry_is_morning == is_morning_entry:
                entered_syms.add(e["symbol"])
        open_syms = {p["ticker"] for p in get_open_positions()}
        return len(entered_syms & open_syms)
    except Exception:
        return 0


def check_max_positions(setup: str = "") -> bool:
    """
    Enforces both the hard cap (MAX_POSITIONS) and time-bucket limits.
    FADE trades use a dedicated FADE_SLOTS pool and do NOT consume long slots.
    Returns True (block entry) if any limit is reached.
    """
    positions = get_open_positions()
    total = len(positions)
    now   = datetime.now(CT)

    if total >= MAX_POSITIONS:
        logger.info(f"Max positions: {total}/{MAX_POSITIONS} open.")
        return True

    # FADE has its own dedicated slot pool
    if setup == "FADE":
        fade_open = _count_bucket_positions_by_setup("FADE")
        if fade_open >= FADE_SLOTS:
            logger.info(f"Fade slots full: {fade_open}/{FADE_SLOTS} open.")
            return True
        return False  # FADE does not consume morning/afternoon long slots

    if _morning_window(now):
        morning_open = _count_bucket_positions(is_morning_entry=True)
        if morning_open >= MORNING_SLOTS:
            logger.info(
                f"Morning slots full: {morning_open}/{MORNING_SLOTS} "
                f"(before {MORNING_CUTOFF_HOUR:02d}:{MORNING_CUTOFF_MINUTE:02d} CT)."
            )
            return True
    else:
        afternoon_open = _count_bucket_positions(is_morning_entry=False)
        if afternoon_open >= AFTERNOON_SLOTS:
            logger.info(
                f"Afternoon slots full: {afternoon_open}/{AFTERNOON_SLOTS} "
                f"(after {MORNING_CUTOFF_HOUR:02d}:{MORNING_CUTOFF_MINUTE:02d} CT)."
            )
            return True

    return False


def _count_bucket_positions_by_setup(setup: str) -> int:
    """Count today's open positions with a specific setup type."""
    today = datetime.now(CT).strftime("%Y-%m-%d")
    try:
        execs = load_log()
        entered_syms: set[str] = set()
        for e in execs:
            if e.get("date") == today and e.get("success") and e.get("setup") == setup:
                entered_syms.add(e["symbol"])
        open_syms = {p["ticker"] for p in get_open_positions()}
        return len(entered_syms & open_syms)
    except Exception:
        return 0


# ── Guard 4: daily loss limit ──────────────────────────────────────────────────

def check_daily_loss_limit(today: str) -> bool:
    # Primary: live account equity vs yesterday's close — catches unrealized losses
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        try:
            from alpaca.trading.client import TradingClient
            client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
            acct = client.get_account()
            daily_pnl = float(acct.equity) - float(acct.last_equity)
            if daily_pnl <= MAX_DAILY_LOSS:
                logger.warning(
                    f"Daily loss limit hit (live): ${daily_pnl:.2f} "
                    f"(limit ${MAX_DAILY_LOSS:.0f}). No new entries."
                )
                return True
            return False
        except Exception as e:
            logger.warning(f"check_daily_loss_limit (live): {e} — falling back to journal")
    # Fallback: closed trades from journal only
    try:
        from managers.trade_journal import get_all_trades
        trades = get_all_trades()
        today_pnl = sum(
            t["realized_pnl"] for t in trades
            if t.get("date") == today and t.get("status") == "closed"
        )
        if today_pnl <= MAX_DAILY_LOSS:
            logger.warning(
                f"Daily loss limit hit (journal): ${today_pnl:.2f} "
                f"(limit ${MAX_DAILY_LOSS:.0f}). No new entries."
            )
            return True
    except Exception as e:
        logger.warning(f"check_daily_loss_limit (journal): {e}")
    return False


# ── Guard 4b: lunch no-entry window ───────────────────────────────────────────

def check_lunch_window(now: datetime) -> bool:
    """Block new entries during the lunch dead-zone (default 11:30–12:30 CT)."""
    if not LUNCH_NO_ENTRY:
        return False
    start = now.replace(hour=LUNCH_START[0], minute=LUNCH_START[1], second=0, microsecond=0)
    end   = now.replace(hour=LUNCH_END[0],   minute=LUNCH_END[1],   second=0, microsecond=0)
    if start <= now < end:
        logger.info(
            f"Lunch window {LUNCH_START[0]:02d}:{LUNCH_START[1]:02d}–"
            f"{LUNCH_END[0]:02d}:{LUNCH_END[1]:02d} CT — skipping entry."
        )
        return True
    return False



# ── Guard 6: SPY market regime ─────────────────────────────────────────────────

def get_spy_regime() -> tuple[str, float | None]:
    """
    Returns (regime, spy_pct_change).
    Uses yfinance for prev close (free tier safe), Alpaca snapshot for today's price.
    """
    try:
        import yfinance as yf
        df = yf.download("SPY", period="5d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 2:
            return "UNKNOWN", None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        prev_close = float(df["close"].iloc[-2])

        # Try Alpaca snapshot for today's current price; fall back to yfinance close
        curr = None
        if ALPACA_API_KEY and ALPACA_SECRET_KEY:
            try:
                from alpaca.data.historical import StockHistoricalDataClient
                from alpaca.data.requests import StockSnapshotRequest
                client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
                snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols="SPY"))
                s = snap.get("SPY")
                if s and s.daily_bar:
                    curr = float(s.daily_bar.close)
            except Exception:
                pass
        if curr is None:
            curr = float(df["close"].iloc[-1])

        change = round((curr - prev_close) / prev_close * 100, 2)
        if change <= SPY_BEAR_THRESHOLD:
            return "BEAR", change
        elif change >= 0.5:
            return "BULL", change
        return "NEUTRAL", change
    except Exception as e:
        logger.warning(f"get_spy_regime: {e}")
        return "UNKNOWN", None


# ── Guard 7: relative volume ───────────────────────────────────────────────────

def get_rvol(symbol: str) -> float | None:
    """
    RVol = today's cumulative volume / (20-day avg daily vol × fraction of day elapsed).
    Uses yfinance for historical avg (free tier); Alpaca snapshot for today's volume.
    """
    try:
        import yfinance as yf
        now = datetime.now(CT)
        df = yf.download(symbol, period=f"{RVOL_LOOKBACK * 2}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

        today_str = now.strftime("%Y-%m-%d")
        today_row = df[df.index.strftime("%Y-%m-%d") == today_str]["volume"]
        today_vol = float(today_row.iloc[-1]) if not today_row.empty else None

        hist = df[df.index.strftime("%Y-%m-%d") != today_str]["volume"].dropna()
        if today_vol is None or hist.empty:
            return None

        hist_vols    = list(hist)[-RVOL_LOOKBACK:]
        avg_daily    = sum(hist_vols) / len(hist_vols)
        open_ct      = now.replace(hour=8, minute=30, second=0, microsecond=0)
        close_ct     = now.replace(hour=15, minute=0,  second=0, microsecond=0)
        total_sec    = (close_ct - open_ct).total_seconds()
        elapsed_sec  = max(60, (now - open_ct).total_seconds())
        fraction     = min(1.0, elapsed_sec / total_sec)
        expected     = avg_daily * fraction
        if expected <= 0:
            return None
        return round(today_vol / expected, 2)
    except Exception as e:
        logger.warning(f"get_rvol {symbol}: {e}")
        return None


# ── Monte Carlo VaR sizing (Ch 7 — Investing for Programmers) ─────────────────
# Simulates 5 000 next-day return scenarios from historical daily return
# distribution (mu, sigma). Returns the max position value that keeps the
# 95th-percentile simulated daily loss within max_risk_dollars.
# Only ever REDUCES the share count — never increases it.
# Falls back to flat risk_per_share sizing if numpy or history unavailable.

VAR_SIMULATIONS = int(os.getenv("VAR_SIMULATIONS", "5000"))
VAR_CONFIDENCE  = float(os.getenv("VAR_CONFIDENCE", "0.95"))   # 95th percentile
VAR_LOOKBACK    = int(os.getenv("VAR_LOOKBACK",    "60"))       # 60 trading days


def var_position_size(symbol: str, price: float, max_risk_dollars: float) -> int | None:
    """
    Monte Carlo VaR-based position sizing.
    Returns max shares that keep simulated 1-day VaR within max_risk_dollars,
    or None if calculation is unavailable (fall through to standard sizing).
    """
    if not _NUMPY_AVAILABLE:
        return None
    try:
        import yfinance as yf
        df = yf.download(symbol, period=f"{VAR_LOOKBACK * 2}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < 20:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

        closes = df["close"].dropna().tail(VAR_LOOKBACK)
        returns = closes.pct_change().dropna()
        if len(returns) < 15:
            return None

        mu    = float(returns.mean())
        sigma = float(returns.std())
        if sigma <= 0:
            return None

        # Simulate return scenarios
        rng          = np.random.default_rng(seed=42)
        sim_returns  = rng.normal(mu, sigma, VAR_SIMULATIONS)

        # VaR: worst-case daily loss at VAR_CONFIDENCE level
        # We want: position_value × abs(percentile(sim_returns, 1-conf)) ≤ max_risk_dollars
        loss_pct     = abs(np.percentile(sim_returns, (1 - VAR_CONFIDENCE) * 100))
        if loss_pct <= 0:
            return None

        max_position_value = max_risk_dollars / loss_pct
        var_shares         = max(1, int(max_position_value / price))
        return var_shares
    except Exception as e:
        logger.debug(f"var_position_size {symbol}: {e}")
        return None


def get_adr(symbol: str, period: int = 10) -> float | None:
    """
    Average Daily Range: mean(high - low) over last N sessions.
    Excludes gap contribution (pure intraday swing), making it better than ATR
    for gap stocks where ATR is inflated by the gap itself.
    Returns None on any failure — caller falls back to structural stop as-is.
    """
    try:
        import yfinance as yf
        df = yf.download(symbol, period=f"{period * 3}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < period:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        ranges = (df["high"] - df["low"]).dropna().tail(period)
        return float(ranges.mean())
    except Exception as e:
        logger.debug(f"get_adr {symbol}: {e}")
        return None


def get_atr(symbol: str, period: int = 14) -> float | None:
    """
    Average True Range (ATR-period): mean(TR) over last N sessions.
    True Range = max(High-Low, |High-PrevClose|, |Low-PrevClose|).
    Unlike ADR, ATR includes overnight gaps — better for gap stocks.
    Returns None on any failure — caller falls back to existing stop logic.
    """
    try:
        import yfinance as yf
        df = yf.download(symbol, period=f"{period * 3}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty or len(df) < period + 1:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        highs  = list(df["high"].values)
        lows   = list(df["low"].values)
        closes = list(df["close"].values)
        tr_values = []
        for i in range(1, len(highs)):
            tr = max(
                float(highs[i])  - float(lows[i]),
                abs(float(highs[i])  - float(closes[i - 1])),
                abs(float(lows[i])   - float(closes[i - 1])),
            )
            tr_values.append(tr)
        if len(tr_values) < period:
            return None
        return round(sum(tr_values[-period:]) / period, 4)
    except Exception as e:
        logger.debug(f"get_atr {symbol}: {e}")
        return None


def check_atr_circuit_breaker() -> bool:
    """
    Returns True (block entry) if SPY is in an extreme volatility spike.
    Compares SPY ATR(5) to ATR(20) baseline. Fires only at VOLATILITY_ATR_CEILING×
    (default 2.5x) — normal high-vol gap days are NOT blocked.
    Returns False (allow entry) on any data failure — fail open.
    """
    if not VOLATILITY_CHECK_ENABLED:
        return False
    try:
        atr5  = get_atr("SPY", period=5)
        atr20 = get_atr("SPY", period=20)
        if atr5 is None or atr20 is None or atr20 <= 0:
            return False
        ratio = atr5 / atr20
        if ratio > VOLATILITY_ATR_CEILING:
            logger.warning(
                f"ATR CIRCUIT BREAKER: SPY ATR(5)={atr5:.2f} / ATR(20)={atr20:.2f} "
                f"= {ratio:.2f}x ≥ ceiling {VOLATILITY_ATR_CEILING:.1f}x"
            )
            return True
        return False
    except Exception as e:
        logger.debug(f"check_atr_circuit_breaker: {e}")
        return False


# ── Correlation check (Ch 7 — sector concentration guard) ─────────────────────
# Computes 20-day Pearson correlation between the incoming symbol and each open
# position. Logs a warning if any pair is ≥ CORR_WARN_THRESHOLD — does NOT block
# the trade, but the warning appears in execution output so the user can decide.

CORR_WARN_THRESHOLD = float(os.getenv("CORR_WARN_THRESHOLD", "0.75"))
CORR_LOOKBACK       = int(os.getenv("CORR_LOOKBACK",        "20"))


def check_correlation_warning(symbol: str) -> list[str]:
    """
    Returns a list of warning strings for any open position highly correlated
    with symbol over the last CORR_LOOKBACK trading days.
    Returns [] if no correlated positions found or data unavailable.
    """
    if not _NUMPY_AVAILABLE:
        return []
    open_positions = get_open_positions()
    open_tickers   = [p["ticker"] for p in open_positions if p["ticker"] != symbol]
    if not open_tickers:
        return []
    warnings = []
    try:
        import yfinance as yf
        all_syms  = [symbol] + open_tickers
        df        = yf.download(all_syms, period=f"{CORR_LOOKBACK * 2}d",
                                interval="1d", progress=False, auto_adjust=True)
        if df.empty:
            return []

        # Flatten multi-level columns if needed
        if hasattr(df.columns, "levels"):
            close_df = df["Close"] if "Close" in df.columns.get_level_values(0) else df.xs("close", axis=1, level=0)
        else:
            close_df = df[["Close"]] if "Close" in df.columns else df

        returns = close_df.pct_change().dropna().tail(CORR_LOOKBACK)

        for ticker in open_tickers:
            try:
                s1 = returns[symbol]
                s2 = returns[ticker]
                if len(s1) < 10 or len(s2) < 10:
                    continue
                corr = float(s1.corr(s2))
                if corr >= CORR_WARN_THRESHOLD:
                    warnings.append(
                        f"⚠ CORRELATION: {symbol} vs {ticker} = {corr:.2f} "
                        f"(≥{CORR_WARN_THRESHOLD:.2f} threshold — sector concentration risk)"
                    )
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"check_correlation_warning: {e}")
    return warnings


# ── Account equity ─────────────────────────────────────────────────────────────

def get_account_equity() -> float | None:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        return float(client.get_account().equity)
    except Exception as e:
        logger.warning(f"get_account_equity: {e}")
        return None


def get_buying_power() -> float | None:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        return float(client.get_account().buying_power)
    except Exception as e:
        logger.warning(f"get_buying_power: {e}")
        return None


# ── Candle type classifier (Finding 6) ────────────────────────────────────────

def get_candle_type(symbol: str) -> str:
    """
    Classify the most recent completed 15-min bar as HAMMER, DOJI, BEARISH, or NEUTRAL.
    Finding 6 (Bulkowski): hammers at EMA have 72% follow-through.
    Non-hammer PULLBACK entries get a 25% position size reduction.
    Returns "UNKNOWN" on any error so callers skip the penalty.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return "UNKNOWN"
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta

        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        now = datetime.now(CT)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=now - timedelta(minutes=60),
            end=now,
        )
        bars  = client.get_stock_bars(req)
        df    = bars.df
        if df.empty:
            return "UNKNOWN"

        bar = df.iloc[-1]
        o, h, l, c = (float(bar["open"]), float(bar["high"]),
                      float(bar["low"]),  float(bar["close"]))
        total_range = h - l
        if total_range < 0.001:
            return "DOJI"

        body        = abs(c - o)
        lower_wick  = min(o, c) - l
        upper_wick  = h - max(o, c)

        # Hammer: small body (≤35% of range), long lower wick (≥2× body), tiny upper wick
        if (lower_wick >= 2 * body
                and upper_wick <= body
                and total_range > 0
                and body / total_range <= 0.35):
            return "HAMMER"

        # Bearish engulfing / shooting star
        if c < o and upper_wick >= 2 * body:
            return "BEARISH"

        return "NEUTRAL"
    except Exception as e:
        logger.warning(f"get_candle_type {symbol}: {e}")
        return "UNKNOWN"


# ── Signal calculation ─────────────────────────────────────────────────────────

EMA_STOP_BUFFER      = float(os.getenv("EMA_STOP_BUFFER",        "0.30"))  # Finding 3: widened to 30¢ below EMA20
PULLBACK_TIMEOUT     = int(os.getenv("PULLBACK_TIMEOUT_MIN",    "25"))    # Fix A
RE_ENTRY_MAX_LOSS    = float(os.getenv("RE_ENTRY_MAX_LOSS",     "0.50"))  # Finding 5: allow re-entry if scratch < this $/share
LOOSE_CONSOL_PENALTY = float(os.getenv("LOOSE_CONSOL_PENALTY",  "0.85"))  # Pennant: -15% size when no CUP on PULLBACK: minutes


def calculate_signal(symbol: str, price: float, orh: float, setup: str,
                     ema_dev: float = 0.0, rvol: float | None = None,
                     candle_type: str = "UNKNOWN",
                     cup: bool = False,
                     htf: bool = False,
                     bb_squeeze: bool = False,
                     vwap: float | None = None) -> dict:
    """
    Build trade signal with stop, targets, and position size.

    Stop placement:
        BREAKOUT / CONTINUATION — 13 cents below ORH
        PULLBACK (Fix C)        — EMA20 minus EMA_STOP_BUFFER cents
                                  (EMA20 derived from price and ema_dev%)
                                  Falls back to STOP_PCT% if ema_dev unavailable.

    Position sizing: risk RISK_PCT% of account equity. Falls back to FIXED_RISK.
    """
    # ATR(14): used to widen stops and calibrate targets to actual stock volatility.
    # Falls back gracefully — if None, existing fixed-% logic is used unchanged.
    atr = get_atr(symbol)

    # Stop
    if setup in ("BREAKOUT", "CONTINUATION"):
        pct_offset = max(round(orh * STOP_OFFSET_PCT / 100, 2), STOP_OFFSET)
        if atr and atr > 0 and ATR_STOP_MULT > 0:
            # Use the LARGER of ATR-based or fixed-% offset so volatile stocks get more room
            atr_offset = round(atr * ATR_STOP_MULT, 2)
            offset = max(pct_offset, atr_offset)
            if atr_offset > pct_offset:
                logger.info(
                    f"ATR stop: ORH-{pct_offset:.2f} → ORH-{atr_offset:.2f} "
                    f"(ATR={atr:.2f} × {ATR_STOP_MULT})"
                )
        else:
            offset = pct_offset
        stop = round(orh - offset, 2)
    elif setup == "PULLBACK":
        # Fix C: use EMA20 as stop anchor (tighter, meaningful support level)
        # ema_dev = (price - ema20) / ema20 * 100  →  ema20 = price / (1 + ema_dev/100)
        if -99 < ema_dev < 100:
            ema20 = round(price / (1 + ema_dev / 100), 4)
            if atr and atr > 0 and ATR_PULLBACK_STOP_MULT > 0:
                atr_buf  = round(atr * ATR_PULLBACK_STOP_MULT, 2)
                old_stop = round(ema20 - EMA_STOP_BUFFER, 2)
                new_stop = round(ema20 - atr_buf, 2)
                if new_stop < old_stop:   # ATR is wider — use it
                    logger.info(
                        f"ATR PULLBACK stop: EMA20-{EMA_STOP_BUFFER:.2f} → EMA20-{atr_buf:.2f} "
                        f"(ATR={atr:.2f} × {ATR_PULLBACK_STOP_MULT})"
                    )
                    stop = new_stop
                else:
                    stop = old_stop
            else:
                stop = round(ema20 - EMA_STOP_BUFFER, 2)
        else:
            stop  = round(price * (1 - STOP_PCT / 100), 2)
    else:
        stop = round(price * (1 - STOP_PCT / 100), 2)

    # VWAP stop tightening (Aziz): if VWAP is provided and VWAP stop is tighter
    # (higher price) than the standard stop, use VWAP stop instead.
    # "Just below VWAP" = cleaner invalidation level than arbitrary % stops.
    if vwap is not None and vwap > 0:
        vwap_stop = round(vwap - VWAP_STOP_OFFSET, 2)
        if vwap_stop > stop:
            logger.info(
                f"VWAP stop: ${stop:.2f} → ${vwap_stop:.2f} "
                f"(VWAP ${vwap:.2f} − ${VWAP_STOP_OFFSET:.2f})"
            )
            stop = vwap_stop

    # Safety guard: stop must always be below entry
    if stop >= price:
        logger.warning(f"Stop ${stop} >= entry ${price} — falling back to {STOP_PCT}% below entry")
        stop = round(price * (1 - STOP_PCT / 100), 2)

    risk_per_share = round(price - stop, 4)
    if risk_per_share < 0.05:
        logger.warning(f"Risk per share too small ({risk_per_share:.2f}) — skipping trade")
        raise ValueError(f"Risk per share {risk_per_share:.2f} too small to size position safely")

    # ADR stop floor: prevent oversizing when structural stop is tighter than normal intraday noise.
    # Uses avg(high-low, 10 days) — excludes gaps, so gap stocks don't inflate the floor.
    if ADR_STOP_FLOOR_PCT > 0:
        adr = get_adr(symbol)
        if adr and adr > 0:
            adr_floor = round(adr * ADR_STOP_FLOOR_PCT, 4)
            if risk_per_share < adr_floor:
                logger.info(
                    f"ADR floor: risk/share ${risk_per_share:.3f} < {ADR_STOP_FLOOR_PCT:.0%}×ADR(10) "
                    f"${adr_floor:.3f} — widening to floor (ADR=${adr:.3f})"
                )
                risk_per_share = adr_floor

    equity           = get_account_equity()
    max_risk_dollars = (equity * RISK_PCT / 100) if (equity and equity > 0) else FIXED_RISK
    position_size    = max(1, int(max_risk_dollars / risk_per_share))

    # #2: Scale position size up when RVOL is elevated (linear from 1.0x → RVOL_SIZE_BOOST_MAX)
    if rvol is not None and rvol > MIN_RVOL and RVOL_SIZE_BOOST_MAX > 1.0:
        boost_range = RVOL_SIZE_BOOST_THRESH - MIN_RVOL
        if boost_range > 0:
            t           = min(1.0, (rvol - MIN_RVOL) / boost_range)
            multiplier  = 1.0 + t * (RVOL_SIZE_BOOST_MAX - 1.0)
            boosted     = max(1, int(position_size * multiplier))
            if boosted != position_size:
                logger.info(
                    f"RVOL boost: {rvol:.2f}x → size {position_size}→{boosted} "
                    f"(x{multiplier:.2f})"
                )
            position_size = boosted

    if setup == "PULLBACK":
        # HTF override: High & Tight Flag (+90% in 3mo) — skip all size penalties.
        # Bulkowski: HTF has 82% target hit rate and 15% failure rate. Full size warranted.
        if htf:
            logger.info(f"HTF setup: bypassing candle and loose-consolidation size penalties")
        else:
            # Finding 6: -25% for non-hammer candles (Bulkowski: hammers at EMA 72% follow-through)
            if candle_type not in ("HAMMER", "UNKNOWN"):
                reduced = max(1, int(position_size * 0.75))
                if reduced != position_size:
                    logger.info(
                        f"Candle type {candle_type}: PULLBACK size {position_size}→{reduced} (-25%)"
                    )
                position_size = reduced

            # Pennant penalty: -15% for loose consolidation (no CUP = widening range)
            # Bulkowski: loose pennants have 54% failure vs tight at 44%.
            if not cup and LOOSE_CONSOL_PENALTY < 1.0:
                reduced = max(1, int(position_size * LOOSE_CONSOL_PENALTY))
                if reduced != position_size:
                    logger.info(
                        f"Loose consolidation (no CUP): PULLBACK size {position_size}→{reduced} "
                        f"(-{round((1-LOOSE_CONSOL_PENALTY)*100):.0f}%)"
                    )
                position_size = reduced

    # Monte Carlo VaR cap: simulate historical return distribution; reduce size if
    # 95th-pct simulated daily loss would exceed max_risk_dollars. Only reduces.
    var_shares = var_position_size(symbol, price, max_risk_dollars)
    if var_shares is not None and var_shares < position_size:
        logger.info(
            f"VaR cap: {position_size}→{var_shares} shares "
            f"(volatility-adjusted 95% daily loss limit ${max_risk_dollars:.0f})"
        )
        position_size = var_shares

    # BB squeeze — upper band extension reduction (Papp Ch 06):
    # If a BB squeeze was flagged at open but price is already extended above the upper
    # band at ORH breakout (ema_dev > 2%), the coil has already fired — entry is late.
    # Reduce size 25% to account for the diminished risk/reward at extended prices.
    if bb_squeeze and setup == "BREAKOUT" and ema_dev > 2.0:
        reduced = max(1, int(position_size * 0.75))
        if reduced != position_size:
            logger.info(
                f"BB squeeze extended: BREAKOUT size {position_size}→{reduced} "
                f"(-25%, price above upper band, EMA Dev {ema_dev:+.2f}%)"
            )
        position_size = reduced

    # Cap to 90% of available buying power to prevent insufficient-funds errors
    bp = get_buying_power()
    if bp is not None and bp > 0 and price > 0:
        max_by_bp = max(1, int(bp * 0.90 / price))
        if position_size > max_by_bp:
            logger.info(
                f"Buying power cap: {position_size}→{max_by_bp} shares "
                f"(bp=${bp:,.0f}, price=${price:.2f})"
            )
            position_size = max_by_bp

    # Targets: ATR-scaled targets applied to BREAKOUT/CONTINUATION only.
    # PULLBACK targets stay fixed — the thesis is "back to ORH," not a multi-ATR move.
    # For BREAKOUT/CONTINUATION: take the BETTER (higher) of ATR or fixed %.
    fixed_t1 = round(price * (1 + T1_PCT / 100), 2)
    fixed_t2 = round(price * (1 + T2_PCT / 100), 2)
    target_3  = round(price * (1 + T3_PCT / 100), 2)

    if setup in ("BREAKOUT", "CONTINUATION") and atr and atr > 0 and ATR_T1_MULT > 0:
        atr_t1 = round(price + atr * ATR_T1_MULT, 2)
        atr_t2 = round(price + atr * ATR_T2_MULT, 2)
        target_1 = max(fixed_t1, atr_t1)
        target_2 = max(fixed_t2, atr_t2)
        if target_1 != fixed_t1 or target_2 != fixed_t2:
            logger.info(
                f"ATR targets: T1 {fixed_t1}→{target_1}, T2 {fixed_t2}→{target_2} "
                f"(ATR={atr:.2f}, ×{ATR_T1_MULT}/{ATR_T2_MULT})"
            )
    else:
        target_1 = fixed_t1
        target_2 = fixed_t2

    return {
        "ticker":        symbol,
        "entry_price":   price,
        "position_size": position_size,
        "stop_loss":     stop,
        "stop":          stop,
        "target_1":      target_1,
        "target_2":      target_2,
        "target_3":      target_3,
        "direction":     "BULLISH",
        "confidence":    1.0,
        "candle_type":   candle_type,
        "htf":           htf,
    }


# ── Execution log ──────────────────────────────────────────────────────────────

def log_execution(symbol: str, setup: str, signal: dict, result,
                  rvol: float | None = None,
                  spy_regime: str | None = None,
                  spy_change: float | None = None,
                  cup: bool = False,
                  bb_squeeze: bool = False,
                  rsi: float | None = None,
                  ema_dev: float | None = None,
                  scanner_signal: str | None = None,
                  orh: float | None = None,
                  orl: float | None = None,
                  candle_type: str | None = None,
                  actual_fill_price: float | None = None):
    now = datetime.now(CT)
    fill_price = actual_fill_price or signal["entry_price"]
    risk_dollars = round(
        signal["position_size"] * (fill_price - signal["stop_loss"]), 2
    )
    entry = {
        "date":           now.strftime("%Y-%m-%d"),
        "time":           now.strftime("%H:%M:%S CT"),
        "symbol":         symbol,
        "setup":          setup,
        "cup":            cup,
        "bb_squeeze":     bb_squeeze,
        "candle_type":    candle_type or signal.get("candle_type"),
        "signal_price":   signal["entry_price"],
        "entry_price":    fill_price,
        "stop_loss":      signal["stop_loss"],
        "target_1":       signal["target_1"],
        "target_2":       signal["target_2"],
        "target_3":       signal["target_3"],
        "position_size":  signal["position_size"],
        "risk_dollars":   risk_dollars,
        "rsi":            rsi,
        "ema_dev":        ema_dev,
        "orh":            orh,
        "orl":            orl,
        "scanner_signal": scanner_signal,
        "rvol":           rvol,
        "spy_regime":     spy_regime,
        "spy_change":     spy_change,
        "success":        result.success,
        "order_id":       result.order_id,
        "error":          result.error,
    }
    log = load_log()
    log.append(entry)
    save_log(log)
    return entry


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City auto-executor")
    parser.add_argument("--symbol",   required=True)
    parser.add_argument("--price",    required=True, type=float)
    parser.add_argument("--orh",      required=True, type=float,
                        help="Opening range high from Tri-City scanner")
    parser.add_argument("--orl",      required=True, type=float,
                        help="Opening range low from Tri-City scanner")
    parser.add_argument("--rsi",      required=True, type=float)
    parser.add_argument("--ema_dev",  required=True, type=float,
                        help="EMA deviation %% from scanner table")
    parser.add_argument("--signal",   required=True,
                        help='Signal text from scanner, e.g. "BREAKOUT"')
    parser.add_argument("--setup",    required=True,
                        choices=["BREAKOUT", "CONTINUATION", "PULLBACK", "EMA20_PULLBACK",
                                 "FADE", "SUPERTREND_FLIP"])
    parser.add_argument("--cup",         action="store_true",
                        help="Cup pattern detected (high-conviction flag from scanner)")
    parser.add_argument("--htf",         action="store_true",
                        help="High & Tight Flag: +90%% in 3 months — bypasses candle/pennant size penalties")
    parser.add_argument("--bb_squeeze",  action="store_true",
                        help="Bollinger Band squeeze detected at open (intraday-compressed, coiled)")
    parser.add_argument("--rvol",         default=None, type=float,
                        help="Scanner RVOL value. If provided, skips internal yfinance RVOL "
                             "calculation and uses this value for guard and sizing.")
    parser.add_argument("--vwap",         default=None, type=float,
                        help="Current session VWAP. If provided, tightens stop to just below "
                             "VWAP when that gives a better (higher) stop than the standard stop.")
    parser.add_argument("--candle_type", default=None,
                        help="Override candle type (HAMMER/NEUTRAL/BEARISH/DOJI). "
                             "Auto-detected from last 1-min bar if not provided.")
    parser.add_argument("--gap",          default=None, type=float,
                        help="Gap-up percent from premarket scanner (used for earnings gap RVOL bypass)")
    parser.add_argument("--earnings",     action="store_true",
                        help="Earnings catalyst flag — allows lower RVOL floor (EARNINGS_GAP_RVOL_FLOOR)")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Print signal without placing orders")
    parser.add_argument("--override-cutoff", action="store_true",
                        help="Allow entry past the time cutoff (user-confirmed late trade)")
    parser.add_argument("--force",       action="store_true",
                        help="Bypass RVOL floor, already-executed, and PM guards (user override)")
    parser.add_argument("--quiet",       action="store_true",
                        help="Suppress routine guard-failure output; only print POST_CUTOFF_SIGNAL "
                             "and execution results (reduces Claude context bloat on each 3-min cycle)")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    now    = datetime.now(CT)
    today  = now.strftime("%Y-%m-%d")

    # ── Guard 0: pre-ORB block ─────────────────────────────────────────────────
    # Signals must not execute until the opening range has closed and ORH/ORL are
    # locked.  Premarket ORH values are frozen scanner prices — not valid levels.
    # Lock time = 8:30 AM CT + ORB_MINUTES.
    if not args.dry_run:
        orb_lock = now.replace(hour=8, minute=30, second=0, microsecond=0) + timedelta(minutes=ORB_MINUTES)
        if now < orb_lock:
            print(
                f"PRE_ORB: {now.strftime('%H:%M CT')} — ORB closes at "
                f"{orb_lock.strftime('%H:%M CT')} ({ORB_MINUTES}-min). "
                f"Levels not locked yet — skipping {args.setup} on {symbol}."
            )
            sys.exit(0)

    # Guard 0b: PULLBACK signals blocked during price discovery window
    # Cutoff = 8:30 AM CT + ORB_MINUTES + 15 min (trend establishment buffer)
    # 5-min ORB → 8:50 AM, 15-min ORB → 9:00 AM, 30-min ORB → 9:15 AM
    # BREAKOUT and CONTINUATION unrestricted (tighter ORH confirmation guards)
    if args.setup in ("PULLBACK",) and not args.dry_run:
        pullback_cutoff = now.replace(hour=8, minute=30, second=0, microsecond=0) + timedelta(minutes=ORB_MINUTES + 15)
        if now < pullback_cutoff:
            print(
                f"PRE_PULLBACK: {now.strftime('%H:%M CT')} — PULLBACK entries blocked until "
                f"{pullback_cutoff.strftime('%H:%M CT')} ({ORB_MINUTES}-min ORB + 15 min buffer). "
                f"Skipping {symbol}."
            )
            sys.exit(0)

    # Guard 0c: MIN_RSI_PULLBACK — set to 999 in .env to disable PULLBACK entries entirely
    # Pullback signal avg: -0.12R over 37 trades. Set to 999 until edge is confirmed.
    MIN_RSI_PULLBACK = float(os.getenv("MIN_RSI_PULLBACK", "0"))
    if args.setup == "PULLBACK" and args.rsi < MIN_RSI_PULLBACK and not args.dry_run:
        if not args.quiet:
            print(f"SKIP: PULLBACK disabled (MIN_RSI_PULLBACK={MIN_RSI_PULLBACK:.0f}, RSI={args.rsi:.1f}).")
        sys.exit(0)

    # Guard 0c.5: PULLBACK master kill switch
    if args.setup == "PULLBACK" and not PULLBACK_ENABLED:
        if not args.quiet:
            print(f"SKIP: PULLBACK disabled (PULLBACK_ENABLED=false in .env).")
        sys.exit(0)

    # Guard 0d: PULLBACK Parkinson trending gate (Davey Ch. 7).
    # PULLBACK has a 9% win rate on mean-reverting stocks — these need to be filtered.
    # Require park_trending=True (Parkinson vol trending flag from premarket scanner).
    # Kill switch: PULLBACK_TRENDING_REQUIRED=false in .env to disable.
    if args.setup == "PULLBACK" and PULLBACK_TRENDING_REQUIRED and not args.dry_run:
        trending = None
        try:
            _cands_file = WORKSPACE / "shared" / "tri-city-candidates.json"
            if _cands_file.exists():
                _cdata = json.loads(_cands_file.read_text())
                for _c in _cdata.get("candidates", []):
                    if _c.get("symbol") == symbol:
                        trending = _c.get("park_trending")
                        break
        except Exception:
            pass
        if trending is False:
            if not args.quiet:
                print(f"SKIP: PULLBACK on {symbol} blocked — park_trending=False "
                      f"(mean-reverting regime, not trending). "
                      f"Set PULLBACK_TRENDING_REQUIRED=false in .env to override.")
            sys.exit(0)

    # Finding 2: PULLBACK only valid when price is at or above EMA (EMA Dev% >= 0).
    # Bulkowski: entries below EMA (-0.5% to 0%) have lower follow-through.
    if args.setup == "PULLBACK" and args.ema_dev < 0.0:
        print(f"SKIP: PULLBACK requires EMA Dev% >= 0.0% (got {args.ema_dev:+.2f}%). "
              f"Price is still below EMA — wait for reclaim.")
        sys.exit(0)

    # Guard 0e: PULLBACK ORB spread filter — tiny opening range = no level to trade against.
    # A sub-0.25% ORH/ORL spread means the stock barely moved in the ORB; there's no
    # meaningful support/resistance to anchor the pullback setup.
    if args.setup == "PULLBACK" and PULLBACK_MIN_ORB_SPREAD_PCT > 0 and args.orh > 0 and args.orl > 0:
        _ref_price = args.price if args.price > 0 else args.orh
        orb_spread_pct = (args.orh - args.orl) / _ref_price * 100
        if orb_spread_pct < PULLBACK_MIN_ORB_SPREAD_PCT:
            if not args.quiet:
                print(f"SKIP: PULLBACK on {symbol} blocked — ORB spread "
                      f"{orb_spread_pct:.2f}% < min {PULLBACK_MIN_ORB_SPREAD_PCT:.2f}%. "
                      f"(ORH ${args.orh:.2f} / ORL ${args.orl:.2f} — range too tight). "
                      f"Set PULLBACK_MIN_ORB_SPREAD_PCT=0 in .env to disable.")
            sys.exit(0)

    cup_tag = " + CUP 🏆" if args.cup else ""
    if not args.quiet:
        print("=" * 64)
        print(f"TRI-CITY EXECUTE — {now.strftime('%Y-%m-%d %H:%M CT')}")
        print(f"Symbol: {symbol} | Setup: {args.setup}{cup_tag} | Price: ${args.price:.2f}")
        print(f"ORH: ${args.orh:.2f} | ORL: ${args.orl:.2f} | "
              f"RSI: {args.rsi:.1f} | EMA Dev%: {args.ema_dev:+.2f}%")
        print("=" * 64)

    # ── Guard 1: already executed ──────────────────────────────────────────────
    if already_executed_today(symbol, args.setup) and not getattr(args, "force", False):
        if not args.quiet:
            print(f"SKIP: {args.setup} already executed for {symbol} today.")
        sys.exit(0)

    # ── Guard 2: already in position ───────────────────────────────────────────
    if already_in_position(symbol):
        if not args.quiet:
            print(f"SKIP: Already holding {symbol}.")
        sys.exit(0)

    # ── Correlation warning (advisory — does not block) ────────────────────────
    corr_warnings = check_correlation_warning(symbol)
    if not args.quiet:
        for w in corr_warnings:
            print(w)

    # ── Guard 3: max positions (time-bucketed; FADE uses own slot pool) ───────
    if check_max_positions(setup=args.setup):
        bucket = "fade" if args.setup == "FADE" else ("morning" if _morning_window(now) else "afternoon")
        log_missed_slot(symbol, args.setup, args, getattr(args, "rvol", None) or 0.0, blocked_by=f"{bucket}_slots")
        if not args.quiet:
            print(f"SKIP: {bucket.capitalize()} slots full.")
        sys.exit(0)

    # ── Guard 4: daily loss limit ──────────────────────────────────────────────
    if check_daily_loss_limit(today):
        if not args.quiet:
            print(f"SKIP: Daily loss limit (${MAX_DAILY_LOSS:.0f}) reached.")
        sys.exit(0)

    # ── Guard 4b: late-entry cutoff — no new positions after 1:30 PM CT ─────────
    # Trades entered after 1:30 PM CT have <90 min to reach T1 before the 2:45 PM
    # EOD close sweep. SPCE 6/1 entered at 2:46 PM ET (1:46 PM CT) — this blocks that.
    last_entry = now.replace(hour=LAST_ENTRY_HOUR, minute=LAST_ENTRY_MINUTE, second=0, microsecond=0)
    if not args.dry_run and now >= last_entry:
        if not args.quiet:
            print(f"SKIP: Past last-entry cutoff {LAST_ENTRY_HOUR:02d}:{LAST_ENTRY_MINUTE:02d} CT — "
                  f"no new positions this late.")
        sys.exit(0)

    # ── Guard 4c: lunch no-entry window (disabled by default, LUNCH_NO_ENTRY=false) ─
    if not args.dry_run and check_lunch_window(now):
        if not args.quiet:
            print(f"SKIP: Lunch window {LUNCH_START[0]:02d}:{LUNCH_START[1]:02d}–"
                  f"{LUNCH_END[0]:02d}:{LUNCH_END[1]:02d} CT — no new entries.")
        sys.exit(0)

    # ── Guard 5: market regime ──────────────────────────────────────────────────
    spy_regime, spy_change = get_spy_regime()
    spy_str = f"{spy_change:+.2f}%" if spy_change is not None else "N/A"
    if not args.quiet:
        print(f"SPY regime: {spy_regime} ({spy_str})")
    if spy_regime == "BEAR" and not args.dry_run and args.setup != "SUPERTREND_FLIP":
        print(f"SKIP: SPY bearish ({spy_str}) — blocking LONG entries.")
        sys.exit(0)
    elif spy_regime == "BEAR" and args.setup == "SUPERTREND_FLIP":
        print(f"WARN: SPY bearish ({spy_str}) — proceeding with SUPERTREND_FLIP (trend-follow override)")

    # ── Guard 5b: ATR volatility circuit breaker (Davey Ch. 6) ───────────────────
    # Blocks all entries when SPY is experiencing an extreme volatility spike (2.5x+ normal).
    # Normal high-vol gap days are NOT blocked — only market-shock events.
    # Kill switch: VOLATILITY_CHECK_ENABLED=false in .env to disable.
    if not args.dry_run and check_atr_circuit_breaker():
        print(f"SKIP: ATR circuit breaker — SPY volatility spike detected "
              f"(ATR(5) > {VOLATILITY_ATR_CEILING:.1f}x ATR(20) baseline). "
              f"Set VOLATILITY_CHECK_ENABLED=false to override.")
        sys.exit(0)

    # ── Guard 6: relative volume (setup-specific + afternoon floor) ───────────
    # If --rvol passed from scanner table, use it directly (avoids yfinance
    # premarket-inflation bug early in session). Fall back to internal calc only
    # when scanner value is unavailable.
    rvol = args.rvol if args.rvol is not None else get_rvol(symbol)
    rvol_str = f"{rvol:.2f}x" if rvol is not None else "N/A"
    is_afternoon = now.hour > PM_START_HOUR or (now.hour == PM_START_HOUR and now.minute >= PM_START_MINUTE)
    # #4: each setup has its own minimum; afternoon tightens the floor further
    _setup_rvol_floor = {
        "BREAKOUT":         BREAKOUT_MIN_RVOL,
        "CONTINUATION":     CONTINUATION_MIN_RVOL,
        "PULLBACK":         PULLBACK_MIN_RVOL,
        "EMA20_PULLBACK":   EMA20_PB_MIN_RVOL,
        "SUPERTREND_FLIP":  0.0,   # Supertrend signal — no RVOL floor
    }
    base_floor   = _setup_rvol_floor.get(args.setup, MIN_RVOL)
    rvol_floor   = max(base_floor, PM_MIN_RVOL) if is_afternoon else base_floor
    # Earnings gap bypass: large-cap gapping >10% on earnings catalyst valid at much lower RVOL
    # DELL 0.6x × 10M+ daily shares = millions of shares in hours — flat RVOL floor misses these
    gap_pct = args.gap if args.gap is not None else 0.0
    if getattr(args, "earnings", False) and gap_pct >= 10.0:
        rvol_floor = min(rvol_floor, EARNINGS_GAP_RVOL_FLOOR)
    pm_note      = ", afternoon" if is_afternoon else ""
    earnings_note = f", earnings gap bypass ({EARNINGS_GAP_RVOL_FLOOR:.1f}x)" if (getattr(args, "earnings", False) and gap_pct >= 10.0) else ""
    if not args.quiet:
        print(f"RVol: {rvol_str} ({args.setup} min {rvol_floor:.2f}x{pm_note}{earnings_note})")
    if rvol is not None and rvol < rvol_floor and not args.dry_run and not getattr(args, "force", False):
        if not args.quiet:
            print(f"SKIP: RVol {rvol_str} below {args.setup} minimum {rvol_floor:.2f}x.")
        sys.exit(0)

    # ── Guard 7: parabolic ORB check + RSI overbought ─────────────────────────
    # EMA Dev was the wrong metric — gap stocks always show high EMA dev relative
    # to a lagged EMA20 regardless of how clean the intraday move is.
    # ORB range % measures whether the OPENING CANDLE ITSELF was explosive:
    #   (orh - orl) / orl × 100 > PARABOLIC_ORB_RANGE_PCT → gap exhausted at open
    # IMPORTANT: Only applies within the first 90 min (before PARABOLIC_WINDOW CT).
    # After 90 min the ORB is stale — intraday breakouts are fresh setups, not ORB plays.
    # Example: TSSI 9:36 CT — ORB range 12.9% → blocked (early, parabolic)
    #          UMAC 12:04 CT — ORB range 10.6% → allowed (4 hrs later, new breakout)
    if args.setup in ("BREAKOUT", "CONTINUATION"):
        # T1-already-passed guard applies even in dry-run — if price is already at/above
        # T1 the setup is invalid regardless of whether we're simulating.
        current_t1 = round(args.orh * (1 + T1_PCT / 100), 2)
        if args.price >= current_t1:
            print(
                f"SKIP: Price ${args.price:.2f} already at or beyond T1 ${current_t1:.2f} "
                f"({T1_PCT:.0f}% above ORH ${args.orh:.2f}) — no upside left in the setup."
            )
            sys.exit(0)

    if args.setup in ("BREAKOUT", "CONTINUATION") and not args.dry_run:
        parabolic_window = now.replace(
            hour=PARABOLIC_WINDOW_HOUR, minute=PARABOLIC_WINDOW_MINUTE,
            second=0, microsecond=0
        )
        within_parabolic_window = now < parabolic_window
        if within_parabolic_window and args.orl > 0:
            orb_range_pct = (args.orh - args.orl) / args.orl * 100
            if orb_range_pct > PARABOLIC_ORB_RANGE_PCT:
                print(
                    f"SKIP: ORB range {orb_range_pct:.1f}% > {PARABOLIC_ORB_RANGE_PCT:.1f}% ceiling "
                    f"({now.strftime('%H:%M CT')} — within 90-min parabolic window). "
                    f"Gap exhausted at open. Consider FADE signal instead."
                )
                sys.exit(0)
        if args.rsi >= BREAKOUT_MAX_RSI:
            print(
                f"SKIP: RSI {args.rsi:.1f} at or above {BREAKOUT_MAX_RSI:.0f} "
                f"cap — overbought, skip and wait for re-base."
            )
            sys.exit(0)

    # ── FADE: gap-and-crap short ───────────────────────────────────────────────
    # FADE is a short-side scalp on exhaustion gaps. The ORB was parabolic, the stock
    # has failed to hold above ORH, and the move is reversing within the first 90 min.
    # Entry: short at current price. Stop: above ORH + FADE_STOP_BUFFER%.
    # Target: -FADE_T1_PCT% from entry. 100% exit at T1 (no T2/T3).
    if args.setup == "FADE":
        if args.orl > 0:
            orb_range_pct = (args.orh - args.orl) / args.orl * 100
            if orb_range_pct < FADE_ORB_MIN_PCT:
                print(f"SKIP: FADE requires ORB range >= {FADE_ORB_MIN_PCT:.1f}% (got {orb_range_pct:.1f}%).")
                sys.exit(0)
        gap_pct_fade = args.gap if args.gap is not None else 0.0
        if gap_pct_fade < GAP_FADE_MIN_PCT:
            print(f"SKIP: FADE requires gap >= {GAP_FADE_MIN_PCT:.1f}% (got {gap_pct_fade:.1f}%).")
            sys.exit(0)
        fade_entry  = args.price
        fade_stop   = round(args.orh * (1 + FADE_STOP_BUFFER / 100), 2)
        fade_t1     = round(fade_entry * (1 - FADE_T1_PCT / 100), 2)
        fade_risk   = round(fade_stop - fade_entry, 2)
        if fade_risk <= 0:
            print(f"SKIP: FADE stop ${fade_stop} is below entry ${fade_entry} — price already above ORH, not a fade.")
            sys.exit(0)
        try:
            equity = float(get_account_equity())
        except Exception:
            equity = FIXED_RISK / (RISK_PCT / 100)
        fade_shares = max(1, int((equity * RISK_PCT / 100) / fade_risk))
        print("=" * 64)
        print(f"TRI-CITY FADE (SHORT) — {now.strftime('%Y-%m-%d %H:%M CT')}")
        print(f"Symbol: {symbol} | Gap: {gap_pct_fade:.1f}% | ORB range: {orb_range_pct:.1f}%")
        print(f"Entry (short): ${fade_entry:.2f}  |  Stop: ${fade_stop:.2f}  |  T1: ${fade_t1:.2f} (-{FADE_T1_PCT:.0f}%)")
        print(f"Shares: {fade_shares}  |  Risk: ${round(fade_shares * fade_risk, 2):.2f}")
        print("=" * 64)
        if args.dry_run:
            print("\n[DRY RUN] — no order placed.")
            return
        try:
            from alpaca.trading.client import TradingClient
            from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
            from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
            client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
            # Short entry (market)
            entry_req = MarketOrderRequest(
                symbol=symbol, qty=fade_shares, side=OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
            )
            entry_order = client.submit_order(entry_req)
            # OCO: cover at T1 (limit buy) + stop above ORH (stop buy)
            cover_req = LimitOrderRequest(
                symbol=symbol, qty=fade_shares, side=OrderSide.BUY,
                type="limit", time_in_force=TimeInForce.GTC,
                order_class=OrderClass.OCO,
                take_profit={"limit_price": str(fade_t1)},
                stop_loss={"stop_price": str(fade_stop)},
            )
            cover_order = client.submit_order(cover_req)
            print(f"\n✅ FADE ORDERS PLACED")
            print(f"   Short entry: {entry_order.id}")
            print(f"   Cover OCO:   {cover_order.id}")
            log_execution(symbol, "FADE", {
                "entry_price": fade_entry, "stop_loss": fade_stop,
                "target_1": fade_t1, "target_2": fade_t1, "target_3": fade_t1,
                "position_size": fade_shares,
            }, type("R", (), {"success": True, "order_id": str(entry_order.id),
                              "avg_fill_price": fade_entry, "shares_filled": fade_shares})(),
                rvol=rvol, spy_regime=spy_regime, spy_change=spy_change,
                cup=False, bb_squeeze=False, rsi=args.rsi, ema_dev=args.ema_dev,
                scanner_signal="FADE", orh=args.orh, orl=args.orl, candle_type="FADE")
        except Exception as e:
            print(f"❌ FADE execution failed: {e}")
            sys.exit(1)
        return

    # ── Guard 8: SMA50 entry filter (Stage 2 — Minervini/Raschke) ────────────────
    # Block BREAKOUT/CONTINUATION entries when price is below the SMA50.
    # Stage 2 uptrends (price > SMA50 > SMA100) have the highest follow-through.
    # Controlled by SMA50_FILTER=true in .env — defaults to false (non-breaking).
    SMA50_FILTER = os.getenv("SMA50_FILTER", "false").lower() == "true"
    if SMA50_FILTER and args.setup in ("BREAKOUT", "CONTINUATION") and not args.dry_run:
        try:
            _cands_path = WORKSPACE / "shared" / "tri-city-candidates.json"
            if _cands_path.exists():
                _cands = json.loads(_cands_path.read_text()).get("candidates", [])
                _sma50 = next((c.get("sma50") for c in _cands if c.get("symbol") == symbol), None)
                if _sma50 and _sma50 > 0 and args.price < _sma50:
                    if not args.quiet:
                        print(
                            f"SKIP: SMA50 filter — {symbol} ${args.price:.2f} < SMA50 ${_sma50:.2f} "
                            f"(Stage 2 guard — price must be above 50-day MA)."
                        )
                    logger.info(f"Guard 8 SMA50: {symbol} ${args.price:.2f} < SMA50 ${_sma50:.2f} — skip")
                    sys.exit(0)
        except Exception as _g8e:
            logger.debug(f"Guard 8 SMA50 lookup: {_g8e}")

    # ── Candle type (Finding 6) — auto-detect if not passed ───────────────────
    candle_type = (args.candle_type or "").upper() or get_candle_type(symbol)
    candle_note = ""
    if args.setup == "PULLBACK":
        if getattr(args, "htf", False):
            candle_note = " ← HTF bypass (no penalty)"
        elif candle_type not in ("HAMMER", "UNKNOWN"):
            candle_note = f" ← -25% size ({candle_type})"
        if not args.cup and LOOSE_CONSOL_PENALTY < 1.0 and not getattr(args, "htf", False):
            candle_note += f" + loose -{ round((1-LOOSE_CONSOL_PENALTY)*100):.0f}%"
    print(f"Candle: {candle_type}{candle_note}")

    # ── Build signal ───────────────────────────────────────────────────────────
    signal       = calculate_signal(symbol, args.price, args.orh, args.setup,
                                    args.ema_dev, rvol=rvol, candle_type=candle_type,
                                    cup=args.cup, htf=getattr(args, "htf", False),
                                    bb_squeeze=getattr(args, "bb_squeeze", False),
                                    vwap=getattr(args, "vwap", None))

    # Fix 4: tighter T1 for large-cap earnings gap plays.
    # Large-caps gapping 15%+ on earnings consolidate the gap rather than extending it.
    # DELL today: +31.8% gap, T1 at +7% = $493 — never came close. At 3.5% → $477, achievable.
    EARNINGS_T1_PCT_VAL = float(os.getenv("EARNINGS_T1_PCT", str(T1_PCT)))
    if getattr(args, "earnings", False) and gap_pct >= 15.0 and EARNINGS_T1_PCT_VAL < T1_PCT:
        original_t1 = signal["target_1"]
        signal["target_1"] = round(args.price * (1 + EARNINGS_T1_PCT_VAL / 100), 2)
        logger.info(
            f"EARNINGS_T1_PCT: T1 {original_t1:.2f} → {signal['target_1']:.2f} "
            f"({T1_PCT:.1f}% → {EARNINGS_T1_PCT_VAL:.1f}%, earnings gap {gap_pct:.1f}%)"
        )

    risk_dollars = round(
        signal["position_size"] * (signal["entry_price"] - signal["stop_loss"]), 2
    )

    # Gap #1 guard: T1 yield must cover minimum banking threshold.
    # If position is too small to produce $DOLLAR_T1_TRIGGER at T1, the free-ride
    # trigger in position_manager can never fire — trade enters with no banking mechanism.
    t1_yield = round((signal["target_1"] - signal["entry_price"]) * (signal["position_size"] // 2), 2)
    if t1_yield < DOLLAR_T1_TRIGGER and not args.dry_run:
        print(
            f"SKIP: T1 yield ${t1_yield:.0f} ({signal['position_size']}sh × "
            f"${signal['target_1'] - signal['entry_price']:.2f}/sh) < "
            f"${DOLLAR_T1_TRIGGER:.0f} minimum. Position too small to bank meaningfully."
        )
        sys.exit(0)

    print(f"\nSignal:")
    print(f"  Entry:  ${signal['entry_price']:.2f}")
    print(f"  Stop:   ${signal['stop_loss']:.2f}  "
          f"(${signal['entry_price'] - signal['stop_loss']:.2f}/share)")
    _t1_pct_display = round((signal['target_1'] / args.price - 1) * 100, 1)
    print(f"  T1:     ${signal['target_1']:.2f}  (+{_t1_pct_display:.1f}%) — sell 40%  [T1 yield: ${t1_yield:.0f}]")
    print(f"  T2:     ${signal['target_2']:.2f}  (+{T2_PCT:.0f}%) — sell 35%")
    print(f"  T3:     ${signal['target_3']:.2f}  (+{T3_PCT:.0f}%) — trail 25%")
    print(f"  Size:   {signal['position_size']} shares")
    print(f"  Risk:   ${risk_dollars:.2f}")
    if args.cup:
        print(f"  Cup:    YES — high-conviction setup")
    if getattr(args, "htf", False):
        print(f"  HTF:    YES — High & Tight Flag (Bulkowski 82% target, 15% failure)")
    if getattr(args, "bb_squeeze", False):
        print(f"  BB:     SQUEEZE — intraday band compressed, coiled for expansion")

    if args.dry_run:
        print("\n[DRY RUN] — no order placed.")
        return

    # ── Execute ────────────────────────────────────────────────────────────────
    if not args.quiet:
        print("\nPlacing 50-25-25 bracket orders via Alpaca...")
    result = execute_tri_city_trade("tri_city", signal)

    if result.success:
        slippage = round(result.avg_fill_price - args.price, 4)
        log_execution(symbol, args.setup, signal, result,
                      rvol=rvol, spy_regime=spy_regime, spy_change=spy_change,
                      cup=args.cup, bb_squeeze=getattr(args, "bb_squeeze", False),
                      rsi=args.rsi, ema_dev=args.ema_dev,
                      scanner_signal=args.signal, orh=args.orh, orl=args.orl,
                      candle_type=candle_type,
                      actual_fill_price=result.avg_fill_price)
        if args.quiet:
            cup_flag = " CUP" if args.cup else ""
            print(f"✅ {symbol} {args.setup}{cup_flag} {result.shares_filled}sh @${result.avg_fill_price:.2f} stop=${signal['stop']:.2f} (slippage {slippage:+.2f})")
        else:
            print(f"\n✅ ORDERS PLACED")
            print(f"   Order ID: {result.order_id}")
            print(f"   Shares:   {result.shares_filled}")
            print(f"   Signal:   ${args.price:.2f}")
            print(f"   Fill:     ${result.avg_fill_price:.2f}  (slippage {slippage:+.4f})")
            print(f"   Logged:   {LOG_FILE}")
    else:
        print(f"❌ {symbol} EXECUTION FAILED: {result.error}")
        log_execution(symbol, args.setup, signal, result,
                      rvol=rvol, spy_regime=spy_regime, spy_change=spy_change,
                      cup=args.cup, bb_squeeze=getattr(args, "bb_squeeze", False),
                      rsi=args.rsi, ema_dev=args.ema_dev,
                      scanner_signal=args.signal, orh=args.orh, orl=args.orl,
                      candle_type=candle_type)
        sys.exit(1)


if __name__ == "__main__":
    main()
