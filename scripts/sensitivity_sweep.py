#!/usr/bin/env python3
"""
TRI-CITY SENSITIVITY SWEEP — Parameter stability analysis.

Tests how key parameters affect strategy performance across a historical
universe of gap-up stocks. Identifies robust parameters (stable across a
range) vs cliff-edge values (small change = big outcome shift = overfit risk).

Parameters swept (daily-bar simulatable):
  MIN_GAP_PCT   — minimum gap % to qualify as entry candidate
  MIN_RVOL      — minimum relative volume at entry
  STOP_PCT      — stop loss % below entry
  SMA_SLOPE     — Weinstein filter: require SMA50 > SMA100

Parameters NOT swept (require intraday data — tested manually):
  PULLBACK_FAIL_BUFFER  — Fix B 0.5% buffer (intraday wick guard)
  EMA_STOP_BUFFER       — 30¢ below EMA20 (intraday EMA20 stop)
  LOOSE_CONSOL_PENALTY  — position sizing only, no outcome effect
  RE_ENTRY_MAX_LOSS     — re-entry logic, needs live tick data

Usage:
    python -W ignore scripts/sensitivity_sweep.py
    python -W ignore scripts/sensitivity_sweep.py --start 2024-01-01
    python -W ignore scripts/sensitivity_sweep.py --symbols NVDA TSLA AMD MARA
    python -W ignore scripts/sensitivity_sweep.py --start 2023-01-01 --save
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

WORKSPACE = Path.home() / "tri-city-inator"
sys.path.insert(0, str(WORKSPACE))

try:
    from dotenv import load_dotenv
    _env = WORKSPACE / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

from scripts.tri_city_backtest import fetch_daily_bars_df, sma, simulate_trade

from zoneinfo import ZoneInfo
CT = ZoneInfo("America/Chicago")

# ── Default universe — gap-up prone names across sectors ─────────────────────
DEFAULT_UNIVERSE = [
    "NVDA", "AMD", "TSLA", "MARA", "RIOT",
    "PLTR", "IONQ", "UPST", "SMCI", "SOXL",
    "META", "COIN", "HOOD", "SOFI", "DKNG",
]

# ── Baseline (current live values) ───────────────────────────────────────────
BASELINE = {
    "min_gap_pct": 3.0,
    "min_rvol":    1.5,
    "stop_pct":    5.0,
    "sma_slope":   True,   # Weinstein: require SMA50 > SMA100
    "t1_pct":      10.0,
    "t2_pct":      20.0,
    "t3_pct":      30.0,
}

# ── Sweep ranges ──────────────────────────────────────────────────────────────
SWEEPS = {
    "min_gap_pct": [1.5, 2.0, 2.5, 3.0, 4.0, 5.0],
    "min_rvol":    [1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0],
    "stop_pct":    [3.0, 4.0, 5.0, 6.0, 7.0, 8.0],
    "sma_slope":   [False, True],
}


# ── Core backtest (parameterized) ─────────────────────────────────────────────

def run_backtest(symbols: list[str], start: datetime, end: datetime,
                 min_gap_pct: float = 3.0, min_rvol: float = 1.5,
                 stop_pct: float = 5.0, sma_slope: bool = True,
                 t1_pct: float = 10.0, t2_pct: float = 20.0,
                 t3_pct: float = 30.0) -> dict:
    """
    Run backtest over all symbols with given parameters.
    Returns aggregated stats dict.
    """
    all_trades = []

    for symbol in symbols:
        df = fetch_daily_bars_df(symbol, start, end)
        if df is None or len(df) < 105:   # 55 SMA50 + 50 SMA100 warmup
            continue

        closes  = list(df["close"])
        opens   = list(df["open"])
        highs   = list(df["high"])
        lows    = list(df["low"])
        volumes = list(df["volume"])
        dates   = list(df.index)

        sma50_series  = sma(closes, 50)
        sma100_series = sma(closes, 100)

        avg_vols = []
        for i in range(len(volumes)):
            avg_vols.append(
                sum(volumes[i - 20:i]) / 20 if i >= 20 else None
            )

        start_idx = 101  # warmup for SMA100

        for i in range(start_idx, len(dates) - 1):
            bar_date = dates[i]
            bar_date_only = bar_date.date() if hasattr(bar_date, "date") else bar_date
            if bar_date_only < start.date() or bar_date_only > end.date():
                continue

            prev_close = closes[i - 1]
            open_price = opens[i]
            curr_vol   = volumes[i]
            sma50      = sma50_series[i]
            sma100     = sma100_series[i]
            avg_vol    = avg_vols[i]

            # Gap filter
            gap_pct = (open_price - prev_close) / prev_close * 100
            if gap_pct < min_gap_pct:
                continue

            # RVOL filter
            rvol = (curr_vol / avg_vol) if avg_vol and avg_vol > 0 else 0
            if rvol < min_rvol:
                continue

            # Stage 2: price above SMA50
            if not sma50 or open_price <= sma50:
                continue

            # Weinstein SMA slope: SMA50 must be above SMA100
            if sma_slope and (not sma100 or sma50 <= sma100):
                continue

            # Enter at next bar's open
            if i + 1 >= len(dates):
                continue

            entry_price  = opens[i + 1]
            future_highs = highs[i + 1:i + 11]
            future_lows  = lows[i + 1:i + 11]
            if not future_highs:
                continue

            result = simulate_trade(
                entry_price, future_highs, future_lows
            )

            outcome = "stop"
            if result["t3_hit"]:   outcome = "full_win"
            elif result["t2_hit"]: outcome = "partial_win"
            elif result["t1_hit"]: outcome = "partial_win"
            elif result["pnl_pct"] > -0.5: outcome = "scratch"

            all_trades.append({
                "symbol":  symbol,
                "date":    str(bar_date_only),
                "gap_pct": round(gap_pct, 2),
                "rvol":    round(rvol, 2),
                "outcome": outcome,
                **result,
            })

    return _aggregate(all_trades)


def _aggregate(trades: list[dict]) -> dict:
    """Compute summary stats from a list of trade dicts."""
    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0,
                "t1_rate": 0, "t2_rate": 0, "t3_rate": 0, "stop_rate": 0,
                "expectancy": 0, "max_dd": 0}

    wins     = sum(1 for t in trades if t["outcome"] in ("full_win", "partial_win"))
    stops    = sum(1 for t in trades if t["outcome"] == "stop")
    t1_hits  = sum(1 for t in trades if t["t1_hit"])
    t2_hits  = sum(1 for t in trades if t["t2_hit"])
    t3_hits  = sum(1 for t in trades if t["t3_hit"])
    total_pnl = sum(t["pnl_$"] for t in trades)
    avg_pnl   = total_pnl / n

    # Max drawdown: largest peak-to-trough in running cumulative P&L
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        cum += t["pnl_$"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    # Expectancy: (win_rate * avg_win) - (loss_rate * avg_loss)
    avg_win  = (sum(t["pnl_$"] for t in trades if t["pnl_$"] > 0) / wins
                if wins > 0 else 0)
    avg_loss = (abs(sum(t["pnl_$"] for t in trades if t["pnl_$"] < 0)) / stops
                if stops > 0 else 0)
    win_rate  = wins / n
    loss_rate = stops / n
    expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)

    return {
        "n":          n,
        "win_rate":   round(win_rate * 100, 1),
        "avg_pnl":    round(avg_pnl, 2),
        "total_pnl":  round(total_pnl, 2),
        "t1_rate":    round(t1_hits / n * 100, 1),
        "t2_rate":    round(t2_hits / n * 100, 1),
        "t3_rate":    round(t3_hits / n * 100, 1),
        "stop_rate":  round(stops / n * 100, 1),
        "expectancy": round(expectancy, 2),
        "max_dd":     round(max_dd, 2),
    }


# ── Stability scoring ─────────────────────────────────────────────────────────

def stability_score(values: list[float]) -> str:
    """
    Rate stability of a metric across sweep values.
    Cliff-edge = large swing = overfit risk.
    """
    if len(values) < 2:
        return "N/A"
    rng = max(values) - min(values)
    avg = sum(values) / len(values)
    if avg == 0:
        return "FLAT"
    cv = rng / abs(avg)   # coefficient of variation (range / mean)
    if cv < 0.15:   return "STABLE   ✓"
    if cv < 0.35:   return "MODERATE ~"
    return "VOLATILE !"


# ── Print helpers ─────────────────────────────────────────────────────────────

def print_sweep(param_name: str, values: list, results: list[dict],
                baseline_val, baseline_result: dict):
    label_map = {
        "min_gap_pct": "MIN_GAP_PCT",
        "min_rvol":    "MIN_RVOL",
        "stop_pct":    "STOP_PCT",
        "sma_slope":   "SMA_SLOPE",
    }
    label = label_map.get(param_name, param_name.upper())
    unit_map = {
        "min_gap_pct": "%",
        "min_rvol":    "x",
        "stop_pct":    "%",
        "sma_slope":   "",
    }
    unit = unit_map.get(param_name, "")

    print(f"\n{'─'*90}")
    print(f"  SWEEP: {label}")
    print(f"  Baseline: {baseline_val}{unit}  "
          f"→  {baseline_result['n']} trades | "
          f"{baseline_result['win_rate']:.1f}% win | "
          f"${baseline_result['avg_pnl']:+.2f}/trade | "
          f"${baseline_result['total_pnl']:+.2f} total")
    print(f"{'─'*90}")
    print(f"  {'VALUE':<10} {'N':>5} {'WIN%':>6} {'AVG_P&L':>9} {'TOTAL':>10} "
          f"{'T1%':>5} {'T2%':>5} {'T3%':>5} {'STOP%':>6} {'EXPECT':>8} {'MAX_DD':>8}")
    print(f"  {'─'*10} {'─'*5} {'─'*6} {'─'*9} {'─'*10} "
          f"{'─'*5} {'─'*5} {'─'*5} {'─'*6} {'─'*8} {'─'*8}")

    for val, r in zip(values, results):
        marker = " ◄ baseline" if val == baseline_val else ""
        val_str = f"{val}{unit}"
        if r["n"] == 0:
            print(f"  {val_str:<10} {'—':>5}  (no trades)")
            continue
        print(f"  {val_str:<10} {r['n']:>5} {r['win_rate']:>5.1f}% "
              f"${r['avg_pnl']:>+8.2f} ${r['total_pnl']:>+9.2f} "
              f"{r['t1_rate']:>4.0f}% {r['t2_rate']:>4.0f}% {r['t3_rate']:>4.0f}% "
              f"{r['stop_rate']:>5.0f}% ${r['expectancy']:>+7.2f} "
              f"${r['max_dd']:>7.2f}{marker}")

    # Stability assessment
    win_rates = [r["win_rate"] for r in results if r["n"] > 0]
    avg_pnls  = [r["avg_pnl"]  for r in results if r["n"] > 0]
    print(f"\n  Win rate stability:   {stability_score(win_rates)}")
    print(f"  Avg P&L stability:    {stability_score(avg_pnls)}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City Sensitivity Sweep")
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_UNIVERSE,
                        help="Symbols to backtest (default: built-in universe of 15)")
    parser.add_argument("--start",   default="2024-01-01",
                        help="Start date YYYY-MM-DD (default: 2024-01-01)")
    parser.add_argument("--end",     default=None,
                        help="End date YYYY-MM-DD (default: today)")
    parser.add_argument("--save",    action="store_true",
                        help="Save full results to logs/sensitivity-results.json")
    args = parser.parse_args()

    start   = datetime.strptime(args.start, "%Y-%m-%d").replace(tzinfo=CT)
    end     = datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=CT) \
              if args.end else datetime.now(CT)
    symbols = [s.upper() for s in args.symbols]

    print(f"\n{'='*90}")
    print(f"  TRI-CITY SENSITIVITY SWEEP")
    print(f"  Period:   {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')}")
    print(f"  Universe: {', '.join(symbols)}")
    print(f"  Baseline: gap>{BASELINE['min_gap_pct']}% | rvol>{BASELINE['min_rvol']}x | "
          f"stop {BASELINE['stop_pct']}% | sma_slope={BASELINE['sma_slope']}")
    print(f"\n  NOTE: PULLBACK_FAIL_BUFFER, EMA_STOP_BUFFER, LOOSE_CONSOL_PENALTY,")
    print(f"  and RE_ENTRY_MAX_LOSS are NOT swept here — they require intraday data.")
    print(f"{'='*90}")

    # Fetch all data up front (avoids re-downloading for each sweep)
    print(f"\n  Fetching data for {len(symbols)} symbols...")

    all_results = {}
    baseline_result = run_backtest(symbols, start, end, **BASELINE)

    print(f"  Baseline: {baseline_result['n']} trades | "
          f"{baseline_result['win_rate']:.1f}% win | "
          f"${baseline_result['avg_pnl']:+.2f}/trade")

    for param, values in SWEEPS.items():
        sweep_results = []
        for val in values:
            kwargs = {**BASELINE, param: val}
            r = run_backtest(symbols, start, end, **kwargs)
            sweep_results.append(r)

        all_results[param] = {
            "values":  values,
            "results": sweep_results,
        }

        print_sweep(param, values, sweep_results, BASELINE[param], baseline_result)

    # ── Overall verdict ───────────────────────────────────────────────────────
    print(f"\n{'='*90}")
    print(f"  OVERALL STABILITY VERDICT")
    print(f"{'='*90}")
    print(f"  {'PARAMETER':<22} {'WIN RATE':^14} {'AVG P&L':^14} {'VERDICT'}")
    print(f"  {'─'*22} {'─'*14} {'─'*14} {'─'*20}")

    verdicts = {}
    for param, data in all_results.items():
        valid = [(v, r) for v, r in zip(data["values"], data["results"]) if r["n"] > 0]
        if not valid:
            continue
        win_rates = [r["win_rate"] for _, r in valid]
        avg_pnls  = [r["avg_pnl"]  for _, r in valid]
        ws = stability_score(win_rates)
        ps = stability_score(avg_pnls)
        # Overall: stable if both are stable or moderate
        if "STABLE" in ws and "STABLE" in ps:
            verdict = "ROBUST — safe to tune"
        elif "VOLATILE" in ws or "VOLATILE" in ps:
            verdict = "FRAGILE — cliff-edge risk"
        else:
            verdict = "MODERATE — watch closely"
        verdicts[param] = verdict

        label = param.upper().replace("_", " ")
        wr_range = f"{min(win_rates):.1f}%–{max(win_rates):.1f}%"
        pnl_range = f"${min(avg_pnls):+.2f}–${max(avg_pnls):+.2f}"
        print(f"  {label:<22} {wr_range:^14} {pnl_range:^14} {verdict}")

    print(f"\n  Legend: STABLE ✓ = CV<15%  MODERATE ~ = CV 15-35%  VOLATILE ! = CV>35%")
    print(f"  CV = (max-min) / mean  — measures how much results swing across the range")
    print(f"{'='*90}\n")

    if args.save:
        out = WORKSPACE / "logs" / "sensitivity-results.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "run_at":   datetime.now(CT).strftime("%Y-%m-%d %H:%M CT"),
            "start":    start.strftime("%Y-%m-%d"),
            "end":      end.strftime("%Y-%m-%d"),
            "symbols":  symbols,
            "baseline": BASELINE,
            "baseline_result": baseline_result,
            "sweeps":   {
                p: {"values": d["values"], "results": d["results"]}
                for p, d in all_results.items()
            },
            "verdicts": verdicts,
        }
        out.write_text(json.dumps(payload, indent=2))
        print(f"  Results saved → {out}\n")


if __name__ == "__main__":
    main()
