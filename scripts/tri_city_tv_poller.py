#!/usr/bin/env python3
"""
TRI-CITY TV POLLER — Phase 3: Headless CDP polling daemon.

Bypasses Claude entirely. Connects directly to TradingView Desktop via
Chrome DevTools Protocol (port 9222), reads the Pine scanner table,
runs detect→execute→manage pipeline, and sends macOS notifications.

Claude is NOT in this loop. Claude is invoked only for:
  - POST_CUTOFF approvals (user types "yes SYMBOL" in Claude Code)
  - EOD summary (user opens Claude Code after close)

Usage:
  python tri_city_tv_poller.py [--poll-interval 180] [--start-after HH:MM]

PID written to: shared/tri-city-poller.pid
Log: logs/tri-city-tv-poller.log
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
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import websocket as ws_client  # websocket-client

# ── Paths ─────────────────────────────────────────────────────────────────────
WORKSPACE       = Path.home() / "tri-city-inator"
SHARED          = WORKSPACE / "shared"
LOGS            = WORKSPACE / "logs"
SCRIPTS         = WORKSPACE / "scripts"

TABLE_FILE      = SHARED / "tri-city-table.json"
SUMMARY_FILE    = SHARED / "tri-city-summary.json"
APPROVALS_FILE  = SHARED / "tri-city-pending-approvals.json"
PID_FILE        = SHARED / "tri-city-poller.pid"

CT              = ZoneInfo("America/Chicago")
CDP_HOST        = "localhost"
CDP_PORT        = 9222
DEFAULT_POLL    = 180       # seconds
STUDY_FILTER    = "Inator"

# Market hours (CT) — poller idles outside these
MARKET_OPEN_H, MARKET_OPEN_M   = 8,  30
MARKET_CLOSE_H, MARKET_CLOSE_M = 15, 5

# Intraday scan cadence — runs tri_city_intraday_scanner.py every 30 min after 9:30 AM CT
INTRADAY_SCAN_INTERVAL_S = 1800   # 30 minutes
INTRADAY_SCAN_START_H    = 9
INTRADAY_SCAN_START_M    = 30

_last_intraday_scan: datetime | None = None

LOGS.mkdir(parents=True, exist_ok=True)

# Load .env so Telegram vars are available when running via nohup
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(WORKSPACE / ".env")
except ImportError:
    pass

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Logging: file only (no StreamHandler — nohup stdout is also redirected to
# the same log file, which would cause every line to appear twice) ─────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOGS / "tri-city-tv-poller.log")],
    force=True,
)
logger = logging.getLogger(__name__)

# Silence noisy libraries
for _lib in ("urllib3", "requests", "websocket"):
    logging.getLogger(_lib).setLevel(logging.CRITICAL)


# ── CDP helpers ───────────────────────────────────────────────────────────────

def find_tv_target() -> dict | None:
    """Return the TradingView chart CDP target that hosts the Tri-City (Kbzkkm) scanner.

    One chart tab → return it directly (cheap hot path). Multiple chart tabs open →
    probe each and return the one whose Pine table is the Inator scanner, so a stray
    or extra chart tab can't make the poller read the wrong indicator's table (the
    silent wrong-table bug). Falls back to the first chart tab if none probe positive.
    """
    try:
        resp = requests.get(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=5)
        targets = resp.json()
    except Exception as e:
        logger.warning(f"CDP target list failed: {e}")
        return None
    charts = [t for t in targets
              if t.get("type") == "page"
              and "tradingview.com/chart" in t.get("url", "").lower()]
    if not charts:
        return None
    if len(charts) == 1:
        return charts[0]
    # More than one chart tab — pick the one that actually hosts the Inator scanner.
    for t in charts:
        ws = t.get("webSocketDebuggerUrl")
        if not ws:
            continue
        try:
            raw = cdp_evaluate(ws, _PINE_TABLE_JS)
            if is_scanner_table(process_pine_tables(raw) if raw else None):
                return t
        except Exception:
            continue
    logger.warning(f"{len(charts)} chart tabs open, none host the Inator scanner — using first")
    return charts[0]


def cdp_evaluate(ws_url: str, expression: str, timeout: int = 20) -> object:
    """
    Execute JS in TradingView via CDP WebSocket.
    Returns the JSON-deserialized return value, or None on failure.

    Chrome CDP rejects WebSocket connections with an Origin header — must suppress it.
    Runtime.enable is not required; Runtime.evaluate works immediately.
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
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
            },
        }))
        msg = _drain_until_id(ws, 1, max_msgs=100)
        if not msg:
            return None
        result = msg.get("result", {}).get("result", {})
        if result.get("type") == "object" and "value" in result:
            return result["value"]
        err = msg.get("result", {}).get("exceptionDetails")
        if err:
            logger.warning(f"CDP JS exception: {err.get('text', err)}")
        return None
    finally:
        try:
            ws.close()
        except Exception:
            pass


