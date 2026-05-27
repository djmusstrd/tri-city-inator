#!/usr/bin/env python3
"""
TRI-CITY HEALTH CHECK — Verifies session state before trading begins.

Runs at 8:31 AM CT (after symbol swap) and prints a status table.
Catches silent failures that would otherwise produce wrong stops, missed
signals, or duplicate trades without any visible error.

Checks:
  1. .env           — exists, ALPACA keys present
  2. candidates.json — exists, date == today, has tv_symbols
  3. flags.json      — exists, date == today
  4. levels.json     — exists, has symbols, all ORH > 0
  5. Alpaca API      — connectivity + buying power
  6. executions.json — no errors logged today

Usage:
  python -W ignore scripts/tri_city_health_check.py
  python -W ignore scripts/tri_city_health_check.py --date 2026-05-27  # override today
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE  = Path.home() / "tri-city-inator"
SHARED     = WORKSPACE / "shared"
LOGS       = WORKSPACE / "logs"

CT = ZoneInfo("America/Chicago")

try:
    from dotenv import load_dotenv
    _env = WORKSPACE / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass


# ── Individual checks ─────────────────────────────────────────────────────────

def check_env() -> tuple[str, str]:
    """Check .env exists and Alpaca keys are set."""
    env_file = WORKSPACE / ".env"
    if not env_file.exists():
        return "FAIL", ".env not found — create from .env.example"
    key = os.getenv("ALPACA_API_KEY", "")
    sec = os.getenv("ALPACA_SECRET_KEY", "")
    if not key or key.startswith("YOUR_"):
        return "FAIL", "ALPACA_API_KEY not set in .env"
    if not sec or sec.startswith("YOUR_"):
        return "FAIL", "ALPACA_SECRET_KEY not set in .env"
    return "PASS", "API keys present"


def check_trading_mode() -> tuple[str, str]:
    """Confirm paper vs live mode. Shows WARN when LIVE so it's impossible to miss."""
    paper = os.getenv("ALPACA_PAPER", "true").lower() == "true"
    if paper:
        return "PASS", "PAPER mode (ALPACA_PAPER=true)"
    return "WARN", "*** LIVE TRADING MODE *** (ALPACA_PAPER=false) — real money"


def check_candidates(today: str) -> tuple[str, str]:
    """Check candidates.json exists, matches today, has tv_symbols."""
    f = SHARED / "tri-city-candidates.json"
    if not f.exists():
        return "FAIL", "tri-city-candidates.json missing — run 7:30 AM scanner"
    try:
        data = json.loads(f.read_text())
        date = data.get("date", "")
        syms = data.get("tv_symbols", [])
        if date != today:
            return "WARN", f"date={date} (expected {today}) — rerun scanner"
        if not syms:
            return "WARN", "tv_symbols empty — symbol swap will push nothing"
        return "PASS", f"{len(syms)} symbols loaded, scanned at {data.get('scanned_at','?')}"
    except Exception as e:
        return "FAIL", f"JSON parse error: {e}"


def check_flags(today: str) -> tuple[str, str]:
    """Check tri-city-flags.json exists and matches today."""
    f = SHARED / "tri-city-flags.json"
    if not f.exists():
        return "WARN", "tri-city-flags.json missing — htf/resistance unavailable until rescan"
    try:
        data  = json.loads(f.read_text())
        date  = data.get("date", "")
        htf   = data.get("htf", [])
        res   = data.get("resistance", [])
        if date != today:
            return "WARN", f"date={date} (expected {today}) — rerun scanner to refresh"
        return "PASS", f"htf={len(htf)} symbols, resistance={len(res)} symbols"
    except Exception as e:
        return "FAIL", f"JSON parse error: {e}"


