"""
TRI-CITY STRATEGY — Default signal classifier for the Tri-City Inator ENHANCED.

This file implements the signal logic used by the Tri-City Inator.
It receives pre-computed market data from the monitor and classifies
each bar as ENTER, CONV, SETUP, or None.

To use a different strategy, set CUSTOM_STRATEGY=true in .env and
point STRATEGY_FILE to your own strategy file. See custom_template.py.
"""

from __future__ import annotations
import os

# ── Strategy metadata ──────────────────────────────────────────────────────────
STRATEGY_NAME   = "Tri-City Inator ENHANCED"
SIGNAL_SOURCE   = "python"   # "python" = Alpaca data; "tradingview" = Pine table

# ── Thresholds (all overridable via .env) ─────────────────────────────────────
MIN_RVOL        = float(os.getenv("MIN_RVOL",          "1.5"))
MIN_GAP_PCT     = float(os.getenv("MIN_GAP_PCT",       "3.0"))
MIN_FROM_OPEN   = float(os.getenv("MIN_FROM_OPEN_PCT", "2.0"))
RSI_MIN         = float(os.getenv("RSI_MIN",           "50"))
RSI_MAX         = float(os.getenv("RSI_MAX",           "75"))
MAX_52W_DIST    = float(os.getenv("MAX_52W_DIST",      "30.0"))
SETUP_RVOL      = float(os.getenv("SETUP_RVOL",        "1.3"))
CONV_GAP_PCT    = float(os.getenv("CONV_GAP_PCT",      "2.0"))


def classify_signal(data: dict) -> str | None:
    """
    Classify a market data snapshot into a signal type.

    Parameters
    ----------
    data : dict
        Pre-computed values supplied by tri_city_monitor.py:
          price        float   Current price
          vwap         float   VWAP from Alpaca snapshot
          ema20        float   EMA(20) from 5-min bars
          rsi          float   RSI(14) from 5-min bars
          rvol         float   Relative volume vs 20-day avg at same time
          gap_pct      float   Gap % from prev close to today's open
          from_open    float   % move from today's open to current price
          dist_52w     float   % distance below 52-week high
          rs_vs_spy    bool    True if stock outperforming SPY today
          is_conv      bool    True if EMA20 and SMA50 are converging
          above_vwap   bool
          above_ema20  bool
          momentum_ok  bool    gap_pct >= MIN_GAP_PCT OR from_open >= MIN_FROM_OPEN
          near_high    bool    dist_52w <= MAX_52W_DIST
          rvol_ok      bool    rvol >= MIN_RVOL

    Returns
    -------
    "ENTER" | "CONV" | "SETUP" | None
    """
    momentum_ok  = data.get("momentum_ok", False)
    above_vwap   = data.get("above_vwap",  False)
    above_ema20  = data.get("above_ema20", False)
    rvol_ok      = data.get("rvol_ok",     False)
    near_high    = data.get("near_high",   False)
    rs_vs_spy    = data.get("rs_vs_spy",   False)
    is_conv      = data.get("is_conv",     False)
    rsi          = data.get("rsi")
    rvol         = data.get("rvol")

    rsi_ok = rsi is not None and RSI_MIN <= rsi <= RSI_MAX

    all_conditions = (
        momentum_ok and above_vwap and rvol_ok and
        rsi_ok and above_ema20 and near_high
    )

    if all_conditions and rs_vs_spy and is_conv:
        return "CONV"
    elif all_conditions:
        return "ENTER"
    elif (momentum_ok and above_vwap and above_ema20 and
          rvol is not None and rvol >= SETUP_RVOL and
          rsi is not None and 45 <= rsi <= 80):
        return "SETUP"

    return None
