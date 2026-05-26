#!/usr/bin/env python3
"""
TRI-CITY INTRADAY SCANNER — Catches flat-open breakouts missed by the 7:30 gap scan.

Filters on intraday % change instead of premarket gap %. Merges results with the
existing gap-scan pool, re-ranks the combined list, and saves back to
tri-city-candidates.json. Symbols in open Alpaca positions are "sticky" and
cannot be bumped from the top-15 TV indicator slots.

Runs at 9:30 AM CT (--source intraday_930) and 11:30 AM CT (--source intraday_1130).

Scoring weights:
  Intraday %     35% — size of the intraday move
  Relative Vol   35% — institutional conviction
  Stage 2        12% — price above SMA50 (uptrend confirmed)
  SMA slope       8% — Weinstein: SMA50 > SMA100
  Catalyst       10% — change >= 5% treated as catalyst-driven

Usage:
    python -W ignore scripts/tri_city_intraday_scanner.py
    python -W ignore scripts/tri_city_intraday_scanner.py --source intraday_1130
    python -W ignore scripts/tri_city_intraday_scanner.py --dry-run
    python -W ignore scripts/tri_city_intraday_scanner.py --top 50
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

CT         = ZoneInfo("America/Chicago")
CANDIDATES = WORKSPACE / "shared" / "tri-city-candidates.json"

MIN_CHANGE_PCT = float(os.getenv("MIN_INTRADAY_PCT", os.getenv("MIN_GAP_PCT", "3.0")))
MIN_PRICE      = float(os.getenv("MIN_PRICE",   "2.0"))
MAX_PRICE      = float(os.getenv("MAX_PRICE",   "500.0"))
MIN_RVOL       = float(os.getenv("MIN_RVOL",    "1.5"))
PARABOLIC_VOL  = 10.0

W_GAP       = 0.35
W_RVOL      = 0.35
W_STAGE     = 0.12
W_SMA_SLOPE = 0.08
W_CATALYST  = 0.10

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Fetch from TradingView screener ───────────────────────────────────────────

def fetch_intraday_movers() -> list[dict]:
    """Pull intraday % change movers from TradingView screener."""
    try:
        from tvscreener import StockScreener, FilterOperator
        import pandas as pd

        ss = StockScreener()
        ss.set_markets("america")
        ss.set_range(0, 500)
        ss.add_filter("change",                   FilterOperator.ABOVE_OR_EQUAL, MIN_CHANGE_PCT)
        ss.add_filter("close",                    FilterOperator.ABOVE_OR_EQUAL, MIN_PRICE)
        ss.add_filter("close",                    FilterOperator.BELOW_OR_EQUAL, MAX_PRICE)
        ss.add_filter("relative_volume_10d_calc", FilterOperator.ABOVE_OR_EQUAL, MIN_RVOL)

        df = ss.get()
        if df is None or df.empty:
            return []

        stocks = []
        for _, row in df.iterrows():
            raw_ticker = str(row.get("Symbol", ""))
            if raw_ticker.startswith("OTC:"):
                continue
            ticker = raw_ticker.split(":")[-1] if ":" in raw_ticker else raw_ticker
            if not ticker:
                continue

            def safe(col, default=0.0):
                v = row.get(col)
                try:
                    return float(v) if pd.notna(v) else default
                except (TypeError, ValueError):
                    return default

            price        = safe("Price")
            change_pct   = safe("Change %")
            gap_pct      = safe("Gap %", 0.0)
            volume       = safe("Volume")
            avg_vol      = safe("Average Volume (10 day)", 1.0)
            sma_50       = safe("Simple Moving Average (50)")
            sma_100      = safe("Simple Moving Average (100)")
            ma_rating    = safe("Moving Averages Rating")
            rsi          = safe("Relative Strength Index (14)", 50.0)
            rel_vol      = safe("Relative Volume", volume / avg_vol if avg_vol > 0 else 1.0)
            float_shares = safe("Float Shares Outstanding") * 1e6 if safe("Float Shares Outstanding") else 0.0
            pm_volume    = safe("Pre-market Volume")
            high_52w     = safe("52 Week High")
            perf_3m      = safe("3-Month Performance")
            candle_hammer = safe("Candle.Hammer") != 0.0

            if ma_rating >= 0.5:    tech_rating = "strong_buy"
            elif ma_rating >= 0.1:  tech_rating = "buy"
            elif ma_rating <= -0.5: tech_rating = "strong_sell"
            elif ma_rating <= -0.1: tech_rating = "sell"
            else:                   tech_rating = "neutral"

            if float_shares > 0 and float_shares < 10e6:
                float_cat = "low"
            elif float_shares <= 500e6:
                float_cat = "medium"
            else:
                float_cat = "large"

            if ":" in raw_ticker:
                tv_symbol = raw_ticker
            else:
                _YF_TO_TV = {"NYSE": "NYSE", "NMS": "NASDAQ", "NCM": "NASDAQ",
                             "NGM": "NASDAQ", "PCX": "AMEX", "AMEX": "AMEX"}
                try:
                    import yfinance as yf
                    _exch = yf.Ticker(ticker).fast_info.exchange or "NMS"
                    tv_prefix = _YF_TO_TV.get(_exch, "NASDAQ")
                except Exception:
                    tv_prefix = "NASDAQ"
                tv_symbol = f"{tv_prefix}:{ticker}"

            stocks.append({
                "symbol":          ticker,
                "tv_symbol":       tv_symbol,
                "price":           round(price, 2),
                "gap_pct":         round(gap_pct, 2),
                "change_pct":      round(change_pct, 2),
                "rvol":            round(rel_vol, 2),
                "rsi":             round(rsi, 1),
                "sma50":           round(sma_50, 2),
                "sma100":          round(sma_100, 2),
                "stage2":          price > sma_50 > 0,
                "sma_rising":      sma_50 > sma_100 > 0,
                "near_52wk_high":  high_52w > 0 and price / high_52w > 0.95,
                "htf":             perf_3m >= 90.0,
                "candle_hammer":   candle_hammer,
                "perf_3m":         round(perf_3m, 1),
                "high_52w":        round(high_52w, 2),
                "catalyst":        change_pct >= 5.0,
                "parabolic":       rel_vol >= PARABOLIC_VOL,
                "float_cat":       float_cat,
                "float_shares":    int(float_shares),
                "tech_rating":     tech_rating,
                "volume":          int(volume),
                "avg_volume":      int(avg_vol),
                "pm_volume":       int(pm_volume),
                "source":          "intraday",
            })

        return stocks

    except ImportError:
        print("ERROR: tvscreener not installed. Run: pip install tvscreener")
        return []
    except Exception as e:
        logger.error(f"tvscreener fetch failed: {e}")
        return []


# ── Scoring ───────────────────────────────────────────────────────────────────

def score_candidate(s: dict) -> float:
    # Use change_pct for intraday candidates, fall back to gap_pct for gap-scan entries
    move_pct    = s.get("change_pct", s.get("gap_pct", 0.0))
    gap_score   = min(move_pct / 20.0, 1.0) * W_GAP
    raw_rvol    = min(s["rvol"] / 5.0, 1.0) * W_RVOL
    rvol_score  = raw_rvol * 0.5 if s["parabolic"] else raw_rvol
    stage_score = W_STAGE if s["stage2"] else 0.0
    sma_score   = W_SMA_SLOPE if s.get("sma_rising") else 0.0
    cat_score   = W_CATALYST if s["catalyst"] else 0.0
    return round(gap_score + rvol_score + stage_score + sma_score + cat_score, 4)


# ── Open position stickiness ──────────────────────────────────────────────────

def get_open_position_symbols() -> set[str]:
    """Return symbols currently in open Alpaca positions (sticky — can't be bumped)."""
    try:
        import alpaca_trade_api as tradeapi
        base_url = (
            "https://paper-api.alpaca.markets"
            if os.getenv("ALPACA_PAPER", "true").lower() == "true"
            else "https://api.alpaca.markets"
        )
        api = tradeapi.REST(
            os.getenv("ALPACA_API_KEY", ""),
            os.getenv("ALPACA_SECRET_KEY", ""),
            base_url=base_url,
        )
        return {p.symbol for p in api.list_positions()}
    except Exception:
        return set()


# ── Pool merge ────────────────────────────────────────────────────────────────

def merge_pools(existing: list[dict], new: list[dict]) -> list[dict]:
    """
    Merge new intraday candidates into existing pool.
    If a symbol already exists, keep whichever version has the higher score.
    """
    pool: dict[str, dict] = {s["symbol"]: s for s in existing}
    for s in new:
        sym = s["symbol"]
        if sym not in pool or s["score"] > pool[sym].get("score", 0.0):
            pool[sym] = s
    return list(pool.values())


# ── Top-15 with stickiness ────────────────────────────────────────────────────

def compute_top20(ranked: list[dict], sticky: set[str]) -> list[str]:
    """
    Pick top 20 tv_symbols for the indicator.
    Sticky symbols (open positions) get priority slots; remaining filled by rank.
    Parabolic stocks excluded from all slots.
    """
    non_para = [s for s in ranked if not s["parabolic"]]
    sticky_first = [s for s in non_para if s["symbol"] in sticky]
    rest         = [s for s in non_para if s["symbol"] not in sticky]
    ordered = sticky_first + rest
    return [s.get("tv_symbol", f"NASDAQ:{s['symbol']}") for s in ordered][:20]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City Intraday Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Print results, don't save")
    parser.add_argument("--top", type=int, default=100, help="Max candidates (default: 100)")
    parser.add_argument(
        "--source",
        default="intraday_930",
        choices=["intraday_930", "intraday_1130"],
        help="Scan source tag (default: intraday_930)",
    )
    args = parser.parse_args()

    now   = datetime.now(CT)
    label = "9:30 AM" if args.source == "intraday_930" else "11:30 AM"

    print(f"\n{'='*72}")
    print(f"  TRI-CITY INTRADAY SCANNER ({label}) — {now.strftime('%Y-%m-%d %H:%M CT')}")
    print(f"  Source: TradingView Screener (Intraday % Change Movers)")
    print(f"  Filters: Change >{MIN_CHANGE_PCT}% | RVol >{MIN_RVOL}x | Price ${MIN_PRICE}–${MAX_PRICE}")
    print(f"{'='*72}")

    new_stocks = fetch_intraday_movers()
    if not new_stocks:
        print("\n  No intraday movers returned from TradingView screener.")
        print(f"\n{'='*72}\n")
        return

    # Score new stocks and tag source
    for s in new_stocks:
        s["score"]  = score_candidate(s)
        s["source"] = args.source

    # Load existing pool
    existing: list[dict] = []
    if CANDIDATES.exists():
        try:
            data = json.loads(CANDIDATES.read_text())
            existing = data.get("candidates", [])
            for s in existing:
                s.setdefault("source", "gap_scan")
                if "score" not in s:
                    s["score"] = score_candidate(s)
        except Exception:
            pass

    # Merge and re-rank
    merged = merge_pools(existing, new_stocks)
    ranked = sorted(merged, key=lambda x: x["score"], reverse=True)[: args.top]
    for i, s in enumerate(ranked, 1):
        s["rank"] = i

    # Identify what's new vs already in pool
    existing_syms = {s["symbol"] for s in existing}
    new_syms      = {s["symbol"] for s in new_stocks}
    additions     = [s for s in ranked if s["symbol"] in new_syms - existing_syms]
    upgrades      = [s for s in ranked if s["symbol"] in new_syms & existing_syms]

    parabolic       = [s["symbol"] for s in ranked if s["parabolic"]]
    resistance_warn = [s["symbol"] for s in ranked if s.get("near_52wk_high")]
    htf_list        = [s["symbol"] for s in ranked if s.get("htf")]

    def fmt_vol(v: int) -> str:
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000:     return f"{v/1_000:.0f}K"
        return str(v) if v > 0 else "---"

    # Print new additions table
    header = (f"\n{'RK':<4} {'SYMBOL':<7} {'PRICE':>7} {'CHG%':>7} {'GAP%':>6} "
              f"{'RVOL':>6} {'RSI':>5} {'S2':>3} {'SMA↑':>4} {'52H':>4} {'HTF':>4} "
              f"{'SRC':<14} {'SCORE':>7}")
    divider = "-" * 90

    if additions:
        print(f"\n  NEW ADDITIONS ({len(additions)} stocks not in morning pool):")
        print(header)
        print(divider)
        for s in additions:
            stage = "Y" if s["stage2"] else "-"
            sma_r = "Y" if s.get("sma_rising") else "-"
            h52   = "⚠" if s.get("near_52wk_high") else "-"
            htf_f = "★" if s.get("htf") else "-"
            para  = "⚠" if s["parabolic"] else " "
            chg   = s.get("change_pct", 0.0)
            gap   = s.get("gap_pct", 0.0)
            print(f"{s['rank']:<4} {s['symbol']:<7} ${s['price']:>6.2f} "
                  f"{chg:>+6.1f}% {gap:>+5.1f}% {s['rvol']:>5.1f}x "
                  f"{s['rsi']:>5.1f} {stage:>3} {sma_r:>4} {h52:>4} {htf_f:>4} "
                  f"{s.get('source',''):<14} {para}{s['score']:>6.4f}")
    else:
        print("\n  No new symbols beyond existing morning pool.")

    if upgrades:
        print(f"\n  RE-SCORED ({len(upgrades)} symbols already in pool, score updated if improved):")
        for s in upgrades:
            print(f"    {s['symbol']}: rank #{s['rank']}, score {s['score']:.4f}, "
                  f"change {s.get('change_pct',0.0):+.1f}%, RVol {s['rvol']:.1f}x")

    print(f"\n{'─'*90}")
    print(f"  Combined pool: {len(ranked)} candidates  |  New: {len(additions)}  |  Re-scored: {len(upgrades)}")
    if parabolic:
        print(f"  ⚠  PARABOLIC (>10x RVol — avoid):        {parabolic}")
    if resistance_warn:
        print(f"  ⚠  RESISTANCE (within 5% of 52wk high):  {resistance_warn}")
    if htf_list:
        print(f"  ★  HIGH & TIGHT FLAG (+90% in 3mo):       {htf_list}")
    print(f"{'─'*90}")

    # Compute sticky top 20 (combined pool → main scanner Kbzkkm)
    sticky     = get_open_position_symbols()
    tv_symbols = compute_top20(ranked, sticky)

    # Compute intraday-only top 5 (new movers not in gap-scan pool → intraday watcher Vk6rtV)
    # These are ranked candidates whose source is intraday (not the morning gap scan)
    intraday_only = [
        s for s in ranked
        if s.get("source") == args.source and not s["parabolic"]
    ][:5]
    intraday_symbols = [
        s.get("tv_symbol", f"NASDAQ:{s['symbol']}") for s in intraday_only
    ]

    if sticky:
        print(f"\n  STICKY (open positions, locked in): {sorted(sticky)}")
    print(f"\n  TV WATCHLIST (top 20, sticky-first, parabolic excluded):")
    print(f"  {', '.join(tv_symbols)}")
    print(f"\n  INTRADAY WATCHER (top 5 new movers for Vk6rtV):")
    print(f"  {', '.join(intraday_symbols) if intraday_symbols else '  (no new intraday movers)'}")

    if not args.dry_run:
        CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date":             now.strftime("%Y-%m-%d"),
            "scanned_at":       now.strftime("%H:%M CT"),
            "last_scan":        args.source,
            "total":            len(ranked),
            "tv_symbols":       tv_symbols,
            "intraday_symbols": intraday_symbols,
            "htf":              htf_list,
            "resistance":       resistance_warn,
            "candidates":       ranked,
        }
        CANDIDATES.write_text(json.dumps(payload, indent=2))
        print(f"\n  Saved → {CANDIDATES}")

        # Update compact flags file for signal monitor (htf + resistance only — ~1KB vs 100KB)
        flags_file = CANDIDATES.parent / "tri-city-flags.json"
        flags_file.write_text(json.dumps({
            "date":       now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%H:%M CT"),
            "source":     args.source,
            "htf":        htf_list,
            "resistance": resistance_warn,
        }, indent=2))
        print(f"  Saved → {flags_file}")

    print(f"\n{'='*72}\n")


if __name__ == "__main__":
    main()
