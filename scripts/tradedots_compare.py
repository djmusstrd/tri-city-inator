#!/usr/bin/env python3
"""
TRADEDOTS COMPARISON — Replays the "Price and Volume Breakout Buy Strategy [TradeDots]"
Pine logic on 5-min bars for today's Tri-City trade symbols, and compares its
entry/exit signals against what Tri-City actually did.

TradeDots logic (adapted to 5-min bars):
  price_highest  = highest(high, 60)   [60 bars = ~5 hours]
  price_lowest   = lowest(low, 60)
  volume_highest = highest(volume, 60)
  sma_trend      = sma(close, 200)     [200 bars = ~2.6 trading days]

  Long entry:  close > price_highest[1] and volume > volume_highest[1]
               and close > sma_trend and flat
  Long exit:   5 consecutive closes < sma_trend while long

  Short entry: close < price_lowest[1] and volume > volume_highest[1]
               and close < sma_trend and flat
  Short exit:  5 consecutive closes > sma_trend while short

Usage:
    python -W ignore scripts/tradedots_compare.py
    python -W ignore scripts/tradedots_compare.py --symbols DSY XOVR RGTU
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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

import pandas as pd
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

CT = ZoneInfo("America/Chicago")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")

PRICE_PERIOD = 60
VOLUME_PERIOD = 60
TREND_LENGTH = 200


def load_today_trades() -> list[dict]:
    journal = json.loads((WORKSPACE / "logs" / "tri-city-journal.json").read_text())
    today = pd.Timestamp.now(tz=CT).strftime("%Y-%m-%d")
    return [t for t in journal if t["date"] == today]


def fetch_bars(client: StockHistoricalDataClient, symbol: str) -> pd.DataFrame:
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(5, TimeFrame.Minute.unit if hasattr(TimeFrame.Minute, "unit") else "Min"),
        limit=1000,
        feed="iex",
        adjustment="raw",
        days=6,
    )
    bars = client.get_stock_bars(req).df
    if bars.empty:
        return bars
    bars = bars.reset_index()
    bars["timestamp_ct"] = bars["timestamp"].dt.tz_convert(CT)
    return bars


def compute_signals(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["price_highest"] = df["high"].rolling(PRICE_PERIOD).max()
    df["price_lowest"] = df["low"].rolling(PRICE_PERIOD).min()
    df["volume_highest"] = df["volume"].rolling(VOLUME_PERIOD).max()
    df["sma_trend"] = df["close"].rolling(TREND_LENGTH).mean()

    prev_high = df["price_highest"].shift(1)
    prev_low = df["price_lowest"].shift(1)
    prev_vol_high = df["volume_highest"].shift(1)

    df["long_entry_cond"] = (
        (df["close"] > prev_high)
        & (df["volume"] > prev_vol_high)
        & (df["close"] > df["sma_trend"])
    )
    df["short_entry_cond"] = (
        (df["close"] < prev_low)
        & (df["volume"] > prev_vol_high)
        & (df["close"] < df["sma_trend"])
    )

    below = df["close"] < df["sma_trend"]
    above = df["close"] > df["sma_trend"]
    df["long_exit_cond"] = below.rolling(5).sum() == 5
    df["short_exit_cond"] = above.rolling(5).sum() == 5
    return df


def simulate(df: pd.DataFrame) -> list[dict]:
    """Walk bar-by-bar like strategy.opentrades==0 gating; returns list of
    {side, entry_time, entry_price, exit_time, exit_price} dicts."""
    position = None  # "long" | "short" | None
    entry_idx = None
    trades = []
    for i, row in df.iterrows():
        if pd.isna(row["sma_trend"]):
            continue
        if position is None:
            if row["long_entry_cond"]:
                position = "long"
                entry_idx = i
            elif row["short_entry_cond"]:
                position = "short"
                entry_idx = i
        elif position == "long" and row["long_exit_cond"]:
            trades.append({
                "side": "long",
                "entry_time": df.loc[entry_idx, "timestamp_ct"],
                "entry_price": df.loc[entry_idx, "close"],
                "exit_time": row["timestamp_ct"],
                "exit_price": row["close"],
            })
            position = None
        elif position == "short" and row["short_exit_cond"]:
            trades.append({
                "side": "short",
                "entry_time": df.loc[entry_idx, "timestamp_ct"],
                "entry_price": df.loc[entry_idx, "close"],
                "exit_time": row["timestamp_ct"],
                "exit_price": row["close"],
            })
            position = None
    if position is not None:
        last = df.iloc[-1]
        trades.append({
            "side": position,
            "entry_time": df.loc[entry_idx, "timestamp_ct"],
            "entry_price": df.loc[entry_idx, "close"],
            "exit_time": None,
            "exit_price": last["close"],
            "open": True,
        })
    return trades


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="*", default=None)
    args = parser.parse_args()

    today_trades = load_today_trades()
    if args.symbols:
        symbols = args.symbols
    else:
        symbols = sorted({t["symbol"] for t in today_trades})

    client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    for symbol in symbols:
        print(f"\n{'='*60}\n{symbol}\n{'='*60}")
        tc_trades = [t for t in today_trades if t["symbol"] == symbol]

        df = fetch_bars(client, symbol)
        if df.empty or len(df) < TREND_LENGTH + 1:
            print(f"  Insufficient bars ({len(df)}) for 200-period SMA — skipping")
            continue

        sig = compute_signals(df)
        td_trades = simulate(sig)

        for tc in tc_trades:
            print(f"  TRI-CITY: {tc['setup']:<16} entry {tc['entry_time']} @ {tc['entry_price']}"
                  f"  -> exit {tc['exit_time']} @ {tc.get('exit_price')}"
                  f"  {tc['outcome']} R={tc['r_multiple']}")

        if not td_trades:
            print("  TRADEDOTS: no long/short signal fired (60-bar breakout + vol + 200sma trend not met)")
        else:
            for tt in td_trades:
                exit_str = (
                    f"-> exit {tt['exit_time'].strftime('%H:%M CT')} @ {tt['exit_price']:.4f} (5-bar SMA exit)"
                    if tt.get("exit_time") is not None
                    else f"-> still open @ {tt['exit_price']:.4f} (no 5-bar SMA exit yet)"
                )
                print(f"  TRADEDOTS: {tt['side']:<5} entry {tt['entry_time'].strftime('%H:%M CT')}"
                      f" @ {tt['entry_price']:.4f}  {exit_str}")

        # current state context
        last = sig.iloc[-1]
        print(f"  context: last close={last['close']:.4f}  sma200={last['sma_trend']:.4f}"
              f"  60bar_high={last['price_highest']:.4f}  60bar_low={last['price_lowest']:.4f}")


if __name__ == "__main__":
    main()
