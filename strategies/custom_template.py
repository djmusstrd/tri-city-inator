"""
CUSTOM STRATEGY TEMPLATE — Plug in your own signal logic here.

HOW TO USE
──────────
1. Copy this file and rename it (e.g. my_orb_strategy.py)
2. Edit classify_signal() with your entry conditions
3. In .env, set:
       CUSTOM_STRATEGY=true
       STRATEGY_FILE=strategies/my_orb_strategy.py
4. Restart the monitor loop. Done.

The monitor handles all data fetching (price, RSI, EMA, RVol, etc.).
Your job is only to decide: given this data, should I enter?

SIGNAL TYPES
──────────────
Return one of:
  "ENTER"  — Execute immediately (full position)
  "CONV"   — Execute immediately (convergence breakout variant)
  "SETUP"  — Print alert only, no order placed
  None     — No signal, skip this symbol

POSITION MANAGEMENT
────────────────────
The 50-25-25 scale-out, stop management, and EOD close are handled
automatically regardless of which strategy generates the signal.
You don't need to change those.

DATA AVAILABLE
───────────────
See the `data` parameter docs in classify_signal() below.
All values are pre-computed by the monitor from Alpaca real-time data.
"""

from __future__ import annotations
import os

# ── Strategy metadata (update these) ──────────────────────────────────────────
STRATEGY_NAME = "My Custom Strategy"
SIGNAL_SOURCE = "python"  # keep as "python" unless you know what you're doing

# ── Your thresholds ────────────────────────────────────────────────────────────
# Define your own parameters here. Pull from .env if you want them configurable.
MY_RSI_MAX   = float(os.getenv("MY_RSI_MAX",  "65"))
MY_MIN_RVOL  = float(os.getenv("MIN_RVOL",    "1.5"))
MY_MIN_GAP   = float(os.getenv("MIN_GAP_PCT", "3.0"))


def classify_signal(data: dict) -> str | None:
    """
    Your signal logic goes here.

    Parameters (all pre-computed by the monitor)
    ─────────────────────────────────────────────
    data["price"]       float   Current price
    data["vwap"]        float   VWAP
    data["ema20"]       float   EMA(20) from 5-min bars
    data["rsi"]         float   RSI(14) from 5-min bars
    data["rvol"]        float   Relative volume vs 20-day avg
    data["gap_pct"]     float   Gap % from prev close
    data["from_open"]   float   % move from today's open
    data["dist_52w"]    float   % below 52-week high
    data["rs_vs_spy"]   bool    Outperforming SPY today?
    data["is_conv"]     bool    EMA20/SMA50 convergence?
    data["above_vwap"]  bool
    data["above_ema20"] bool
    data["momentum_ok"] bool    gap OR intraday move meets minimum
    data["near_high"]   bool    within MAX_52W_DIST of 52-week high
    data["rvol_ok"]     bool    rvol >= MIN_RVOL

    Returns: "ENTER" | "CONV" | "SETUP" | None
    """
    rsi   = data.get("rsi")
    rvol  = data.get("rvol")
    price = data.get("price")
    vwap  = data.get("vwap")

    # ── Example: simple ORB pullback strategy ─────────────────────────────────
    # Replace this logic with your own conditions.

    if (data.get("above_vwap") and
        data.get("momentum_ok") and
        rvol is not None and rvol >= MY_MIN_RVOL and
        rsi is not None and 45 <= rsi <= MY_RSI_MAX):
        return "ENTER"

    # Alert only — almost ready
    if (data.get("above_vwap") and
        rsi is not None and 40 <= rsi <= 70 and
        rvol is not None and rvol >= 1.2):
        return "SETUP"

    return None