def check_levels() -> tuple[str, str]:
    """Check tri-city-levels.json exists and has non-zero ORH values."""
    f = SHARED / "tri-city-levels.json"
    if not f.exists():
        return "WARN", "tri-city-levels.json missing — level lock hasn't run yet (normal pre-8:45 AM)"
    try:
        data = json.loads(f.read_text())
        if not data:
            return "WARN", "levels file empty — level lock produced no symbols"
        zero_orh = [sym for sym, v in data.items() if v.get("orh", 0) <= 0]
        ok_count = len(data) - len(zero_orh)
        if zero_orh:
            return "WARN", f"{ok_count}/{len(data)} valid ORH; zeros: {zero_orh[:5]}"
        return "PASS", f"{len(data)} symbols with valid ORH/ORL"
    except Exception as e:
        return "FAIL", f"JSON parse error: {e}"


def check_alpaca() -> tuple[str, str]:
    """Check Alpaca API connectivity and minimum account balance."""
    min_balance = float(os.getenv("MIN_ACCOUNT_BALANCE", "500"))
    try:
        sys.path.insert(0, str(WORKSPACE))
        from scripts.tri_city_execute import get_account_equity, get_buying_power
        equity = get_account_equity()
        bp     = get_buying_power()
        if equity is None:
            return "WARN", "get_account_equity() returned None — check API keys"
        bp_str = f", buying_power=${bp:,.0f}" if bp else ""
        if equity < min_balance:
            return "FAIL", (
                f"equity=${equity:,.0f}{bp_str} — BELOW minimum "
                f"${min_balance:,.0f} (set MIN_ACCOUNT_BALANCE in .env)"
            )
        return "PASS", f"equity=${equity:,.0f}{bp_str}  (min=${min_balance:,.0f} ✓)"
    except Exception as e:
        return "FAIL", f"Alpaca connection failed: {e}"


def check_executions(today: str) -> tuple[str, str]:
    """Check for execution errors logged today."""
    f = LOGS / "tri-city-executions.json"
    if not f.exists():
        return "PASS", "no executions today (clean slate)"
    try:
        entries = json.loads(f.read_text())
        today_entries = [e for e in entries if e.get("date") == today]
        errors   = [e for e in today_entries if not e.get("success") and e.get("error")]
        success  = [e for e in today_entries if e.get("success")]
        if errors:
            syms = [e.get("symbol", "?") for e in errors[:3]]
            return "WARN", f"{len(errors)} execution error(s) today: {syms}"
        if today_entries:
            return "PASS", f"{len(success)} executed, {len(today_entries)-len(success)} skipped today"
        return "PASS", "no executions today (clean slate)"
    except Exception as e:
        return "WARN", f"JSON parse error: {e}"


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City Session Health Check")
    parser.add_argument("--date", default=None, help="Override today's date (YYYY-MM-DD)")
    args = parser.parse_args()

    today = args.date or datetime.now(CT).strftime("%Y-%m-%d")
    now   = datetime.now(CT).strftime("%H:%M CT")

    checks = [
        ("ENV / API Keys",      check_env()),
        ("Trading Mode",        check_trading_mode()),
        ("candidates.json",     check_candidates(today)),
        ("flags.json",          check_flags(today)),
        ("levels.json (ORH)",   check_levels()),
        ("Alpaca / Balance",    check_alpaca()),
        ("Execution Log",       check_executions(today)),
    ]

    all_pass = all(status in ("PASS", "WARN") for _, (status, _) in checks)
    any_fail = any(status == "FAIL" for _, (status, _) in checks)

    print(f"\n{'='*68}")
    print(f"  TRI-CITY HEALTH CHECK — {today}  {now}")
    print(f"{'='*68}")
    print(f"  {'CHECK':<24} {'STATUS':<6} {'DETAIL'}")
    print(f"  {'-'*24} {'-'*6} {'-'*34}")

    for name, (status, detail) in checks:
        icon = "✅" if status == "PASS" else "⚠️ " if status == "WARN" else "❌"
        print(f"  {name:<24} {icon} {status:<4}  {detail}")

    print(f"{'='*68}")
    if any_fail:
        print(f"  ❌ SESSION NOT READY — fix FAIL items before 8:30 AM")
    elif any(s == "WARN" for _, (s, _) in checks):
        print(f"  ⚠️  SESSION OK — review warnings above before trading")
    else:
        print(f"  ✅ ALL CHECKS PASSED — session ready")
    print(f"{'='*68}\n")

    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    main()
