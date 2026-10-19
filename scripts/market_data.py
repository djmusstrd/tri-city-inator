"""Market data: Alpaca SIP (through feed-inator when FEED_DATA_URL is set), shaped like yfinance.

`import market_data as yf` keeps every call site unchanged:
  - yf.download(...) fetches Alpaca bars and returns what yfinance 1.x returns (MultiIndex
    columns, tz-naive daily / America/New_York intraday index), so existing parsing still works.
  - yf.Ticker(...) and anything else falls through to real yfinance (news, calendar, info, float).
  - Symbols Alpaca doesn't carry (^VIX and other indices, futures, crypto pairs), periods or
    intervals it can't express, and any Alpaca failure also fall back to yfinance (logged).
  - StockHistoricalDataClient(...) is alpaca-py's client pointed at feed-inator, and DATA_FEED
    is the feed to request ("sip" on the paid login).
Keep this file identical across the bots that use it."""

from __future__ import annotations

import logging
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger("market_data")
ET = ZoneInfo("America/New_York")
DATA_FEED = os.getenv("ALPACA_DATA_FEED", "sip")
FIELDS = ["Close", "High", "Low", "Open", "Volume"]  # yfinance 1.x column order
_INTERVALS = {"1m": (1, "Minute"), "2m": (2, "Minute"), "5m": (5, "Minute"), "15m": (15, "Minute"),
              "30m": (30, "Minute"), "60m": (1, "Hour"), "1h": (1, "Hour"), "1d": (1, "Day")}


def data_url() -> str | None:
    return os.getenv("FEED_DATA_URL", "").rstrip("/") or None


def StockHistoricalDataClient(api_key=None, secret_key=None, **kw):
    """alpaca-py's data client, through feed-inator when FEED_DATA_URL is set."""
    from alpaca.data.historical import StockHistoricalDataClient as _Client
    kw.setdefault("url_override", data_url())
    return _Client(api_key, secret_key, **kw)


def __getattr__(name):  # yf.Ticker, yf.Tickers, ... → real yfinance
    import yfinance
    return getattr(yfinance, name)


def _yfinance(tickers, **kw):
    import yfinance
    return yfinance.download(tickers, **kw)


def _symbols(tickers) -> list[str]:
    if isinstance(tickers, str):
        tickers = tickers.replace(",", " ").split()
    return [str(t).strip().upper() for t in tickers if str(t).strip()]


def _alpaca_can(sym: str) -> bool:
    return not any(c in sym for c in "^=/:") and not sym.endswith("-USD")


def _alpaca_symbol(sym: str) -> str:
    return sym.replace("-", ".")  # yfinance writes class shares BRK-B; Alpaca writes BRK.B


def download(tickers, period=None, interval="1d", start=None, end=None, prepost=False,
             auto_adjust=True, group_by="column", multi_level_index=True, **kw):
    syms = _symbols(tickers)
    yf_kw = dict(period=period, interval=interval, start=start, end=end, prepost=prepost,
                 auto_adjust=auto_adjust, group_by=group_by, multi_level_index=multi_level_index, **kw)
    if (not syms or interval not in _INTERVALS or not auto_adjust or not all(map(_alpaca_can, syms))
            or (period and not str(period).endswith("d")) or (not period and not start)):
        return _yfinance(tickers, **yf_kw)
    try:
        frames = _alpaca_bars(syms, period, interval, start, end, prepost)
    except Exception as exc:
        log.warning("Alpaca bars failed (%s) — falling back to yfinance for %s", exc, ",".join(syms[:5]))
        return _yfinance(tickers, **yf_kw)
    return _shape(frames, syms, interval, group_by, multi_level_index)


def _alpaca_bars(syms, period, interval, start, end, prepost) -> dict[str, pd.DataFrame]:
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    amount, unit = _INTERVALS[interval]
    sessions = int(str(period)[:-1]) if period else None
    if start is not None:
        start_dt = pd.Timestamp(start)
        start_dt = start_dt.tz_localize(ET) if start_dt.tzinfo is None else start_dt
    else:  # enough calendar days to cover `sessions` trading days, weekends and holidays included
        start_dt = pd.Timestamp(datetime.now(ET) - timedelta(days=int(sessions * 1.5) + 7))
    end_dt = None
    if end is not None:
        end_dt = pd.Timestamp(end)
        end_dt = end_dt.tz_localize(ET) if end_dt.tzinfo is None else end_dt
    client = StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))
    df = client.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=[_alpaca_symbol(s) for s in syms], timeframe=TimeFrame(amount, TimeFrameUnit[unit]),
        start=start_dt.to_pydatetime(), end=end_dt.to_pydatetime() if end_dt is not None else None,
        adjustment="all", feed=DATA_FEED)).df
    out = {}
    if df.empty:
        return out
    daily = interval == "1d"
    for sym in syms:
        try:
            g = df.xs(_alpaca_symbol(sym), level="symbol")
        except KeyError:
            continue
        g = g.rename(columns=str.title)[FIELDS].copy()
        idx = g.index.tz_convert(ET)
        if daily:
            g.index = pd.DatetimeIndex(idx.tz_localize(None).normalize(), name="Date")
        else:  # yfinance 1.x returns intraday stamps in UTC; sessions are still cut on ET clock time
            if not prepost:
                tod = idx.time
                keep = (tod >= time(9, 30)) & (tod < time(16, 0))
                g, idx = g[keep], idx[keep]
            g.index = pd.DatetimeIndex(idx.tz_convert("UTC"), name="Datetime")
        if sessions:  # yfinance's "Nd" = the last N trading sessions
            day = g.index.normalize() if daily else g.index.tz_convert(ET).tz_localize(None).normalize()
            g = g[day.isin(pd.Index(day).unique()[-sessions:])]
        if not g.empty:
            out[sym] = g
    return out


def _shape(frames: dict[str, pd.DataFrame], syms, interval, group_by, multi_level_index) -> pd.DataFrame:
    if not frames:
        return pd.DataFrame()
    name = "Date" if interval == "1d" else "Datetime"
    idx = pd.DatetimeIndex(sorted(set().union(*(f.index for f in frames.values()))), name=name)
    by_ticker = {s: (frames[s].reindex(idx) if s in frames else pd.DataFrame(index=idx, columns=FIELDS, dtype=float))
                 for s in syms}  # yfinance keeps a NaN column block for symbols with no data
    if len(syms) == 1 and not multi_level_index:
        return by_ticker[syms[0]]
    df = pd.concat(by_ticker, axis=1)  # (Ticker, Price)
    df.columns.names = ["Ticker", "Price"]
    if group_by != "ticker":
        df = df.swaplevel(axis=1)[FIELDS]  # (Price, Ticker), fields first like yfinance
        df.columns.names = ["Price", "Ticker"]
    return df
