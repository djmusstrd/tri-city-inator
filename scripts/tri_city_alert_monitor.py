#!/usr/bin/env python3
"""
TRI-CITY ALERT MONITOR — Indicator-agnostic, alert-driven trade monitor.

Problem solved: the TV poller reads 20 fixed Pine scanner slots every 3 minutes.
Breakouts between 3-min windows are missed; symbols outside the 20 slots are
completely invisible. This monitor catches any alert on any symbol on the watchlist
the moment TradingView fires it.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ALERT CONTRACT — paste this as the message when creating a new alert in TV:

  {"tc_v":"1","ticker":"{{ticker}}","signal":"BREAKOUT","price":{{close}},"time":"{{time}}"}

  Change "signal" to: BREAKOUT | CONTINUATION | PULLBACK | EMA20_PULLBACK | FADE
  The tc_v key tells the monitor to trust the signal field exactly.

For third-party indicators (LuxAlgo, etc.) where the message cannot be edited,
the monitor automatically falls back to scanning all watchlist symbols against
their locked ORH levels to identify which symbol triggered the alert.

Changing indicators: recreate the alert with the same contract JSON pasted in.
No code changes needed.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When a new alert fires:
  1. Parse signal type from message (contract JSON > structured JSON > keyword > default)
  2. Resolve symbol: per-symbol alerts use alert.symbol directly;
     watchlist alerts scan all watchlist symbols via Alpaca to find which crossed ORH
  3. Run entry guards (mirrors tri_city_execute.py)
  4. If valid → call tri_city_execute.py --quiet
  5. Send macOS + Telegram notification (executed or skipped w/ reason)

Architecture mirrors tri_city_tv_poller.py exactly:
  - CDP connection to TradingView Desktop on port 9222
  - evaluateAsync JS that calls pricealerts.tradingview.com/list_alerts
  - PID file at shared/tri-city-alert-monitor.pid
  - Log at logs/tri-city-alert-monitor.log
  - Auto-stops at 3:05 PM CT

State file: shared/tri-city-alert-state.json
  {
    "last_checked": "2026-06-09T09:00:00",
    "seen_fires": {"12345678": "2026-06-09T09:00:43"}
  }

Usage:
  python tri_city_alert_monitor.py [--poll-interval 60]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import websocket as ws_client  # websocket-client

# ── Paths ─────────────────────────────────────────────────────────────────────
WORKSPACE  = Path.home() / "tri-city-inator"
SHARED     = WORKSPACE / "shared"
LOGS       = WORKSPACE / "logs"
SCRIPTS    = WORKSPACE / "scripts"

ALERT_STATE_FILE  = SHARED / "tri-city-alert-state.json"
ALERT_CONFIG_FILE = SHARED / "tri-city-alert-config.json"
LEVELS_FILE       = SHARED / "tri-city-levels.json"
CANDIDATES_FILE   = SHARED / "tri-city-candidates.json"
WATCHLIST_SEEDS   = SHARED / "tri-city-watchlist-seeds.json"
LOG_FILE_EXEC     = WORKSPACE / "logs" / "tri-city-executions.json"
PID_FILE          = SHARED / "tri-city-alert-monitor.pid"

CT            = ZoneInfo("America/Chicago")
CDP_HOST      = "localhost"
CDP_PORT      = 9222
DEFAULT_POLL  = 60        # seconds between alert polls

# Market hours (CT)
MARKET_OPEN_H,  MARKET_OPEN_M  = 8,  30
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 5

LOGS.mkdir(parents=True, exist_ok=True)

# ── Load .env ─────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(WORKSPACE / ".env")
except ImportError:
    pass

ALPACA_API_KEY     = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY  = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_PAPER       = os.getenv("ALPACA_PAPER", "true").lower() == "true"
ORB_MINUTES        = int(os.getenv("ORB_MINUTES", "15"))
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS", "5"))
MAX_DAILY_LOSS     = float(os.getenv("MAX_DAILY_LOSS", "-300"))
SPY_BEAR_THRESHOLD = float(os.getenv("SPY_BEAR_THRESHOLD", "-1.5"))
TELEGRAM_BOT_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID     = os.getenv("TELEGRAM_CHAT_ID", "")
ALERT_SIGNAL_DEFAULT = os.getenv("ALERT_SIGNAL_DEFAULT", "BREAKOUT")

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOGS / "tri-city-alert-monitor.log")],
    force=True,
)
logger = logging.getLogger(__name__)

for _lib in ("urllib3", "requests", "websocket"):
    logging.getLogger(_lib).setLevel(logging.CRITICAL)


# ── CDP helpers (mirrors tri_city_tv_poller.py exactly) ───────────────────────

def find_tv_target() -> dict | None:
    """Return the TradingView chart CDP target, or None."""
    try:
        resp = requests.get(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=5)
        targets = resp.json()
        for t in targets:
            if t.get("type") == "page" and "tradingview.com/chart" in t.get("url", "").lower():
                return t
    except Exception as e:
        logger.warning(f"CDP target list failed: {e}")
    return None


def cdp_evaluate_async(ws_url: str, expression: str, timeout: int = 30) -> object:
    """
    Execute async JS (Promise-returning) in TradingView via CDP WebSocket.
    Uses awaitPromise=True so fetch() calls resolve fully before returning.
    Returns deserialized value or None on failure.
    """
    ws = ws_client.create_connection(
        ws_url,
        timeout=timeout,
        host=f"{CDP_HOST}:{CDP_PORT}",
        suppress_origin=True,
    )
    ws.settimeout(timeout)
    try:
        ws.send(json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        }))
        msg = _drain_until_id(ws, 2, max_msgs=200)
        if not msg:
            return None
        result = msg.get("result", {}).get("result", {})
        if result.get("type") == "object" and "value" in result:
            return result["value"]
        err = msg.get("result", {}).get("exceptionDetails")
        if err:
            logger.warning(f"CDP async JS exception: {err.get('text', err)}")
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _drain_until_id(ws, target_id: int, max_msgs: int = 200) -> dict | None:
    for _ in range(max_msgs):
        try:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == target_id:
                return msg
        except Exception:
            break
    return None


# ── Alert list JS — mirrors MCP server core/alerts.js list() exactly ──────────
# Uses pricealerts.tradingview.com REST API via browser fetch() with session
# cookies. No separate auth needed — runs inside the logged-in TV browser context.

_ALERT_LIST_JS = """
fetch('https://pricealerts.tradingview.com/list_alerts', { credentials: 'include' })
  .then(function(r) { return r.json(); })
  .then(function(data) {
    if (data.s !== 'ok' || !Array.isArray(data.r)) {
      return { alerts: [], error: data.errmsg || 'Unexpected response' };
    }
    return {
      alerts: data.r.map(function(a) {
        var sym = '';
        try { sym = JSON.parse(a.symbol.replace(/^=/, '')).symbol || a.symbol; } catch(e) { sym = a.symbol; }
        return {
          alert_id:   a.alert_id,
          symbol:     sym,
          type:       a.type,
          message:    a.message,
          active:     a.active,
          condition:  a.condition,
          resolution: a.resolution,
          created:    a.create_time,
          last_fired: a.last_fire_time,
          expiration: a.expiration
        };
      })
    };
  })
  .catch(function(e) { return { alerts: [], error: e.message }; })
