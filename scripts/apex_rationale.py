#!/usr/bin/env python3
"""
APEX Layer 6 — Trade Rationale capture.

On every entry, persist a complete decision snapshot answering "why this stock, why now."
This single record powers (1) the rich Telegram alert, (2) the dashboard "Why" page, and
(3) Layer 4's per-setup/per-regime expectancy analysis. Foundational, not cosmetic.

See docs/STRATEGY_V2_DESIGN.md (Layer 6).
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from apex_config import RATIONALE_LOG

ET = ZoneInfo("America/New_York")


def build_rationale(signal, rs_pct, score, regime, entry_price, stop, atr, qty) -> dict:
    """Assemble the full decision snapshot + a plain-language 'why'."""
    why_bits = [
        f"RS leader (universe pct {rs_pct:.0f})",
        f"{signal.trigger} trigger @ {signal.bar_time}",
        f"RVOL {signal.rvol:.1f}x on breakout",
        "above VWAP" if entry_price >= signal.vwap else "below VWAP",
        f"regime: {regime}",
    ]
    return {
        "timestamp": datetime.now(ET).isoformat(),
        "symbol": signal.symbol,
        "trigger": signal.trigger,
        "composite_score": score,
        "rs_pct": round(rs_pct, 1),
        "regime": regime,
        "entry": round(entry_price, 4),
        "stop": round(stop, 4),
        "atr": round(atr, 4),
        "qty": qty,
        "risk_dollars": round(abs(entry_price - stop) * qty, 2),
        "orb_high": round(signal.orb_high, 4),
        "orb_low": round(signal.orb_low, 4),
        "vwap_at_entry": round(signal.vwap, 4),
        "rvol": signal.rvol,
        "bar_time": signal.bar_time,
        "context": signal.context,
        "why": " | ".join(why_bits),
        # health-at-entry baseline for Layer 3 (filled when health monitor exists)
        "health_at_entry": 100,
    }


def log_rationale(rationale: dict) -> None:
    RATIONALE_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(RATIONALE_LOG.read_text()) if RATIONALE_LOG.exists() else []
    except Exception:
        existing = []
    existing.append(rationale)
    RATIONALE_LOG.write_text(json.dumps(existing, indent=2))


def telegram_entry_message(rationale: dict) -> str:
    """Rich entry alert — why this stock, why now."""
    r = rationale
    return (
        f"🟢 <b>APEX ENTRY</b> — {r['symbol']}  ({r['trigger']})\n"
        f"<b>Why:</b> {r['why']}\n"
        f"Score {r['composite_score']} | RS {r['rs_pct']} | RVOL {r['rvol']}x\n"
        f"Entry ${r['entry']}  Stop ${r['stop']}  Qty {r['qty']}\n"
        f"Risk ${r['risk_dollars']}  ORB {r['orb_low']}–{r['orb_high']}"
    )