def _drain_until_id(ws, target_id: int, max_msgs: int = 50) -> dict | None:
    """Receive messages until we see one with the given id, return it."""
    for _ in range(max_msgs):
        try:
            raw = ws.recv()
            msg = json.loads(raw)
            if msg.get("id") == target_id:
                return msg
        except Exception:
            break
    return None


# ── Pine table JS (verbatim logic from MCP core/data.js buildGraphicsJS) ──────

_PINE_TABLE_JS = """
(function() {
  var chart = window.TradingViewApi._activeChartWidgetWV.value()._chartWidget;
  var panes = chart.model().panes();
  var results = [];
  for (var pi = 0; pi < panes.length; pi++) {
    var sources = panes[pi].dataSources();
    for (var si = 0; si < sources.length; si++) {
      var s = sources[si];
      var isTarget = false;
      try { if (s.id && s.id() === 'Kbzkkm') isTarget = true; } catch(e) {}
      if (!isTarget && s.metaInfo) {
        try {
          var meta = s.metaInfo();
          var name = meta.description || meta.shortDescription || '';
          if (name.indexOf('Inator') !== -1) isTarget = true;
        } catch(e) {}
      }
      if (!isTarget) continue;
      var g = s._graphics;
      if (!g || !g._primitivesCollection) continue;
      var pc = g._primitivesCollection;
      var items = [];
      try {
        var outer = pc.dwgtablecells;
        if (outer) {
          var inner = outer.get('tableCells');
          if (inner && inner._primitivesDataById && inner._primitivesDataById.size > 0) {
            inner._primitivesDataById.forEach(function(v, id) {
              items.push({id: String(id), raw: {tid: v.tid||0, row: v.row||0, col: v.col||0, t: v.t||''}});
            });
          }
        }
      } catch(e) {}
      var studyName = 'Tri-City Inator';
      try { var m = s.metaInfo(); studyName = m.description || m.shortDescription || studyName; } catch(e) {}
      if (items.length > 0) results.push({name: studyName, count: items.length, items: items});
    }
  }
  return results;
})()
"""


def process_pine_tables(raw: list) -> list[str] | None:
    """
    Convert raw CDP result into a list of formatted row strings.
    Returns the rows array from the FIRST matching study.
    Returns None if no data.
    """
    if not raw:
        return None

    # Mirror MCP getPineTables logic
    for study in raw:
        tables: dict = {}
        for item in study.get("items", []):
            v = item["raw"]
            tid = v.get("tid", 0)
            row = v.get("row", 0)
            col = v.get("col", 0)
            text = v.get("t", "")
            if tid not in tables:
                tables[tid] = {}
            if row not in tables[tid]:
                tables[tid][row] = {}
            tables[tid][row][col] = text

        for tid in sorted(tables):
            rows = tables[tid]
            formatted = []
            for rn in sorted(rows):
                cols = rows[rn]
                row_str = " | ".join(cols[cn] for cn in sorted(cols) if cols[cn])
                if row_str:
                    formatted.append(row_str)
            if formatted:
                return formatted  # return first study's first non-empty table

    return None


