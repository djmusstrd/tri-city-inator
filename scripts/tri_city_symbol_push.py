#!/usr/bin/env python3
"""
TRI-CITY SYMBOL PUSH — Push today's tv_symbols into Pine scanner via CDP.

Correct approach discovered 2026-06-16:
  - indicator_set_inputs MCP fails (Kbzkkm is on pane 1, not pane 0)
  - study.restart() reverts to server-saved state (wrong)
  - _chartApiInstance.modifyStudy fails (handles quote sessions, not chart sessions)
  - CORRECT: study._apiInputs() + _sendRequestImpl('modify_study', [sessionId, ...])
    where sessionId comes from _studyCounter keys (NOT from sessionId() which returns "")

Usage:
  python tri_city_symbol_push.py [--candidates shared/tri-city-candidates.json]
"""

import json
import logging
import sys
import time
from pathlib import Path

import requests
import websocket as ws_client

WORKSPACE   = Path.home() / "tri-city-inator"
SHARED      = WORKSPACE / "shared"
CANDIDATES  = SHARED / "tri-city-candidates.json"
CDP_PORT    = 9222
CDP_HOST    = "localhost"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("symbol_push")

# JavaScript that finds Kbzkkm and pushes new symbols via _sendRequestImpl.
# Must be called with symbols JSON-encoded as a JS array literal injected below.
_PUSH_JS_TEMPLATE = """(function() {{
  try {{
    var api = window.TradingViewApi;
    var inst = api._chartApiInstance;

    // Get session ID from _studyCounter (populated when chart session is active).
    // chartSession.sessionId() always returns "" — do NOT use it.
    var studyCounterKeys = Object.keys(inst._studyCounter);
    var sessionId = studyCounterKeys.find(function(k) {{ return k.startsWith('cs_'); }});
    if (!sessionId) return JSON.stringify({{error: 'No cs_ session in _studyCounter — chart API not ready'}});

    // Find Kbzkkm across all panes (it lives on pane 1, not pane 0)
    var panes = api._activeChartWidgetWV.value()._chartWidget.model().panes();
    var study = null;
    for (var pi = 0; pi < panes.length; pi++) {{
      var srcs = panes[pi].dataSources();
      for (var si = 0; si < srcs.length; si++) {{
        try {{ if (srcs[si].id() === 'Kbzkkm') {{ study = srcs[si]; break; }} }} catch(e) {{}}
      }}
      if (study) break;
    }}
    if (!study) return JSON.stringify({{error: 'Kbzkkm study not found in any pane'}});

    // Get full api inputs — includes encrypted Pine text + all in_0 through in_26
    var apiInputs = study._apiInputs();
    var turnaround = study._turnaround;

    // Update symbol slots in_7 through in_26
    var syms = {syms_json};
    syms.forEach(function(s, i) {{
      apiInputs['in_' + (7 + i)] = {{v: s, f: true, t: 'symbol'}};
    }});
    // Clear slots beyond the symbol count
    for (var i = 7 + syms.length; i <= 26; i++) {{
      apiInputs['in_' + i] = {{v: '', f: true, t: 'symbol'}};
    }}

    // Send modify_study via the chart API's WebSocket pipeline
    var result = inst._sendRequestImpl('modify_study', [sessionId, 'Kbzkkm', turnaround, apiInputs]);

    return JSON.stringify({{
      ok: true,
      sessionId: sessionId,
      turnaround: turnaround,
      symbolsPushed: syms.length,
      first: syms[0],
      last: syms[syms.length - 1],
      sendResult: result
    }});
  }} catch(e) {{
    return JSON.stringify({{error: e.message, stack: e.stack ? e.stack.slice(0, 300) : ''}});
  }}
}})()"""


_PROBE_JS = """(function() {
  try {
    var panes = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget.model().panes();
    for (var pi=0; pi<panes.length; pi++) {
      var srcs = panes[pi].dataSources();
      for (var si=0; si<srcs.length; si++) {
        try { if (srcs[si].id() === 'Kbzkkm') return 'found'; } catch(e) {}
      }
    }
    return 'not_found';
  } catch(e) { return 'err'; }
})()"""


def get_tv_tab():
    """Return the TV chart tab that contains the Kbzkkm study."""
    try:
        resp = requests.get(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=5)
        tabs = [t for t in resp.json()
                if t.get("type") == "page" and "tradingview.com/chart" in t.get("url", "").lower()]
        # Try each tab — return the one where Kbzkkm is found
        for t in tabs:
            ws_url = t.get("webSocketDebuggerUrl")
            if not ws_url:
                continue
            try:
                result = cdp_evaluate(ws_url, _PROBE_JS, timeout=5)
                if result == "found":
                    return t
            except Exception:
                pass
        # Fallback: return first tab
        return tabs[0] if tabs else None
    except Exception as e:
        logger.warning(f"CDP list failed: {e}")
    return None


def cdp_evaluate(ws_url: str, expression: str, timeout: int = 15) -> object:
    ws = ws_client.create_connection(
        ws_url, timeout=timeout,
        host=f"{CDP_HOST}:{CDP_PORT}",
        suppress_origin=True,
    )
    ws.settimeout(timeout)
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": False},
        }))
        raw = ws.recv()
        msg = json.loads(raw)
        result = msg.get("result", {}).get("result", {})
        if result.get("type") == "string":
            v = result.get("value")
            try:
                return json.loads(v)
            except Exception:
                return v
        return result.get("value")
    finally:
        ws.close()


def push_symbols(symbols: list[str]) -> dict:
    """Push up to 20 symbols into the Tri-City Inator scanner (in_7 through in_26)."""
    tab = get_tv_tab()
    if not tab:
        return {"error": "No TradingView chart tab found via CDP"}

    ws_url = tab.get("webSocketDebuggerUrl")
    if not ws_url:
        return {"error": "No webSocketDebuggerUrl in target"}

    syms = symbols[:20]
    syms_json = json.dumps(syms)
    js = _PUSH_JS_TEMPLATE.format(syms_json=syms_json)

    result = cdp_evaluate(ws_url, js)
    if isinstance(result, dict) and result.get("error"):
        return result

    return result or {"error": "No result from CDP evaluate"}


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", default=str(CANDIDATES))
    args = parser.parse_args()

    cands_path = Path(args.candidates)
    if not cands_path.exists():
        logger.error(f"Candidates file not found: {cands_path}")
        sys.exit(1)

    with open(cands_path) as f:
        data = json.load(f)

    symbols = data.get("tv_symbols", [])[:20]
    if not symbols:
        logger.error("No tv_symbols in candidates file")
        sys.exit(1)

    logger.info(f"Pushing {len(symbols)} symbols: {symbols}")

    result = push_symbols(symbols)
    if result.get("error"):
        logger.error(f"Push failed: {result}")
        sys.exit(1)

    logger.info(
        f"Push OK: {result.get('symbolsPushed')} symbols → "
        f"{result.get('first')} … {result.get('last')} "
        f"(session={result.get('sessionId')}, st={result.get('turnaround')})"
    )


if __name__ == "__main__":
    main()
