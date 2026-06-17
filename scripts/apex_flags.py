#!/usr/bin/env python3
"""
APEX — operator flags (the manual-override layer on top of the autonomous engine).

Standing preferences, NOT per-trade approvals — you set them once, APEX stays autonomous within
them. Stored in shared/apex-flags.json:
  - avoid:      blacklist — never enter these (blocks new entries; does not flatten a held name)
  - prioritize: soft whitelist — kept real-time (warm) + a relaxed entry threshold; APEX still
                trades the broader universe
  - strict:     bool — when true, APEX trades ONLY prioritized names (small curated test universe)

Each flag records the date + an optional note so a reappearing stock can be reminded ("you
flagged this on X"). Avoid wins over prioritize.
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import apex_config as cfg

ET = ZoneInfo("America/New_York")
FLAGS_FILE = cfg.SHARED / "apex-flags.json"


def load_flags() -> dict:
    if FLAGS_FILE.exists():
        try:
            f = json.loads(FLAGS_FILE.read_text())
            f.setdefault("avoid", {})
            f.setdefault("prioritize", {})
            f.setdefault("strict", False)
            return f
        except Exception:
            pass
    return {"avoid": {}, "prioritize": {}, "strict": False}


def save_flags(f: dict) -> None:
    FLAGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    FLAGS_FILE.write_text(json.dumps(f, indent=2))


def set_flag(kind: str, symbol: str, note: str = "") -> None:
    """kind = 'avoid' | 'prioritize'. Avoid clears any prioritize on the same symbol (avoid wins)."""
    f = load_flags()
    f.setdefault(kind, {})[symbol] = {"date": datetime.now(ET).date().isoformat(), "note": note}
    if kind == "avoid":
        f.get("prioritize", {}).pop(symbol, None)
    save_flags(f)


def clear_flag(kind: str, symbol: str) -> None:
    f = load_flags()
    f.get(kind, {}).pop(symbol, None)
    save_flags(f)


def set_strict(on: bool) -> None:
    f = load_flags()
    f["strict"] = bool(on)
    save_flags(f)


def flag_label(symbol: str, flags: dict | None = None) -> str | None:
    """Short label for a symbol's flag (for dashboard/alerts), or None."""
    f = flags or load_flags()
    if symbol in f.get("avoid", {}):
        return "🚫 avoid"
    if symbol in f.get("prioritize", {}):
        return "⭐ prioritized"
    return None
