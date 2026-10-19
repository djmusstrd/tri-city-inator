"""Market data: Alpaca SIP (through feed-inator when FEED_DATA_URL is set), shaped like yfinance.

`import market_data as yf` keeps every call site unchanged:
  - yf.download(...) fetches Alpaca bars and returns what yfinance 1.x returns (MultiIndex columns,
    tz-naive daily / UTC intraday index), so existing parsing still works.
  - yf.Ticker(sym).history(...) and the price fields of .info (currentPrice, previousClose, ...)
    come from Alpaca; every other attribute (news, calendar, floatShares, ...) is real yfinance.
  - Symbols Alpaca doesn't carry (^VIX and other indices, futures, crypto pairs), periods or
    intervals it can't express, and any Alpaca failure fall back to yfinance (logged).
  - StockHistoricalDataClient(...) is alpaca-py's client pointed at feed-inator, and DATA_FEED is
    the feed to request ("sip" on the paid login).
Plain HTTP (requests or urllib), Python 3.9+. Keep this file identical across the bots that use it."""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

log = logging.getLogger("market_data")
ET = ZoneInfo("America/New_York")
ALPACA_DATA = "https://data.alpaca.markets"
DATA_FEED = os.getenv("ALPACA_DATA_FEED", "sip")
FIELDS = ["Close", "High", "Low", "Open", "Volume"]  # yfinance 1.x download() column order
HISTORY_FIELDS = ["Open", "High", "Low", "Close", "Volume"]  # Ticker.history() column order
_TIMEFRAMES = {"1m": "1Min", "2m": "2Min", "5m": "5Min", "15m": "15Min", "30m": "30Min",
               "60m": "1Hour", "1h": "1Hour", "1d": "1Day"}
_PRICE_KEYS = {"currentPrice", "regularMarketPrice", "previousClose", "regularMarketPreviousClose",
               "open", "regularMarketOpen", "dayHigh", "dayLow", "regularMarketDayHigh",
               "regularMarketDayLow", "volume", "regularMarketVolume", "bid", "ask"}


def data_url() -> str | None:
    return os.getenv("FEED_DATA_URL", "").rstrip("/") or None


def StockHistoricalDataClient(api_key=None, secret_key=None, **kw):
    """alpaca-py's data client, through feed-inator when FEED_DATA_URL is set."""
    from alpaca.data.historical import StockHistoricalDataClient as _Client
    kw.setdefault("url_override", data_url())
    return _Client(api_key, secret_key, **kw)


def __getattr__(name):  # yf.Tickers, yf.Search, ... → real yfinance
    import yfinance
    return getattr(yfinance, name)


# ── HTTP ─────────────────────────────────────────────────────────────────────────────────────

def _get(path: str, params: dict) -> dict:
    url = (data_url() or ALPACA_DATA) + path + "?" + urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None})
    headers = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
               "APCA-API-SECRET-KEY": os.getenv("ALPACA_SECRET_KEY", ""), "Accept": "application/json"}
    if os.getenv("FEED_CLIENT_NAME"):
        headers["X-Hub-Client"] = os.environ["FEED_CLIENT_NAME"]
    try:
        import requests
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except ImportError:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as resp:
            return json.loads(resp.read())


def _alpaca_symbol(sym: str) -> str:
    return sym.replace("-", ".")  # yfinance writes class shares BRK-B; Alpaca writes BRK.B


def _alpaca_can(sym: str) -> bool:
    return bool(sym) and not any(c in sym for c in "^=/:") and not sym.endswith("-USD")


def _span(period) -> tuple[int | None, timedelta]:
    """(sessions to keep, calendar lookback) for a yfinance period like 5d, 1mo, 6mo, 1y."""
    p = str(period)
    if p.endswith("mo"):
        return None, timedelta(days=31 * int(p[:-2]))
    if p.endswith("y"):
        return None, timedelta(days=366 * int(p[:-1]))
    if p.endswith("d"):
        n = int(p[:-1])
        return n, timedelta(days=int(n * 1.5) + 7)
    raise ValueError(f"period {period!r}")


def _supported(period, interval, start) -> bool:
    if interval not in _TIMEFRAMES:
        return False
    if start is not None:
        return True
    if not period:
        return False
    try:
        _span(period)
        return True
    except ValueError:
        return False


