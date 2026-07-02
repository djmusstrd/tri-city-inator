#!/usr/bin/env python3
"""
TRI-CITY POSITION MANAGER — Post-entry management for open Tri-City positions.

Called every 3 minutes as part of the tri_city_monitor.py loop.

Actions (in order):
  0. FREE RIDE:   Bank 50% when unrealized >= max($300, 1R) AND price >= entry+2%; stop → entry-$0.05
  1. T1 CHECK:    If price >= T1% target → stop → entry-$0.05 (% trigger fallback)
  2. T2 CHECK:    If price >= T2 → move stop to T2 level (lock T2 gains on T3)
  3. TRAILING:    Bar close < EMA20 → close T3 lot (free ride only, after breakeven set)
  4. EOD CLOSE:   If time >= 2:45 PM CT → cancel all orders → close all positions

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
EOD_HOUR            = int(os.getenv("EOD_HOUR",             "14"))
EOD_MINUTE          = int(os.getenv("EOD_MINUTE",           "45"))
FREE_RIDE_PCT       = float(os.getenv("FREE_RIDE_PCT",       "3.0"))
T3_TRAIL_PCT        = float(os.getenv("T3_TRAIL_PCT",        "5.0"))
PULLBACK_TIMEOUT     = int(os.getenv("PULLBACK_TIMEOUT_MIN",   "25"))    # Fix A
PULLBACK_FAIL_BUFFER = float(os.getenv("PULLBACK_FAIL_BUFFER", "0.005")) # Fix B: 0.5% buffer below entry
RVOL_EXIT_FLOOR      = float(os.getenv("RVOL_EXIT_FLOOR",      "1.0"))  # #5: collapse exit
RVOL_LOOKBACK       = int(os.getenv("RVOL_LOOKBACK",         "20"))

# ── Pro T1 banking rules ──────────────────────────────────────────────────────
# Pro fix 1: bank at $DOLLAR_T1_TRIGGER OR 1R — whichever is greater
# Pro fix 2: price must be >= entry*(1+T1_MIN_MOVE_PCT%) before $ trigger activates
# Pro fix 3: stop set to entry - BREAKEVEN_BUFFER (not exact entry) to survive noise
DOLLAR_T1_TRIGGER   = float(os.getenv("DOLLAR_T1_TRIGGER",   "300"))   # min $ unrealized to bank T1
T1_MIN_MOVE_PCT     = float(os.getenv("T1_MIN_MOVE_PCT",      "2.0"))   # min % from entry before trigger
BREAKEVEN_BUFFER    = float(os.getenv("BREAKEVEN_BUFFER",     "0.05"))  # stop = entry - $0.05
IDLE_EXIT_HOURS     = float(os.getenv("IDLE_EXIT_HOURS",      "1.5"))   # close if flat after this many hours
IDLE_MIN_MOVE_PCT   = float(os.getenv("IDLE_MIN_MOVE_PCT",    "2.0"))   # "flat" = unrealized < this %

# ── Quick-lock stop promotion ──────────────────────────────────────────────────
# Promotes stop to breakeven when trade is up $QUICK_LOCK_MIN_DOLLAR, before the
# full free-ride $300 threshold. No share selling — purely stop protection.
QUICK_LOCK_MIN_DOLLAR   = float(os.getenv("QUICK_LOCK_MIN_DOLLAR",   "100"))  # $ unrealized to trigger
QUICK_LOCK_MIN_HOLD_MIN = float(os.getenv("QUICK_LOCK_MIN_HOLD_MIN",   "3"))  # min minutes open (avoid spread noise)
EXEC_LOG            = WORKSPACE / "logs" / "tri-city-executions.json"

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

_PM_LOG_DIR = Path.home() / "tri-city-inator" / "logs"
_PM_LOG_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(_PM_LOG_DIR / "tri-city-position-manager.log")],
    force=True,  # override any handlers set by imported modules
)
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

        # VWAP from snapshot (IEX feed — no SIP required)
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol, feed="iex"))
        s = snap.get(symbol)
        vwap = float(s.daily_bar.vwap) if s and s.daily_bar else None

        # EMA20 from 1-min bars via IEX feed
        now = datetime.now(CT)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=now - timedelta(minutes=60),
            end=now,
            feed="iex",
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


# ── RVOL helper (#5) ──────────────────────────────────────────────────────────

def get_rvol(symbol: str) -> float | None:
    """
    RVol = today's cumulative volume / (20-day avg daily vol × fraction of day elapsed).
    Same logic as tri_city_execute.py — duplicated here to keep imports simple.
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

        hist_vols   = list(hist)[-RVOL_LOOKBACK:]
        avg_daily   = sum(hist_vols) / len(hist_vols)
        open_ct     = now.replace(hour=8, minute=30, second=0, microsecond=0)
        close_ct    = now.replace(hour=15, minute=0,  second=0, microsecond=0)
        total_sec   = (close_ct - open_ct).total_seconds()
        elapsed_sec = max(60, (now - open_ct).total_seconds())
        fraction    = min(1.0, elapsed_sec / total_sec)
        expected    = avg_daily * fraction
        if expected <= 0:
            return None
        return round(today_vol / expected, 2)
    except Exception as e:
        logger.warning(f"get_rvol {symbol}: {e}")
        return None


# ── Last completed bar close (Finding 4) ─────────────────────────────────────

