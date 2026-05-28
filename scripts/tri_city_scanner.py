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

MIN_GAP_PCT   = float(os.getenv("MIN_GAP_PCT",  "1.5"))   # matches .env default
MIN_PRICE     = float(os.getenv("MIN_PRICE",    "2.0"))
MAX_PRICE     = float(os.getenv("MAX_PRICE",    "500.0"))
MIN_RVOL      = float(os.getenv("MIN_RVOL",     "1.5"))
MIN_AVG_VOL   = float(os.getenv("MIN_AVG_VOL",  "500000"))
PARABOLIC_VOL = 10.0

# Scoring weights — sum to 1.0 (positive) minus W_52WK_PENALTY when applicable
W_GAP          = 0.30  # gap % — size of overnight move
W_RVOL         = 0.30  # relative volume — institutional conviction
W_STAGE        = 0.10  # Weinstein Stage 2: price above SMA50
W_SMA_SLOPE    = 0.07  # Weinstein: SMA50 above SMA100 (rising trend)
W_CATALYST     = 0.08  # gap ≥5% treated as catalyst-driven
W_FLOAT        = 0.06  # low float amplifies gap moves
W_RSI          = 0.05  # RSI in optimal entry range (50–70 full, 40–80 half)
W_PARK_VOL     = 0.02  # Parkinson vol trending (Ch 05 — Algo Trading Cookbook)
W_BB_SQUEEZE   = 0.02  # BB squeeze bonus (Ch 06 — Investing for Programmers)
# Positive weights sum: 0.30+0.30+0.10+0.07+0.08+0.06+0.05+0.02+0.02 = 1.00
W_52WK_PENALTY = 0.08  # subtracted when price within 5% of 52-week high (resistance)

PARK_VOL_WINDOW  = int(os.getenv("PARK_VOL_WINDOW",  "14"))   # rolling window for Parkinson vol
PARK_VOL_LOOKBACK = int(os.getenv("PARK_VOL_LOOKBACK", "60")) # days of history for baseline
BB_SQUEEZE_PERIOD   = int(os.getenv("BB_SQUEEZE_PERIOD",   "20"))   # BB period (standard)
BB_SQUEEZE_STD      = float(os.getenv("BB_SQUEEZE_STD",     "2.0"))  # BB std multiplier
BB_SQUEEZE_PCTILE   = float(os.getenv("BB_SQUEEZE_PCTILE",  "20.0")) # bottom N% = squeeze

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


# ── Bollinger Band Squeeze (Ch 06 — Investing for Programmers / Papp) ────────
# A BB squeeze means current band width is compressed relative to its recent
# intraday history — coiled energy that often precedes a strong directional move.
# Per Papp: MUST use 5-minute bars, not daily closes — daily timeframe destroys
# the signal for intraday ORB entries (timeframe mismatch).
# We compute BB(20, 2) width = (upper - lower) / middle on 5-min closes.
# yfinance gives up to 60 days of 5-min history — sufficient baseline.
# If the most recent bar's width is in the bottom BB_SQUEEZE_PCTILE% of
# the 60-day distribution, the stock is intraday-squeezed → score bonus.

