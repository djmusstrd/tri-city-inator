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
RS_MIN               = float(os.getenv("APEX_RS_MIN", "90"))        # leader watchlist already gates this
ORB_MINUTES          = int(os.getenv("APEX_ORB_MINUTES", "15"))
ATR_LEN              = 14
ATR_STOP_MULT        = float(os.getenv("APEX_ATR_STOP", "2.0"))
MAX_STOP_PCT         = float(os.getenv("APEX_MAX_STOP_PCT", "0.10"))  # cap stop distance at 10% of entry

# ── Layer 4 guardrails (bands Layer 4 may move within) ────────────────────────
SIZE_BAND        = (0.5, 1.5)    # sizing multiplier range
THRESH_BAND      = (60.0, 75.0)  # entry threshold range

# ── Cadence (seconds) ─────────────────────────────────────────────────────────
POLL_FAST = int(os.getenv("APEX_POLL_FAST", "60"))    # first hour after open
POLL_SLOW = int(os.getenv("APEX_POLL_SLOW", "300"))   # rest of day

# ── Telegram ──────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Files ─────────────────────────────────────────────────────────────────────
LEADERS_FILE   = SHARED / "apex-leaders.json"
STATE_FILE     = SHARED / "apex-state.json"
PID_FILE       = SHARED / "apex-poller.pid"
EXEC_LOG       = LOGS / "apex-executions.json"
RATIONALE_LOG  = LOGS / "apex-rationale.json"
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
        "disabled_setups": ov.get("disabled_setups", []),
    }