"""


def fetch_alerts(ws_url: str) -> list[dict]:
    """
    Call pricealerts.tradingview.com/list_alerts via CDP evaluateAsync.
    Returns list of alert dicts, or [] on failure.
    """
    result = cdp_evaluate_async(ws_url, _ALERT_LIST_JS, timeout=30)
    if not result:
        logger.warning("Alert list returned None from CDP")
        return []
    if result.get("error"):
        logger.warning(f"Alert list error: {result['error']}")
    alerts = result.get("alerts", [])
    logger.info(f"Fetched {len(alerts)} alerts from TradingView")
    return alerts


# ── State file helpers ─────────────────────────────────────────────────────────

def load_alert_state() -> dict:
    """Load seen_fires from disk. Returns {'seen_fires': {alert_id: last_fired_ts}}."""
    if not ALERT_STATE_FILE.exists():
        return {"seen_fires": {}}
    try:
        return json.loads(ALERT_STATE_FILE.read_text())
    except Exception as e:
        logger.warning(f"Alert state parse error: {e}")
        return {"seen_fires": {}}


def save_alert_state(state: dict) -> None:
    SHARED.mkdir(parents=True, exist_ok=True)
    state["last_checked"] = datetime.now(CT).isoformat()
    ALERT_STATE_FILE.write_text(json.dumps(state, indent=2))


# ── Alert message parser ───────────────────────────────────────────────────────
# Indicator-agnostic: works with any alert source. Parsing chain:
#   1. Contract JSON (tc_v key) → trust signal field directly
#   2. Structured JSON (indicator + signal keys) → map buy/sell → signal type
#   3. Keyword scan in plain text → longest-match-first
#   4. Returns (None, None) → caller uses ALERT_SIGNAL_DEFAULT

_VALID_SIGNALS = ("EMA20_PULLBACK", "CONTINUATION", "PULLBACK", "BREAKOUT", "FADE")
_TEMPLATE_TICKERS = {"{{ticker}}", "{{TICKER}}", "", "none", "null"}


def parse_alert_message_v2(msg: str | None) -> tuple[str | None, str | None]:
    """
    Returns (signal, ticker_if_known).

    signal: BREAKOUT | CONTINUATION | PULLBACK | EMA20_PULLBACK | FADE | None
    ticker_if_known: resolved ticker string if the message contained one, else None.
      - None means caller must resolve the symbol via watchlist scan.
      - Non-None means the message explicitly named the symbol (contract or structured JSON).
    """
    if not msg:
        return None, None

    # 1. Contract JSON: tc_v key present → trust signal field completely
    try:
        data = json.loads(msg)
        if "tc_v" in data:
            raw_signal = str(data.get("signal", "")).upper().strip()
            signal = raw_signal if raw_signal in _VALID_SIGNALS else "BREAKOUT"
            ticker_raw = str(data.get("ticker", "")).upper().strip()
            ticker = None if ticker_raw.lower() in _TEMPLATE_TICKERS else ticker_raw
            return signal, ticker
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. Structured JSON with indicator + signal keys (existing TV JSON format)
    try:
        data = json.loads(msg)
        indicator  = str(data.get("indicator", "")).upper()
        signal_val = str(data.get("signal",    "")).upper()
        ticker_raw = str(data.get("ticker",    "")).upper().strip()
        ticker = None if ticker_raw.lower() in _TEMPLATE_TICKERS else ticker_raw
        # Longest match first
        for sig in _VALID_SIGNALS:
            if sig in indicator or sig in signal_val:
                return sig, ticker
        if signal_val in ("BUY", "LONG"):
            return "BREAKOUT", ticker
        if signal_val in ("SELL", "SHORT"):
            return "FADE", ticker
        if ticker:
            return "BREAKOUT", ticker  # JSON with ticker but unknown signal → default
    except (json.JSONDecodeError, TypeError):
        pass

    # 3. Keyword scan in plain text (longest first to avoid PULLBACK matching before EMA20_PULLBACK)
    msg_upper = msg.strip().upper()
    for sig in _VALID_SIGNALS:
        if sig in msg_upper:
            return sig, None

    return None, None


# ── Market data helpers ────────────────────────────────────────────────────────

def get_alpaca_snapshot(symbol: str) -> dict | None:
    """
    Fetch Alpaca snapshot: latest price, daily bar (OHLCV + VWAP).
    Returns dict with 'price', 'open', 'high', 'low', 'close', 'vwap' or None.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=symbol))
        s = snap.get(symbol)
        if not s:
            return None
        price = None
        if s.latest_trade:
            price = float(s.latest_trade.price)
        elif s.latest_quote:
            ask = float(s.latest_quote.ask_price or 0)
            bid = float(s.latest_quote.bid_price or 0)
            if ask > 0 and bid > 0:
                price = round((ask + bid) / 2, 4)
        if price is None:
            return None
        result: dict = {"price": price}
        if s.daily_bar:
            result["open"]  = float(s.daily_bar.open)
            result["high"]  = float(s.daily_bar.high)
            result["low"]   = float(s.daily_bar.low)
            result["close"] = float(s.daily_bar.close)
            result["vwap"]  = float(s.daily_bar.vwap) if hasattr(s.daily_bar, "vwap") and s.daily_bar.vwap else None
        return result
    except Exception as e:
        logger.warning(f"get_alpaca_snapshot {symbol}: {e}")
        return None


