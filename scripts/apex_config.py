#!/usr/bin/env python3
"""
APEX config — central tunables for the V2 engine.

Defaults here are the human-set baseline. Layer 4 (self-improvement) will later read/write
shared/apex-config.json WITHIN GUARDRAILS to auto-tune the *_TUNABLE values; the *_GUARD
caps are hard limits Layer 4 may never cross. See docs/STRATEGY_V2_DESIGN.md.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

WORKSPACE = Path.home() / "tri-city-inator"
SHARED = WORKSPACE / "shared"
LOGS = WORKSPACE / "logs"
CONFIG_FILE = SHARED / "apex-config.json"

# Load .env once, centrally, so every APEX module sees the keys.
try:
    from dotenv import load_dotenv
    _env = WORKSPACE / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

# ── Account / sizing ──────────────────────────────────────────────────────────
ACCOUNT_EQUITY   = float(os.getenv("APEX_EQUITY", "5000"))      # scales by compounding, not leverage
RISK_PCT_TUNABLE = float(os.getenv("APEX_RISK_PCT", "1.0"))     # % equity risked per trade
MAX_RISK_GUARD   = float(os.getenv("APEX_MAX_RISK", "150"))     # hard $ risk cap per trade
MAX_POSITIONS_GUARD = int(os.getenv("APEX_MAX_POSITIONS", "5"))
DAILY_LOSS_GUARD = float(os.getenv("APEX_DAILY_LOSS", "-250"))  # circuit breaker

# ── Entry ───────────────────────────────────────────────────────────────────
ENTRY_THRESH_TUNABLE = float(os.getenv("APEX_ENTRY_THRESH", "65"))  # composite score gate (Layer 4 band 60-75)
PRIORITIZE_RELAX     = float(os.getenv("APEX_PRIORITIZE_RELAX", "5"))  # lower the entry threshold by this for prioritized names
RS_MIN               = float(os.getenv("APEX_RS_MIN", "90"))        # leader watchlist already gates this
ORB_MINUTES          = int(os.getenv("APEX_ORB_MINUTES", "15"))
ATR_LEN              = 14
ATR_STOP_MULT        = float(os.getenv("APEX_ATR_STOP", "2.0"))
MAX_STOP_PCT         = float(os.getenv("APEX_MAX_STOP_PCT", "0.10"))  # cap stop distance at 10% of entry
# Price band — apex_band_backtest.py (30d/1174 entries) showed the $2-30 zone carries ~4x the
# per-trade R and ~3x the $/trade of >$30 names, while $100+ is ~zero-edge / forced-1-share.
# Bias the book to the compoundable band. Tunable; raise PRICE_MAX to re-include a hot theme.
PRICE_MIN            = float(os.getenv("APEX_PRICE_MIN", "2"))
PRICE_MAX            = float(os.getenv("APEX_PRICE_MAX", "100"))

# ── Layer 3 (health monitor / exit) ──────────────────────────────────────────
EXIT_HEALTH   = float(os.getenv("APEX_EXIT_HEALTH", "40"))   # proactive exit below this score
# Fresh-entry phantom-churn guard: a just-entered symbol isn't warm in the live quote feed yet, so
# health can be computed off a stale (delayed) bar and read a phantom loss → instant exit seconds
# after entry (e.g. INTW/AMDL). Don't let a PROACTIVE health exit fire on a stale price, nor within
# ENTRY_GRACE_SEC of entry. The broker hard stop still protects either way.
EXIT_REQUIRE_LIVE = os.getenv("APEX_EXIT_REQUIRE_LIVE", "true").lower() == "true"
ENTRY_GRACE_SEC   = int(os.getenv("APEX_ENTRY_GRACE_SEC", "90"))
CARRY_HEALTH  = float(os.getenv("APEX_CARRY_HEALTH", "70"))  # min health to carry overnight
EOD_CLOSE_ET  = os.getenv("APEX_EOD_CLOSE_ET", "15:45")      # ET wall-clock to start the conditional EOD pass
GRAD_DAYS     = int(os.getenv("APEX_GRAD_DAYS", "5"))        # days_held to graduate swing → multi-week position

# Overnight carry: when true, at EOD the system PROPOSES healthy runners as overnight swings
# (Telegram + dashboard) and carries them BY DEFAULT unless you deny in the close window; the
# swing manager then owns them. When false, everything flattens at EOD (intraday-only validation).
ALLOW_OVERNIGHT_CARRY = os.getenv("APEX_ALLOW_OVERNIGHT_CARRY", "true").lower() == "true"
CARRY_DECISIONS = SHARED / "apex-carry-decisions.json"   # dashboard approve/deny for pending carries

# ── Swing tier (daily-bar management, runs independent of the intraday poller) ────────
SWING_TREND_EMA = int(os.getenv("APEX_SWING_TREND_EMA", "10"))   # daily close below this EMA = trend break → exit

# Entries at/after this ET time are tagged "late" (last hour before the 16:00 ET close) — tracked
# for evaluation (intraday-fade vs overnight-runner), not blocked yet.
LATE_ENTRY_ET = os.getenv("APEX_LATE_ENTRY_ET", "15:00")

# ── Layer 4 guardrails (bands Layer 4 may move within) ────────────────────────
SIZE_BAND        = (0.5, 1.5)    # sizing multiplier range
THRESH_BAND      = (60.0, 75.0)  # entry threshold range

# ── Market data ───────────────────────────────────────────────────────────────
DATA_FEED     = os.getenv("APEX_DATA_FEED", "sip")        # sip | iex
# Hybrid feed: bars (levels) from Alpaca, live TRIGGER price from TradingView's real-time
# quote session over CDP (no Alpaca data upgrade, no Pine table). Falls back to delayed bars
# per-symbol if TV/CDP is down or a symbol has no live tick. See apex_tv_quotes.py.
USE_TV_QUOTES = os.getenv("APEX_USE_TV_QUOTES", "true").lower() == "true"
TV_QUOTE_WAIT_MS = int(os.getenv("APEX_TV_QUOTE_WAIT_MS", "3000"))
MAX_LIVE_QUOTES = int(os.getenv("APEX_MAX_LIVE_QUOTES", "25"))   # cap the WARM real-time set (bounded streaming load on the TV app)
# Basic Alpaca data plans serve SIP on a ~15-min delay and REJECT any request whose window
# reaches into that delay ("subscription does not permit querying recent SIP data"). Cap every
# intraday request's end at now − SIP_DELAY_MIN. Set to 0 if you upgrade to real-time SIP.
SIP_DELAY_MIN = int(os.getenv("APEX_SIP_DELAY_MIN", "16"))

# ── Cadence (seconds) ─────────────────────────────────────────────────────────
POLL_FAST = int(os.getenv("APEX_POLL_FAST", "60"))    # first hour after open
POLL_SLOW = int(os.getenv("APEX_POLL_SLOW", "300"))   # rest of day

# ── TradingView (hybrid feed already uses CDP; these drive the chart + watchlist) ─────
APEX_LAYOUT_ID    = os.getenv("APEX_LAYOUT_ID", "131204932")        # the saved "APEX" layout
APEX_WATCHLIST_ID = int(os.getenv("APEX_WATCHLIST_ID", "336036336"))  # the "APEX" watchlist

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Files ─────────────────────────────────────────────────────────────────────
LEADERS_FILE   = SHARED / "apex-leaders.json"
STATE_FILE     = SHARED / "apex-state.json"
PID_FILE       = SHARED / "apex-poller.pid"
EXEC_LOG       = LOGS / "apex-executions.json"
RATIONALE_LOG  = LOGS / "apex-rationale.json"
APEX_JOURNAL   = LOGS / "apex-journal.json"   # closed trades (Layer 3 exits) — feeds Layer 4
POLLER_LOG     = LOGS / "apex-poller.log"


def load_overrides() -> dict:
    """Layer 4 writes tuned values here; merge over defaults at runtime."""
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {}


def effective() -> dict:
    """Resolve config = defaults overridden by Layer 4's tuned values (clamped to guardrails)."""
    ov = load_overrides()
    risk_mult = ov.get("size_mult", 1.0)
    risk_mult = max(SIZE_BAND[0], min(SIZE_BAND[1], risk_mult))
    thresh = ov.get("entry_thresh", ENTRY_THRESH_TUNABLE)
    thresh = max(THRESH_BAND[0], min(THRESH_BAND[1], thresh))
    return {
        "equity": ACCOUNT_EQUITY,
        "risk_pct": RISK_PCT_TUNABLE * risk_mult,
        "max_risk": MAX_RISK_GUARD,
        "max_positions": MAX_POSITIONS_GUARD,
        "daily_loss": DAILY_LOSS_GUARD,
        "entry_thresh": thresh,
        "rs_min": RS_MIN,
        "orb_minutes": ORB_MINUTES,
        "atr_stop_mult": ATR_STOP_MULT,
        "max_stop_pct": MAX_STOP_PCT,
        "price_min": PRICE_MIN,
        "price_max": PRICE_MAX,
        "exit_health": ov.get("exit_health", EXIT_HEALTH),
        "exit_require_live": EXIT_REQUIRE_LIVE,
        "entry_grace_sec": ENTRY_GRACE_SEC,
        "carry_health": ov.get("carry_health", CARRY_HEALTH),
        "grad_days": GRAD_DAYS,
        "disabled_setups": ov.get("disabled_setups", []),
    }
