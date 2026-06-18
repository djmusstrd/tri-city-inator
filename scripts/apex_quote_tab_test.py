#!/usr/bin/env python3
"""
APEX — dedicated-quote-tab isolation test.

Tests the hypothesis behind the "safest fix": does streaming quotes on a SEPARATE TradingView
chart tab isolate the streaming load from the user's interactive chart? If yes, the poller can
stream a large set (toward the full universe) on its own tab without lagging the chart you work
on. If no, the load is app-global and a dedicated tab won't help.

Method:
  1. find the existing (user) chart tab, baseline its CDP eval latency (idle),
  2. open a fresh DEDICATED chart tab (or use --ws-url if you opened one),
  3. stream increasing symbol batches on the dedicated tab (real apex_tv_quotes path),
  4. re-measure the USER tab's eval latency UNDER LOAD after each batch,
  5. report coverage + latency delta, then CLOSE the dedicated tab it opened.

Read:
  user-tab latency ~flat as the dedicated tab's load grows  -> ISOLATED  (dedicated tab is safe;
      scales toward a full-universe scan)
  user-tab latency climbs with the dedicated tab's load     -> APP-GLOBAL (dedicated tab won't
      fix the hang; need a different approach)

Usage:
  python -W ignore scripts/apex_quote_tab_test.py [--max 150] [--ws-url WS] [--keep]
"""

from __future__ import annotations

import argparse
import json
import statistics as st
import sys
import time
from pathlib import Path

import requests

WORKSPACE = Path.home() / "tri-city-inator"
SHARED = WORKSPACE / "shared"
sys.path.insert(0, str(WORKSPACE / "scripts"))
sys.path.insert(0, str(WORKSPACE))

import apex_tv_quotes as tq  # cdp_evaluate, get_quotes, release_all, _tabs

CDP = "http://localhost:9222"
CHART_URL = "https://www.tradingview.com/chart/"


def _ms(fn, n=5):
    """median ms of n calls of fn(); returns (median, max)."""
    xs = []
    for _ in range(n):
        t0 = time.perf_counter()
        try:
            fn()
        except Exception:
            xs.append(float("nan"))
            continue
        xs.append((time.perf_counter() - t0) * 1000)
    good = [x for x in xs if x == x]
    return (st.median(good) if good else float("nan"),
            max(good) if good else float("nan"))


def _probe(ws):
    return lambda: tq.cdp_evaluate(ws, "1+1", timeout=8)


def load_syms(n):
    p = SHARED / "apex-leaders.json"
    if not p.exists():
        return ["AAPL", "NVDA", "TSLA", "SPY", "QQQ", "AMD", "META", "MSFT"][:n]
    d = json.loads(p.read_text())
    return [l["symbol"] for l in d.get("leaders", [])[:n]]


def open_tab():
    """Open a dedicated chart tab via CDP; return (target_id, ws_url) or (None, None)."""
    for verb in (requests.put, requests.get):
        try:
            r = verb(f"{CDP}/json/new?{CHART_URL}", timeout=8)
            if r.status_code in (200, 201):
                j = r.json()
                return j.get("id"), j.get("webSocketDebuggerUrl")
        except Exception:
            continue
    return None, None


def close_tab(tid):
    try:
        requests.get(f"{CDP}/json/close/{tid}", timeout=8)
        return True
    except Exception:
        return False


def wait_quote_session(ws, timeout_s=25):
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        try:
            if tq.cdp_evaluate(ws, tq._PROBE_QSI, timeout=6) is True:
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=150, help="largest symbol batch to stream")
    ap.add_argument("--ws-url", help="use an already-open dedicated tab's ws url (skip auto-open)")
    ap.add_argument("--user-ws-url", help="pin the USER (interactive) tab ws url to measure")
    ap.add_argument("--keep", action="store_true", help="don't close the dedicated tab after")
    args = ap.parse_args()

    # ── user tab ──
    chart_tabs = tq._tabs()
    if not chart_tabs:
        print("No TradingView chart tab found — is TV up with CDP on 9222?")
        sys.exit(1)
    if args.user_ws_url:
        user_ws = args.user_ws_url
        print("user tab   : (pinned via --user-ws-url)")
    else:
        user = chart_tabs[0]
        user_ws = user["webSocketDebuggerUrl"]
        print(f"user tab   : {user.get('url','')[:46]}")

    base_med, base_max = _ms(_probe(user_ws))
    print(f"baseline user-tab eval latency: median {base_med:.0f}ms  max {base_max:.0f}ms\n")

    # ── dedicated tab ──
    opened_id = None
    if args.ws_url:
        ded_ws = args.ws_url
        print("dedicated  : (provided ws-url)")
    else:
        print("opening dedicated chart tab…")
        opened_id, ded_ws = open_tab()
        if not ded_ws:
            print("  ✗ could not open a tab via CDP /json/new (TV desktop may block it).")
            print("    → open a 2nd TV chart tab manually, then re-run with --ws-url <wsUrl>")
            print("      (find it: curl -s localhost:9222/json/list | grep webSocketDebuggerUrl)")
            sys.exit(2)
        print(f"  opened target {opened_id[:12]}… waiting for quote session…")
        if not wait_quote_session(ded_ws):
            print("  ✗ dedicated tab never exposed a quote session; closing.")
            if opened_id and not args.keep:
                close_tab(opened_id)
            sys.exit(2)
        print("  ✓ dedicated tab quote session ready\n")

    # ── scaling load test ──
    batches = [b for b in (25, 50, 100, args.max) if b <= args.max]
    batches = sorted(set(batches))
    print(f"  {'BATCH':>6} {'LIVE':>6} {'COVER':>6}  {'USER-LAT median/max':>22}  VERDICT")
    print("  " + "-" * 62)
    worst = base_med
    try:
        for b in batches:
            syms = load_syms(b)
            t0 = time.perf_counter()
            q = tq.get_quotes(syms, wait_ms=3000, ws_url=ded_ws)
            read_s = time.perf_counter() - t0
            live = len(q)
            cover = 100 * live / max(1, len(syms))
            # user-tab latency immediately after kicking the dedicated stream
            med, mx = _ms(_probe(user_ws))
            worst = max(worst, med)
            delta = med - base_med
            flag = "ok" if delta < base_med * 1.5 + 60 else "⚠ LAG"
            print(f"  {b:>6} {live:>6} {cover:>5.0f}%  {med:>9.0f}ms /{mx:>7.0f}ms   {flag}"
                  f"  (read {read_s:.1f}s)")
    finally:
        # cleanup: release the dedicated session + close the tab we opened
        try:
            tq.release_all(ws_url=ded_ws)
        except Exception:
            pass
        if opened_id and not args.keep:
            close_tab(opened_id)
            print("\n  (closed the dedicated test tab)")

    print("\n  " + "=" * 62)
    print(f"  baseline user-tab latency : {base_med:.0f}ms")
    print(f"  worst under dedicated load: {worst:.0f}ms")
    if worst < base_med * 1.5 + 60:
        print("  → ISOLATED: user tab stayed responsive while the dedicated tab streamed.")
        print("    Dedicated-quote-tab fix is SAFE and can scale toward a full-universe scan.")
    else:
        print("  → APP-GLOBAL: the dedicated tab's load bled into the user tab.")
        print("    A dedicated tab will NOT fix the hang — load is shared across the TV app.")
    print("  " + "=" * 62)


if __name__ == "__main__":
    main()
