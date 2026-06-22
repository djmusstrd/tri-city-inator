#!/usr/bin/env python3
"""
APEX Layer 2 — live intraday poller (Alpaca-native, NO TradingView/CDP dependency).

Loop:
  1. Read the Layer-1 leader watchlist (shared/apex-leaders.json)
  2. Fast cadence in the first hour after the open, slower after
  3. For each leader not yet traded: fetch today's regular-session 5-min bars,
     run the entry detector (ORB15 primary / VWAP_PB secondary), execute on a signal
  4. (TODO Layer 3) manage open positions via health monitor
  5. Persist state + summary

PID  : shared/apex-poller.pid     Log: logs/apex-poller.log
Modes: --dry-run        log + rationale + Telegram, no live orders
       --self-test [YYYY-MM-DD]   replay one past session through the detector (no market needed)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal as _signal
import sys
import time
from datetime import datetime, timedelta, time as dtime, date as ddate
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path.home() / "tri-city-inator"
sys.path.insert(0, str(WORKSPACE / "scripts"))

import apex_config as cfg
from apex_entry_engine import detect_entry
from apex_execute import execute
from apex_health import manage_positions, reconcile_with_broker, alert_fault
import apex_tv_quotes

import numpy as np
import pandas as pd

ET = ZoneInfo("America/New_York")
SESS_OPEN, SESS_CLOSE = dtime(9, 30), dtime(16, 0)

cfg.LOGS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    handlers=[logging.FileHandler(cfg.POLLER_LOG), logging.StreamHandler()],
                    force=True)
for _l in ("urllib3", "requests", "alpaca"):
    logging.getLogger(_l).setLevel(logging.CRITICAL)
logger = logging.getLogger("apex.poller")


# ── data ──────────────────────────────────────────────────────────────────────
def _data_client():
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"))


def fetch_intraday(symbols, the_day: ddate) -> dict:
    """Regular-session 5-min bars for `the_day`, keyed by symbol with a 'tod' column."""
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    dc = _data_client()
    start = datetime.combine(the_day, dtime(0, 0))
    end = datetime.combine(the_day + timedelta(days=1), dtime(0, 0))
    # Delayed-SIP guard: never request into the last SIP_DELAY_MIN minutes (basic plan rejects it)
    end = min(end, datetime.utcnow() - timedelta(minutes=cfg.SIP_DELAY_MIN))
    out = {}
    for i in range(0, len(symbols), 200):
        batch = symbols[i:i + 200]
        try:
            df = dc.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame(5, TimeFrameUnit.Minute),
                start=start, end=end, feed=cfg.DATA_FEED, adjustment="raw")).df
        except Exception as e:
            logger.warning(f"intraday batch {i} failed: {e}")
            continue
        if df.empty:
            continue
        for sym in batch:
            try:
                sub = df.xs(sym, level="symbol").copy()
            except KeyError:
                continue
            et_idx = sub.index.tz_convert(ET)
            sub["tod"] = et_idx.time
            sub = sub[(sub["tod"] >= SESS_OPEN) & (sub["tod"] < SESS_CLOSE)]
            if not sub.empty:
                out[sym] = sub
    return out


def fetch_daily(symbols, days=70) -> pd.DataFrame:
    # NOTE: must stay >= 50 trading days so classify_regime's 50-day SMA is
    # real, not NaN. days=70 calendar -> start now-90 cal -> ~64 trading days.
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame
    dc = _data_client()
    start = datetime.now() - timedelta(days=days + 20)
    return dc.get_stock_bars(StockBarsRequest(
        symbol_or_symbols=symbols, timeframe=TimeFrame.Day,
        start=start, feed="sip", adjustment="all")).df


def atr_from_daily(close_df: pd.DataFrame, length=14) -> float:
    h, l, c = close_df["high"], close_df["low"], close_df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    v = tr.rolling(length).mean().iloc[-1]
    return float(v) if not pd.isna(v) else 0.0


def live_watch_set(leaders, intraday, positions, executed=None, prefer=None) -> set:
    """
    Symbols worth a real-time quote this cycle: every open position (for health, always) + any
    operator-prioritized names + the not-yet-traded leaders sitting in the ORB-high CROSSING ZONE
    (just below to barely above the level — the imminent-breakout moment), ranked by closeness and
    capped at MAX_LIVE_QUOTES so TV streams them reliably as 'fast' symbols. A leader left out
    simply uses its delayed price for this cycle (graceful) and gets picked up next cycle.
    """
    watch = set(positions) | (set(prefer or []) & {l["symbol"] for l in leaders})
    skip = watch | set(executed or [])
    end_min = SESS_OPEN.hour * 60 + SESS_OPEN.minute + cfg.ORB_MINUTES
    orb_end = dtime(end_min // 60, end_min % 60)
    cands = []  # (distance_from_orb_high, symbol)
    for ld in leaders:
        sym = ld["symbol"]
        if sym in skip:
            continue
        bars = intraday.get(sym)
        if bars is None or bars.empty:
            continue
        g = bars.sort_values("tod")
        orb = g[g["tod"] < orb_end]
        if orb.empty:
            continue
        orb_high = float(orb["high"].max())
        if orb_high <= 0:
            continue
        last = float(g.iloc[-1]["close"])
        # crossing zone: -1.5% below to +0.7% above the ORB high (about to / just breaking)
        if orb_high * 0.985 <= last <= orb_high * 1.007:
            cands.append((abs(last / orb_high - 1.0), sym))
    cands.sort()
    room = max(0, cfg.MAX_LIVE_QUOTES - len(watch))
    watch.update(sym for _, sym in cands[:room])
    return watch


def fetch_live_quotes(symbols) -> dict:
    """Real-time TV quotes for the given symbols (hybrid trigger feed). {} on any failure."""
    if not cfg.USE_TV_QUOTES or not symbols:
        return {}
    try:
        q = apex_tv_quotes.get_quotes(list(symbols), wait_ms=cfg.TV_QUOTE_WAIT_MS)
        if q:
            logger.info(f"live quotes: {len(q)}/{len(symbols)} symbols from TV real-time")
        else:
            logger.debug("no live TV quotes (CDP/TV down?) — using delayed bars")
        return q
    except Exception as e:
        logger.warning(f"live quote fetch failed: {e} — using delayed bars")
        return {}


def classify_regime(daily: pd.DataFrame) -> str:
    """Layer 5 STUB: SPY vs 50-day SMA. Refined in Phase 4 (VIX, breadth)."""
    try:
        spy = daily.xs("SPY", level="symbol")["close"]
        # min_periods=30 is a safety net: if the window is ever short, degrade to
        # an SMA of available bars instead of silently NaN-ing into risk_off.
        sma = spy.rolling(50, min_periods=30).mean().iloc[-1]
        return "risk_on" if spy.iloc[-1] > sma else "risk_off"
    except Exception:
        return "unknown"


# ── state ─────────────────────────────────────────────────────────────────────
def load_state() -> dict:
    if cfg.STATE_FILE.exists():
        try:
            return json.loads(cfg.STATE_FILE.read_text())
        except Exception as e:
            # A corrupt state must NOT be silently discarded (that would forget every open
            # position + zero daily_pnl). Preserve it for diagnosis and alert loudly; the
            # startup reconcile then re-adopts any open broker positions as orphans.
            try:
                bad = cfg.STATE_FILE.with_name(cfg.STATE_FILE.name + ".corrupt")
                cfg.STATE_FILE.replace(bad)
                alert_fault("state_corrupt",
                            f"⚠️ APEX state file was corrupt ({e}); preserved as {bad.name}. "
                            f"Reconcile will recover open positions from the broker.")
            except Exception:
                pass
    return {"date": "", "daily_pnl": 0.0, "positions": {}, "executed_today": []}


def save_state(s: dict) -> None:
    # Atomic write: a kill mid-write must never truncate apex-state.json. Write a sibling temp
    # then os.replace (atomic on the same filesystem) so the live file is always complete JSON.
    tmp = cfg.STATE_FILE.with_name(cfg.STATE_FILE.name + ".tmp")
    tmp.write_text(json.dumps(s, indent=2))
    os.replace(tmp, cfg.STATE_FILE)


def load_leaders() -> list:
    if not cfg.LEADERS_FILE.exists():
        return []
    data = json.loads(cfg.LEADERS_FILE.read_text())
    return data.get("leaders", [])


# ── one detection+execution pass over the leaders ──────────────────────────────
def run_pass(leaders, intraday, daily, regime, state, dry_run, live_quotes=None) -> int:
    c = cfg.effective()
    lq = live_quotes or {}
    import apex_flags
    flags = apex_flags.load_flags()
    avoid = set(flags.get("avoid", {}))
    prefer = set(flags.get("prioritize", {}))
    strict = bool(flags.get("strict"))
    fired = 0
    for ld in leaders:
        sym = ld["symbol"]
        if sym in avoid:                       # operator blacklist — never trade
            continue
        if strict and sym not in prefer:        # strict mode: only prioritized names
            continue
        if sym in state.get("executed_today", []) or sym in state.get("positions", {}):
            continue
        bars = intraday.get(sym)
        if bars is None or bars.empty:
            continue
        live_price = lq.get(sym, {}).get("last")
        sig = detect_entry(sym, bars, orb_minutes=c["orb_minutes"], live_price=live_price)
        if sig is None:
            continue
        try:
            dsub = daily.xs(sym, level="symbol")
            atr = atr_from_daily(dsub, cfg.ATR_LEN)
        except Exception:
            dsub, atr = None, 0.0
        if execute(sig, ld.get("rs_pct", 0.0), atr, regime, state,
                   dry_run=dry_run, daily_bars=dsub, prioritized=(sym in prefer)):
            fired += 1
    save_state(state)
    return fired


# ── self-test: replay one past session (no live market) ────────────────────────
def self_test(day_str: str | None, dry_run=True) -> None:
    leaders = load_leaders()
    if not leaders:
        print("No leaders file — run apex_daily_filter.py first.")
        return
    # Sandbox: run_pass() persists via save_state(cfg.STATE_FILE); redirect it to a throwaway file
    # for the duration of the replay so a self-test can never overwrite the live apex-state.json.
    orig_state_file = cfg.STATE_FILE
    cfg.STATE_FILE = orig_state_file.parent / "apex-state-selftest.json"
    try:
        syms = [l["symbol"] for l in leaders]
        if day_str:
            day = datetime.strptime(day_str, "%Y-%m-%d").date()
        else:
            day = (datetime.now(ET) - timedelta(days=1)).date()
        print(f"APEX self-test | {len(syms)} leaders | session {day} | dry_run={dry_run}")
        daily = fetch_daily(syms + ["SPY"])
        regime = classify_regime(daily)
        intraday = fetch_intraday(syms, day)
        print(f"  intraday sessions returned for {len(intraday)} leaders | regime={regime}")
        state = {"date": str(day), "daily_pnl": 0.0, "positions": {}, "executed_today": []}
        fired = run_pass(leaders, intraday, daily, regime, state, dry_run=True)
        print(f"\n  {fired} entries detected:")
        for sym, p in state["positions"].items():
            print(f"    {sym:6s} {p['trigger']:7s} entry ${p['entry']:.2f} stop ${p['stop']:.2f} qty {p['qty']}")
        print(f"\n  rationale -> {cfg.RATIONALE_LOG}")
        print(f"  (state sandboxed to {cfg.STATE_FILE.name} — live apex-state.json untouched)")
    finally:
        cfg.STATE_FILE = orig_state_file


# ── live loop ──────────────────────────────────────────────────────────────────
_CLOCK_CACHE = {"ts": 0.0, "open": None}


def _market_open_now() -> bool:
    # Authoritative: the Alpaca clock — it knows holidays AND half-days, which the old
    # weekday+time heuristic did not (it ran on Juneteenth 6/19). Cached ~60s to avoid
    # hammering the API; falls back to the local heuristic only if the clock is unreachable.
    now_l = datetime.now(ET)
    if now_l.weekday() >= 5:
        return False                       # weekend: skip the API call entirely
    nowt = time.time()
    if _CLOCK_CACHE["open"] is not None and (nowt - _CLOCK_CACHE["ts"]) < 60:
        return _CLOCK_CACHE["open"]
    try:
        from apex_health import _trading_client
        client = _trading_client()
        if client is not None:
            is_open = bool(client.get_clock().is_open)
            _CLOCK_CACHE.update(ts=nowt, open=is_open)
            return is_open
    except Exception as e:
        logger.debug(f"clock check failed, using local heuristic: {e}")
    return SESS_OPEN <= now_l.time() < SESS_CLOSE


def _cadence() -> int:
    now = datetime.now(ET).time()
    if now < dtime(10, 30):
        return cfg.POLL_FAST
    try:                                    # EOD window — be responsive for carry deny/keep replies
        eh, em = (int(x) for x in cfg.EOD_CLOSE_ET.split(":"))
        if now >= dtime(eh, em):
            return cfg.POLL_FAST
    except Exception:
        pass
    return cfg.POLL_SLOW


def run_live(dry_run: bool) -> None:
    cfg.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    cfg.PID_FILE.write_text(str(os.getpid()))
    logger.info(f"APEX poller started | PID {os.getpid()} | dry_run={dry_run}")
    # Clear any leftover quote subscriptions so we start with a clean (light) load on the TV app
    if cfg.USE_TV_QUOTES:
        try:
            apex_tv_quotes.release_all()
        except Exception:
            pass

    running = [True]
    def _stop(s, f): running[0] = False; logger.info("shutdown signal")
    _signal.signal(_signal.SIGTERM, _stop)
    _signal.signal(_signal.SIGINT, _stop)

    def _sleep(secs: int) -> None:
        # Interruptible sleep: SIGTERM/SIGINT doesn't break time.sleep (PEP 475 retries it),
        # so poll running[] in 1s steps → prompt shutdown, no lingering duplicate poller.
        for _ in range(max(1, int(secs))):
            if not running[0]:
                return
            time.sleep(1)

    state = load_state()
    # Heal any state↔broker divergence left by a crash/restart BEFORE the loop trusts state
    # (e.g. a position that stopped out while the poller was down).
    try:
        if reconcile_with_broker(state, dry_run):
            save_state(state)
    except Exception as e:
        logger.error(f"startup reconcile failed: {e}", exc_info=True)
    daily = None
    regime = "unknown"
    last_daily_day = None
    intraday = None
    live_quotes = None
    was_open = False

    while running[0]:
        today = datetime.now(ET).date()
        if state.get("date") != str(today):
            # New session: keep open swing/position carries (Layer 3 graduation), age them,
            # and only reset the daily counters. Force-closes happen via the EOD health pass.
            carried = state.get("positions", {})
            for _p in carried.values():
                _p["days_held"] = _p.get("days_held", 0) + 1
                _p.pop("pending_deny", None)   # carry confirmed overnight → now a settled swing
            state = {"date": str(today), "daily_pnl": 0.0,
                     "positions": carried, "executed_today": []}
            save_state(state)

        if not _market_open_now():
            if was_open:
                # Session just closed (incl. an early half-day close). Run ONE final EOD pass so
                # open positions get their carry/close decision even though the local EOD wall
                # time may not have been reached. Broker stops still protect throughout.
                was_open = False
                try:
                    final = manage_positions(intraday or {}, state, regime, dry_run,
                                             live_quotes, force_eod=True)
                    if final:
                        save_state(state)
                        logger.info(f"final EOD pass: {final} positions actioned")
                except Exception as e:
                    logger.error(f"final EOD pass failed: {e}", exc_info=True)
                    alert_fault("final_eod", f"🚨 APEX final EOD pass failed: {e}")
            _sleep(cfg.POLL_SLOW)
            continue
        was_open = True

        leaders = load_leaders()
        syms = [l["symbol"] for l in leaders]
        if not syms:
            alert_fault("no_leaders", "⚠️ APEX has NO leaders for today — did apex_daily_filter.py run? Entries impossible.")
            _sleep(cfg.POLL_SLOW)
            continue

        try:
            reconciled = reconcile_with_broker(state, dry_run)
            if last_daily_day != today:
                daily = fetch_daily(syms + ["SPY"])
                regime = classify_regime(daily)
                if regime == "unknown":
                    alert_fault("regime_unknown",
                                "⚠️ APEX regime=unknown — SPY history insufficient/unreadable; longs gated.")
                last_daily_day = today
            intraday = fetch_intraday(syms, today)
            # Hybrid feed: real-time TV prices for positions + prioritized + breakout candidates
            import apex_flags
            _prefer = set(apex_flags.load_flags().get("prioritize", {}))
            watch = live_watch_set(leaders, intraday, state.get("positions", {}),
                                   state.get("executed_today", []), prefer=_prefer)
            live_quotes = fetch_live_quotes(watch)
            fired = run_pass(leaders, intraday, daily, regime, state, dry_run, live_quotes)
            # Layer 3 — recompute health, proactive-exit breakdowns, carry/close at EOD
            managed = manage_positions(intraday, state, regime, dry_run, live_quotes)
            # Inbound Telegram: apply any "deny/keep SYM" carry replies from the phone
            try:
                import apex_telegram
                apex_telegram.poll_replies(state)
            except Exception as e:
                logger.debug(f"telegram poll failed: {e}")
            save_state(state)
            logger.info(f"pass: {len(state['positions'])} open, "
                        f"{len(state['executed_today'])} done today, {fired} new, "
                        f"{managed} managed, {reconciled} reconciled | regime {regime}")
        except Exception as e:
            logger.error(f"pass error: {e}", exc_info=True)
            alert_fault("pass_error", f"🚨 APEX pass error: {e}")

        _sleep(_cadence())

    # Only remove the PID file if it's still OURS — a lingering old poller must never delete
    # the PID file a freshly-started one just wrote.
    try:
        if cfg.PID_FILE.exists() and cfg.PID_FILE.read_text().strip() == str(os.getpid()):
            cfg.PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    logger.info("APEX poller stopped")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", nargs="?", const="", default=None,
                    help="replay one past session (optional YYYY-MM-DD)")
    args = ap.parse_args()

    if not os.getenv("ALPACA_API_KEY"):
        print("ERROR: ALPACA_API_KEY not set")
        sys.exit(1)

    if args.self_test is not None:
        self_test(args.self_test or None, dry_run=True)
        return
    run_live(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
