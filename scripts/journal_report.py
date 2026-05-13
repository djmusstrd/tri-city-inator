#!/usr/bin/env python3
"""
JOURNAL REPORT — Prints trade performance for the Tri-City system.

Usage:
    python -W ignore scripts/journal_report.py           # All-time
    python -W ignore scripts/journal_report.py --today   # Today only
    python -W ignore scripts/journal_report.py --date 2026-05-12
"""

import argparse
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

from managers.trade_journal import get_all_trades, compute_metrics

CT = ZoneInfo("America/Chicago")

OUTCOME_ICON = {
    "full_win":    "🟢",
    "partial_win": "🟡",
    "scratch":     "⚪",
    "loss":        "🔴",
}


def print_report(trades: list, label: str = "ALL TIME"):
    closed = [t for t in trades if t.get("status") == "closed"]

    print("\n" + "=" * 72)
    print(f"  TRI-CITY TRADE JOURNAL — {label}")
    print("=" * 72)

    if not closed:
        print("  No closed trades found.")
        print("=" * 72 + "\n")
        return

    # Trade log
    print(f"\n{'#':<4} {'DATE':<12} {'SYM':<6} {'SETUP':<10} "
          f"{'ENTRY':>7} {'EXIT':>7} {'P&L':>8} {'R':>6} {'DUR':>5} {'OUT'}")
    print("-" * 72)

    running = 0.0
    for i, t in enumerate(closed, 1):
        icon    = OUTCOME_ICON.get(t["outcome"], "?")
        running += t["realized_pnl"]
        pnl_str = f"${t['realized_pnl']:+.2f}"
        r_str   = f"{t['r_multiple']:+.2f}R"
        dur     = f"{t['duration_min']:.0f}m" if t.get("duration_min") else "—"
        print(f"{i:<4} {t['date']:<12} {t['symbol']:<6} {t['setup']:<10} "
              f"${t['entry_price']:>6.2f} ${t['exit_price']:>6.2f} "
              f"{pnl_str:>8} {r_str:>7}  {dur:>5}  {icon} {t['outcome']}")

    # Metrics
    m = compute_metrics(closed)
    print("\n" + "─" * 72)
    print(f"  SUMMARY ({m['total']} trades)")
    print("─" * 72)
    print(f"  Total P&L:      ${m['total_pnl']:>+8.2f}")
    print(f"  Win Rate:       {m['win_rate']*100:>5.1f}%  "
          f"({m['wins']}W / {m['scratches']}S / {m['losses']}L)")
    print(f"  Avg R:          {m['avg_r']:>+.3f}R")
    print(f"  Best / Worst:   {m['best_r']:+.3f}R  /  {m['worst_r']:+.3f}R")
    print(f"  Profit Factor:  {m['profit_factor']}")
    print(f"  Gross Profit:   ${m['gross_profit']:>+8.2f}")
    print(f"  Gross Loss:     ${m['gross_loss']:>+8.2f}")

    if m.get("by_setup"):
        print("\n  BY SIGNAL TYPE:")
        print(f"  {'SETUP':<12} {'TRADES':>6} {'P&L':>9} {'AVG R':>8}")
        print(f"  {'─'*12} {'─'*6} {'─'*9} {'─'*8}")
        for setup, v in sorted(m["by_setup"].items()):
            print(f"  {setup:<12} {v['trades']:>6} "
                  f"${v['pnl']:>+8.2f} {v['avg_r']:>+8.3f}R")

    print("\n" + "=" * 72 + "\n")


def main():
    parser = argparse.ArgumentParser(description="Tri-City Journal Report")
    parser.add_argument("--today", action="store_true")
    parser.add_argument("--date",  help="Filter by date YYYY-MM-DD")
    args = parser.parse_args()

    all_trades = get_all_trades()

    if args.today:
        today = datetime.now(CT).strftime("%Y-%m-%d")
        trades = [t for t in all_trades if t.get("date") == today]
        print_report(trades, f"TODAY — {today}")
    elif args.date:
        trades = [t for t in all_trades if t.get("date") == args.date]
        print_report(trades, args.date)
    else:
        print_report(all_trades, "ALL TIME")


if __name__ == "__main__":
    main()
