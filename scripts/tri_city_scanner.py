#!/usr/bin/env python3
"""
TRI-CITY PREMARKET SCANNER — Morning candidate list for Tri-City Inator setups.

Pulls fresh gap-up movers from TradingView's screener (same source as the
TradingView Hotlists: Volume Gainers, Gap Gainers, Percent Change Gainers).
No static watchlist — every morning is a clean, ranked list of today's movers.

Scoring weights:
  Gap %          35% — size of the overnight move
  Relative Vol   35% — institutional conviction
  Stage 2        20% — price above SMA50 (uptrend confirmed)
  Catalyst       10% — gap >= 5% treated as catalyst-driven

Saves top 100 candidates to shared/tri-city-candidates.json.
The monitor reads this file each scan cycle — no manual watchlist needed.

Usage:
    python -W ignore scripts/tri_city_scanner.py
    python -W ignore scripts/tri_city_scanner.py --top 50
    python -W ignore scripts/tri_city_scanner.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
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

CT         = ZoneInfo("America/Chicago")
CANDIDATES = WORKSPACE / "shared" / "tri-city-candidates.json"

MIN_GAP_PCT   = float(os.getenv("MIN_GAP_PCT",  "3.0"))
MIN_PRICE     = float(os.getenv("MIN_PRICE",    "2.0"))
MAX_PRICE     = float(os.getenv("MAX_PRICE",    "500.0"))
MIN_RVOL      = float(os.getenv("MIN_RVOL",     "1.5"))
MIN_AVG_VOL   = float(os.getenv("MIN_AVG_VOL",  "500000"))
PARABOLIC_VOL = 10.0

W_GAP       = 0.35
W_RVOL      = 0.35
W_STAGE     = 0.12   # Weinstein: split with SMA slope
W_SMA_SLOPE = 0.08   # Weinstein: bonus when SMA50 > SMA100 (rising)
W_CATALYST  = 0.10
W_PARK_VOL  = 0.05   # Parkinson vol trending bonus (Ch 05 — Algo Trading Cookbook)

PARK_VOL_WINDOW  = int(os.getenv("PARK_VOL_WINDOW",  "14"))   # rolling window for Parkinson vol
PARK_VOL_LOOKBACK = int(os.getenv("PARK_VOL_LOOKBACK", "60")) # days of history for baseline

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Parkinson Volatility (Ch 05 — Python for Algorithmic Trading Cookbook) ────
# Uses high/low range rather than close-to-close — more efficient estimator for
# gap-up stocks where intraday range carries more information than the close.
# A stock with Parkinson vol ABOVE its rolling average is in a trending regime
# (good for ORB); below average suggests mean-reversion (avoid for breakouts).

def compute_parkinson_trending(symbols: list[str]) -> dict[str, bool]:
    """
    Batch-fetches 60-day daily H/L for all symbols.
    Returns {symbol: True} if current Parkinson vol > rolling avg (trending),
    False if below average (mean-reverting), omitted if data unavailable.
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf
        import math
        df = yf.download(
            symbols, period=f"{PARK_VOL_LOOKBACK * 2}d", interval="1d",
            progress=False, auto_adjust=True
        )
        if df.empty:
            return {}

        # Extract High and Low — handle single vs multi-symbol column structure
        if hasattr(df.columns, "levels"):
            highs = df["High"]
            lows  = df["Low"]
        else:
            # Single symbol — wrap in dict-like structure
            highs = df[["High"]].rename(columns={"High": symbols[0]})
            lows  = df[["Low"]].rename(columns={"Low":  symbols[0]})

        result = {}
        for sym in symbols:
            try:
                h = highs[sym].dropna()
                l = lows[sym].dropna()
                if len(h) < PARK_VOL_WINDOW + 5:
                    continue
                # Parkinson estimator per bar: (ln(H/L))^2 / (4 * ln(2))
                hl_sq  = [(math.log(float(hi) / float(lo)) ** 2) / (4 * math.log(2))
                          for hi, lo in zip(h, l) if float(lo) > 0]
                if len(hl_sq) < PARK_VOL_WINDOW + 2:
                    continue
                # Rolling window vol values
                park_series = [
                    math.sqrt(sum(hl_sq[i - PARK_VOL_WINDOW:i]) / PARK_VOL_WINDOW)
                    for i in range(PARK_VOL_WINDOW, len(hl_sq) + 1)
                ]
                if len(park_series) < 2:
                    continue
                current_vol = park_series[-1]
                avg_vol     = sum(park_series[:-1]) / len(park_series[:-1])
                result[sym] = current_vol > avg_vol  # True = trending regime
            except Exception:
                continue
        return result
    except Exception as e:
        logger.debug(f"compute_parkinson_trending: {e}")
        return {}


