#!/usr/bin/env python3
"""
JOURNAL REPORT — Prints trade performance for the Tri-City system.

Usage:
    python -W ignore scripts/journal_report.py           # All-time
    python -W ignore scripts/journal_report.py --today   # Today only
    python -W ignore scripts/journal_report.py --date 2026-05-12
"""

import argparse
import math
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
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

# Rolling drawdown config (overridable via .env)
DD_WINDOW     = int(os.getenv("DD_WINDOW", "20"))      # rolling window in trading days
DD_WARN_PCT   = float(os.getenv("DD_WARN_PCT", "15"))  # warn if rolling DD > 15% of peak equity


def _build_equity_curve(closed: list) -> list[tuple[str, float, float]]:
    """Return sorted list of (date, daily_pnl, cumulative_equity) tuples."""
    by_date: dict[str, float] = defaultdict(float)
    for t in closed:
        if t.get("date"):
            by_date[t["date"]] += t["realized_pnl"]
    if not by_date:
        return []
    sorted_dates = sorted(by_date)
    equity, curve = 0.0, []
    for d in sorted_dates:
        equity += by_date[d]
        curve.append((d, by_date[d], round(equity, 2)))
    return curve



def _compute_sharpe_daily(closed: list) -> Tuple[Optional[float], int]:
    """
    Sharpe on daily P&L series. Annualized via √252.
    Meaningful once you have ~20+ trading days. Dollar-denominated (changes with account size).
    Returns (sharpe, num_days).
    """
    curve = _build_equity_curve(closed)
    if len(curve) < 3:
        return None, len(curve)
    daily_pnls = [c[1] for c in curve]
    n     = len(daily_pnls)
    mean  = sum(daily_pnls) / n
    std   = math.sqrt(sum((x - mean) ** 2 for x in daily_pnls) / (n - 1))
    if std == 0:
        return None, n
    return round((mean / std) * math.sqrt(252), 2), n


def _compute_sharpe_r(closed: list) -> Tuple[Optional[float], int]:
    """
    Sharpe on per-trade R-multiples. Size-independent and works on trade-level data.
    Scales by √(252 / avg_trades_per_day) to annualize.
    Meaningful at ~20+ trades. Preferred metric for evaluating edge consistency.
    Returns (sharpe, num_trades).
    """
    r_vals = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    n = len(r_vals)
    if n < 3:
        return None, n
    mean = sum(r_vals) / n
    std  = math.sqrt(sum((x - mean) ** 2 for x in r_vals) / (n - 1))
    if std == 0:
        return None, n
    # Estimate avg trades per day from date range
    dates = sorted({t["date"] for t in closed if t.get("date")})
    avg_per_day = n / max(len(dates), 1)
    annualize   = math.sqrt(252 / max(avg_per_day, 0.01))
    return round((mean / std) * annualize, 2), n


def print_rolling_drawdown(closed: list) -> None:
    """Print rolling drawdown analysis section. Only shown when >= 5 closed trades."""
    if len(closed) < 5:
        return

    curve = _build_equity_curve(closed)
    if not curve:
        return

    # Compute drawdown series: equity - running peak
    peak, drawdowns = float("-inf"), []
    for date, daily_pnl, equity in curve:
        peak = max(peak, equity)
        drawdown = equity - peak  # <= 0
        drawdowns.append((date, equity, peak, drawdown))

    # Rolling DD_WINDOW max-drawdown: worst (most negative) drawdown within each window
    rolling_worst = []
    for i in range(len(drawdowns)):
        window = drawdowns[max(0, i - DD_WINDOW + 1): i + 1]
        worst_in_window = min(d[3] for d in window)
        rolling_worst.append(worst_in_window)

    current_equity  = drawdowns[-1][1]
    current_peak    = drawdowns[-1][2]
    current_dd      = drawdowns[-1][3]
    max_rolling_dd  = min(rolling_worst)                         # most negative ever in any window
    last_window_dd  = min(rolling_worst[-DD_WINDOW:])            # worst in most recent 20 days

    # Last 20 trading-day P&L
    last_n = curve[-DD_WINDOW:]
    last_n_pnl = sum(r[1] for r in last_n)

    print("\n" + "─" * 72)
    print(f"  ROLLING DRAWDOWN  (window: {DD_WINDOW} trading days)")
    print("─" * 72)
    print(f"  Equity (cum P&L) : ${current_equity:>+8.2f}")
    print(f"  Peak equity      : ${current_peak:>+8.2f}")
    print(f"  Current drawdown : ${current_dd:>+8.2f}", end="")
    if current_peak != 0 and current_dd < 0:
        print(f"  ({abs(current_dd/current_peak)*100:.1f}% from peak)", end="")
    print()
    print(f"  Max roll-{DD_WINDOW}d DD  : ${max_rolling_dd:>+8.2f}")
    print(f"  Last {DD_WINDOW}d P&L    : ${last_n_pnl:>+8.2f}  "
          f"({len(last_n)} trading days)")

    # Advisory
    warn_threshold = -(abs(current_peak) * DD_WARN_PCT / 100) if current_peak > 0 else -300
    if last_window_dd <= warn_threshold:
        print(f"\n  ⚠  DRAWDOWN ADVISORY: Last {DD_WINDOW}-day worst DD "
              f"(${last_window_dd:+.2f}) exceeds {DD_WARN_PCT}% of peak.")
        print(f"     Consider reducing MAX_POSITIONS 3 → 2 next session.")
    elif last_n_pnl < 0:
        print(f"\n  ↘  Last {DD_WINDOW} days negative (${last_n_pnl:+.2f}) — monitor closely.")
    else:
        print(f"\n  ✓  Drawdown within normal range — no position adjustment needed.")


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

    sharpe_r, n_trades = _compute_sharpe_r(closed)
    sharpe_d, n_days   = _compute_sharpe_daily(closed)

    def _rating(s):
        if s is None: return "N/A"
        return ("excellent" if s >= 2.0 else "good" if s >= 1.0 else
                "below avg" if s >= 0.0 else "negative")

    if sharpe_r is not None:
        print(f"  Sharpe (R):     {sharpe_r:>+.2f}  ({n_trades} trades — {_rating(sharpe_r)})  ← preferred")
    else:
        print(f"  Sharpe (R):     N/A  ({n_trades} trades — need ≥3)")
    if sharpe_d is not None:
        print(f"  Sharpe (daily): {sharpe_d:>+.2f}  ({n_days} days — {_rating(sharpe_d)})")
    else:
        print(f"  Sharpe (daily): N/A  ({n_days} trading days — need ≥3)")

    if m.get("by_setup"):
        print("\n  BY SIGNAL TYPE:")
        print(f"  {'SETUP':<12} {'TRADES':>6} {'P&L':>9} {'AVG R':>8}")
        print(f"  {'─'*12} {'─'*6} {'─'*9} {'─'*8}")
        for setup, v in sorted(m["by_setup"].items()):
            print(f"  {setup:<12} {v['trades']:>6} "
                  f"${v['pnl']:>+8.2f} {v['avg_r']:>+8.3f}R")

    print_rolling_drawdown(closed)
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
