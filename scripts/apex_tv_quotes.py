#!/usr/bin/env python3
"""
APEX — TradingView real-time quote reader (hybrid feed).

The headless poller uses delayed Alpaca *bars* to compute stable intraday LEVELS (ORB high/low,
VWAP, EMA), but takes the live TRIGGER price from TradingView's real-time quote session over CDP.
This closes the ~15-min delay on the part that matters (current price) without an Alpaca data
upgrade and without the fragile Pine-table scrape — we read raw quotes, no custom indicator.

Mechanism (discovered by probing a live TV via CDP, 2026-06-17):
  window.getQuoteSessionInstance()  → singleton quote session
    .setFields([...])               → which quote fields to stream
    .subscribe('apex', 'WDC')       → bare symbols resolve to primary listing
    ._symbol_data[key].values       → { last_price, change, change_percent, volume,
                                        current_session, lp_time, open_price, ... }

Everything degrades gracefully: if TV/CDP is down or a symbol has no live tick, the caller
(poller) falls back to the delayed Alpaca price for that symbol.
"""

from __future__ import annotations

import json
import logging
import time

import requests
import websocket as ws_client

CDP_HOST = "localhost"
CDP_PORT = 9222

logger = logging.getLogger("apex.tvquotes")

# quote fields we stream (names verified against a live session)
_FIELDS = ["last_price", "change", "change_percent", "volume", "open_price",
           "high_price", "low_price", "prev_close_price", "current_session",
           "lp_time", "short_name", "pro_name"]

_PROBE_QSI = "(function(){return typeof window.getQuoteSessionInstance==='function';})()"


def _tabs() -> list:
    r = requests.get(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=5).json()
    return [t for t in r if t.get("type") == "page"
            and "tradingview.com/chart" in t.get("url", "").lower()]


def cdp_evaluate(ws_url: str, expression: str, timeout: int = 20):
    """Runtime.evaluate a JS expression over CDP; awaits promises, parses JSON strings."""
    ws = ws_client.create_connection(ws_url, timeout=timeout,
                                     host=f"{CDP_HOST}:{CDP_PORT}", suppress_origin=True)
    ws.settimeout(timeout)
    try:
        ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }))
        msg = json.loads(ws.recv())
        res = msg.get("result", {}).get("result", {})
        if res.get("type") == "string":
            try:
                return json.loads(res.get("value"))
            except Exception:
                return res.get("value")
        return res.get("value")
    finally:
        ws.close()


def get_quote_tab() -> dict | None:
    """Return the first TV chart tab that exposes the quote session, or None if CDP/TV is down."""
    try:
        for t in _tabs():
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            try:
                if cdp_evaluate(ws_url, _PROBE_QSI, timeout=5) is True:
                    return t
            except Exception:
                continue
    except Exception as e:
        logger.debug(f"CDP list failed (TV/CDP down?): {e}")
    return None


def _read_js(symbols: list[str], wait_ms: int) -> str:
    """Build JS that subscribes the symbols, waits for ticks, then returns their quote values."""
    syms = json.dumps(symbols)
    fields = json.dumps(_FIELDS)
    return f"""(function(){{
      return new Promise(function(resolve){{
        try {{
          var inst = window.getQuoteSessionInstance();
          var syms = {syms};
          if (inst.setFields) inst.setFields({fields});
          syms.forEach(function(s){{ try{{ inst.subscribe('apex', s); }}catch(e){{}} }});
          // mark as "fast" so TV streams them in real-time instead of throttling (key for >~20 syms)
          try {{ if (inst.setFastSymbols) inst.setFastSymbols('apex', syms); }} catch(e){{}}
          setTimeout(function(){{
            var sd = inst._symbol_data || {{}};
            var allKeys = Object.keys(sd);
            var out = {{}};
            syms.forEach(function(s){{
              var key = allKeys.find(function(k){{
                var e = sd[k]; var v = e && e.values;
                return (k === s) || (v && (v.short_name === s ||
                       (v.pro_name && v.pro_name.split(':').pop() === s)));
              }});
              if (key && sd[key].values) {{
                var v = sd[key].values;
                if (v.last_price !== undefined && v.last_price !== null) {{
                  out[s] = {{ last: v.last_price, change_pct: v.change_percent,
                             volume: v.volume, session: v.current_session,
                             lp_time: v.lp_time, open: v.open_price, high: v.high_price,
                             low: v.low_price, prev_close: v.prev_close_price }};
                }}
              }}
            }});
            resolve(JSON.stringify(out));
          }}, {wait_ms});
        }} catch(e){{ resolve(JSON.stringify({{__error: e.message}})); }}
      }});
    }})()"""


def get_quotes(symbols: list[str], wait_ms: int = 3000, ws_url: str | None = None) -> dict:
    """
    Real-time quotes for `symbols` (bare tickers). Returns {sym: {last, change_pct, volume,
    session, lp_time, open, high, low, prev_close}} for symbols with a live last price; symbols
    without a tick (or if TV/CDP is down) are simply absent → caller falls back to Alpaca.
    """
    if not symbols:
        return {}
    if ws_url is None:
        tab = get_quote_tab()
        if not tab:
            logger.debug("no TV quote tab — falling back to delayed feed")
            return {}
        ws_url = tab["webSocketDebuggerUrl"]
    try:
        result = cdp_evaluate(ws_url, _read_js(symbols, wait_ms), timeout=int(wait_ms / 1000) + 12)
    except Exception as e:
        logger.warning(f"quote read failed: {e}")
        return {}
    if not isinstance(result, dict):
        return {}
    if result.get("__error"):
        logger.warning(f"quote JS error: {result['__error']}")
        return {}
    return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    syms = sys.argv[1:] or ["AAPL", "WDC", "NUAI", "MULL", "QH", "SPY"]
    tab = get_quote_tab()
    if not tab:
        print("TV/CDP not available (is TradingView running with --remote-debugging-port=9222?)")
        sys.exit(1)
    print(f"quote tab: {tab.get('url','')[:50]}")
    q = get_quotes(syms)
    print(f"got {len(q)}/{len(syms)} live quotes:")
    for s in syms:
        v = q.get(s)
        if v:
            chp = v.get("change_pct")
            chp_s = f"{chp:+.2f}%" if isinstance(chp, (int, float)) else "  —  "
            print(f"  {s:6s} ${v['last']:<10.4f} {chp_s}  vol {str(v.get('volume','—')):>12}  "
                  f"{v.get('session','—')}")
        else:
            print(f"  {s:6s} — no live tick")