def _ts(value) -> str | None:
    if value is None:
        return None
    t = pd.Timestamp(value)
    t = t.tz_localize(ET) if t.tzinfo is None else t
    return t.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _frames(syms, period, interval, start, end, prepost, index_tz) -> dict[str, pd.DataFrame]:
    """Alpaca bars per symbol, trimmed like yfinance. index_tz: 'UTC' (download) or ET (history)."""
    sessions, lookback = _span(period) if period else (None, None)
    start = start if start is not None else datetime.now(ET) - lookback
    by_alp = {_alpaca_symbol(s): s for s in syms}
    params = {"symbols": ",".join(by_alp), "timeframe": _TIMEFRAMES[interval], "start": _ts(start),
              "end": _ts(end), "adjustment": "all", "feed": DATA_FEED, "limit": 10000}
    rows: dict[str, list] = {}
    while True:
        page = _get("/v2/stocks/bars", params)
        for sym, bars in (page.get("bars") or {}).items():
            rows.setdefault(sym, []).extend(bars)
        token = page.get("next_page_token")
        if not token:
            break
        params["page_token"] = token
    daily = interval == "1d"
    out = {}
    for alp, bars in rows.items():
        g = pd.DataFrame(bars)
        g = g.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
        idx = pd.DatetimeIndex(pd.to_datetime(g["t"], utc=True)).tz_convert(ET)
        g = g[HISTORY_FIELDS].astype(float)
        g.index = idx
        if not daily and not prepost:
            tod = idx.time
            keep = (tod >= time(9, 30)) & (tod < time(16, 0))
            g = g[keep]
        day = g.index.tz_localize(None).normalize()
        if sessions:  # yfinance's "Nd" = the last N trading sessions
            g = g[day.isin(pd.Index(day).unique()[-sessions:])]
            day = g.index.tz_localize(None).normalize()
        if daily:
            g.index = pd.DatetimeIndex(day, name="Date") if index_tz == "UTC" else \
                pd.DatetimeIndex(day.tz_localize(ET), name="Date")
        else:
            g.index = pd.DatetimeIndex(g.index.tz_convert(index_tz), name="Datetime")
        if not g.empty:
            out[by_alp[alp]] = g
    return out


# ── yf.download ──────────────────────────────────────────────────────────────────────────────

def _symbols(tickers) -> list[str]:
    if isinstance(tickers, str):
        tickers = tickers.replace(",", " ").split()
    return [str(t).strip().upper() for t in tickers if str(t).strip()]


def download(tickers, period=None, interval="1d", start=None, end=None, prepost=False,
             auto_adjust=True, group_by="column", multi_level_index=True, **kw):
    syms = _symbols(tickers)
    yf_kw = dict(period=period, interval=interval, start=start, end=end, prepost=prepost,
                 auto_adjust=auto_adjust, group_by=group_by, multi_level_index=multi_level_index, **kw)
    if not syms or not auto_adjust or not all(map(_alpaca_can, syms)) or not _supported(period, interval, start):
        import yfinance
        return yfinance.download(tickers, **yf_kw)
    try:
        frames = _frames(syms, period, interval, start, end, prepost, "UTC")
    except Exception as exc:
        log.warning("Alpaca bars failed (%s) — falling back to yfinance for %s", exc, ",".join(syms[:5]))
        import yfinance
        return yfinance.download(tickers, **yf_kw)
    if not frames:
        return pd.DataFrame()
    name = "Date" if interval == "1d" else "Datetime"
    idx = pd.DatetimeIndex(sorted(set().union(*(f.index for f in frames.values()))), name=name)
    by_ticker = {s: (frames[s][FIELDS].reindex(idx) if s in frames
                     else pd.DataFrame(index=idx, columns=FIELDS, dtype=float)) for s in syms}
    if len(syms) == 1 and not multi_level_index:
        return by_ticker[syms[0]]
    df = pd.concat(by_ticker, axis=1)  # (Ticker, Price)
    df.columns.names = ["Ticker", "Price"]
    if group_by != "ticker":
        df = df.swaplevel(axis=1)[FIELDS]  # (Price, Ticker), fields first like yfinance
        df.columns.names = ["Price", "Ticker"]
    return df