def get_batch_alpaca_snapshots(symbols: list[str]) -> dict[str, dict]:
    """
    Batch-fetch Alpaca snapshots for any number of symbols.
    Chunks into 100-symbol requests (Alpaca's per-call limit).
    Returns dict: bare_symbol -> {price, open, high, low, close, vwap}
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY or not symbols:
        return {}
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        result: dict[str, dict] = {}
        for i in range(0, len(symbols), 100):
            chunk = symbols[i:i + 100]
            try:
                snaps = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols=chunk))
                for sym, s in snaps.items():
                    if not s:
                        continue
                    price = None
                    if s.latest_trade:
                        price = float(s.latest_trade.price)
                    elif s.latest_quote:
                        ask = float(s.latest_quote.ask_price or 0)
                        bid = float(s.latest_quote.bid_price or 0)
                        if ask > 0 and bid > 0:
                            price = round((ask + bid) / 2, 4)
                    if price is None:
                        continue
                    entry: dict = {"price": price}
                    if s.daily_bar:
                        entry["open"]  = float(s.daily_bar.open)
                        entry["high"]  = float(s.daily_bar.high)
                        entry["low"]   = float(s.daily_bar.low)
                        entry["close"] = float(s.daily_bar.close)
                        entry["vwap"]  = (
                            float(s.daily_bar.vwap)
                            if hasattr(s.daily_bar, "vwap") and s.daily_bar.vwap
                            else None
                        )
                    result[sym] = entry
            except Exception as e:
                logger.warning(f"batch_snapshot chunk {chunk[:3]}: {e}")
        return result
    except Exception as e:
        logger.warning(f"get_batch_alpaca_snapshots: {e}")
        return {}


def _ema_series(values: list[float], period: int) -> list[float]:
    """Standard EMA series computation. Same helper as signal_detector.py."""
    k = 2 / (period + 1)
    ema = [values[0]]
    for v in values[1:]:
        ema.append(v * k + ema[-1] * (1 - k))
    return ema


def compute_rsi_ema_dev(symbol: str, price: float) -> tuple[float, float]:
    """
    Compute 14-period RSI and EMA20 deviation % from Alpaca 1-min bars.
    Returns (rsi, ema_dev_pct). Returns (0.0, 0.0) on any failure -- never blocks.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return 0.0, 0.0
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        now = datetime.now(CT)
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame.Minute,
            start=now - timedelta(hours=7),
            end=now,
            limit=200,
        )
        bars = client.get_stock_bars(req)
        df   = bars.df
        if df.empty or len(df) < 20:
            return 0.0, 0.0

        closes = [float(x) for x in df["close"].values]

        # RSI(14) -- classic Wilder smoothing approximation
        rsi = 50.0
        if len(closes) >= 15:
            deltas   = [closes[i] - closes[i-1] for i in range(1, len(closes))]
            gains    = [max(0.0, d) for d in deltas]
            losses   = [max(0.0, -d) for d in deltas]
            avg_gain = sum(gains[-14:]) / 14
            avg_loss = sum(losses[-14:]) / 14
            if avg_loss == 0:
                rsi = 100.0
            else:
                rs  = avg_gain / avg_loss
                rsi = round(100.0 - (100.0 / (1.0 + rs)), 2)

        # EMA20 deviation %
        ema_dev = 0.0
        if len(closes) >= 20:
            ema20 = _ema_series(closes, 20)[-1]
            if ema20 > 0:
                ema_dev = round((price - ema20) / ema20 * 100, 2)

        return rsi, ema_dev
    except Exception as e:
        logger.debug(f"compute_rsi_ema_dev {symbol}: {e}")
        return 0.0, 0.0