def get_last_bar_close(symbol: str) -> float | None:
    """
    Return the close of the most recent completed 1-min bar from Alpaca (IEX feed).
    Used by Fix B so intrabar wicks (live tick) can't trigger a premature exit.
    Falls back to None if unavailable — caller uses live price as fallback.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
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
            start=now - timedelta(minutes=45),
            end=now,
            feed="iex",
        )
        bars = client.get_stock_bars(req)
        df   = bars.df
        if df.empty:
            return None
        return round(float(df["close"].iloc[-1]), 4)
    except Exception as e:
        logger.warning(f"get_last_bar_close {symbol}: {e}")
        return None


# ── Supertrend indicator (mirrors tri_city_signal_detector.py) ───────────────

def compute_supertrend(bars: list[dict], period: int = 10, mult: float = 3.0) -> dict | None:
    """
    Compute Supertrend from OHLC bars. Returns {"trend": 1|-1, "band": float} or None.
    trend=1 bullish, trend=-1 bearish.
    """
    if len(bars) < period + 2:
        return None
    highs  = [b["high"]  for b in bars]
    lows   = [b["low"]   for b in bars]
    closes = [b["close"] for b in bars]
    tr = [highs[0] - lows[0]]
    for i in range(1, len(bars)):
        tr.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i]  - closes[i - 1]),
        ))
    atrs: list[float] = []
    for i in range(period - 1, len(tr)):
        atrs.append(sum(tr[i - period + 1 : i + 1]) / period)
    up_bands: list[float] = []
    dn_bands: list[float] = []
    trends:   list[int]   = []
    for i, atr_val in enumerate(atrs):
        bar_idx = i + period - 1
        hl2 = (highs[bar_idx] + lows[bar_idx]) / 2.0
        raw_up = hl2 - mult * atr_val
        raw_dn = hl2 + mult * atr_val
        if i == 0:
            up_bands.append(raw_up); dn_bands.append(raw_dn); trends.append(1)
            continue
        prev_up = up_bands[-1]; prev_dn = dn_bands[-1]
        prev_close = closes[bar_idx - 1]; curr_close = closes[bar_idx]
        prev_trend = trends[-1]
        up = max(raw_up, prev_up) if prev_close > prev_up else raw_up
        dn = min(raw_dn, prev_dn) if prev_close < prev_dn else raw_dn
        up_bands.append(up); dn_bands.append(dn)
        if prev_trend == -1 and curr_close > prev_dn:
            trend = 1
        elif prev_trend == 1 and curr_close < prev_up:
            trend = -1
        else:
            trend = prev_trend
        trends.append(trend)
    if len(trends) < 2:
        return None
    current_trend = trends[-1]
    prev_trend    = trends[-2]
    band = up_bands[-1] if current_trend == 1 else dn_bands[-1]
    return {"trend": current_trend, "prev_trend": prev_trend, "band": round(band, 4)}


def fetch_bars_for_supertrend(symbol: str) -> list[dict]:
    """Fetch last 2 hours of 1-min bars from Alpaca for Supertrend computation."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return []
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
            start=now - timedelta(hours=2),
            end=now,
            feed="iex",
        )
        df = client.get_stock_bars(req).df
        if df.empty:
            return []
        return [
            {"open": float(r["open"]), "high": float(r["high"]),
             "low": float(r["low"]),  "close": float(r["close"])}
            for _, r in df.iterrows()
        ]
    except Exception as e:
        logger.warning(f"fetch_bars_for_supertrend {symbol}: {e}")
        return []


# ── Action -2: Supertrend bearish exit ───────────────────────────────────────