def compute_bb_squeeze(symbols: list[str]) -> dict[str, bool]:
    """
    Fetches 60 days of 5-min bars per symbol in parallel (ThreadPoolExecutor).
    Returns {symbol: True} if the most recent BB(20,2) width is in the bottom
    BB_SQUEEZE_PCTILE% of the 60-day intraday width distribution.
    Parallelized: ~8x faster than serial fetch (30–60s vs 5–10 min for 100 symbols).
    """
    if not symbols:
        return {}
    try:
        import math
        import random
        import time
        import yfinance as yf
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _fetch_one(sym: str) -> tuple:
            try:
                time.sleep(random.uniform(0.05, 0.25))   # jitter to avoid Yahoo rate-limit
                df = yf.download(sym, period="60d", interval="5m",
                                 progress=False, auto_adjust=True)
                if df.empty:
                    return sym, False
                if hasattr(df.columns, "levels"):
                    df.columns = df.columns.get_level_values(0)
                df.columns = [c.lower() for c in df.columns]
                c = df["close"].dropna()
                if len(c) < BB_SQUEEZE_PERIOD + 10:
                    return sym, False
                vals = list(c)
                widths = []
                for i in range(BB_SQUEEZE_PERIOD, len(vals) + 1):
                    window = vals[i - BB_SQUEEZE_PERIOD:i]
                    mid = sum(window) / BB_SQUEEZE_PERIOD
                    std = math.sqrt(sum((v - mid) ** 2 for v in window) / BB_SQUEEZE_PERIOD)
                    upper = mid + BB_SQUEEZE_STD * std
                    lower = mid - BB_SQUEEZE_STD * std
                    width = (upper - lower) / mid if mid > 0 else 0.0
                    widths.append(width)
                if len(widths) < 10:
                    return sym, False
                current_width = widths[-1]
                threshold = sorted(widths)[int(len(widths) * BB_SQUEEZE_PCTILE / 100)]
                return sym, current_width <= threshold
            except Exception:
                return sym, False

        result = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_fetch_one, sym): sym for sym in symbols}
            for future in as_completed(futures):
                sym, squeezed = future.result()
                result[sym] = squeezed
        return result
    except Exception as e:
        logger.debug(f"compute_bb_squeeze: {e}")
        return {}


