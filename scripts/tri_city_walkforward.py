#!/usr/bin/env python3
"""
TRI-CITY WALK-FORWARD VALIDATOR — Rolling in-sample/out-of-sample analysis.

Wraps tri_city_backtest.backtest_symbol() with walk-forward validation,
splitting historical data into rolling windows to check whether the strategy's
edge is consistent across time periods.

Each window:
  - In-sample  : preceding N days (stability reference — not used for tuning)
  - Out-of-sample: following M days (the measured test window)

Metrics per window:
  - Total P&L ($), Win Rate (%), Profit Factor, Sharpe (annualized)
  - Trade count per window

Summary across all windows:
  - % windows with positive P&L
  - % windows with Sharpe > 0.5
  - Worst window (for regime awareness)
  - Consistency (std of window Sharpes)

Usage:
    python -W ignore scripts/tri_city_walkforward.py --symbols NVDA TSLA AAPL
    python -W ignore scripts/tri_city_walkforward.py --symbols NVDA \\
        --in-sample-days 180 --out-sample-days 60 --step-days 60
    python -W ignore scripts/tri_city_walkforward.py --symbols NVDA TSLA \\
        --start 2023-01-01 --out-sample-days 90
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
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

from scripts.tri_city_backtest import backtest_symbol  # noqa: E402

CT = ZoneInfo("America/Chicago")

# Optional numpy for Sharpe calculation
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


# ── Sharpe helpers ─────────────────────────────────────────────────────────────

def _daily_pnl_series(trades: list[dict]) -> list[float]:
    """Aggregate trade P&L ($) by date into a daily series."""
    by_date: dict[str, float] = defaultdict(float)
    for t in trades:
        by_date[t["date"]] += t["pnl_$"]
    return list(by_date.values())


def _sharpe(daily_pnls: list[float]) -> float | None:
    """Annualised Sharpe from a list of daily P&L values.

    Returns None if numpy is unavailable or there are fewer than 2 data points.
    """
    if not _HAS_NUMPY or len(daily_pnls) < 2:
        return None
    arr = np.array(daily_pnls, dtype=float)
    std = arr.std(ddof=1)
    if std == 0:
        return None
    return float((arr.mean() / std) * (252 ** 0.5))


def _profit_factor(trades: list[dict]) -> float:
    """Gross profit / gross loss. Returns inf if no losses."""
    gross_profit = sum(t["pnl_$"] for t in trades if t["pnl_$"] > 0)
    gross_loss   = abs(sum(t["pnl_$"] for t in trades if t["pnl_$"] < 0))
    if gross_loss == 0:
        return float("inf")
    return round(gross_profit / gross_loss, 3)


# ── Window generator ───────────────────────────────────────────────────────────

def generate_windows(
    total_start: datetime,
    total_end: datetime,
    in_sample_days: int,
    out_sample_days: int,
    step_days: int,
) -> list[tuple[datetime, datetime, datetime, datetime]]:
    """Yield (is_start, is_end, oos_start, oos_end) tuples.

    The first out-of-sample window begins at total_start + in_sample_days.
    Windows step forward by step_days until OOS end exceeds total_end.
    """
    windows = []
    oos_start = total_start + timedelta(days=in_sample_days)

    while True:
        oos_end = oos_start + timedelta(days=out_sample_days)
        if oos_end > total_end:
            break
        is_start = oos_start - timedelta(days=in_sample_days)
        is_end   = oos_start - timedelta(days=1)
        windows.append((is_start, is_end, oos_start, oos_end))
        oos_start += timedelta(days=step_days)

    return windows


# ── Per-window analysis ────────────────────────────────────────────────────────

def run_window(
    symbols: list[str],
    oos_start: datetime,
    oos_end: datetime,
) -> dict:
    """Run backtest across all symbols for a single OOS window.

    Returns a dict with aggregated metrics.
    """
    all_trades: list[dict] = []
    for sym in symbols:
        trades = backtest_symbol(sym, oos_start, oos_end)
        all_trades.extend(trades)

    if not all_trades:
        return {
            "trade_count":   0,
            "total_pnl":     0.0,
            "win_rate":      None,
            "profit_factor": None,
            "sharpe":        None,
        }

    wins = sum(
        1 for t in all_trades
        if t.get("outcome") in ("full_win", "partial_win")
    )
    total_pnl    = sum(t["pnl_$"] for t in all_trades)
    win_rate     = wins / len(all_trades) if all_trades else 0.0
    profit_factor = _profit_factor(all_trades)
    daily_pnls   = _daily_pnl_series(all_trades)
    sharpe       = _sharpe(daily_pnls)

    return {
        "trade_count":   len(all_trades),
        "total_pnl":     round(total_pnl, 2),
        "win_rate":      round(win_rate * 100, 1),
        "profit_factor": profit_factor,
        "sharpe":        round(sharpe, 3) if sharpe is not None else None,
    }


# ── Table printing ─────────────────────────────────────────────────────────────

def _fmt_sharpe(v: float | None) -> str:
    if v is None:
        return "  N/A"
    return f"{v:+.2f}"


def _fmt_pf(v: float | None) -> str:
    if v is None:
        return "  N/A"
    if v == float("inf"):
        return "  inf"
    return f"{v:.2f}"


def print_window_table(results: list[dict]) -> None:
    """Print the per-window results table."""
    col_w = 72
    print(f"\n{'─' * col_w}")
    print(
        f"  {'WINDOW #':<9} {'OOS START':<12} {'OOS END':<12} "
        f"{'TRADES':>6} {'P&L($)':>9} {'WIN%':>6} {'PF':>6} {'SHARPE':>7}"
    )
    print(f"{'─' * col_w}")

    for r in results:
        sharpe_str = _fmt_sharpe(r["metrics"]["sharpe"])
        pf_str     = _fmt_pf(r["metrics"]["profit_factor"])
        win_str    = (
            f"{r['metrics']['win_rate']:.1f}%"
            if r["metrics"]["win_rate"] is not None
            else "  N/A"
        )
        pnl_str    = f"${r['metrics']['total_pnl']:>+8.2f}"

        print(
            f"  {r['window']:>3}        "
            f"{r['oos_start']:<12} {r['oos_end']:<12} "
            f"{r['metrics']['trade_count']:>6} {pnl_str:>9} "
            f"{win_str:>6} {pf_str:>6} {sharpe_str:>7}"
        )

    print(f"{'─' * col_w}")


def print_summary(results: list[dict]) -> None:
    """Print cross-window summary statistics."""
    col_w = 72
    print(f"\n{'=' * col_w}")
    print("  WALK-FORWARD SUMMARY")
    print(f"{'=' * col_w}")

    total_windows = len(results)
    if total_windows == 0:
        print("  No windows with data.")
        print(f"{'=' * col_w}\n")
        return

    windows_with_data   = [r for r in results if r["metrics"]["trade_count"] > 0]
    windows_positive    = [r for r in windows_with_data if r["metrics"]["total_pnl"] > 0]
    sharpes             = [
        r["metrics"]["sharpe"]
        for r in windows_with_data
        if r["metrics"]["sharpe"] is not None
    ]
    windows_sharpe_pass = [s for s in sharpes if s > 0.5]

    pct_positive = (
        len(windows_positive) / len(windows_with_data) * 100
        if windows_with_data else 0.0
    )
    pct_sharpe   = (
        len(windows_sharpe_pass) / len(sharpes) * 100
        if sharpes else None
    )

    print(f"  Total windows       : {total_windows}")
    print(f"  Windows with trades : {len(windows_with_data)}")
    print(f"  Positive P&L windows: {len(windows_positive)} "
          f"({pct_positive:.0f}%)")

    if pct_sharpe is not None:
        print(f"  Sharpe > 0.5 windows: {len(windows_sharpe_pass)} of "
              f"{len(sharpes)} ({pct_sharpe:.0f}%)")
    else:
        print(f"  Sharpe > 0.5 windows: N/A (numpy not available)")

    # Worst window
    if windows_with_data:
        worst = min(windows_with_data, key=lambda r: r["metrics"]["total_pnl"])
        print(
            f"  Worst window        : #{worst['window']} "
            f"{worst['oos_start']} → {worst['oos_end']}  "
            f"P&L=${worst['metrics']['total_pnl']:+.2f}  "
            f"Trades={worst['metrics']['trade_count']}"
        )

    # Sharpe consistency (std)
    if len(sharpes) >= 2:
        if _HAS_NUMPY:
            import numpy as np
            sharpe_std  = float(np.std(sharpes, ddof=1))
            sharpe_mean = float(np.mean(sharpes))
            print(f"  Sharpe mean / std   : {sharpe_mean:+.3f} / {sharpe_std:.3f}", end="")
            if sharpe_std < 0.5:
                print("  → CONSISTENT")
            elif sharpe_std < 1.0:
                print("  → MODERATE VARIABILITY")
            else:
                print("  → HIGH VARIABILITY (regime-sensitive)")
        else:
            n = len(sharpes)
            mean = sum(sharpes) / n
            variance = sum((s - mean) ** 2 for s in sharpes) / (n - 1)
            std = variance ** 0.5
            print(f"  Sharpe mean / std   : {mean:+.3f} / {std:.3f}")

    # Overall assessment
    print(f"\n  ASSESSMENT:")
    if not windows_with_data:
        print("  ⚠  No trades found in any window — check date range and symbols.")
    elif pct_positive >= 70 and (pct_sharpe is None or pct_sharpe >= 60):
        print("  PASS — Edge appears consistent across walk-forward windows.")
    elif pct_positive >= 50:
        print("  MARGINAL — Edge exists but is inconsistent across periods.")
    else:
        print("  FAIL — Less than 50% of windows profitable. "
              "Strategy may be curve-fit or regime-dependent.")

    print(f"{'=' * col_w}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    two_years_ago = (datetime.now(CT) - timedelta(days=730)).strftime("%Y-%m-%d")

    parser = argparse.ArgumentParser(
        description="Tri-City Walk-Forward Validator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--symbols", nargs="+", required=True,
        help="Space-separated list of symbols, e.g. NVDA TSLA AAPL",
    )
    parser.add_argument(
        "--start", default=two_years_ago,
        help=f"Overall start date YYYY-MM-DD (default: 2 years ago = {two_years_ago})",
    )
    parser.add_argument(
        "--end", default=None,
        help="Overall end date YYYY-MM-DD (default: today)",
    )
    parser.add_argument(
        "--in-sample-days", type=int, default=365, dest="in_sample_days",
        help="Length of the in-sample (preceding) window in calendar days (default: 365)",
    )
    parser.add_argument(
        "--out-sample-days", type=int, default=90, dest="out_sample_days",
        help="Length of each out-of-sample test window in calendar days (default: 90)",
    )
    parser.add_argument(
        "--step-days", type=int, default=90, dest="step_days",
        help="Days to advance the window on each step (default: 90)",
    )
    args = parser.parse_args()

    total_start = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=CT)
    total_end   = (
        datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=CT)
        if args.end
        else datetime.now(CT)
    )
    symbols = [s.upper() for s in args.symbols]

    print(f"\n{'=' * 72}")
    print("  TRI-CITY WALK-FORWARD VALIDATION")
    print(f"  Symbols     : {', '.join(symbols)}")
    print(f"  Period      : {total_start.strftime('%Y-%m-%d')} → {total_end.strftime('%Y-%m-%d')}")
    print(f"  In-sample   : {args.in_sample_days} days  |  "
          f"Out-of-sample: {args.out_sample_days} days  |  "
          f"Step: {args.step_days} days")
    if not _HAS_NUMPY:
        print("  NOTE: numpy not found — Sharpe ratios will not be calculated.")
    print(f"{'=' * 72}")

    windows = generate_windows(
        total_start, total_end,
        args.in_sample_days,
        args.out_sample_days,
        args.step_days,
    )

    if not windows:
        print(
            "\n  ERROR: Date range is too short to form even one walk-forward window.\n"
            f"  Need at least {args.in_sample_days + args.out_sample_days} calendar days "
            f"between {total_start.strftime('%Y-%m-%d')} and {total_end.strftime('%Y-%m-%d')}.\n"
            "  Try a longer --start date, smaller --in-sample-days, or smaller --out-sample-days."
        )
        sys.exit(1)

    print(f"\n  {len(windows)} walk-forward window(s) to evaluate.\n")

    results: list[dict] = []
    for idx, (is_start, is_end, oos_start, oos_end) in enumerate(windows, 1):
        oos_label = f"{oos_start.strftime('%Y-%m-%d')} → {oos_end.strftime('%Y-%m-%d')}"
        print(
            f"  Window {idx}/{len(windows)}  "
            f"[in-sample {is_start.strftime('%Y-%m-%d')} → {is_end.strftime('%Y-%m-%d')}]  "
            f"OOS: {oos_label} ... ",
            end="",
            flush=True,
        )

        metrics = run_window(symbols, oos_start, oos_end)
        print(
            f"{metrics['trade_count']} trades  "
            f"P&L=${metrics['total_pnl']:+.2f}"
        )

        results.append({
            "window":    idx,
            "is_start":  is_start.strftime("%Y-%m-%d"),
            "is_end":    is_end.strftime("%Y-%m-%d"),
            "oos_start": oos_start.strftime("%Y-%m-%d"),
            "oos_end":   oos_end.strftime("%Y-%m-%d"),
            "metrics":   metrics,
        })

    print_window_table(results)
    print_summary(results)


if __name__ == "__main__":
    main()
