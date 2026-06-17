#!/usr/bin/env python3
"""
APEX end-of-session — flatten every open position and print the day's summary.

Called by apex_session_end.sh (i.e. when you type "end session"). Each position is closed at its
live TV price (falling back to its last known / entry price if TV is down). Real paper positions
are liquidated via Alpaca; dry-run positions are just journaled. Reuses the Layer 3 exit path so
exits land in logs/apex-journal.json exactly like a normal managed exit.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

import apex_config as cfg
from apex_health import _close
from apex_tv_quotes import get_quotes

ET = ZoneInfo("America/New_York")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("apex.eod")


def _load_state() -> dict:
    if cfg.STATE_FILE.exists():
        try:
            return json.loads(cfg.STATE_FILE.read_text())
        except Exception:
            pass
    return {"date": "", "daily_pnl": 0.0, "positions": {}, "executed_today": []}


def _save_state(s: dict) -> None:
    cfg.STATE_FILE.write_text(json.dumps(s, indent=2))


def _today_rows(path) -> list:
    today = datetime.now(ET).date().isoformat()
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text())
    except Exception:
        return []
    return [r for r in rows if str(r.get("timestamp", "")).startswith(today)]


def flatten(scope: str = "intraday") -> None:
    """
    scope="intraday" (default, used by "end session"): close only intraday-tier positions and
    LEAVE swing / multi-week holdings open — they're durable and exit on swing rules (managed by
    apex_swing.py), not on session lifecycle.
    scope="all": close everything (explicit `apex-flatten` / `apex-end --all`).
    """
    state = _load_state()
    positions = state.get("positions", {})
    if scope == "all":
        targets = list(positions)
    else:
        targets = [s for s, p in positions.items() if p.get("status", "intraday") == "intraday"]
    kept = [s for s in positions if s not in targets]

    if not targets:
        print("No open positions." if scope == "all" else "No intraday positions to flatten.")
    else:
        quotes = get_quotes(targets)
        reason = "manual flatten-all" if scope == "all" else "session end — intraday flatten"
        for sym in targets:
            p = positions[sym]
            price = quotes.get(sym, {}).get("last") or p.get("last_price") or p["entry"]
            price = float(price)
            h = {"price": price, "health": p.get("health", 0),
                 "gain_pct": round((price - p["entry"]) / p["entry"] * 100, 2),
                 "reasons": ["session end"]}
            _close(sym, p, h, reason, state, dry_run=(p.get("order_id") == "DRY-RUN"))
        _save_state(state)
        print(f"Flattened {len(targets)} {scope} position(s).")
    if kept:
        print(f"Kept {len(kept)} swing/position holding(s) open "
              f"(durable — managed by the swing manager): {', '.join(kept)}")
    _summary()


def _summary() -> None:
    entries = _today_rows(cfg.EXEC_LOG)
    exits = _today_rows(cfg.APEX_JOURNAL)
    pnl = sum(r.get("pnl", 0) for r in exits)
    wins = sum(1 for r in exits if r.get("pnl", 0) > 0)
    print("\n────── APEX session summary ──────")
    line = f"  entries {len(entries)} | exits {len(exits)} | net P&L ${pnl:,.2f}"
    if exits:
        line += f" | win rate {wins / len(exits) * 100:.0f}%"
    print(line)
    for r in sorted(exits, key=lambda x: x.get("pnl", 0)):
        print(f"    {r.get('symbol','?'):6s} {r.get('trigger','?'):7s} "
              f"{r.get('gain_pct',0):+5.1f}%  ${r.get('pnl',0):>8.2f}  "
              f"{'(DRY)' if r.get('dry_run') else ''} {r.get('reason','')[:34]}")
    print("──────────────────────────────────")


if __name__ == "__main__":
    import sys
    flatten(scope="all" if "--all" in sys.argv else "intraday")