def compute_earnings_flags(symbols: list[str]) -> dict[str, bool]:
    """
    Returns {symbol: True} if earnings are within the next 5 trading days.
    Fetches yfinance calendar per-symbol in parallel.
    Earnings gaps behave differently (wider range, news-driven) — flagged for awareness,
    not filtered out, since some earnings gaps are the best ORB setups.
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf
        import random
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        today = datetime.now(CT).date()
        lookahead_days = 7   # calendar days (covers ~5 trading days)

        def _check_earnings(sym: str) -> tuple:
            try:
                time.sleep(random.uniform(0.05, 0.20))
                cal = yf.Ticker(sym).calendar
                if cal is None or cal.empty:
                    return sym, False
                # calendar index contains event names; "Earnings Date" is a row
                if "Earnings Date" in cal.index:
                    ed = cal.loc["Earnings Date"]
                    # May be a Series with multiple dates or a single value
                    dates = ed.values if hasattr(ed, "values") else [ed]
                    for d in dates:
                        try:
                            import pandas as pd
                            ed_date = pd.Timestamp(d).date()
                            if today <= ed_date <= today + timedelta(days=lookahead_days):
                                return sym, True
                        except Exception:
                            continue
                return sym, False
            except Exception:
                return sym, False

        result = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_check_earnings, sym): sym for sym in symbols}
            for future in as_completed(futures):
                sym, has_earnings = future.result()
                result[sym] = has_earnings
        return result
    except Exception as e:
        logger.debug(f"compute_earnings_flags: {e}")
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

def score_candidate(s: dict) -> dict:
    """Returns a dict with 'score' (composite) and all sc_* component values."""
    gap_score   = min(s["gap_pct"] / 20.0, 1.0) * W_GAP
    raw_rvol    = min(s["rvol"] / 5.0, 1.0) * W_RVOL
    rvol_score  = raw_rvol * 0.5 if s["parabolic"] else raw_rvol
    stage_score = W_STAGE     if s["stage2"]            else 0.0
    sma_score   = W_SMA_SLOPE if s.get("sma_rising")    else 0.0
    cat_score   = W_CATALYST  if s["catalyst"]          else 0.0
    park_score  = W_PARK_VOL  if s.get("park_trending") else 0.0
    bb_score    = W_BB_SQUEEZE if s.get("bb_squeeze")   else 0.0

    # Float score: low float (<10M shares) amplifies gap moves
    float_score = (W_FLOAT if s.get("float_cat") == "low"
                   else W_FLOAT * 0.5 if s.get("float_cat") == "medium"
                   else 0.0)

    # RSI score: 50–70 = full credit (ideal ORB entry zone), 40–80 = half, outside = 0
    rsi = s.get("rsi", 50.0)
    rsi_score = (W_RSI if 50 <= rsi <= 70
                 else W_RSI * 0.5 if 40 <= rsi <= 80
                 else 0.0)

    # 52-week high penalty: within 5% of high = resistance zone, reduces score
    penalty = W_52WK_PENALTY if s.get("near_52wk_high") else 0.0

    total = round(
        gap_score + rvol_score + stage_score + sma_score + cat_score +
        park_score + bb_score + float_score + rsi_score - penalty,
        4
    )
    return {
        "score":        total,
        "sc_gap":       round(gap_score,   4),
        "sc_rvol":      round(rvol_score,  4),
        "sc_stage":     round(stage_score, 4),
        "sc_sma":       round(sma_score,   4),
        "sc_catalyst":  round(cat_score,   4),
        "sc_park":      round(park_score,  4),
        "sc_bb":        round(bb_score,    4),
        "sc_float":     round(float_score, 4),
        "sc_rsi":       round(rsi_score,   4),
        "sc_penalty":   round(-penalty,    4),
    }


def fetch_news(symbols: list[str]) -> dict[str, dict]:
    """
    Fetches the most recent news headline + URL per symbol via yfinance (parallel).
    Returns {symbol: {"headline": str, "url": str, "publisher": str}}.
    Used by the dashboard Candidates page to surface the catalyst behind each gap.
    """
    if not symbols:
        return {}
    try:
        import yfinance as yf
        import random
        import time
        from concurrent.futures import ThreadPoolExecutor, as_completed

        def _get_news(sym: str) -> tuple:
            try:
                time.sleep(random.uniform(0.05, 0.15))
                news = yf.Ticker(sym).news
                if news:
                    item = news[0]
                    # yfinance v0.2.38+ nests data under item["content"]
                    content = item.get("content", {})
                    if content:
                        headline  = content.get("title", "")
                        url       = (content.get("clickThroughUrl") or {}).get("url", "") \
                                    or (content.get("canonicalUrl") or {}).get("url", "")
                        publisher = (content.get("provider") or {}).get("displayName", "")
                    else:
                        headline  = item.get("title", "")
                        url       = item.get("link", "")
                        publisher = item.get("publisher", "")
                    if headline:
                        return sym, {"headline": headline, "url": url, "publisher": publisher}
                return sym, {}
            except Exception:
                return sym, {}

        result = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {pool.submit(_get_news, sym): sym for sym in symbols}
            for future in as_completed(futures):
                sym, news_data = future.result()
                result[sym] = news_data
        return result
    except Exception as e:
        logger.debug(f"fetch_news: {e}")
        return {}


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
    syms         = [s["symbol"] for s in stocks]
    park_results = compute_parkinson_trending(syms)
    for s in stocks:
        s["park_trending"] = park_results.get(s["symbol"], False)
    park_trending_count = sum(1 for s in stocks if s["park_trending"])
    print(f"  Parkinson: {park_trending_count}/{len(stocks)} in trending vol regime")

    # Bollinger Band squeeze check (5-min bars, 60-day intraday — now parallelized)
    print(f"  Computing Bollinger Band squeeze for {len(stocks)} candidates (parallel)...")
    bb_results = compute_bb_squeeze(syms)
    for s in stocks:
        s["bb_squeeze"] = bb_results.get(s["symbol"], False)
    bb_squeeze_count = sum(1 for s in stocks if s["bb_squeeze"])
    print(f"  BB Squeeze: {bb_squeeze_count}/{len(stocks)} in compressed band (coiled)")

    # Earnings check — flag symbols with earnings within next 5 trading days
    print(f"  Checking earnings dates for {len(stocks)} candidates (parallel)...")
    earnings_results = compute_earnings_flags(syms)
    for s in stocks:
        s["earnings_soon"] = earnings_results.get(s["symbol"], False)
    earnings_count = sum(1 for s in stocks if s["earnings_soon"])
    print(f"  Earnings: {earnings_count}/{len(stocks)} reporting within 5 trading days")

    # News headlines for dashboard catalyst links
    print(f"  Fetching news headlines for {len(stocks)} candidates (parallel)...")
    news_results = fetch_news(syms)
    for s in stocks:
        nd = news_results.get(s["symbol"], {})
        s["news_headline"] = nd.get("headline", "")
        s["news_url"]      = nd.get("url", "")
        s["news_publisher"] = nd.get("publisher", "")
    news_found = sum(1 for s in stocks if s.get("news_url"))
    print(f"  News: {news_found}/{len(stocks)} headlines found")

    # Score and rank — returns dict with score + sc_* components
    for s in stocks:
        s.update(score_candidate(s))
    ranked = sorted(stocks, key=lambda x: x["score"], reverse=True)[:args.top]
    for i, s in enumerate(ranked, 1):
        s["rank"] = i

    parabolic       = [s["symbol"] for s in ranked if s["parabolic"]]
    resistance_warn = [s["symbol"] for s in ranked if s.get("near_52wk_high")]
    htf_list        = [s["symbol"] for s in ranked if s.get("htf")]
    earnings_list   = [s["symbol"] for s in ranked if s.get("earnings_soon")]

    def fmt_vol(v: int) -> str:
        if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
        if v >= 1_000:     return f"{v/1_000:.0f}K"
        return str(v) if v > 0 else "---"

    # Print ranked table
    print(f"\n{'RK':<4} {'SYMBOL':<7} {'PRICE':>7} {'GAP%':>7} {'RVOL':>6} "
          f"{'RSI':>5} {'PM VOL':>8} {'S2':>3} {'SMA↑':>4} {'52H':>4} {'HTF':>4} {'PK':>3} {'BB':>3} {'E':>2} {'SCORE':>7}")
    print("-" * 101)

    for s in ranked:
        stage  = "Y" if s["stage2"] else "-"
        sma_r  = "Y" if s.get("sma_rising") else "-"
        h52    = "⚠" if s.get("near_52wk_high") else "-"
        htf_f  = "★" if s.get("htf") else "-"
        pk     = "T" if s.get("park_trending") else "-"
        bb     = "S" if s.get("bb_squeeze")    else "-"
        earn   = "E" if s.get("earnings_soon") else "-"   # earnings within 5 days
        para   = "⚠" if s["parabolic"] else " "
        pmv    = fmt_vol(s["pm_volume"])
        print(f"{s['rank']:<4} {s['symbol']:<7} ${s['price']:>6.2f} "
              f"{s['gap_pct']:>+6.1f}% {s['rvol']:>5.1f}x "
              f"{s['rsi']:>5.1f} {pmv:>8} {stage:>3} {sma_r:>4} {h52:>4} {htf_f:>4} {pk:>3} {bb:>3} {earn:>2} "
              f"{para}{s['score']:>6.4f}")

    park_trending_list = [s["symbol"] for s in ranked if s.get("park_trending")]
    bb_squeeze_list    = [s["symbol"] for s in ranked if s.get("bb_squeeze")]
    print(f"\n{'─'*101}")
    print(f"  {len(ranked)} candidates ranked.")
    if parabolic:
        print(f"  ⚠  PARABOLIC (>10x RVol — avoid):              {parabolic}")
    if resistance_warn:
        print(f"  ⚠  RESISTANCE (within 5% of 52wk high — penalized in score): {resistance_warn}")
    if earnings_list:
        print(f"  E  EARNINGS SOON (within 5 trading days — wider range expected): {earnings_list}")
    if htf_list:
        print(f"  ★  HIGH & TIGHT FLAG (+90% in 3mo):             {htf_list}")
    if park_trending_list:
        print(f"  T  PARKINSON TRENDING (vol regime, ORB-ready):  {park_trending_list}")
    if bb_squeeze_list:
        print(f"  S  BB SQUEEZE (compressed band, coiled):         {bb_squeeze_list}")
    print(f"{'─'*101}")

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
            "bb_squeeze": bb_squeeze_list,
            "candidates": ranked,
        }
        CANDIDATES.write_text(json.dumps(payload, indent=2))
        print(f"\n  Saved → {CANDIDATES}")

        # Write compact flags file for signal monitor (~1KB vs 100KB full candidates)
        flags_file = CANDIDATES.parent / "tri-city-flags.json"
        flags_file.write_text(json.dumps({
            "date":         now.strftime("%Y-%m-%d"),
            "updated_at":   now.strftime("%H:%M CT"),
            "source":       "gap_scan",
            "htf":          htf_list,
            "resistance":   resistance_warn,
            "bb_squeeze":   bb_squeeze_list,
            "earnings_soon": earnings_list,
        }, indent=2))
        print(f"  Saved → {flags_file}")

    print(f"\n{'='*72}\n")


if __name__ == "__main__":
    main()