def check_supertrend_exit(positions: list, today: str) -> list[str]:
    """
    Exit any open position when Supertrend flips bearish (trend: 1 → -1).

    Applies to ALL setups, not just SUPERTREND_FLIP entries — the Supertrend
    level acts as a trailing stop for any position. Uses the last 2 bars of
    the computed Supertrend series to detect a fresh flip (not a persistent
    bearish state that already existed before entry).

    Only fires when the trend JUST flipped (prev=1, current=-1). A position
    that entered while Supertrend was already bearish will not be exited until
    trend flips bearish again after a bullish period.
    """
    actions  = []
    entries  = load_executions()

    for pos in positions:
        ticker     = pos["ticker"]
        curr_price = pos["current_price"]

        entry = next(
            (e for e in reversed(entries)
             if e.get("symbol") == ticker and e.get("date") == today and e.get("success")),
            None,
        )
        if not entry:
            continue
        # Skip if already protected at T2 stop — let T2 lock do its job
        if entry.get("t2_stop_set"):
            continue
        # Skip if already flagged for ST exit this session
        if entry.get("supertrend_exit"):
            continue

        bars = fetch_bars_for_supertrend(ticker)
        if not bars:
            continue

        st = compute_supertrend(bars)
        if st is None:
            continue

        # Only act on a fresh flip: previous bar bullish, current bar bearish
        if st["trend"] == -1 and st.get("prev_trend") == 1:
            logger.warning(
                f"SUPERTREND EXIT: {ticker} trend flipped bearish — "
                f"band={st['band']:.2f}, price={curr_price:.2f}"
            )
            cancel_all_orders(ticker)
            success = close_position(ticker, reason="Supertrend bearish flip")
            if success:
                entry["supertrend_exit"] = True
                try:
                    from managers.trade_journal import log_exit, fetch_exit_price
                    exit_price = fetch_exit_price(ticker) or curr_price
                    log_exit(
                        symbol=ticker,
                        setup=entry.get("setup", "UNKNOWN"),
                        date=today,
                        exit_price=exit_price,
                        exit_reason=f"Supertrend bearish flip (band={st['band']:.2f})",
                        shares=entry.get("position_size", 0),
                    )
                except Exception as _je:
                    logger.warning(f"journal.log_exit SUPERTREND EXIT {ticker}: {_je}")
                actions.append(
                    f"SUPERTREND EXIT: {ticker} @ ${curr_price:.2f} — "
                    f"trend flipped bearish (band ${st['band']:.2f})"
                )
            else:
                actions.append(f"SUPERTREND EXIT FAILED: {ticker} — check Alpaca manually")

    if any("supertrend_exit" in (next((e for e in reversed(entries)
            if e.get("symbol") == p["ticker"] and e.get("date") == today
            and e.get("success")), {}) or {}) for p in positions):
        save_executions(entries)

    return actions


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

        # ── Fix B: immediate breakdown — bar close below entry*(1-buffer) AND ORH ──
        # Finding 4: use last completed bar close to avoid intrabar-wick false exits.
        # Finding 1: require PULLBACK_FAIL_BUFFER (0.5%) below entry, not just $0.01.
        last_close   = get_last_bar_close(ticker)
        fix_b_price  = last_close if last_close is not None else curr_price
        fix_b_thresh = round(entry_price * (1 - PULLBACK_FAIL_BUFFER), 4)

        if orh > 0 and fix_b_price < fix_b_thresh and fix_b_price < orh:
            logger.info(
                f"PULLBACK FAIL B: {ticker} bar_close=${fix_b_price:.2f} < "
                f"entry*(1-{PULLBACK_FAIL_BUFFER:.1%}) ${fix_b_thresh:.2f} AND < ORH ${orh:.2f}"
            )
            cancel_all_orders(ticker)
            success = close_position(ticker, reason="Failed pullback — below entry & ORH")
            if success:
                # Finding 5: flag this exit so already_executed_today can allow re-entry
                pnl_per_share = round(fix_b_price - entry_price, 4)
                entry["fix_b_exit"]    = True
                entry["pnl_per_share"] = pnl_per_share
                save_executions(entries)
                actions.append(
                    f"PULLBACK FAIL: {ticker} bar_close=${fix_b_price:.2f} "
                    f"— {PULLBACK_FAIL_BUFFER:.1%} below entry ${entry_price:.2f} "
                    f"and ORH ${orh:.2f} (P&L/share: ${pnl_per_share:+.4f})"
                )
                try:
                    from managers.trade_journal import log_exit, fetch_exit_price
                    exit_price_j = fetch_exit_price(ticker) or fix_b_price
                    log_exit(
                        symbol=ticker,
                        setup=entry.get("setup", "UNKNOWN"),
                        date=today,
                        exit_price=exit_price_j,
                        exit_reason=f"Failed pullback — bar close ${fix_b_price:.2f} below entry & ORH",
                        shares=entry.get("position_size", 0),
                    )
                except Exception as _je:
                    logger.warning(f"journal.log_exit failed for {ticker}: {_je}")
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
                        try:
                            from managers.trade_journal import log_exit, fetch_exit_price
                            exit_price_j = fetch_exit_price(ticker) or curr_price
                            log_exit(
                                symbol=ticker,
                                setup=entry.get("setup", "UNKNOWN"),
                                date=today,
                                exit_price=exit_price_j,
                                exit_reason=f"Pullback timeout — {PULLBACK_TIMEOUT}min no ORH reclaim",
                                shares=entry.get("position_size", 0),
                            )
                        except Exception as _je:
                            logger.warning(f"journal.log_exit failed for {ticker}: {_je}")
                    else:
                        actions.append(f"PULLBACK TIMEOUT CLOSE FAILED: {ticker} — check Alpaca")
            except Exception as e:
                logger.warning(f"check_failed_pullback time parse {ticker}: {e}")

    return actions


# ── Action -0.5: Quick-lock stop promotion ────────────────────────────────────

def check_quick_lock(positions: list, today: str, now: datetime) -> list[str]:
    """
    Promote stop to breakeven the moment a trade is up QUICK_LOCK_MIN_DOLLAR ($100).

    Fires before check_free_ride so gains are protected even when the full $300
    free-ride floor hasn't been reached. No shares are sold — purely stop movement.
    Requires the trade to be open at least QUICK_LOCK_MIN_HOLD_MIN minutes to avoid
    triggering on spread noise immediately after fill.
    """
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

        # Skip if already protected (quick-lock, breakeven, or T2 stop already set)
        if entry.get("quick_lock_set") or entry.get("breakeven_set") or entry.get("t2_stop_set"):
            continue

        unrealized_pnl = (curr_price - entry_price) * shares
        if unrealized_pnl < QUICK_LOCK_MIN_DOLLAR:
            continue

        # Enforce minimum hold time to avoid spread-noise false triggers
        try:
            time_part = entry.get("time", "").replace(" CT", "").split(" ")[0]
            h, m, s   = [int(x) for x in time_part.split(":")]
            entry_dt  = now.replace(hour=h, minute=m, second=s, microsecond=0)
            mins_open = (now - entry_dt).total_seconds() / 60
        except Exception:
            mins_open = QUICK_LOCK_MIN_HOLD_MIN  # assume eligible if time parse fails

        if mins_open < QUICK_LOCK_MIN_HOLD_MIN:
            continue

        be_stop = round(entry_price - BREAKEVEN_BUFFER, 4)
        logger.info(
            f"QUICK LOCK: {ticker} ${curr_price:.2f} — unrealized ${unrealized_pnl:.0f} "
            f">= ${QUICK_LOCK_MIN_DOLLAR:.0f} at {mins_open:.1f}min — stop → ${be_stop:.2f}"
        )

        cancel_all_orders(ticker)
        time.sleep(1.0)
        be_set = set_stop_loss(
            order_id=entry.get("order_id", ""),
            ticker=ticker,
            stop_price=be_stop,
            shares=shares,
            direction="BULLISH",
        )

        if be_set:
            entry["quick_lock_set"]   = True
            entry["quick_lock_price"] = curr_price
            entry["quick_lock_pnl"]   = round(unrealized_pnl, 2)
            modified = True
            actions.append(
                f"QUICK LOCK: {ticker} @ ${curr_price:.2f} — "
                f"${unrealized_pnl:.0f} unrealized at {mins_open:.0f}min "
                f"— stop promoted to ${be_stop:.2f} (was ${entry.get('stop_loss', '?')})"
            )
        else:
            actions.append(f"QUICK LOCK FAILED: {ticker} — stop not updated, check Alpaca")

    if modified:
        save_executions(entries)

    return actions


