#!/usr/bin/env python3
"""
DAILY RECAP (Tri-City) — EOD journal sync + performance report.

Run automatically at 3:50 PM CT (5 min after EOD close).

Steps:
  1. Pull today's FILL activities from Alpaca to find any unlogged entries
  2. Write synthetic execution log entries for fills missing from tri-city-executions.json
  3. Call log_exit() for entries with no matching journal record
  4. Compute day metrics, write markdown to logs/reports/daily-YYYY-MM-DD-tricty.md
  5. Print the report to stdout

Usage:
    python -W ignore scripts/daily_recap.py
    python -W ignore scripts/daily_recap.py --date 2026-05-14
    python -W ignore scripts/daily_recap.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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

CT = ZoneInfo("America/Chicago")
REPORTS_DIR = WORKSPACE / "logs" / "reports"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"


# ── Alpaca helpers ─────────────────────────────────────────────────────────────

def get_alpaca_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.trading.client import TradingClient
        return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    except Exception as e:
        logger.warning(f"Alpaca client error: {e}")
        return None


def get_today_fills(date: str) -> list[dict]:
    client = get_alpaca_client()
    if not client:
        return []
    try:
        activities = client.get_activities(activity_types="FILL", date=date)
        fills = []
        for a in activities:
            activity_date = str(a.transaction_time)[:10] if hasattr(a, "transaction_time") else ""
            if activity_date != date:
                continue
            fills.append({
                "symbol":   str(a.symbol),
                "side":     str(a.side),
                "qty":      float(a.qty),
                "price":    float(a.price),
                "time":     str(a.transaction_time),
                "order_id": str(a.order_id) if hasattr(a, "order_id") else "",
            })
        return fills
    except Exception as e:
        logger.warning(f"get_today_fills error: {e}")
        return []


def get_account_equity() -> float | None:
    client = get_alpaca_client()
    if not client:
        return None
    try:
        return float(client.get_account().equity)
    except Exception:
        return None


# ── Sync logic ─────────────────────────────────────────────────────────────────

def sync_missing_entries(fills: list[dict], date: str, dry_run: bool) -> list[str]:
    from managers.trade_journal import write_synthetic_entry
    exec_log = WORKSPACE / "logs" / "tri-city-executions.json"
    try:
        existing = json.loads(exec_log.read_text()) if exec_log.exists() else []
    except Exception:
        existing = []

    logged_symbols = {
        e["symbol"] for e in existing
        if e.get("date") == date and e.get("success")
    }

    buy_fills = [f for f in fills if f["side"].lower() in ("buy", "buy_to_open")]
    actions = []

    for fill in buy_fills:
        sym = fill["symbol"]
        if sym in logged_symbols:
            continue

        entry_price = fill["price"]
        stop_loss   = round(entry_price - 0.13, 2)
        risk_ps     = round(entry_price - stop_loss, 2)
        qty         = int(fill["qty"])
        target_1    = round(entry_price * 1.10, 2)
        target_2    = round(entry_price * 1.20, 2)
        target_3    = round(entry_price * 1.30, 2)

        msg = (
            f"SYNTHETIC ENTRY: {sym} @ ${entry_price:.2f} "
            f"({qty} shares) — reconstructed from Alpaca fill"
        )
        actions.append(msg)

        if not dry_run:
            write_synthetic_entry(
                symbol=sym,
                setup="UNKNOWN",
                date=date,
                entry_price=entry_price,
                stop_loss=stop_loss,
                target_1=target_1,
                target_2=target_2,
                target_3=target_3,
                position_size=qty,
                order_id=fill.get("order_id", ""),
            )

    return actions


def sync_missing_exits(date: str, dry_run: bool) -> list[str]:
    from managers.trade_journal import get_open_entries, log_exit, fetch_exit_price
    open_entries = get_open_entries(date)
    actions = []

    for e in open_entries:
        sym   = e["symbol"]
        setup = e.get("setup", "UNKNOWN")
        exit_price = fetch_exit_price(sym)
        if exit_price is None:
            actions.append(f"SKIP EXIT: {sym} — no sell fill found in Alpaca")
            continue

        msg = f"LOGGING EXIT: {sym} {setup} @ ${exit_price:.2f} (reconstructed)"
        actions.append(msg)

        if not dry_run:
            log_exit(
                symbol=sym,
                setup=setup,
                date=date,
                exit_price=exit_price,
                exit_reason="Reconstructed from Alpaca fill (recap sync)",
                shares=e.get("position_size", 0),
            )

    return actions


# ── Report generation ──────────────────────────────────────────────────────────

def build_report(date: str, sync_actions: list[str]) -> str:
    from managers.trade_journal import get_all_trades, compute_metrics

    all_trades = get_all_trades()
    today_trades = [t for t in all_trades if t.get("date") == date and t.get("status") == "closed"]
    metrics = compute_metrics(today_trades) if today_trades else {"total": 0}

    lines = [
        f"# Tri-City — Daily Recap: {date}",
        f"",
        f"Generated: {datetime.now(CT).strftime('%Y-%m-%d %H:%M CT')}",
        f"",
    ]

    equity = get_account_equity()
    if equity:
        lines += [f"**Account Equity:** ${equity:,.2f}", ""]

    if sync_actions:
        lines += ["## Sync Actions", ""]
        for a in sync_actions:
            lines.append(f"- {a}")
        lines.append("")

    if not today_trades:
        lines += ["## Trades", "", "No closed trades recorded today.", ""]
    else:
        lines += [
            "## Trades",
            "",
            "| # | Symbol | Setup | Cup | Entry | Exit | Shares | P&L | R | Outcome | Duration |",
            "|---|--------|-------|-----|-------|------|--------|-----|---|---------|----------|",
        ]
        for i, t in enumerate(today_trades, 1):
            cup = "Y" if t.get("cup") else "-"
            lines.append(
                f"| {i} | {t['symbol']} | {t['setup']} | {cup} "
                f"| ${t['entry_price']:.2f} | ${t['exit_price']:.2f} "
                f"| {t['position_size']} "
                f"| ${t['realized_pnl']:+.2f} | {t['r_multiple']:+.2f}R "
                f"| {t['outcome']} | {t.get('duration_min', '?')} min |"
            )
        lines.append("")

    if metrics.get("total", 0) > 0:
        lines += [
            "## Day Metrics",
            "",
            f"- **Trades:** {metrics['total']}  "
            f"({metrics['wins']}W / {metrics['losses']}L / {metrics.get('scratches', 0)}S)",
            f"- **Win Rate:** {metrics['win_rate']:.0%}",
            f"- **Total P&L:** ${metrics['total_pnl']:+.2f}",
            f"- **Avg R:** {metrics['avg_r']:+.2f}R",
            f"- **Best R:** {metrics['best_r']:+.2f}R",
            f"- **Worst R:** {metrics['worst_r']:+.2f}R",
            f"- **Profit Factor:** {metrics['profit_factor']}",
            "",
        ]

        if metrics.get("by_setup"):
            lines += ["### By Setup", ""]
            for setup, sv in metrics["by_setup"].items():
                lines.append(
                    f"- **{setup}:** {sv['trades']} trades | "
                    f"P&L ${sv['pnl']:+.2f} | Avg R {sv['avg_r']:+.2f}R"
                )
            lines.append("")

    lines += ["## Notes / Improvement Flags", ""]
    flags = []
    for t in today_trades:
        if t["r_multiple"] < -0.8:
            flags.append(f"- {t['symbol']}: stopped out at {t['r_multiple']:+.2f}R — review entry timing")
        if t.get("duration_min") and t["duration_min"] < 5:
            flags.append(f"- {t['symbol']}: very fast exit ({t['duration_min']} min) — whipsaw risk")
        if t.get("exit_reason", "").startswith("Reconstructed"):
            flags.append(f"- {t['symbol']}: exit NOT auto-logged — execution log gap at entry")

    if not flags:
        flags = ["- No red flags today."]
    lines += flags
    lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City daily recap")
    parser.add_argument("--date",    default=datetime.now(CT).strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    date = args.date
    print(f"\n{'='*60}")
    print(f"TRI-CITY DAILY RECAP — {date}")
    print(f"{'='*60}")

    fills = get_today_fills(date)
    print(f"Alpaca fills found: {len(fills)}")

    sync_actions = []
    sync_actions += sync_missing_entries(fills, date, args.dry_run)
    sync_actions += sync_missing_exits(date, args.dry_run)

    for a in sync_actions:
        print(f"  {a}")

    report = build_report(date, sync_actions)

    if not args.dry_run:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / f"daily-{date}-tricty.md"
        report_path.write_text(report)
        print(f"\nReport saved: {report_path}")

    print("\n" + report)


if __name__ == "__main__":
    main()