def compute_rvol(symbol: str) -> float | None:
    """
    RVOL = today volume / (20-day avg daily vol * fraction of day elapsed).
    Returns None on failure -- callers never block on missing RVOL.
    """
    try:
        import yfinance as yf
        now = datetime.now(CT)
        df  = yf.download(symbol, period="30d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 5:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        today_str = now.strftime("%Y-%m-%d")
        today_row = df[df.index.strftime("%Y-%m-%d") == today_str]["volume"]
        today_vol = float(today_row.iloc[-1]) if not today_row.empty else None
        hist      = df[df.index.strftime("%Y-%m-%d") != today_str]["volume"].dropna()
        if today_vol is None or hist.empty:
            return None
        avg_daily  = float(hist.tail(20).mean())
        open_ct    = now.replace(hour=8, minute=30, second=0, microsecond=0)
        close_ct   = now.replace(hour=15, minute=0,  second=0, microsecond=0)
        elapsed    = max(60.0, (now - open_ct).total_seconds())
        fraction   = min(1.0, elapsed / (close_ct - open_ct).total_seconds())
        expected   = avg_daily * fraction
        return round(today_vol / expected, 2) if expected > 0 else None
    except Exception as e:
        logger.debug(f"compute_rvol {symbol}: {e}")
        return None


# ── Entry guards ───────────────────────────────────────────────────────────────
# Each returns (blocked: bool, reason: str).
# Guards mirror tri_city_execute.py ordering exactly.

def guard_pre_orb(now: datetime) -> tuple[bool, str]:
    orb_lock = now.replace(hour=8, minute=30, second=0, microsecond=0) + timedelta(minutes=ORB_MINUTES)
    if now < orb_lock:
        return True, f"pre-ORB (lock at {orb_lock.strftime('%H:%M CT')})"
    return False, ""


def guard_already_executed(symbol: str, setup: str, execs_today: list[dict]) -> tuple[bool, str]:
    for e in execs_today:
        if e.get("symbol") == symbol and e.get("setup") == setup:
            return True, f"already executed {setup} today"
    return False, ""


def guard_already_in_position(symbol: str) -> tuple[bool, str]:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, ""
    try:
        from alpaca.trading.client import TradingClient
        client    = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        positions = client.get_all_positions()
        held_syms = {p.symbol for p in positions}
        if symbol in held_syms:
            return True, "already in position"
        return False, ""
    except Exception as e:
        logger.warning(f"guard_already_in_position {symbol}: {e}")
        return False, ""


def guard_max_positions() -> tuple[bool, str]:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, ""
    try:
        from alpaca.trading.client import TradingClient
        client    = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        positions = client.get_all_positions()
        total     = len(positions)
        if total >= MAX_POSITIONS:
            return True, f"max positions {total}/{MAX_POSITIONS}"
        return False, ""
    except Exception as e:
        logger.warning(f"guard_max_positions: {e}")
        return False, ""


def guard_daily_loss() -> tuple[bool, str]:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, ""
    try:
        from alpaca.trading.client import TradingClient
        client    = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        acct      = client.get_account()
        daily_pnl = float(acct.equity) - float(acct.last_equity)
        if daily_pnl <= MAX_DAILY_LOSS:
            return True, f"daily loss ${daily_pnl:.2f} <= limit ${MAX_DAILY_LOSS:.0f}"
        return False, ""
    except Exception as e:
        logger.warning(f"guard_daily_loss: {e}")
        return False, ""


def guard_market_regime() -> tuple[bool, str]:
    """Fail open on data error -- never block on missing data."""
    try:
        import yfinance as yf
        df = yf.download("SPY", period="5d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) < 2:
            return False, ""
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]
        prev_close = float(df["close"].iloc[-2])
        curr       = float(df["close"].iloc[-1])
        change     = round((curr - prev_close) / prev_close * 100, 2)
        if change <= SPY_BEAR_THRESHOLD:
            return True, f"SPY {change:+.2f}% bearish"
        return False, ""
    except Exception as e:
        logger.debug(f"guard_market_regime: {e}")
        return False, ""


def guard_extension(rsi: float, ema_dev: float, setup: str) -> tuple[bool, str]:
    if setup in ("BREAKOUT", "CONTINUATION"):
        if rsi > 82:
            return True, f"RSI {rsi:.1f} overbought (>82)"
        if ema_dev > 12.0:
            return True, f"EMA dev {ema_dev:.2f}% extended (>12%)"
    return False, ""


# ── Levels loader ──────────────────────────────────────────────────────────────

def load_levels() -> dict:
    """Load ORH/ORL from tri-city-levels.json. Returns {} if stale or missing."""
    if not LEVELS_FILE.exists():
        return {}
    try:
        raw       = json.loads(LEVELS_FILE.read_text())
        today_str = datetime.now(CT).strftime("%Y-%m-%d")
        file_date = raw.get("_date", today_str)
        if file_date != today_str:
            logger.warning(f"levels.json is from {file_date} -- stale, ignoring")
            return {}
        return {k: v for k, v in raw.items() if k != "_date"}
    except Exception as e:
        logger.warning(f"load_levels: {e}")
        return {}


def load_executions_today() -> list[dict]:
    today = datetime.now(CT).strftime("%Y-%m-%d")
    if not LOG_FILE_EXEC.exists():
        return []
    try:
        all_entries = json.loads(LOG_FILE_EXEC.read_text())
        return [e for e in all_entries if e.get("date") == today and e.get("success")]
    except Exception:
        return []


# ── Watchlist symbol resolution ───────────────────────────────────────────────

def _load_watchlist_universe() -> list[str]:
    """
    Build the full symbol universe to scan when a watchlist alert fires.
    Sources: tri-city-candidates.json (tv_symbols) + tri-city-watchlist-seeds.json.
    Handles any watchlist size — no cap.
    """
    symbols: set[str] = set()

    # Source 1: candidates.json tv_symbols
    try:
        cands = json.loads(CANDIDATES_FILE.read_text())
        for tv_sym in cands.get("tv_symbols", []):
            bare = tv_sym.split(":")[-1] if ":" in tv_sym else tv_sym
            symbols.add(bare.upper())
    except Exception as e:
        logger.debug(f"_load_watchlist_universe candidates: {e}")

    # Source 2: watchlist-seeds.json (written at 7:30 AM session start)
    try:
        seeds_data = json.loads(WATCHLIST_SEEDS.read_text())
        seeds_age_h = (time.time() - WATCHLIST_SEEDS.stat().st_mtime) / 3600
        if seeds_age_h > 6:
            logger.warning(
                f"watchlist-seeds.json is {seeds_age_h:.1f}h old — "
                "symbols added to TV watchlist today may be missing"
            )
        for sym in seeds_data.get("symbols", []):
            bare = sym.split(":")[-1] if ":" in sym else sym
            symbols.add(bare.upper())
    except Exception as e:
        logger.debug(f"_load_watchlist_universe seeds: {e}")

    return sorted(symbols)


def resolve_watchlist_symbols(watchlist_id: str, levels: dict) -> list[str]:
    """
    When a watchlist alert fires, identify which symbol(s) just crossed their ORH.

    1. Load full symbol universe (candidates + watchlist seeds, any size)
    2. Batch-fetch Alpaca snapshots in chunks of 100
    3. Filter to symbols at or above their locked ORH (0.1% tolerance)
    4. Return top 3 sorted by distance above ORH descending
    """
    sym_list = _load_watchlist_universe()
    if not sym_list:
        logger.warning(f"resolve_watchlist {watchlist_id}: no symbols in universe")
        return []

    logger.info(f"resolve_watchlist {watchlist_id}: scanning {len(sym_list)} symbols")
    snaps = get_batch_alpaca_snapshots(sym_list)
    if not snaps:
        logger.warning(f"resolve_watchlist {watchlist_id}: Alpaca batch returned empty")
        return []

    candidates: list[tuple[float, str]] = []
    for sym in sym_list:
        snap = snaps.get(sym)
        if not snap:
            continue
        price = snap["price"]

        lvl = levels.get(sym, {})
        orh = float(lvl.get("orh", 0.0))
        if orh <= 0:
            # Fallback: today's daily bar high
            orh = snap.get("high", 0.0)
        if orh <= 0:
            continue

        if price >= orh * 0.999:
            distance = (price - orh) / orh
            candidates.append((distance, sym))

    if not candidates:
        logger.info(f"resolve_watchlist {watchlist_id}: no symbols found at/above ORH")
        return []

    candidates.sort(reverse=True)
    result = [sym for _, sym in candidates[:3]]
    logger.info(f"resolve_watchlist {watchlist_id}: above-ORH candidates: {result}")
    return result


def sync_alert_config(alerts: list[dict]) -> None:
    """
    Extract unique watchlist IDs from active alerts, write to tri-city-alert-config.json.
    Runs once at monitor startup for diagnostics/logging.
    """
    watchlist_ids = sorted({
        a["symbol"].replace("WATCHLIST:", "")
        for a in alerts
        if a.get("active") and a.get("symbol", "").startswith("WATCHLIST:")
    })
    config = {
        "updated":       datetime.now(CT).isoformat(),
        "watchlist_ids": watchlist_ids,
        "default_signal": ALERT_SIGNAL_DEFAULT,
        "contract_template": '{"tc_v":"1","ticker":"{{ticker}}","signal":"BREAKOUT","price":{{close}},"time":"{{time}}"}',
    }
    SHARED.mkdir(parents=True, exist_ok=True)
    ALERT_CONFIG_FILE.write_text(json.dumps(config, indent=2))
    logger.info(f"Alert config synced: watchlist_ids={watchlist_ids}, default={ALERT_SIGNAL_DEFAULT}")


# ── Notifications ──────────────────────────────────────────────────────────────

def _esc(s: str) -> str:
    return s.replace('"', '\\"').replace("\\", "\\\\")


def notify(title: str, body: str, sound: str = "Ping") -> None:
    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(title)}" '
        f'sound name "{sound}"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)