# ── Fetch from TradingView screener ───────────────────────────────────────────

def fetch_gappers() -> list[dict]:
    """
    Pull premarket gap-up candidates from TradingView screener.
    Same data source as TradingView Hotlists (Gap Gainers, Volume Gainers, etc.)
    """
    try:
        from tvscreener import StockScreener, FilterOperator
        import pandas as pd

        ss = StockScreener()
        ss.set_markets("america")
        ss.set_range(0, 500)
        ss.add_filter("gap",                      FilterOperator.ABOVE_OR_EQUAL, MIN_GAP_PCT)
        ss.add_filter("close",                    FilterOperator.ABOVE_OR_EQUAL, MIN_PRICE)
        ss.add_filter("close",                    FilterOperator.BELOW_OR_EQUAL, MAX_PRICE)
        ss.add_filter("relative_volume_10d_calc", FilterOperator.ABOVE_OR_EQUAL, MIN_RVOL)
        ss.add_filter("average_volume_10d_calc",  FilterOperator.ABOVE_OR_EQUAL, MIN_AVG_VOL)

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
            gap_pct      = safe("Gap %")
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
            perf_3m      = safe("3-Month Performance")      # % gain over 3 months (Bulkowski HTF: ≥90%)
            candle_hammer = safe("Candle.Hammer") != 0.0    # TradingView daily candle detection

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
                # yfinance exchange → TradingView prefix (PCX = NYSE Arca = AMEX in TV)
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
                "rvol":            round(rel_vol, 2),
                "rsi":             round(rsi, 1),
                "sma50":           round(sma_50, 2),
                "sma100":          round(sma_100, 2),
                "stage2":          price > sma_50 > 0,
                "sma_rising":      sma_50 > sma_100 > 0,     # Weinstein: SMA50 above SMA100 = uptrend
                "near_52wk_high":  high_52w > 0 and price / high_52w > 0.95,  # Weinstein: resistance zone
                "htf":             perf_3m >= 90.0,           # Bulkowski HTF: +90% in ≤3 months
                "candle_hammer":   candle_hammer,             # TradingView daily hammer detection
                "perf_3m":         round(perf_3m, 1),
                "high_52w":        round(high_52w, 2),
                "catalyst":        gap_pct >= 5.0,
                "parabolic":       rel_vol >= PARABOLIC_VOL,
                "float_cat":       float_cat,
                "float_shares":    int(float_shares),
                "tech_rating":     tech_rating,
                "volume":          int(volume),
                "avg_volume":      int(avg_vol),
                "pm_volume":       int(pm_volume),
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
    gap_score   = min(s["gap_pct"] / 20.0, 1.0) * W_GAP
    raw_rvol    = min(s["rvol"] / 5.0, 1.0) * W_RVOL
    rvol_score  = raw_rvol * 0.5 if s["parabolic"] else raw_rvol
    stage_score = W_STAGE if s["stage2"] else 0.0
    sma_score   = W_SMA_SLOPE if s.get("sma_rising") else 0.0   # Weinstein
    cat_score   = W_CATALYST if s["catalyst"] else 0.0
    park_score  = W_PARK_VOL if s.get("park_trending") else 0.0  # Parkinson vol trending
    return round(gap_score + rvol_score + stage_score + sma_score + cat_score + park_score, 4)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City Premarket Scanner")
    parser.add_argument("--dry-run", action="store_true", help="Print results, don't save")
    parser.add_argument("--top", type=int, default=100, help="Max candidates to show (default: 100)")
    args = parser.parse_args()

    now = datetime.now(CT)
    print(f"\n{'='*72}")
    print(f"  TRI-CITY PREMARKET SCANNER — {now.strftime('%Y-%m-%d %H:%M CT')}")
    print(f"  Source: TradingView Screener (Gap Gainers / Volume Gainers / % Gainers)")
    print(f"  Filters: Gap >{MIN_GAP_PCT}% | RVol >{MIN_RVOL}x | AvgVol >{MIN_AVG_VOL:,.0f} | Price ${MIN_PRICE}–${MAX_PRICE}")
    print(f"{'='*72}")

    stocks = fetch_gappers()
    if not stocks:
        print("\n  No data returned from TradingView screener.")
        print("  This is normal before 4:00 AM CT — try again closer to market open.")
        print(f"\n{'='*72}\n")
        return

    # Parkinson volatility regime check (batch yfinance — top 100 symbols only)
    # Identifies trending vs mean-reverting stocks before scoring; boosts trending ones.
    print(f"  Computing Parkinson volatility regime for {len(stocks)} candidates...")
    park_syms    = [s["symbol"] for s in stocks]
    park_results = compute_parkinson_trending(park_syms)
    for s in stocks:
        s["park_trending"] = park_results.get(s["symbol"], False)
    park_trending_count = sum(1 for s in stocks if s["park_trending"])
    print(f"  Parkinson: {park_trending_count}/{len(stocks)} in trending vol regime")

    # Score and rank
    for s in stocks:
        s["score"] = score_candidate(s)
    ranked = sorted(stocks, key=lambda x: x["score"], reverse=True)[:args.top]
    for i, s in enumerate(ranked, 1):
        s["rank"] = i

    parabolic       = [s["symbol"] for s in ranked if s["parabolic"]]
    resistance_warn = [s["symbol"] for s in ranked if s.get("near_52wk_high")]
    htf_list        = [s["symbol"] for s in ranked if s.get("htf")]

    def fmt_vol(v: int) -> str:
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000:     return f"{v/1_000:.0f}K"
        return str(v) if v > 0 else "---"

    # Print ranked table
    print(f"\n{'RK':<4} {'SYMBOL':<7} {'PRICE':>7} {'GAP%':>7} {'RVOL':>6} "
          f"{'RSI':>5} {'PM VOL':>8} {'S2':>3} {'SMA↑':>4} {'52H':>4} {'HTF':>4} {'PK':>3} {'SCORE':>7}")
    print("-" * 91)

    for s in ranked:
        stage  = "Y" if s["stage2"] else "-"
        sma_r  = "Y" if s.get("sma_rising") else "-"
        h52    = "⚠" if s.get("near_52wk_high") else "-"
        htf_f  = "★" if s.get("htf") else "-"
        pk     = "T" if s.get("park_trending") else "-"   # Parkinson trending
        para   = "⚠" if s["parabolic"] else " "
        pmv    = fmt_vol(s["pm_volume"])
        print(f"{s['rank']:<4} {s['symbol']:<7} ${s['price']:>6.2f} "
              f"{s['gap_pct']:>+6.1f}% {s['rvol']:>5.1f}x "
              f"{s['rsi']:>5.1f} {pmv:>8} {stage:>3} {sma_r:>4} {h52:>4} {htf_f:>4} {pk:>3} "
              f"{para}{s['score']:>6.4f}")

    park_trending_list = [s["symbol"] for s in ranked if s.get("park_trending")]
    print(f"\n{'─'*91}")
    print(f"  {len(ranked)} candidates ranked.")
    if parabolic:
        print(f"  ⚠  PARABOLIC (>10x RVol — avoid):           {parabolic}")
    if resistance_warn:
        print(f"  ⚠  RESISTANCE (within 5% of 52wk high):     {resistance_warn}")
    if htf_list:
        print(f"  ★  HIGH & TIGHT FLAG (+90% in 3mo):          {htf_list}")
    if park_trending_list:
        print(f"  T  PARKINSON TRENDING (vol regime, ORB-ready): {park_trending_list}")
    print(f"{'─'*86}")

    # Top 15 non-parabolic exchange-prefixed symbols for TradingView indicator swap
    tv_symbols = [
        s.get("tv_symbol", f"NASDAQ:{s['symbol']}")
        for s in ranked if not s["parabolic"]
    ][:20]

    print(f"\n  TV WATCHLIST (top 20, parabolic excluded, exchange-prefixed):")
    print(f"  {', '.join(tv_symbols)}")

    if not args.dry_run:
        CANDIDATES.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "date":       now.strftime("%Y-%m-%d"),
            "scanned_at": now.strftime("%H:%M CT"),
            "total":      len(ranked),
            "tv_symbols": tv_symbols,
            "htf":        htf_list,
            "resistance": resistance_warn,
            "candidates": ranked,
        }
        CANDIDATES.write_text(json.dumps(payload, indent=2))
        print(f"\n  Saved → {CANDIDATES}")

        # Write compact flags file for signal monitor (htf + resistance only — ~1KB vs 100KB)
        flags_file = CANDIDATES.parent / "tri-city-flags.json"
        flags_file.write_text(json.dumps({
            "date":       now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%H:%M CT"),
            "source":     "gap_scan",
            "htf":        htf_list,
            "resistance": resistance_warn,
        }, indent=2))
        print(f"  Saved → {flags_file}")

    print(f"\n{'='*72}\n")


if __name__ == "__main__":
    main()
