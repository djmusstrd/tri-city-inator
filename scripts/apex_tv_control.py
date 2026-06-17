#!/usr/bin/env python3
"""
APEX — TradingView desktop control over CDP (separate from the read-only quote feed).

Two actions, both against the running TradingView desktop app via CDP (port 9222):
  - set_chart_symbol(sym): drive the active chart to a symbol — the "desktop-drive" link mode.
  - watchlist_add(sym):     append a symbol to the APEX watchlist via TV's cookie-authed REST
                            API, run as a fetch() inside the page (the chart widget API's
                            watchlist() is "not implemented", so we use the web endpoint).

Both are best-effort: if TV/CDP is down they no-op and return False — nothing else breaks.
Discovered 2026-06-17: append endpoint POST /api/v1/symbols_list/custom/{id}/append/ [syms],
APEX watchlist id 336036336, APEX layout id 131204932.
"""

from __future__ import annotations

import json
import logging

import apex_config as cfg
from apex_tv_quotes import get_quote_tab, cdp_evaluate

logger = logging.getLogger("apex.tvctl")


def set_chart_symbol(symbol: str) -> bool:
    """Point the running desktop TV chart at `symbol` (bare ticker is fine). False if CDP down."""
    tab = get_quote_tab()
    if not tab:
        return False
    js = f"""(function(){{
      try {{ window.TradingViewApi.activeChart().setSymbol({json.dumps(symbol)}); return 'ok'; }}
      catch(e){{ return 'err:'+e.message; }}
    }})()"""
    try:
        return cdp_evaluate(tab["webSocketDebuggerUrl"], js, timeout=8) == "ok"
    except Exception as e:
        logger.debug(f"set_chart_symbol({symbol}) failed: {e}")
        return False


def watchlist_add(symbol: str, list_id: int | None = None) -> bool:
    """
    Append `symbol` to the APEX watchlist. Resolves the bare ticker to its exchange-qualified
    form (NASDAQ:XXX) via the live quote session when possible, then POSTs to TV's REST API
    with the page's own cookies. Returns True on HTTP-ok.
    """
    list_id = list_id or cfg.APEX_WATCHLIST_ID
    if not list_id:
        return False
    tab = get_quote_tab()
    if not tab:
        return False
    js = f"""(function(){{
      return new Promise(function(resolve){{
        try {{
          var sym={json.dumps(symbol)};
          // resolve to exchange-qualified (NASDAQ:XXX) via the quote session cache if present
          try {{
            var sd=window.getQuoteSessionInstance()._symbol_data||{{}};
            for (var k in sd) {{
              var v=sd[k]&&sd[k].values;
              if (v && v.pro_name && (v.short_name===sym || v.pro_name.split(':').pop()===sym)) {{
                sym=v.pro_name; break;
              }}
            }}
          }} catch(e) {{}}
          fetch('/api/v1/symbols_list/custom/{list_id}/append/', {{
            method:'POST', credentials:'include',
            headers:{{'Content-Type':'application/json','Accept':'application/json'}},
            body: JSON.stringify([sym])
          }}).then(function(r){{ resolve(r.ok ? 'ok' : 'http'+r.status); }})
            .catch(function(e){{ resolve('err:'+e.message); }});
        }} catch(e) {{ resolve('err:'+e.message); }}
      }});
    }})()"""
    try:
        r = cdp_evaluate(tab["webSocketDebuggerUrl"], js, timeout=10)
        if r == "ok":
            logger.info(f"watchlist_add {symbol} → APEX ({list_id})")
            return True
        logger.debug(f"watchlist_add {symbol} → {r}")
        return False
    except Exception as e:
        logger.debug(f"watchlist_add({symbol}) failed: {e}")
        return False


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) >= 3 and sys.argv[1] == "symbol":
        print("set_chart_symbol:", set_chart_symbol(sys.argv[2]))
    elif len(sys.argv) >= 3 and sys.argv[1] == "watch":
        print("watchlist_add:", watchlist_add(sys.argv[2]))
    else:
        print("usage: apex_tv_control.py symbol SYM   |   apex_tv_control.py watch SYM")
