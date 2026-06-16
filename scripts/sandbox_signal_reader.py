#!/usr/bin/env python3
"""
SANDBOX SIGNAL READER
Reads QQQ/SPY/IWM strategy signals from the SANDBOX TradingView layout via CDP.

Architecture:
  The SANDBOX layout uses 3 separate chart widgets (one per symbol) stored in
  window.TradingViewApi._chartWidgetCollection._chartWidgetsDefs[0..2].

  Signal source: strategy._reportData.filledOrders
    {b: bool,  c: str (signal id),  e: bool (is_entry),  p: float (price),
     q: int (qty),  tm: int (bar index)}

  b=True + e=True  → buy entry  → LONG signal
  b=False + e=True → sell entry → EXIT signal (Supertrend flip to short)

Freshness: only orders with tm > state["last_order_tms"][symbol] are new.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import requests

try:
    import websocket as ws_client
except ImportError:
    ws_client = None  # type: ignore

logger = logging.getLogger(__name__)

CDP_HOST = "localhost"
CDP_PORT = 9222

TARGET_FRAGMENTS = ["Super Scalper", "Supertrend", "SuperTrend"]

LONG_SIGNAL_IDS  = {"long", "my long entry id"}
EXIT_SIGNAL_IDS  = {"long_exit", "my short entry id"}


@dataclass
class SandboxSignal:
    symbol:     str
    direction:  str       # "long" | "exit"
    price:      float
    bar_tm:     int       # filledOrders.tm (bar index)
    strategy:   str       # "super_scalper" | "supertrend"
    signal_id:  str       # e.g. "long", "My Long Entry Id"


# ── CDP helpers ───────────────────────────────────────────────────────────────

_SANDBOX_JS = """
(function() {
  var api = window.TradingViewApi;
  var defs = api._chartWidgetCollection._chartWidgetsDefs;
  var result = {charts: []};
  var TARGET_FRAGMENTS = ['Super Scalper', 'Supertrend', 'SuperTrend'];

  for (var idx = 0; idx < 3; idx++) {
    var d = defs[idx];
    if (!d) continue;
    var cw = d.chartWidget;
    if (!cw) continue;

    var chartResult = {idx: idx, sym: '', orders: [], error: null};
    try {
      var model = cw.model();
      try { chartResult.sym = model.mainSeries().symbolInfo().name || ''; } catch(e) {}

      var sources = model.panes()[0].dataSources();
      for (var si = 0; si < sources.length; si++) {
        var s = sources[si];
        if (!s._reportData) continue;
        try {
          var name = '';
          try { name = s.metaInfo().description || ''; } catch(e) {}
          var isTarget = TARGET_FRAGMENTS.some(function(f) { return name.indexOf(f) !== -1; });
          if (!isTarget) continue;

          var rd = s._reportData;
          if (rd && typeof rd.value === 'function') rd = rd.value();
          var orders = rd ? rd.filledOrders : null;
          if (!Array.isArray(orders)) continue;

          chartResult.orders = orders.slice(-20).map(function(o) {
            return {b: o.b, c: o.c || '', e: o.e, p: o.p || 0, q: o.q || 0, tm: o.tm || 0};
          });
          break;
        } catch(e) { chartResult.error = String(e); }
      }
    } catch(e) { chartResult.error = String(e); }
    result.charts.push(chartResult);
  }
  return result;
})()
"""


def _find_sandbox_target() -> dict | None:
    try:
        resp = requests.get(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=5)
        targets = resp.json()
        tv = [t for t in targets
              if t.get("type") == "page"
              and "tradingview.com/chart" in t.get("url", "").lower()]
        if not tv:
            return None
        for t in tv:
            if "sandbox" in t.get("title", "").lower():
                return t
        return tv[0]
    except Exception as e:
        logger.warning(f"CDP target list failed: {e}")
        return None


def _cdp_evaluate(ws_url: str, expression: str, timeout: int = 20) -> object:
    if ws_client is None:
        raise RuntimeError("websocket-client not installed")
    ws = ws_client.create_connection(
        ws_url, timeout=timeout,
        host=f"{CDP_HOST}:{CDP_PORT}",
        suppress_origin=True,
    )
    ws.settimeout(timeout)
    try:
        ws.send(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": False},
        }))
        for _ in range(100):
            try:
                msg = json.loads(ws.recv())
                if msg.get("id") == 1:
                    res = msg.get("result", {}).get("result", {})
                    if res.get("type") == "object" and "value" in res:
                        return res["value"]
                    exc = msg.get("result", {}).get("exceptionDetails")
                    if exc:
                        logger.warning(f"CDP JS error: {exc.get('text', exc)}")
                    return None
            except Exception:
                break
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


# ── Signal parsing ─────────────────────────────────────────────────────────────

def _strategy_type(indicator_name: str) -> str:
    if "Super Scalper" in indicator_name:
        return "super_scalper"
    if "Supertrend" in indicator_name or "SuperTrend" in indicator_name:
        return "supertrend"
    return "unknown"


def _classify_order(order: dict) -> str | None:
    """Return 'long', 'exit', or None based on order fields."""
    is_buy   = order.get("b", False)
    is_entry = order.get("e", False)
    signal_c = (order.get("c") or "").lower()

    if is_buy and is_entry:
        return "long"
    if not is_buy and is_entry:
        return "exit"
    # Also handle pure exit orders (b=False, e=False)
    if not is_buy and not is_entry:
        if any(k in signal_c for k in ["exit", "close"]):
            return "exit"
    return None


def read_signals(last_order_tms: dict) -> tuple[list[SandboxSignal], dict]:
    """
    Connect to the SANDBOX TV tab, read filledOrders from each strategy.
    Returns (new_signals, updated_last_order_tms).

    last_order_tms: {symbol: max_tm_seen_previously}
    Only orders with tm > last_order_tms[symbol] are returned as new signals.
    """
    target = _find_sandbox_target()
    if not target:
        logger.error("SANDBOX CDP target not found")
        return [], last_order_tms

    ws_url = target.get("webSocketDebuggerUrl", "")
    if not ws_url:
        logger.error("No WebSocket debugger URL for SANDBOX target")
        return [], last_order_tms

    raw = _cdp_evaluate(ws_url, _SANDBOX_JS)
    if not raw:
        logger.warning("CDP JS returned no data")
        return [], last_order_tms

    signals:          list[SandboxSignal] = []
    new_last_tms:     dict               = dict(last_order_tms)

    for chart in raw.get("charts", []):
        sym_full  = chart.get("sym", "")
        symbol    = sym_full.split(":")[-1]
        orders    = chart.get("orders", [])
        stype     = "super_scalper" if symbol == "QQQ" else "supertrend"

        if not symbol:
            continue

        prev_tm = last_order_tms.get(symbol, -1)
        max_tm  = prev_tm

        for order in orders:
            tm: int = order.get("tm", 0)
            if tm <= prev_tm:
                continue

            max_tm = max(max_tm, tm)
            direction = _classify_order(order)
            if direction is None:
                continue

            signals.append(SandboxSignal(
                symbol=symbol,
                direction=direction,
                price=float(order.get("p", 0.0)),
                bar_tm=tm,
                strategy=stype,
                signal_id=order.get("c", ""),
            ))

        new_last_tms[symbol] = max_tm

    logger.info(
        f"CDP read: {len(signals)} new signal(s) | "
        f"tms={new_last_tms}"
    )
    return signals, new_last_tms