# ── Action 0: Free-ride check ──────────────────────────────────────────────────

def check_free_ride(positions: list, today: str) -> list[str]:
    """
    PRO T1 BANKING: Bank 50% and go to free ride as soon as possible.

    Trigger fires when ANY of the following is true (whichever comes first):
      A) % trigger: price >= entry * (1 + FREE_RIDE_PCT%)    [default 3%]
      B) $ trigger: unrealized P&L >= max($DOLLAR_T1_TRIGGER, 1R)
                    AND price >= entry * (1 + T1_MIN_MOVE_PCT%)  [min 2% move guard]

    Pro fix 1: 1R = risk_dollars from execution log — dollar trigger scales with position size
    Pro fix 2: T1_MIN_MOVE_PCT guard prevents banking on micro-moves on large positions
    Pro fix 3: Stop set to entry - BREAKEVEN_BUFFER ($0.05) not exact entry — survives spread noise
    Pro fix 4: Remaining 50% trails EMA20 bar close (handled by check_trailing)
    Pro fix 5: Net P&L (wins + losses) tracked — T1 partial logged so journal fallback is accurate
    """
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

        # ── Determine trigger ─────────────────────────────────────────────────
        # Primary: unrealized P&L >= max($DOLLAR_T1_TRIGGER, 1R) AND price >= entry+T1_MIN_MOVE_PCT%
        # The dollar floor is ALWAYS enforced — % trigger alone cannot fire below $300 unrealized.
        # This guarantees the user banks a meaningful amount, not a micro-gain from a large position.
        unrealized_pnl = (curr_price - entry_price) * shares
        one_r_dollars  = float(entry.get("risk_dollars") or 0)
        dollar_floor   = max(DOLLAR_T1_TRIGGER, one_r_dollars)  # $300 OR 1R, whichever greater
        min_move_price = entry_price * (1 + T1_MIN_MOVE_PCT / 100)

        # % trigger as an additional check — only fires if dollar floor is also met
        pct_trigger_price = entry_price * (1 + FREE_RIDE_PCT / 100) if FREE_RIDE_PCT > 0 else None
        pct_met    = pct_trigger_price is not None and curr_price >= pct_trigger_price
        dollar_met = curr_price >= min_move_price and unrealized_pnl >= dollar_floor

        # Both paths require dollar floor — no banking below $DOLLAR_T1_TRIGGER
        if not (dollar_met or (pct_met and unrealized_pnl >= dollar_floor)):
            continue

        if dollar_met:
            trigger_label = (
                f"${unrealized_pnl:.0f} unrealized >= "
                f"max(${DOLLAR_T1_TRIGGER:.0f}, 1R=${one_r_dollars:.0f}) = ${dollar_floor:.0f}"
            )
        else:
            trigger_label = (
                f"+{FREE_RIDE_PCT}% price target (${unrealized_pnl:.0f} unrealized >= ${dollar_floor:.0f} floor)"
            )

        # ── Execute T1 bank ───────────────────────────────────────────────────
        t1_shares  = max(1, shares // 2)
        t3_shares  = max(1, (shares - t1_shares) // 2)
        remaining  = shares - t1_shares
        # Pro fix 3: stop = entry - buffer, not exact entry (survives spread/noise)
        be_stop    = round(entry_price - BREAKEVEN_BUFFER, 4)

        logger.info(
            f"FREE RIDE: {ticker} ${curr_price:.2f} — {trigger_label} "
            f"— selling {t1_shares} T1 shares, stop → ${be_stop:.2f}"
        )

        cancel_all_orders(ticker)
        time.sleep(1.5)

        sold   = sell_shares_at_market(ticker, t1_shares)
        time.sleep(1.0)
        be_set = set_stop_loss(
            order_id=entry.get("order_id", ""),
            ticker=ticker,
            stop_price=be_stop,           # Pro fix 3: entry - $0.05
            shares=remaining,
            direction="BULLISH",
        )
        trail_set = place_trailing_stop(ticker, t3_shares, T3_TRAIL_PCT)

        if sold and be_set:
            entry["breakeven_set"]       = True
            entry["breakeven_price"]     = be_stop      # actual stop price logged
            entry["free_ride_triggered"] = True
            entry["free_ride_price"]     = curr_price
            entry["t1_banked_shares"]    = t1_shares
            entry["t1_banked_price"]     = curr_price
            entry["t1_banked_pnl"]       = round((curr_price - entry_price) * t1_shares, 2)
            modified = True
            trail_note = f", EMA20 trail on {t3_shares} remaining" if trail_set else ""
            actions.append(
                f"FREE RIDE: {ticker} @ ${curr_price:.2f} — {trigger_label} "
                f"— banked {t1_shares}sh (${entry['t1_banked_pnl']:+.2f}), "
                f"stop → ${be_stop:.2f}{trail_note}"
            )
            # Pro fix 5: log T1 partial to journal so net P&L fallback is accurate
            try:
                from managers.trade_journal import log_exit, fetch_exit_price
                actual_price = fetch_exit_price(ticker) or curr_price
                log_exit(
                    symbol=ticker,
                    setup=entry.get("setup", "UNKNOWN"),
                    date=today,
                    exit_price=actual_price,
                    exit_reason=f"T1 bank — {trigger_label}",
                    shares=t1_shares,
                )
            except Exception as _je:
                logger.warning(f"journal.log_exit T1 partial failed for {ticker}: {_je}")
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
            be_stop = round(entry_price - BREAKEVEN_BUFFER, 4)  # Pro fix 3
            success = set_stop_loss(
                order_id=entry.get("order_id", ""),
                ticker=ticker,
                stop_price=be_stop,       # entry - $0.05, not exact entry
                shares=max(1, shares - shares // 2),
                direction="BULLISH",
            )
            if success:
                entry["breakeven_set"]   = True
                entry["breakeven_price"] = be_stop
                modified = True
                actions.append(
                    f"T1 HIT: {ticker} @ ${curr_price:.2f} >= T1 ${t1:.2f} "
                    f"— stop moved to ${be_stop:.2f} (entry ${entry_price:.2f} - ${BREAKEVEN_BUFFER:.2f} buffer)"
                )

    if modified:
        save_executions(entries)

    return actions


# ── Action 2.5: RVOL collapse exit (#5) ───────────────────────────────────────

def check_rvol_collapse(positions: list, today: str) -> list[str]:
    """
    Exit position if RVOL collapses below RVOL_EXIT_FLOOR after entry.
    Only fires when breakeven has been set (T1 hit) — we never exit a winner
    into a stop loss just because volume dried up; the stop handles that.
    Disabled when RVOL_EXIT_FLOOR <= 0.
    """
    if RVOL_EXIT_FLOOR <= 0:
        return []

    actions = []
    entries = load_executions()

    for pos in positions:
        ticker = pos["ticker"]

        entry = None
        for e in reversed(entries):
            if e.get("symbol") == ticker and e.get("date") == today and e.get("success"):
                entry = e
                break

        if not entry:
            continue
        # Only check after T1 hit (we're in the profit zone managing T3)
        if not entry.get("breakeven_set"):
            continue
        # Skip if already checked and exited this cycle
        if entry.get("rvol_collapse_exit"):
            continue

        rvol = get_rvol(ticker)
        if rvol is None:
            continue
        if rvol >= RVOL_EXIT_FLOOR:
            continue

        logger.info(
            f"RVOL COLLAPSE: {ticker} RVol={rvol:.2f}x < floor {RVOL_EXIT_FLOOR:.2f}x "
            f"— closing T3 lot"
        )
        cancel_all_orders(ticker)
        success = close_position(ticker, reason=f"RVOL collapse ({rvol:.2f}x < {RVOL_EXIT_FLOOR:.2f}x)")
        if success:
            entry["rvol_collapse_exit"] = True
            actions.append(
                f"RVOL COLLAPSE EXIT: {ticker} @ ${pos['current_price']:.2f} "
                f"— RVol {rvol:.2f}x fell below floor {RVOL_EXIT_FLOOR:.2f}x"
            )
            try:
                from managers.trade_journal import log_exit, fetch_exit_price
                exit_price = fetch_exit_price(ticker) or pos["current_price"]
                log_exit(
                    symbol=ticker,
                    setup=entry.get("setup", "UNKNOWN"),
                    date=today,
                    exit_price=exit_price,
                    exit_reason=f"RVOL collapse ({rvol:.2f}x)",
                    shares=entry.get("position_size", 0),
                )
            except Exception as _je:
                logger.warning(f"journal.log_exit failed for {ticker}: {_je}")
        else:
            actions.append(f"RVOL COLLAPSE CLOSE FAILED: {ticker} — check Alpaca")

    return actions


# ── Action 2.5: Idle position timeout (Davey Ch. 8) ──────────────────────────
# Exit dead-money BREAKOUT/CONTINUATION trades that haven't hit T1 after IDLE_EXIT_HOURS.
# Logic: if position is flat (unrealized < IDLE_MIN_MOVE_PCT%) AND held > IDLE_EXIT_HOURS
# AND T1 has not been banked → the setup failed to follow through → free up capital.
# Skip if T1 already banked (winning trades are allowed to breathe and trail).

def check_idle_timeout(positions: list, today: str, now: datetime) -> list[str]:
    """
    Close positions that are flat after IDLE_EXIT_HOURS with no T1 hit.
    Only applies to BREAKOUT and CONTINUATION — PULLBACK has its own 25-min timeout.
    """
    actions = []
    entries = load_executions()

    for pos in positions:
        ticker     = pos["ticker"]
        curr_price = pos["current_price"]
        entry_price = pos["entry_price"]

        entry = None
        for e in reversed(entries):
            if e.get("symbol") == ticker and e.get("date") == today and e.get("success"):
                entry = e
                break

        if not entry:
            continue

        # Only apply to BREAKOUT / CONTINUATION (PULLBACK has its own timeout)
        setup = entry.get("setup", "")
        if setup not in ("BREAKOUT", "CONTINUATION"):
            continue

        # Skip if T1 already banked — winner, let it run
        if entry.get("breakeven_set"):
            continue

        # Parse entry time
        try:
            entry_time_str = entry.get("time", "")  # "HH:MM:SS CT"
            entry_hms = entry_time_str.split(" ")[0]   # "HH:MM:SS"
            h, m, s = [int(x) for x in entry_hms.split(":")]
            entry_dt = now.replace(hour=h, minute=m, second=s, microsecond=0)
        except Exception:
            continue

        hours_held = (now - entry_dt).total_seconds() / 3600.0
        if hours_held < IDLE_EXIT_HOURS:
            continue

        # Check if position is flat (< IDLE_MIN_MOVE_PCT% unrealized gain)
        unrealized_pct = ((curr_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0
        if unrealized_pct >= IDLE_MIN_MOVE_PCT:
            continue  # Position is making progress — leave it alone

        logger.info(
            f"IDLE TIMEOUT: {ticker} {setup} held {hours_held:.1f}h, "
            f"unrealized {unrealized_pct:+.2f}% < {IDLE_MIN_MOVE_PCT:.1f}% threshold"
        )
        cancel_all_orders(ticker)
        success = close_position(ticker, reason=f"Idle timeout ({hours_held:.1f}h, {unrealized_pct:+.1f}%)")
        if success:
            actions.append(
                f"IDLE TIMEOUT EXIT: {ticker} @ ${curr_price:.2f} "
                f"({setup}, held {hours_held:.1f}h, {unrealized_pct:+.1f}% — no T1)"
            )
            try:
                from managers.trade_journal import log_exit, fetch_exit_price
                exit_price = fetch_exit_price(ticker) or curr_price
                log_exit(
                    symbol=ticker,
                    setup=setup,
                    date=today,
                    exit_price=exit_price,
                    exit_reason=f"Idle timeout ({hours_held:.1f}h, flat)",
                    shares=entry.get("position_size", 0),
                )
            except Exception as _je:
                logger.warning(f"journal.log_exit failed for {ticker}: {_je}")
        else:
            actions.append(f"IDLE TIMEOUT EXIT FAILED: {ticker} — check Alpaca")

    return actions


# ── Action 3: Trailing stop (T3 lot) ──────────────────────────────────────────

def check_trailing(positions: list, today: str) -> list[str]:
    """
    For T3 (trailing) lot: exit on first bar close below EMA20.
    Only applies when breakeven has been set (T1 hit) and T2 stop not yet active.
    Uses bar close (not live price) to avoid wick-triggered exits.
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

        ema20, _ = get_intraday_ema_vwap(ticker)
        if ema20 is None:
            continue

        bar_close = get_last_bar_close(ticker)
        check_price = bar_close if bar_close is not None else curr_price

        if check_price < ema20:
            price_label = f"bar close ${bar_close:.2f}" if bar_close is not None else f"live ${curr_price:.2f}"
            logger.info(
                f"TRAIL EXIT: {ticker} {price_label} < EMA20 ${ema20:.2f}"
            )
            cancel_all_orders(ticker)
            success = close_position(ticker, reason="Trailing stop (EMA20 bar close breach)")
            if success:
                actions.append(
                    f"TRAIL EXIT: {ticker} @ ${curr_price:.2f} "
                    f"(EMA20 bar close breach — {price_label} < EMA20 ${ema20:.2f})"
                )
                try:
                    from managers.trade_journal import log_exit, fetch_exit_price
                    exit_price = fetch_exit_price(ticker) or curr_price
                    log_exit(
                        symbol=ticker,
                        setup=entry.get("setup", "UNKNOWN"),
                        date=today,
                        exit_price=exit_price,
                        exit_reason="Trailing stop (EMA20 bar close breach)",
                        shares=entry.get("position_size", 0),
                    )
                except Exception as _je:
                    logger.warning(f"journal.log_exit failed for {ticker}: {_je}")
            else:
                actions.append(f"TRAIL EXIT FAILED: {ticker} — check Alpaca manually")

    return actions


# ── Action 4: EOD close ────────────────────────────────────────────────────────

def _has_pending_sell(ticker: str) -> bool:
    """Return True if there are any open sell orders for ticker (T1/T2 in flight)."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.enums import OrderSide
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        orders = client.get_orders()
        for o in orders:
            if str(o.symbol) == ticker and o.side == OrderSide.SELL:
                return True
    except Exception as e:
        logger.warning(f"_has_pending_sell {ticker}: {e}")
    return False


def check_eod(positions: list, now: datetime) -> list[str]:
    """Close all positions at 2:45 PM CT (15 min before 3:00 PM CT market close).

    Gap #5 fix: if a T1 sell is in flight when EOD fires, wait up to 5 seconds
    for it to settle before canceling. Prevents EOD cancel racing a T1 fill
    and wiping out banked profit from the journal.
    """
    actions = []
    eod = now.replace(hour=EOD_HOUR, minute=EOD_MINUTE, second=0, microsecond=0)

    if now < eod or not positions:
        return actions

    logger.info(f"EOD at {now.strftime('%H:%M CT')} — closing {len(positions)} position(s)")

    today   = now.strftime("%Y-%m-%d")
    entries = load_executions()

    for pos in positions:
        ticker = pos["ticker"]
        # Gap #5: wait for any inflight T1/T2 sells to settle before canceling
        if _has_pending_sell(ticker):
            logger.info(f"EOD: pending sell detected for {ticker} — waiting 5s for fill")
            time.sleep(5)
        cancel_all_orders(ticker)
        time.sleep(1.5)
        success = close_position(ticker, reason="EOD auto-close 2:45 PM CT")
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


# ── Advisory: EMA20 reclaim failure ───────────────────────────────────────────
# Tracks consecutive cycles where price is below EMA20 AND below VWAP.
# After 2+ cycles → advisory message so trader can decide to exit early.
# Does NOT auto-close — that remains a manual decision.
_ema20_below_cycles: dict[str, int] = {}


def check_ema20_reclaim_advisory(positions: list, today: str) -> list[str]:
    """
    Advisory exit signal: position is below EMA20 AND VWAP for 2+ consecutive cycles.

    DELL lesson (2026-06-01): entry at $461, EMA20 at $457.59. Within minutes price
    fell below EMA20 and never reclaimed. Manual exit at ~$452 saved ~$150 vs -5% stop.
    The rule: lose EMA20 + lose VWAP = setup invalidated. Don't wait for the full stop.
    """
    advisories = []
    entries = load_executions()

    for pos in positions:
        ticker     = pos["ticker"]
        curr_price = pos["current_price"]

        entry = next(
            (e for e in reversed(entries)
             if e.get("symbol") == ticker and e.get("date") == today and e.get("success")),
            None,
        )
        if not entry:
            continue
        # Only advisory after entry; skip if already at breakeven (T1 has been banked)
        if entry.get("breakeven_set") or entry.get("t2_stop_set"):
            _ema20_below_cycles.pop(ticker, None)
            continue

        ema20, vwap = get_intraday_ema_vwap(ticker)
        if ema20 is None:
            continue

        ema_dev_pct = (curr_price - ema20) / ema20 * 100 if ema20 > 0 else 0

        below_ema20 = ema_dev_pct < -0.5   # more than 0.5% below EMA20
        below_vwap  = vwap is not None and curr_price < vwap

        if below_ema20 and below_vwap:
            _ema20_below_cycles[ticker] = _ema20_below_cycles.get(ticker, 0) + 1
            count = _ema20_below_cycles[ticker]
            if count >= 2:
                msg = (
                    f"EMA20_EXIT_ADVISORY: {ticker} @ ${curr_price:.2f} — "
                    f"below EMA20 ${ema20:.2f} ({ema_dev_pct:+.1f}%) + below VWAP "
                    f"${vwap:.2f} for {count} cycles — consider early exit"
                )
                logger.warning(msg)
                advisories.append(msg)
        else:
            _ema20_below_cycles.pop(ticker, None)

    return advisories


# ── Advisory: EMA8 bearish crossover ─────────────────────────────────────────
# EMA8 crosses below EMA20 = Ross Cameron's primary momentum exit signal.
# Fires 1-2 bars earlier than the EMA20 breach alone.


def _calc_ema(prices: list[float], period: int) -> float | None:
    """Exponential moving average from a list of closes. Returns None if insufficient data."""
    if len(prices) < period:
        return None
    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period
    for p in prices[period:]:
        ema = p * k + ema * (1 - k)
    return round(ema, 4)


def check_ema8_exit(positions: list, today: str) -> list[str]:
    """
    EMA8/EMA20 bearish cross handler — auto-exit for PULLBACK, advisory for others.

    Ross Cameron: EMA8 breach is the primary exit signal for gap-and-go plays,
    firing 1-2 bars earlier than EMA20 breach.
    Condition: price < EMA8 AND EMA8 < EMA20 (full bearish alignment).

    PULLBACK trades that haven't yet hit T1: auto-close immediately — the momentum
    thesis is invalidated and PULLBACK has historically negative avg R in this system.
    BREAKOUT / CONTINUATION: advisory only — these have wider targets and the cross
    may be transient noise on a healthy trend.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return []

    results = []
    entries = load_executions()

    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        from datetime import timedelta

        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

        for pos in positions:
            ticker     = pos["ticker"]
            curr_price = pos["current_price"]

            entry = next(
                (e for e in reversed(entries)
                 if e.get("symbol") == ticker and e.get("date") == today and e.get("success")),
                None,
            )

            try:
                now = datetime.now(CT)
                req = StockBarsRequest(
                    symbol_or_symbols=ticker,
                    timeframe=TimeFrame.Minute,
                    start=now - timedelta(minutes=60),
                    end=now,
                    feed="iex",
                )
                bars = client.get_stock_bars(req)
                df   = bars.df
                if df.empty or len(df) < 20:
                    continue

                closes = list(df["close"])
                ema8   = _calc_ema(closes, 8)
                ema20  = _calc_ema(closes, 20)

                if ema8 is None or ema20 is None:
                    continue

                if not (curr_price < ema8 and ema8 < ema20):
                    continue

                setup = entry.get("setup", "") if entry else ""
                already_protected = entry and (entry.get("breakeven_set") or entry.get("t2_stop_set"))

                if setup == "PULLBACK" and not already_protected:
                    # Auto-close — PULLBACK momentum is definitively lost
                    logger.warning(
                        f"EMA8 CROSS EXIT: {ticker} PULLBACK @ ${curr_price:.2f} — "
                        f"EMA8 ${ema8:.2f} < EMA20 ${ema20:.2f} — auto-closing"
                    )
                    cancel_all_orders(ticker)
                    success = close_position(ticker, reason="EMA8 cross exit — PULLBACK momentum lost")
                    if success:
                        results.append(
                            f"EMA8 CROSS EXIT: {ticker} @ ${curr_price:.2f} — "
                            f"EMA8 ${ema8:.2f} < EMA20 ${ema20:.2f} (PULLBACK closed)"
                        )
                        try:
                            from managers.trade_journal import log_exit, fetch_exit_price
                            exit_price = fetch_exit_price(ticker) or curr_price
                            log_exit(
                                symbol=ticker,
                                setup=setup,
                                date=today,
                                exit_price=exit_price,
                                exit_reason="EMA8 cross exit — PULLBACK momentum lost",
                                shares=entry.get("position_size", 0) if entry else pos["shares"],
                            )
                        except Exception as _je:
                            logger.warning(f"journal.log_exit EMA8 exit failed for {ticker}: {_je}")
                    else:
                        results.append(f"EMA8 CROSS EXIT FAILED: {ticker} — close manually")
                else:
                    # Advisory for BREAKOUT / CONTINUATION or already-protected positions
                    msg = (
                        f"EMA8_EXIT_ADVISORY: {ticker} @ ${curr_price:.2f} — "
                        f"EMA8 ${ema8:.2f} crossed below EMA20 ${ema20:.2f} "
                        f"(bearish momentum shift — Ross Cameron exit signal)"
                    )
                    logger.warning(msg)
                    results.append(msg)

            except Exception as e:
                logger.debug(f"check_ema8_exit {ticker}: {e}")

    except Exception as e:
        logger.debug(f"check_ema8_exit init: {e}")

    return results


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


# ── Bracket stop reconciliation ────────────────────────────────────────────────

def reconcile_bracket_stops(today: str, open_tickers: set) -> list[str]:
    """
    Detect bracket stop fills that fired via Alpaca (bypassing this manager).

    For each today's execution that has no journal exit and no open Alpaca position,
    fetch the most recent filled stop/sell order from Alpaca and log the exit.
    Returns a list of reconciliation messages (printed by caller).
    """
    actions = []
    entries = load_executions()
    today_entries = [e for e in entries if e.get("date") == today and e.get("success")]
    if not today_entries:
        return actions

    try:
        JOURNAL_PATH = WORKSPACE / "logs" / "tri-city-journal.json"
        journal = json.loads(JOURNAL_PATH.read_text()) if JOURNAL_PATH.exists() else []
    except Exception:
        journal = []

    # Gap #6 fix: index journal by trade_id so we can check exit_reason, not just presence.
    # Partial exits (T1 bank) must NOT mark a trade as covered — only closing exits count.
    _CLOSING_KEYWORDS = ("stop", "trail", "eod", "timeout", "failed pullback",
                         "rvol collapse", "reconciled")
    journal_by_id = {t["trade_id"]: t for t in journal}

    def _has_closing_exit(base_id: str) -> bool:
        """True if journal has at least one CLOSING exit for this base_id."""
        for jid, jentry in journal_by_id.items():
            if jid == base_id or jid.startswith(base_id + "-"):
                reason = jentry.get("exit_reason", "").lower()
                if any(kw in reason for kw in _CLOSING_KEYWORDS):
                    return True
        return False

    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import OrderStatus, QueryOrderStatus
        from datetime import timedelta, timezone

        tc = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    except Exception as e:
        logger.warning(f"reconcile_bracket_stops: Alpaca client init failed: {e}")
        return actions

    # Build a de-dup set: (symbol, setup) pairs we already reconciled this call
    reconciled = set()

    for entry in reversed(today_entries):
        symbol = entry.get("symbol")
        setup  = entry.get("setup", "UNKNOWN")
        key    = (symbol, setup)

        if key in reconciled:
            continue

        base_id = f"{today}-{symbol}-{setup}"
        # Gap #6: only treat as covered if a CLOSING exit is logged, not just any exit
        # (T1 partials logged via check_free_ride have "T1 bank" reason — not closing)
        covered = _has_closing_exit(base_id)
        if covered and symbol not in open_tickers:
            # Journal has a closing exit and no open position — already reconciled
            reconciled.add(key)
            continue

        if symbol in open_tickers:
            # Position still open — nothing to reconcile yet
            continue

        # Position is closed but no journal exit — find the bracket stop fill
        try:
            today_start = datetime.now(CT).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).astimezone(timezone.utc)

            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                symbols=[symbol],
                after=today_start,
                limit=20,
            )
            orders = tc.get_orders(filter=req)
        except Exception as e:
            logger.warning(f"reconcile_bracket_stops: order fetch failed for {symbol}: {e}")
            continue

        # Find the most recent filled sell (stop or market) order
        stop_fill = None
        for o in sorted(orders, key=lambda x: x.filled_at or x.submitted_at, reverse=True):
            if (o.side.value == "sell"
                    and o.status.value == "filled"
                    and o.filled_avg_price is not None):
                stop_fill = o
                break

        if not stop_fill:
            continue

        fill_price = float(stop_fill.filled_avg_price)
        fill_time_utc = stop_fill.filled_at
        fill_time_ct  = fill_time_utc.astimezone(CT).strftime("%H:%M:%S CT") if fill_time_utc else "unknown"
        order_type = stop_fill.order_type.value if stop_fill.order_type else "stop"
        shares = entry.get("position_size", int(stop_fill.filled_qty or 0))

        try:
            from managers.trade_journal import log_exit
            log_exit(
                symbol=symbol,
                setup=setup,
                date=today,
                exit_price=fill_price,
                exit_reason=f"Stop loss hit — Alpaca bracket {order_type} filled (auto-reconciled)",
                shares=shares,
            )
            reconciled.add(key)
            actions.append(
                f"RECONCILED: {symbol} {setup} stop fill ${fill_price:.3f} @ {fill_time_ct} "
                f"({shares} shares)"
            )
        except Exception as e:
            logger.warning(f"reconcile_bracket_stops: log_exit failed for {symbol}: {e}")

    return actions


# ── Main ───────────────────────────────────────────────────────────────────────

def _owned_positions(today: str) -> list:
    """Open positions Tri-City actually entered TODAY.

    Tri-City may share an Alpaca account with other strategies (e.g. Compounder's Gap-and-Go
    Momentum on the same keys). This manager must ONLY manage — and especially only EOD-flatten —
    positions it opened itself, never another strategy's. Ownership = a Tri-City execution record
    for today. (Caveat: if two strategies enter the SAME symbol the same day on one account,
    Alpaca aggregates them into one position and attribution is ambiguous — the real fix is a
    dedicated Alpaca account per strategy.)
    """
    owned = {e.get("symbol") for e in load_executions()
             if e.get("date") == today and e.get("success") and e.get("symbol")}
    return [p for p in get_open_positions() if p.get("ticker") in owned]


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

    positions = _owned_positions(today)
    open_tickers = {p["ticker"] for p in positions}

    # Reconcile any bracket stops that fired via Alpaca without our manager seeing them
    recon_actions = reconcile_bracket_stops(today, open_tickers)
    for msg in recon_actions:
        print(msg)

    if args.status:
        print_status(positions, today)
        return

    if not positions:
        return  # Nothing to manage — silent

    all_actions = []

    if not args.eod:
        all_actions += check_supertrend_exit(positions, today)
        positions = _owned_positions(today)
        all_actions += check_failed_pullback(positions, today, now)
        positions = _owned_positions(today)
        all_actions += check_quick_lock(positions, today, now)
        positions = _owned_positions(today)
        all_actions += check_free_ride(positions, today)
        positions = _owned_positions(today)
        all_actions += check_targets(positions, today)
        positions = _owned_positions(today)
        all_actions += check_rvol_collapse(positions, today)
        positions = _owned_positions(today)
        all_actions += check_idle_timeout(positions, today, now)
        positions = _owned_positions(today)
        all_actions += check_trailing(positions, today)
        positions = _owned_positions(today)
        all_actions += check_ema20_reclaim_advisory(positions, today)
        all_actions += check_ema8_exit(positions, today)

    all_actions += check_eod(positions, now)

    for action in all_actions:
        print(action)


if __name__ == "__main__":
    main()