def is_scanner_table(rows: list[str] | None) -> bool:
    """
    True if `rows` looks like the Tri-City Inator scanner table
    (header: "SYMBOL | PRICE | RSI ..."). Used to detect when the CDP
    table-read accidentally captured a different indicator's table
    (e.g. a single-symbol HUD) after an indicator_set_inputs reload
    leaves Kbzkkm's table briefly empty.
    """
    if not rows:
        return False
    header = rows[0].upper()
    return header.startswith("SYMBOL") and "PRICE" in header and "RSI" in header


# ── macOS notifications ────────────────────────────────────────────────────────

def notify(title: str, body: str, sound: str = "Ping") -> None:
    """Send a macOS desktop notification."""
    script = (
        f'display notification "{_esc(body)}" '
        f'with title "{_esc(title)}" '
        f'sound name "{sound}"'
    )
    subprocess.run(["osascript", "-e", script], capture_output=True)


def _esc(s: str) -> str:
    return s.replace('"', '\\"').replace("\\", "\\\\")


# ── Telegram notifications ─────────────────────────────────────────────────────

def send_telegram(msg: str) -> None:
    """Send a message to the Tri-City Telegram bot. Silent on failure."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.parse, urllib.request as _ur
        url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}).encode()
        _ur.urlopen(_ur.Request(url, data=data), timeout=5)
    except Exception as _e:
        logger.debug(f"Telegram send failed: {_e}")


def notify_execution(e: dict) -> None:
    raw = e.get("raw", f"{e['symbol']} {e['setup']}")
    notify("✅ TRI-CITY TRADE", raw, sound="Glass")
    sym   = e.get("symbol", "?")
    stp   = e.get("setup", "?")
    px    = e.get("entry_price", e.get("price", 0))
    stop  = e.get("stop_loss", "?")
    t1    = e.get("target_1", "?")
    sh    = e.get("position_size", e.get("shares", "?"))
    cup   = " 🏆" if e.get("cup") else ""
    send_telegram(
        f"✅ <b>ENTRY — {sym}</b>{cup}\n"
        f"{stp} | {sh}sh @ ${px}\n"
        f"Stop: ${stop}  T1: ${t1}"
    )


def notify_post_cutoff(e: dict) -> None:
    sym  = e.get("symbol", "?")
    stp  = e.get("setup", "?")
    px   = e.get("price", 0)
    stop = e.get("stop", "N/A")
    rps  = e.get("risk_per_share", "N/A")
    sh   = e.get("shares", 0)
    cup  = "YES" if e.get("cup") else "NO"
    cut  = e.get("cutoff", "?")
    body = (
        f"{sym} {stp} @${px} | stop=${stop} | R/sh=${rps} | {sh}sh | "
        f"Cup:{cup} | after {cut} — type 'yes {sym}' in Claude Code to approve"
    )
    notify(f"⚠️ POST-CUTOFF: {sym}", body, sound="Sosumi")
    send_telegram(
        f"⚠️ <b>POST-CUTOFF: {sym}</b>\n"
        f"{stp} @${px} | {sh}sh | stop=${stop}\n"
        f"Type <code>yes {sym}</code> in Claude Code to approve"
    )


def notify_rvol_spike(e: dict) -> None:
    body = f"{e['symbol']} RVOL {e['prev']:.1f}x → {e['now']:.1f}x"
    notify("📈 RVOL SPIKE", body)
    send_telegram(f"📈 <b>RVOL SPIKE</b> — {e['symbol']}: {e['prev']:.1f}x → {e['now']:.1f}x")


def notify_position_event(msg: str) -> None:
    notify("📊 POSITION EVENT", msg)
    # Only send Telegram for exits and stop promotions — skip advisory-only messages
    keywords = ("EXIT", "CLOSED", "QUICK LOCK", "FREE RIDE", "T1 HIT", "T2 HIT",
                 "TRAIL EXIT", "PULLBACK FAIL", "PULLBACK TIMEOUT", "EMA8 CROSS EXIT")
    if any(kw in msg.upper() for kw in keywords):
        send_telegram(f"📊 <b>POSITION</b>\n{msg}")


# ── Pending approvals (POST_CUTOFF) ───────────────────────────────────────────

def write_pending_approval(e: dict) -> None:
    """Append a POST_CUTOFF event to pending-approvals.json."""
    existing: list = []
    if APPROVALS_FILE.exists():
        try:
            existing = json.loads(APPROVALS_FILE.read_text())
        except Exception:
            existing = []
    # Avoid duplicate entries for same symbol+setup
    key = (e.get("symbol"), e.get("setup"))
    existing = [x for x in existing if (x.get("symbol"), x.get("setup")) != key]
    existing.append({**e, "pending_at": datetime.now(CT).strftime("%H:%M CT")})
    APPROVALS_FILE.write_text(json.dumps(existing, indent=2))


def clear_pending_approval(symbol: str) -> None:
    """Remove a symbol from pending approvals (after user approves)."""
    if not APPROVALS_FILE.exists():
        return
    try:
        existing = json.loads(APPROVALS_FILE.read_text())
        existing = [x for x in existing if x.get("symbol") != symbol]
        APPROVALS_FILE.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass


# ── Market hours ──────────────────────────────────────────────────────────────

def is_market_hours() -> bool:
    now = datetime.now(CT)
    if now.weekday() >= 5:          # Saturday=5, Sunday=6
        return False
    minutes = now.hour * 60 + now.minute
    open_min  = MARKET_OPEN_H  * 60 + MARKET_OPEN_M
    close_min = MARKET_CLOSE_H * 60 + MARKET_CLOSE_M
    return open_min <= minutes <= close_min


def seconds_until_open() -> float:
    now = datetime.now(CT)
    open_today = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0)
    if now >= open_today:
        return 0
    return (open_today - now).total_seconds()


# ── One poll cycle ────────────────────────────────────────────────────────────

def run_cycle() -> None:
    now_str = datetime.now(CT).strftime("%H:%M CT")
    logger.info(f"── Cycle {now_str} ─────────────────────────")

    # (a) Read Pine table via CDP
    target = find_tv_target()
    if not target:
        logger.warning("No TradingView chart target found on CDP port 9222 — skipping cycle")
        return

    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        logger.warning("No webSocketDebuggerUrl in target — skipping cycle")
        return

    raw = cdp_evaluate(ws_url, _PINE_TABLE_JS)
    rows = process_pine_tables(raw) if raw else None

    # If the Inator table came back empty/wrong-shaped (e.g. right after an
    # indicator_set_inputs reload, Kbzkkm's table can take a moment to
    # repopulate and the CDP read falls back to a different indicator's
    # table), retry once after a short delay before giving up.
    if not is_scanner_table(rows):
        time.sleep(2)
        raw = cdp_evaluate(ws_url, _PINE_TABLE_JS)
        rows = process_pine_tables(raw) if raw else None

    # (b) Write tri-city-table.json only when we have a valid scanner table;
    # otherwise keep the last-known-good table.json so the detector doesn't
    # silently run on garbage data. Position management still runs regardless.
    if is_scanner_table(rows):
        TABLE_FILE.write_text(json.dumps(rows))
        logger.info(f"Wrote table.json: {len(rows)} rows")
    elif rows:
        logger.warning(
            f"Pine table read returned wrong indicator ({len(rows)} rows, "
            f"header={rows[0]!r}) — keeping stale table.json"
        )
    else:
        logger.warning("Pine table returned no rows — signal detection skipped; position management will still run")

    # (c) Run tri_city_monitor.py (detect + execute + manage + writes summary.json)
    rc = subprocess.run(
        ["python", "-W", "ignore", str(SCRIPTS / "tri_city_monitor.py")],
        capture_output=True,
    ).returncode
    if rc != 0:
        logger.error(f"tri_city_monitor.py exited {rc}")

    # (c2) Intraday scan — every 30 min after 9:30 AM CT
    global _last_intraday_scan
    cycle_now = datetime.now(CT)
    intraday_start = cycle_now.replace(
        hour=INTRADAY_SCAN_START_H, minute=INTRADAY_SCAN_START_M,
        second=0, microsecond=0,
    )
    if cycle_now >= intraday_start:
        elapsed = (
            (cycle_now - _last_intraday_scan).total_seconds()
            if _last_intraday_scan else INTRADAY_SCAN_INTERVAL_S + 1
        )
        if elapsed >= INTRADAY_SCAN_INTERVAL_S:
            logger.info("Running intraday scanner (30-min refresh)...")
            scan_source = "intraday_1130" if cycle_now.hour >= 11 and cycle_now.minute >= 30 else "intraday_930"
            scan_rc = subprocess.run(
                ["python", "-W", "ignore",
                 str(SCRIPTS / "tri_city_intraday_scanner.py"), "--source", scan_source],
                capture_output=True,
            ).returncode
            if scan_rc == 0:
                _last_intraday_scan = cycle_now
                logger.info("Intraday scan complete")
            else:
                logger.error(f"Intraday scanner exited {scan_rc}")

    # (d) Read summary.json and send notifications
    if not SUMMARY_FILE.exists():
        logger.warning("tri-city-summary.json missing after monitor run")
        return

    try:
        summary = json.loads(SUMMARY_FILE.read_text())
    except Exception as e:
        logger.error(f"summary.json parse error: {e}")
        return

    executions      = summary.get("executions", [])
    post_cutoffs    = summary.get("post_cutoff", [])
    rvol_spikes     = summary.get("rvol_spikes", [])
    position_events = summary.get("position_events", [])
    resistance_flags= summary.get("resistance_flags", [])
    errors          = summary.get("errors", [])

    for e in executions:
        logger.info(f"EXEC: {e.get('raw', e)}")
        notify_execution(e)

    for e in post_cutoffs:
        logger.info(f"POST_CUTOFF: {e.get('symbol')} {e.get('setup')} @${e.get('price')}")
        write_pending_approval(e)
        notify_post_cutoff(e)

    for e in rvol_spikes:
        logger.info(f"RVOL SPIKE: {e['symbol']} {e['prev']:.1f}x → {e['now']:.1f}x")
        notify_rvol_spike(e)

    for sym in resistance_flags:
        logger.info(f"RESISTANCE: {sym} near 52-wk high")
        notify("⚠️ RESISTANCE", f"{sym} near 52-wk high — caution only")

    for msg in position_events:
        logger.info(f"POS_EVENT: {msg}")
        notify_position_event(msg)

    for err in errors:
        logger.error(f"EXEC_ERROR: {err}")
        notify("❌ EXECUTION ERROR", err, sound="Basso")
        send_telegram(f"❌ <b>EXECUTION ERROR</b>\n{err}")

    total_actionable = len(executions) + len(post_cutoffs) + len(rvol_spikes) + len(position_events) + len(errors)
    logger.info(
        f"Done — {len(executions)} exec | {len(post_cutoffs)} post-cutoff | "
        f"{len(rvol_spikes)} spikes | {len(position_events)} pos-events | "
        f"{len(resistance_flags)} resistance | {len(errors)} errors"
    )
    if total_actionable == 0:
        logger.info("(silent — no actionable events)")


# ── EOD Telegram summary ──────────────────────────────────────────────────────

def _send_eod_telegram_summary() -> None:
    """Send daily P&L summary to Telegram when the poller shuts down at 3:05 PM CT."""
    try:
        journal_path = WORKSPACE / "logs" / "tri-city-journal.json"
        if not journal_path.exists():
            return
        journal = json.loads(journal_path.read_text())
        today = date.today().isoformat()
        trades = [t for t in journal if t.get("date") == today]
        if not trades:
            send_telegram("📋 <b>TRI-CITY EOD</b>\nNo trades today.")
            return

        total_pnl = sum(t.get("realized_pnl", 0) for t in trades)
        wins   = [t for t in trades if t.get("outcome") in ("full_win", "partial_win")]
        losses = [t for t in trades if t.get("outcome") == "loss"]
        scratches = [t for t in trades if t.get("outcome") == "scratch"]
        pnl_sign = "🟢" if total_pnl >= 0 else "🔴"

        lines = [f"{pnl_sign} <b>TRI-CITY EOD — {today}</b>",
                 f"Net P&amp;L: <b>${total_pnl:+.2f}</b> | {len(trades)} trades "
                 f"({len(wins)}W / {len(scratches)}S / {len(losses)}L)"]
        for t in trades:
            pnl   = t.get("realized_pnl", 0)
            sym   = t.get("symbol", "?")
            stp   = t.get("setup", "?")
            sign  = "✅" if pnl > 0 else ("⚪" if abs(pnl) < 50 else "🔴")
            lines.append(f"  {sign} {sym} {stp}: ${pnl:+.2f}")

        send_telegram("\n".join(lines))
    except Exception as e:
        logger.debug(f"EOD Telegram summary failed: {e}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL,
                        help=f"Seconds between polls (default {DEFAULT_POLL})")
    parser.add_argument("--start-after", type=str, default=None,
                        help="Don't start polling until HH:MM CT (e.g. 08:45)")
    parser.add_argument("--once", action="store_true",
                        help="Run one cycle and exit (used by webhook server)")
    args = parser.parse_args()

    # --once: single cycle, no PID file, no loop
    if args.once:
        try:
            run_cycle()
        except Exception as e:
            logger.exception(f"Cycle error: {e}")
            sys.exit(1)
        sys.exit(0)

    # Write PID file
    PID_FILE.write_text(str(os.getpid()))

    # Graceful shutdown on SIGTERM/SIGINT
    def _shutdown(sig, frame):
        logger.info(f"Received signal {sig} — shutting down poller")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    logger.info(
        f"TRI-CITY TV POLLER started | PID {os.getpid()} | "
        f"poll every {args.poll_interval}s | CDP localhost:{CDP_PORT}"
    )

    # Optional delayed start
    if args.start_after:
        now = datetime.now(CT)
        try:
            hh, mm = map(int, args.start_after.split(":"))
            start_time = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            wait = (start_time - now).total_seconds()
            if wait > 0:
                logger.info(f"Waiting {wait:.0f}s until {args.start_after} CT to begin polling")
                time.sleep(wait)
        except ValueError:
            logger.warning(f"Invalid --start-after value: {args.start_after} — starting immediately")

    while True:
        now = datetime.now(CT)

        # Weekday market hours check
        if now.weekday() >= 5:
            logger.info("Weekend — sleeping 1h")
            time.sleep(3600)
            continue

        minutes_now = now.hour * 60 + now.minute
        close_min   = MARKET_CLOSE_H * 60 + MARKET_CLOSE_M
        open_min    = MARKET_OPEN_H  * 60 + MARKET_OPEN_M

        if minutes_now > close_min:
            logger.info(f"Market closed ({now.strftime('%H:%M CT')}) — poller shutting down")
            _send_eod_telegram_summary()
            PID_FILE.unlink(missing_ok=True)
            sys.exit(0)

        if minutes_now < open_min:
            wait = (open_min - minutes_now) * 60
            logger.info(f"Pre-market — sleeping {wait}s until {MARKET_OPEN_H:02d}:{MARKET_OPEN_M:02d} CT")
            time.sleep(wait)
            continue

        # Run cycle
        try:
            run_cycle()
        except Exception as e:
            logger.exception(f"Cycle error (non-fatal): {e}")

        time.sleep(args.poll_interval)


if __name__ == "__main__":
    main()