def send_telegram(msg: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.parse
        import urllib.request as _ur
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"
        }).encode()
        _ur.urlopen(_ur.Request(url, data=data), timeout=5)
    except Exception as e:
        logger.debug(f"Telegram send failed: {e}")


# ── Core alert processing ──────────────────────────────────────────────────────

def _evaluate_and_execute(
    symbol: str, setup: str, alert_id: str,
    levels: dict, execs_today: list[dict], now: datetime,
) -> str:
    """
    Run all entry guards for a single symbol + setup, then call tri_city_execute.py.
    Returns: "executed" | "skipped:<reason>" | "post_cutoff" | "error:<detail>"
    """
    # Guard 0: pre-ORB
    blocked, reason = guard_pre_orb(now)
    if blocked:
        logger.info(f"{symbol}: SKIP -- {reason}")
        notify("Alert Skipped", f"{symbol} {setup} -- {reason}")
        return f"skipped:{reason}"

    # Guard 1: already executed today
    blocked, reason = guard_already_executed(symbol, setup, execs_today)
    if blocked:
        logger.info(f"{symbol}: SKIP -- {reason}")
        return f"skipped:{reason}"

    # Fetch real-time price from Alpaca
    snap = get_alpaca_snapshot(symbol)
    if not snap:
        logger.warning(f"{symbol}: could not fetch Alpaca snapshot -- skipping")
        return "skipped:no_quote"

    price = snap["price"]
    vwap  = snap.get("vwap")
    logger.info(f"{symbol}: price=${price:.2f}" + (f", VWAP=${vwap:.2f}" if vwap else ""))

    # Guard 2: already in position
    blocked, reason = guard_already_in_position(symbol)
    if blocked:
        logger.info(f"{symbol}: SKIP -- {reason}")
        return f"skipped:{reason}"

    # Guard 3: max positions
    blocked, reason = guard_max_positions()
    if blocked:
        logger.info(f"{symbol}: SKIP -- {reason}")
        notify("Alert Skipped", f"{symbol} {setup} -- {reason}")
        return f"skipped:{reason}"

    # Guard 4: daily loss limit
    blocked, reason = guard_daily_loss()
    if blocked:
        logger.info(f"{symbol}: SKIP -- {reason}")
        notify("Alert Skipped", f"{symbol} {setup} -- {reason}")
        return f"skipped:{reason}"

    # Guard 5: market regime
    blocked, reason = guard_market_regime()
    if blocked:
        logger.info(f"{symbol}: SKIP -- {reason}")
        return f"skipped:{reason}"

    # Compute RSI and EMA dev from Alpaca 1-min bars
    rsi, ema_dev = compute_rsi_ema_dev(symbol, price)
    logger.info(f"{symbol}: RSI={rsi:.1f}, EMA_dev={ema_dev:.2f}%")

    # Guard 6: extension
    blocked, reason = guard_extension(rsi, ema_dev, setup)
    if blocked:
        logger.info(f"{symbol}: SKIP -- {reason}")
        notify("Alert Skipped", f"{symbol} {setup} -- {reason}")
        return f"skipped:{reason}"

    # Load ORH/ORL from levels file (fall back to daily bar for off-list symbols)
    lvl = levels.get(symbol, {})
    orh = float(lvl.get("orh", 0.0))
    orl = float(lvl.get("orl", 0.0))
    if orh <= 0:
        logger.warning(f"{symbol}: no locked ORH/ORL -- using daily bar proxy")
        orh = snap.get("high",  price * 1.01)
        orl = snap.get("low",   price * 0.95)
        if orh <= orl:
            orh = price * 1.01
            orl = price * 0.95

    # Compute RVOL (yfinance)
    rvol     = compute_rvol(symbol)
    rvol_str = f"{rvol:.2f}x" if rvol is not None else "N/A"
    logger.info(f"{symbol}: RVOL={rvol_str}")

    # All guards passed — delegate to tri_city_execute.py
    cmd = [
        "python", "-W", "ignore",
        str(SCRIPTS / "tri_city_execute.py"),
        "--symbol",  symbol,
        "--price",   str(round(price,   4)),
        "--orh",     str(round(orh,     4)),
        "--orl",     str(round(orl,     4)),
        "--rsi",     str(round(rsi,     2)),
        "--ema_dev", str(round(ema_dev, 4)),
        "--signal",  setup,
        "--setup",   setup,
        "--quiet",
    ]
    if rvol is not None:
        cmd += ["--rvol", str(round(rvol, 2))]
    if vwap is not None:
        cmd += ["--vwap", str(round(vwap, 4))]

    logger.info(f"Calling: {' '.join(cmd[4:])}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        output = (result.stdout + result.stderr).strip()
        logger.info(f"Execute [{result.returncode}]: {output[:200] or '(silent)'}")
    except subprocess.TimeoutExpired:
        logger.error(f"{symbol}: execute timed out after 60s")
        return "skipped:execute_timeout"
    except Exception as e:
        logger.error(f"{symbol}: execute error: {e}")
        return "skipped:execute_error"

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("✅"):
            body = f"{symbol} {setup} @ ${price:.2f} -- EXECUTED (alert-driven)"
            notify("TRI-CITY ALERT TRADE", body, sound="Glass")
            send_telegram(
                f"\U0001f6a8 <b>ALERT ENTRY -- {symbol}</b>\n"
                f"{setup} @ ${price:.2f} | RSI={rsi:.1f} EMA={ema_dev:+.2f}%\n"
                f"RVOL={rvol_str} | ORH=${orh:.2f} | ORL=${orl:.2f}\n"
                f"Alert ID: {alert_id}"
            )
            logger.info(f"EXECUTED {symbol} {setup} @ ${price:.2f}")
            return "executed"

        if "SKIP" in line.upper() or "PRE_ORB" in line.upper() or "PRE_PULLBACK" in line.upper():
            skip_reason = line.split(":", 1)[-1].strip() if ":" in line else line
            notify("Alert Skipped", f"{symbol} {setup}: {skip_reason}")
            send_telegram(f"⏸ <b>ALERT SKIPPED</b> -- {symbol} {setup}\n{skip_reason}")
            return f"skipped:{skip_reason}"

        if "POST_CUTOFF_SIGNAL" in line:
            notify("Alert Post-Cutoff", f"{symbol} {setup} @ ${price:.2f}", sound="Sosumi")
            send_telegram(
                f"⚠️ <b>ALERT POST-CUTOFF</b> -- {symbol}\n"
                f"{setup} @ ${price:.2f} -- type <code>yes {symbol}</code> to approve"
            )
            return "post_cutoff"

        if line.startswith("❌"):
            notify("Alert Exec Error", f"{symbol}: {line}", sound="Basso")
            send_telegram(f"❌ <b>ALERT EXEC ERROR</b> -- {symbol}\n{line}")
            return f"error:{line}"

    if result.returncode == 0:
        return "skipped:silent_guard"
    return f"skipped:rc_{result.returncode}"


def process_new_alert(alert: dict, levels: dict, execs_today: list[dict]) -> str:
    """
    Route a newly-fired alert to the right symbol(s) and evaluate.

    Per-symbol alert  (alert.symbol = "NASDAQ:SLNG"):  symbol known directly.
    Watchlist alert   (alert.symbol = "WATCHLIST:..."):  scan all watchlist symbols
                      via Alpaca to find which crossed ORH, try up to 3 candidates.

    Returns: "executed" | "skipped:<reason>" | "post_cutoff" | "no_candidates" | "error:<detail>"
    """
    alert_id = str(alert.get("alert_id", ""))
    raw_sym  = alert.get("symbol", "")
    msg      = alert.get("message", "")
    now      = datetime.now(CT)

    # Parse signal + any resolved ticker from message
    signal, ticker_from_msg = parse_alert_message_v2(msg)
    if signal is None:
        signal = ALERT_SIGNAL_DEFAULT
        logger.info(f"Alert {alert_id}: no signal in msg — using default {ALERT_SIGNAL_DEFAULT}")

    # Determine candidates
    if raw_sym.startswith("WATCHLIST:"):
        watchlist_id = raw_sym.replace("WATCHLIST:", "")
        if ticker_from_msg:
            # Contract alert resolved the ticker (tc_v path)
            candidates = [ticker_from_msg]
            logger.info(f"Alert {alert_id}: watchlist {watchlist_id}, contract ticker={ticker_from_msg}")
        else:
            # Scan watchlist symbols to find which crossed ORH
            candidates = resolve_watchlist_symbols(watchlist_id, levels)
            if not candidates:
                logger.info(f"Alert {alert_id}: watchlist {watchlist_id} fired, no symbol above ORH")
                notify("Alert Monitor", f"Watchlist alert fired — no symbol above ORH")
                return "no_candidates"
    else:
        bare = raw_sym.split(":")[-1].upper() if raw_sym else ""
        if not bare:
            logger.warning(f"Alert {alert_id}: empty symbol")
            return "parse_error"
        candidates = [bare]

    logger.info(f"Alert {alert_id}: {signal} signal, candidates={candidates}")

    # Evaluate each candidate; stop at first successful execution
    last_outcome = "no_candidates"
    for symbol in candidates:
        try:
            outcome = _evaluate_and_execute(symbol, signal, alert_id, levels, execs_today, now)
            last_outcome = outcome
            if outcome == "executed":
                return "executed"
            if len(candidates) > 1:
                logger.info(f"Candidate {symbol} → {outcome}, trying next")
        except Exception as e:
            logger.exception(f"_evaluate_and_execute {symbol} crashed: {e}")
            last_outcome = f"error:{e}"

    return last_outcome


# ── Main poll cycle ────────────────────────────────────────────────────────────

def run_cycle(ws_url: str, state: dict) -> dict:
    """
    Fetch TV alerts, detect new fires vs seen_fires state, process each new fire.
    Returns updated state dict.
    """
    now_str = datetime.now(CT).strftime("%H:%M CT")
    logger.info(f"-- Alert cycle {now_str} -----------------------------------------")

    alerts = fetch_alerts(ws_url)
    if not alerts:
        logger.info("No alerts returned -- waiting for next cycle")
        return state

    # Sync alert config once at startup (writes watchlist IDs + contract template)
    if not state.get("_config_synced"):
        sync_alert_config(alerts)
        state["_config_synced"] = True

    seen_fires  = state.get("seen_fires", {})
    levels      = load_levels()
    execs_today = load_executions_today()

    new_fires: list[dict] = []
    for alert in alerts:
        alert_id   = str(alert.get("alert_id", ""))
        last_fired = alert.get("last_fired")
        active     = alert.get("active", False)

        if not alert_id:
            continue

        if not last_fired:
            # Alert has never fired -- record but don't act
            seen_fires.setdefault(alert_id, None)
            continue

        if not active:
            # Expired/inactive alert -- update seen state silently
            seen_fires.setdefault(alert_id, last_fired)
            continue

        prev_fired = seen_fires.get(alert_id)

        if prev_fired is None:
            # First time seeing this alert that has fired -- record WITHOUT executing.
            # This prevents the monitor re-executing an alert from before it started.
            logger.info(
                f"Alert {alert_id} ({alert.get('symbol', '?')}): "
                f"first seen with last_fired={last_fired!r} -- recording, not executing"
            )
            seen_fires[alert_id] = last_fired

        elif last_fired != prev_fired:
            # Fire timestamp changed since last cycle -- new fire
            logger.info(
                f"Alert {alert_id} ({alert.get('symbol', '?')}): "
                f"new fire: {prev_fired!r} -> {last_fired!r}"
            )
            new_fires.append(alert)
            seen_fires[alert_id] = last_fired
        # else: unchanged -- already processed this fire

    state["seen_fires"] = seen_fires

    if not new_fires:
        logger.info(f"No new fires (scanned {len(alerts)} alerts)")
        return state

    logger.info(f"Processing {len(new_fires)} new alert fire(s)")
    for alert in new_fires:
        sym = alert.get("symbol", "?")
        try:
            outcome = process_new_alert(alert, levels, execs_today)
            logger.info(f"Outcome {sym} {alert.get('alert_id')}: {outcome}")
        except Exception as e:
            logger.exception(f"process_new_alert {sym} crashed: {e}")
            notify("Alert Monitor Crash", f"{sym}: {e}", sound="Basso")
            send_telegram(f"❌ <b>ALERT MONITOR ERROR</b>\n{sym}: {e}")

    return state


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Tri-City Alert Monitor")
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL,
                        help=f"Seconds between alert polls (default {DEFAULT_POLL})")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle and exit (for testing)")
    args = parser.parse_args()

    PID_FILE.write_text(str(os.getpid()))

    def _shutdown(sig, frame):
        logger.info(f"Received signal {sig} -- shutting down alert monitor")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        f"TRI-CITY ALERT MONITOR started | PID {os.getpid()} | "
        f"poll every {args.poll_interval}s | CDP localhost:{CDP_PORT}"
    )

    state = load_alert_state()

    if args.once:
        target = find_tv_target()
        if not target:
            logger.error("No TradingView chart target on CDP -- exiting")
            PID_FILE.unlink(missing_ok=True)
            sys.exit(1)
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            logger.error("No webSocketDebuggerUrl -- exiting")
            PID_FILE.unlink(missing_ok=True)
            sys.exit(1)
        state = run_cycle(ws_url, state)
        save_alert_state(state)
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    # Daemon loop
    while True:
        now  = datetime.now(CT)

        if now.weekday() >= 5:
            logger.info("Weekend -- sleeping 1h")
            time.sleep(3600)
            continue

        mmin      = now.hour * 60 + now.minute
        close_min = MARKET_CLOSE_H * 60 + MARKET_CLOSE_M
        open_min  = MARKET_OPEN_H  * 60 + MARKET_OPEN_M

        if mmin > close_min:
            logger.info(f"Market closed ({now.strftime('%H:%M CT')}) -- alert monitor shutting down")
            PID_FILE.unlink(missing_ok=True)
            sys.exit(0)

        if mmin < open_min:
            wait = (open_min - mmin) * 60
            logger.info(f"Pre-market -- sleeping {wait}s until {MARKET_OPEN_H:02d}:{MARKET_OPEN_M:02d} CT")
            time.sleep(wait)
            continue

        target = find_tv_target()
        if not target:
            logger.warning("No TradingView chart on CDP port 9222 -- sleeping 30s")
            time.sleep(30)
            continue

        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            logger.warning("No webSocketDebuggerUrl -- sleeping 30s")
            time.sleep(30)
            continue

        try:
            state = run_cycle(ws_url, state)
            save_alert_state(state)
        except Exception as e:
            logger.exception(f"Cycle error (non-fatal): {e}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
