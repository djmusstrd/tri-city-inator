#!/usr/bin/env python3
"""
TRI-CITY BACKTEST SCREENER — Find symbols with a structural edge for the
Tri-City Inator strategy and produce a "proven performers" evergreen list.

Backtests a curated universe of ~200-300 momentum/mid-cap names over a rolling
window using the same simulation logic as tri_city_backtest.py. Symbols that
pass all filter criteria are scored and saved to shared/tri-city-evergreen.json.

The scanner (tri_city_scanner.py) blends top evergreen names into tv_symbols
slots when they are also showing a gap on the day.

Usage:
    python -W ignore scripts/tri_city_backtest_screener.py
    python -W ignore scripts/tri_city_backtest_screener.py --window 90
    python -W ignore scripts/tri_city_backtest_screener.py --min-trades 15 --min-winrate 0.48
    python -W ignore scripts/tri_city_backtest_screener.py --output shared/tri-city-evergreen.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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

CT = ZoneInfo("America/Chicago")

# ── Config from .env (mirrors tri_city_backtest.py) ───────────────────────────
MIN_GAP_PCT   = float(os.getenv("MIN_GAP_PCT",  "3.0"))
MIN_VOL_MULTI = float(os.getenv("MIN_RVOL",     "1.5"))
STOP_PCT      = float(os.getenv("STOP_PCT",     "5.0"))
T1_PCT        = float(os.getenv("T1_PCT",       "10.0"))
T2_PCT        = float(os.getenv("T2_PCT",       "20.0"))
T3_PCT        = float(os.getenv("T3_PCT",       "30.0"))
FIXED_RISK    = float(os.getenv("FIXED_RISK",   "150"))

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ── Curated momentum universe ─────────────────────────────────────────────────
# Covers proven gap-and-go names across semis, biotech, tech, EV, space, fintech.
# Supplemented at runtime with candidates from executions/journal history and the
# current premarket scanner top-100.

BASE_UNIVERSE: list[str] = [
    # Mega-cap / flagship momentum
    "NVDA", "AMD", "TSLA", "AMZN", "META", "GOOGL", "MSFT", "AAPL",
    # High-beta tech / software
    "PLTR", "SNOW", "MDB", "DDOG", "NET", "CRWD", "OKTA", "ZS", "VEEV",
    "BILL", "HUBS", "TTD", "TWLO", "U", "RBLX", "AFRM", "UPST", "SOFI",
    "HOOD", "COIN", "MSTR", "PYPL", "SQ",
    # Semis (ORB-ready high-beta)
    "AMAT", "LRCX", "KLAC", "MRVL", "ONTO", "CRDO", "RMBS", "AMBA",
    "WOLF", "AEHR", "CEVA", "SMCI", "NVTS", "AOSL", "POWI", "MPWR",
    "SLAB", "IMOS", "HIMX", "UMC", "MX", "ACLS", "ENTG", "UCTT",
    "ICHR", "MKSI", "FORM", "PLAB", "CRUS", "DIOD", "SIMO", "SITM", "MTSI",
    # Space / defense tech
    "RKLB", "LUNR", "SPCE", "RDW", "ASTS", "PL", "MNTS",
    "ASTR", "LLAP",
    # Biotech / pharma (high-vol on catalysts)
    "NRXP", "IMRX", "QURE", "ASPI", "GRDX", "SLS", "HLIT", "IPWR",
    "ARDX", "XNCR", "AGEN", "VKTX", "PRAX", "ACAD", "INSM", "KRYS",
    "BEAM", "EDIT", "NTLA", "CRSP", "DNLI", "JANX", "KROS", "RNAC",
    "AGIO", "FOLD", "SAGE", "PTGX", "PRTA", "FATE", "TGTX", "REGN",
    "SGEN", "ALKS", "ACMR", "AXNX", "NVAX", "BNTX", "MRNA", "INO", "OCGN",
    # EV / clean energy
    "RIVN", "LCID", "NKLA", "CHPT", "BLNK", "EVGO",
    "FCEL", "PLUG", "BLDP", "BE", "WKHS",
    # Fintech / crypto-adjacent
    "MARA", "HUT", "RIOT", "CLSK", "BITF", "BTBT", "EOSE", "HIVE",
    "CIFR", "IREN",
    # AI / quantum / data
    "AI", "BBAI", "SOUN", "ARQQ", "QUBT", "IONQ", "RGTI", "QBTS",
    "PONY", "RCAT", "JOBY", "ACHR",
    # Small/mid momentum names
    "FUTU", "TIGR", "MOMO", "RUM", "PD", "ASAN", "DOMO",
    "NCNO", "ALKT", "DWSN", "GCTS", "TSEM", "TSSI", "FUTG",
    "NXT", "NBIS", "MGM", "NCLH", "WYY", "UTI", "SKM", "UMAC",
    "FATN", "FIG", "RGTX", "DELL", "HPE", "UNH", "XOM",
    # Mid-cap industrials / special situations
    "CECO", "AMKR", "OUST", "INDI", "APLD", "CRMD", "HOVR",
    "HTT", "IPST", "NBIL", "ONDG", "QNTM", "RVI",
    "SOFX", "TDIC", "AAOI", "BRUN", "CHA", "DLTR",
    "FIGG", "GEMI", "GO", "TE",
    # Consumer / retail momentum
    "GME", "AMC", "BB", "TLRY", "CGC", "SNDL",
    # Additional fintech
    "OPEN", "LMND", "ROOT",
]


def _dedupe(symbols: list[str]) -> list[str]:
    """Deduplicate preserving order."""
    seen: set[str] = set()
    out = []
    for s in symbols:
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def build_universe() -> list[str]:
    """
    Build full backtest universe by combining:
    1. BASE_UNIVERSE curated list
    2. Top-100 symbols from current tri-city-candidates.json
    3. All symbols from tri-city-executions.json and tri-city-journal.json history
    Returns deduplicated, uppercase list.
    """
    universe = list(BASE_UNIVERSE)

    # From today's scanner candidates
    candidates_path = WORKSPACE / "shared" / "tri-city-candidates.json"
    if candidates_path.exists():
        try:
            data = json.loads(candidates_path.read_text())
            cands = data.get("candidates", [])
            for c in cands:
                sym = c.get("symbol", "").upper().strip()
                if sym:
                    universe.append(sym)
        except Exception as e:
            logger.debug(f"candidates merge: {e}")

    # From execution history
    exe_path = WORKSPACE / "logs" / "tri-city-executions.json"
    if exe_path.exists():
        try:
            exe_data = json.loads(exe_path.read_text())
            for entry in exe_data:
                sym = entry.get("symbol", "").upper().strip()
                if sym and len(sym) <= 8:
                    universe.append(sym)
        except Exception as e:
            logger.debug(f"executions merge: {e}")

    # From journal history
    journal_path = WORKSPACE / "logs" / "tri-city-journal.json"
    if journal_path.exists():
        try:
            j_data = json.loads(journal_path.read_text())
            for entry in j_data:
                sym = entry.get("symbol", "").upper().strip()
                if sym and len(sym) <= 8:
                    universe.append(sym)
        except Exception as e:
            logger.debug(f"journal merge: {e}")

    # Clean up: remove obviously invalid tokens (warrants, units, special chars)
    cleaned = []
    for s in universe:
        s = s.strip().upper()
        if not s:
            continue
        # Skip warrants/units/rights/preferred (contain / or are > 6 chars)
        if "/" in s or len(s) > 6:
            continue
        cleaned.append(s)

    return _dedupe(cleaned)


# ── Data fetching (mirrors tri_city_backtest.py) ───────────────────────────────

def fetch_daily_bars_df(symbol: str, start: datetime, end: datetime):
    """Return pandas DataFrame of daily bars using yfinance."""
    try:
        import yfinance as yf
        fetch_start = start - timedelta(days=80)   # SMA50 warm-up buffer
        df = yf.download(
            symbol,
            start=fetch_start.strftime("%Y-%m-%d"),
            end=(end + timedelta(days=1)).strftime("%Y-%m-%d"),
            progress=False,
            auto_adjust=True,
        )
        if df.empty:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        if hasattr(df.index, "tz") and df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df.sort_index()
        return df
    except Exception:
        return None


def sma(series: list[float], period: int) -> list[float | None]:
    result = []
    for i in range(len(series)):
        if i < period - 1:
            result.append(None)
        else:
            result.append(sum(series[i - period + 1:i + 1]) / period)
    return result


# ── Trade simulation (mirrors tri_city_backtest.py) ───────────────────────────

def simulate_trade(entry_price: float, highs: list[float], lows: list[float]) -> dict:
    """50-25-25 scale-out simulation. Returns P&L breakdown dict."""
    stop_price = entry_price * (1 - STOP_PCT / 100)
    t1_price   = entry_price * (1 + T1_PCT / 100)
    t2_price   = entry_price * (1 + T2_PCT / 100)
    t3_price   = entry_price * (1 + T3_PCT / 100)

    remaining  = 1.0
    pnl_pct    = 0.0
    t1_hit = t2_hit = t3_hit = stop_hit = False
    bars_held  = 0
    exit_price = entry_price

    for high, low in zip(highs, lows):
        bars_held += 1

        if low <= stop_price:
            pnl_pct  += remaining * ((stop_price - entry_price) / entry_price)
            remaining = 0
            stop_hit  = True
            exit_price = stop_price
            break

        if not t3_hit and high >= t3_price and remaining > 0:
            lot = min(remaining, 0.25)
            pnl_pct  += lot * ((t3_price - entry_price) / entry_price)
            remaining -= lot
            t3_hit    = True
            exit_price = t3_price

        if not t2_hit and high >= t2_price and remaining > 0:
            lot = min(remaining, 0.25)
            pnl_pct  += lot * ((t2_price - entry_price) / entry_price)
            remaining -= lot
            t2_hit    = True

        if not t1_hit and high >= t1_price and remaining > 0:
            lot = min(remaining, 0.50)
            pnl_pct  += lot * ((t1_price - entry_price) / entry_price)
            remaining -= lot
            t1_hit    = True

        if remaining <= 0:
            break

    if remaining > 0:
        pnl_pct += remaining * ((exit_price - entry_price) / entry_price)

    return {
        "pnl_pct":   round(pnl_pct * 100, 3),
        "pnl_$":     round(FIXED_RISK * pnl_pct / (STOP_PCT / 100), 2),
        "t1_hit":    t1_hit,
        "t2_hit":    t2_hit,
        "t3_hit":    t3_hit,
        "stop_hit":  stop_hit,
        "bars_held": bars_held,
    }


# ── Per-symbol backtest ────────────────────────────────────────────────────────

def backtest_symbol(symbol: str, start: datetime, end: datetime) -> list[dict]:
    """Run full backtest for one symbol. Returns list of trade records."""
    df = fetch_daily_bars_df(symbol, start, end)
    if df is None or len(df) < 55:
        return []

    closes  = list(df["close"])
    opens   = list(df["open"])
    highs   = list(df["high"])
    lows    = list(df["low"])
    volumes = list(df["volume"])
    dates   = list(df.index)

    sma50_series = sma(closes, 50)

    avg_vols = []
    for i in range(len(volumes)):
        if i < 20:
            avg_vols.append(None)
        else:
            avg_vols.append(sum(volumes[i - 20:i]) / 20)

    trades = []
    start_idx = max(51, 0)

    for i in range(start_idx, len(dates) - 1):
        bar_date = dates[i]
        if hasattr(bar_date, "date"):
            bar_date_only = bar_date.date()
        else:
            bar_date_only = bar_date

        if bar_date_only < start.date() or bar_date_only > end.date():
            continue

        prev_close = closes[i - 1]
        open_price = opens[i]
        curr_vol   = volumes[i]
        sma50      = sma50_series[i]
        avg_vol    = avg_vols[i]

        gap_pct = (open_price - prev_close) / prev_close * 100
        if gap_pct < MIN_GAP_PCT:
            continue

        rvol = (curr_vol / avg_vol) if avg_vol and avg_vol > 0 else 0
        if rvol < MIN_VOL_MULTI:
            continue

        if sma50 and open_price <= sma50:
            continue

        if i + 1 >= len(dates):
            continue

        entry_price  = opens[i + 1]
        future_highs = highs[i + 1:i + 11]
        future_lows  = lows[i + 1:i + 11]

        if not future_highs:
            continue

        result = simulate_trade(entry_price, future_highs, future_lows)

        outcome = "stop"
        if result["t3_hit"]:
            outcome = "full_win"
        elif result["t2_hit"] or result["t1_hit"]:
            outcome = "partial_win"
        elif result["pnl_pct"] > -0.5:
            outcome = "scratch"

        trades.append({
            "date":        str(bar_date_only),
            "symbol":      symbol,
            "gap_pct":     round(gap_pct, 2),
            "rvol":        round(rvol, 2),
            "entry_price": round(entry_price, 4),
            "outcome":     outcome,
            **result,
        })

    return trades


# ── Symbol metrics aggregation ────────────────────────────────────────────────

def compute_symbol_metrics(symbol: str, trades: list[dict]) -> dict:
    """Aggregate trade list into per-symbol performance metrics."""
    n = len(trades)
    wins = sum(1 for t in trades if t["outcome"] in ("full_win", "partial_win"))
    total_pnl = sum(t["pnl_$"] for t in trades)
    avg_pnl   = total_pnl / n if n else 0.0
    win_rate  = wins / n if n else 0.0
    t1_hits   = sum(1 for t in trades if t["t1_hit"])
    t3_hits   = sum(1 for t in trades if t["t3_hit"])
    t1_rate   = t1_hits / n if n else 0.0
    t3_rate   = t3_hits / n if n else 0.0
    return {
        "symbol":    symbol,
        "trades":    n,
        "wins":      wins,
        "win_rate":  round(win_rate, 4),
        "total_pnl": round(total_pnl, 2),
        "avg_pnl":   round(avg_pnl, 2),
        "t1_rate":   round(t1_rate, 4),
        "t3_rate":   round(t3_rate, 4),
    }


# ── Composite scoring ─────────────────────────────────────────────────────────

def score_symbol(metrics: dict, max_pnl: float, max_trades: int) -> float:
    """
    Composite score (0-1):
      win_rate        x 0.35
      normalized_pnl  x 0.35  (normalized by max total P&L in universe)
      t3_hit_rate     x 0.20  (runners -- full T3 exit = highest R)
      trade_frequency x 0.10  (normalized by max trades; more setups = more signal)
    """
    win_component  = metrics["win_rate"] * 0.35
    pnl_component  = (metrics["total_pnl"] / max_pnl * 0.35) if max_pnl > 0 else 0.0
    t3_component   = metrics["t3_rate"] * 0.20
    freq_component = (metrics["trades"] / max_trades * 0.10) if max_trades > 0 else 0.0
    return round(win_component + pnl_component + t3_component + freq_component, 4)


# ── Parallel backtest runner ───────────────────────────────────────────────────

def run_screener(
    universe:    list[str],
    start:       datetime,
    end:         datetime,
    min_trades:  int,
    min_winrate: float,
    max_workers: int = 12,
) -> list[dict]:
    """
    Run backtests for all symbols in parallel.
    Returns list of passing symbols with metrics, unsorted.
    """
    results: list[dict]      = []
    skipped: list[str]       = []
    failed_filter: list[str] = []

    print(f"  Backtesting {len(universe)} symbols ({max_workers} workers)...")

    def _run(sym: str) -> tuple[str, list[dict]]:
        try:
            trades = backtest_symbol(sym, start, end)
            return sym, trades
        except Exception as e:
            logger.debug(f"{sym} backtest error: {e}")
            return sym, []

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run, sym): sym for sym in universe}
        done = 0
        for future in as_completed(futures):
            done += 1
            sym, trades = future.result()

            if done % 25 == 0 or done == len(universe):
                print(f"  [{done}/{len(universe)}] completed...")

            if not trades:
                skipped.append(sym)
                continue

            m = compute_symbol_metrics(sym, trades)

            # Apply filters
            if m["trades"] < min_trades:
                failed_filter.append(sym)
                continue
            if m["win_rate"] < min_winrate:
                failed_filter.append(sym)
                continue
            if m["total_pnl"] <= 0:
                failed_filter.append(sym)
                continue
            if m["avg_pnl"] <= 0:
                failed_filter.append(sym)
                continue

            results.append(m)

    print(f"  {len(universe)} symbols processed -> "
          f"{len(results)} passed filters | "
          f"{len(skipped)} no data | "
          f"{len(failed_filter)} failed criteria")

    return results


# ── Ranked table printer ───────────────────────────────────────────────────────

def print_ranked_table(ranked: list[dict], start: datetime, end: datetime,
                       min_trades: int, min_winrate: float, top_n: int = 25):
    print(f"\n{'='*82}")
    print(f"  TRI-CITY EVERGREEN SCREENER -- {start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')}")
    print(f"  Filters: min {min_trades} trades | win rate >={min_winrate*100:.0f}% | P&L > $0")
    print(f"  Scoring: win_rate x0.35 + norm_pnl x0.35 + t3_rate x0.20 + freq x0.10")
    print(f"{'='*82}")

    if not ranked:
        print("  No symbols passed all filter criteria.")
        print(f"{'='*82}\n")
        return

    top = ranked[:top_n]
    print(f"\n{'RK':<4} {'SYMBOL':<8} {'TRADES':>7} {'WIN%':>6} "
          f"{'TOTAL P&L':>10} {'AVG P&L':>8} {'T1%':>5} {'T3%':>5} {'SCORE':>7}")
    print("-" * 82)

    for s in top:
        print(f"{s['rank']:<4} {s['symbol']:<8} {s['trades']:>7} "
              f"{s['win_rate']*100:>5.1f}% "
              f"${s['total_pnl']:>+9.2f} "
              f"${s['avg_pnl']:>+7.2f} "
              f"{s['t1_rate']*100:>4.1f}% "
              f"{s['t3_rate']*100:>4.1f}% "
              f"{s['score']:>7.4f}")

    total_shown = min(top_n, len(ranked))
    print(f"\n{'─'*82}")
    print(f"  Showing top {total_shown} of {len(ranked)} qualifying symbols.")
    print(f"{'─'*82}")

    if len(ranked) > top_n:
        rest = [s["symbol"] for s in ranked[top_n:]]
        print(f"  Also qualifying: {', '.join(rest)}")

    print(f"\n{'='*82}\n")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City Backtest Screener")
    parser.add_argument("--window",      type=int,   default=180,
                        help="Rolling lookback window in days (default: 180)")
    parser.add_argument("--min-trades",  type=int,   default=15,
                        help="Minimum trades to qualify (default: 15)")
    parser.add_argument("--min-winrate", type=float, default=0.48,
                        help="Minimum win rate to qualify (default: 0.48)")
    parser.add_argument("--output",      type=str,
                        default="shared/tri-city-evergreen.json",
                        help="Output path (default: shared/tri-city-evergreen.json)")
    parser.add_argument("--workers",     type=int,   default=12,
                        help="Parallel workers (default: 12)")
    parser.add_argument("--dry-run",     action="store_true",
                        help="Print results, don't save")
    args = parser.parse_args()

    end   = datetime.now(CT)
    start = end - timedelta(days=args.window)

    now_str = end.strftime("%Y-%m-%d %H:%M CT")
    print(f"\n{'='*82}")
    print(f"  TRI-CITY BACKTEST SCREENER -- {now_str}")
    print(f"  Window: {args.window} days ({start.strftime('%Y-%m-%d')} -> {end.strftime('%Y-%m-%d')})")
    print(f"  Conditions: Gap >{MIN_GAP_PCT:.0f}% | RVol >{MIN_VOL_MULTI:.1f}x | "
          f"Stage 2 | 50-25-25 exits")
    print(f"{'='*82}")

    # Build universe
    universe = build_universe()
    print(f"\n  Universe: {len(universe)} symbols (base + candidates + history)")

    # Run parallel backtests
    passing = run_screener(
        universe, start, end,
        min_trades=args.min_trades,
        min_winrate=args.min_winrate,
        max_workers=args.workers,
    )

    if not passing:
        print("\n  No symbols passed all filter criteria.")
        print("  Try: --min-trades 5 --min-winrate 0.35 --window 90")
        return

    # Normalize for scoring
    max_pnl    = max(m["total_pnl"] for m in passing)
    max_trades = max(m["trades"]    for m in passing)

    for m in passing:
        m["score"] = score_symbol(m, max_pnl, max_trades)

    ranked = sorted(passing, key=lambda x: x["score"], reverse=True)
    for i, m in enumerate(ranked, 1):
        m["rank"] = i

    print_ranked_table(ranked, start, end, args.min_trades, args.min_winrate)

    if not args.dry_run:
        out_path = WORKSPACE / args.output
        out_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "generated":        end.strftime("%Y-%m-%d"),
            "generated_at":     end.strftime("%H:%M CT"),
            "window_days":      args.window,
            "min_trades":       args.min_trades,
            "min_winrate":      args.min_winrate,
            "total_qualifying": len(ranked),
            "symbols": ranked,
        }
        out_path.write_text(json.dumps(payload, indent=2))
        print(f"  Saved -> {out_path}")
        print(f"  {len(ranked)} evergreen symbols written.\n")


if __name__ == "__main__":
    main()