# ── yf.Ticker ────────────────────────────────────────────────────────────────────────────────

class _Info(dict):
    """yfinance's .info, with the price fields from an Alpaca snapshot; the rest from yfinance on demand."""

    def __init__(self, ticker: "Ticker"):
        super().__init__()
        self._t = ticker
        self._snap = self._rest = False

    def _load_snapshot(self) -> None:
        self._snap = True
        if not _alpaca_can(self._t.ticker):
            return
        try:
            s = _get(f"/v2/stocks/{_alpaca_symbol(self._t.ticker)}/snapshot", {"feed": DATA_FEED})
        except Exception as exc:
            log.warning("Alpaca snapshot failed for %s (%s) — using yfinance", self._t.ticker, exc)
            return
        trade, quote = s.get("latestTrade") or {}, s.get("latestQuote") or {}
        day, prev = s.get("dailyBar") or {}, s.get("prevDailyBar") or {}
        vals = {"currentPrice": trade.get("p"), "regularMarketPrice": trade.get("p"),
                "previousClose": prev.get("c"), "regularMarketPreviousClose": prev.get("c"),
                "open": day.get("o"), "regularMarketOpen": day.get("o"),
                "dayHigh": day.get("h"), "regularMarketDayHigh": day.get("h"),
                "dayLow": day.get("l"), "regularMarketDayLow": day.get("l"),
                "volume": day.get("v"), "regularMarketVolume": day.get("v"),
                "bid": quote.get("bp"), "ask": quote.get("ap")}
        self.update({k: v for k, v in vals.items() if v is not None})

    def _load_rest(self) -> None:
        self._rest = True
        try:
            for k, v in (self._t._real().info or {}).items():
                if k not in self:  # Alpaca prices win
                    dict.__setitem__(self, k, v)
        except Exception as exc:
            log.warning("yfinance info failed for %s (%s)", self._t.ticker, exc)

    def _ensure(self, key=None) -> None:
        if not self._snap and (key is None or key in _PRICE_KEYS):
            self._load_snapshot()
        if not self._rest and (key is None or not dict.__contains__(self, key)):
            self._load_rest()

    def __getitem__(self, key):
        self._ensure(key)
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._ensure(key)
        return dict.get(self, key, default)

    def __contains__(self, key):
        self._ensure(key)
        return dict.__contains__(self, key)

    def keys(self):
        self._ensure()
        return dict.keys(self)

    def items(self):
        self._ensure()
        return dict.items(self)

    def values(self):
        self._ensure()
        return dict.values(self)

    def __iter__(self):
        self._ensure()
        return dict.__iter__(self)

    def __len__(self):
        self._ensure()
        return dict.__len__(self)


class Ticker:
    def __init__(self, ticker, session=None):
        self.ticker = str(ticker).strip().upper()
        self._yf = None
        self._info = None

    def _real(self):
        if self._yf is None:
            import yfinance
            self._yf = yfinance.Ticker(self.ticker)
        return self._yf

    def history(self, period="1mo", interval="1d", start=None, end=None, prepost=False,
                actions=True, auto_adjust=True, **kw):
        if _alpaca_can(self.ticker) and auto_adjust and _supported(period if start is None else None, interval, start):
            try:
                frames = _frames([self.ticker], period if start is None else None, interval, start, end,
                                 prepost, ET)
                df = frames.get(self.ticker, pd.DataFrame(columns=HISTORY_FIELDS))
                if actions:
                    df = df.assign(Dividends=0.0, **{"Stock Splits": 0.0})
                return df
            except Exception as exc:
                log.warning("Alpaca history failed for %s (%s) — falling back to yfinance", self.ticker, exc)
        return self._real().history(period=period, interval=interval, start=start, end=end,
                                    prepost=prepost, actions=actions, auto_adjust=auto_adjust, **kw)

    @property
    def info(self):
        if self._info is None:
            self._info = _Info(self)
        return self._info

    def __getattr__(self, name):  # news, calendar, fast_info, ... → real yfinance
        return getattr(self._real(), name)

    def __repr__(self):
        return f"market_data.Ticker object <{self.ticker}>"
