#!/usr/bin/env python3
"""
TRI-CITY LEVEL LOCK — standalone, launchd-scheduled ORB level lock.

Runs independently of Claude Code (which only fires CronCreate jobs while
the session is idle, causing the level-lock cron to fire late or be skipped
on busy mornings). Connects directly to TradingView via CDP (port 9222,
same as tri_city_tv_poller.py), reads the Tri-City Inator scanner table,
locks ORH/ORL for every symbol, writes shared/tri-city-levels.json, and
launches the poller.

Multiple launchd entries fire this script at 8:36, 8:46, and 9:01 CT
(covering ORB_MINUTES = 5 / 15 / 30). Each invocation checks ORB_MINUTES
in .env and exits silently unless it's currently within the lock window
for that setting, and exits if today's levels are already locked.

Usage:
  python tri_city_level_lock.py [--force]
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import websocket as ws_client  # websocket-client

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

WORKSPACE   = Path.home() / "tri-city-inator"
SHARED      = WORKSPACE / "shared"
LOGS        = WORKSPACE / "logs"
SCRIPTS     = WORKSPACE / "scripts"
LEVELS_FILE = SHARED / "tri-city-levels.json"

CT          = ZoneInfo("America/Chicago")
CDP_HOST    = "localhost"
CDP_PORT    = 9222
STUDY_FILTER = "Inator"

MARKET_OPEN_H, MARKET_OPEN_M = 8, 30
LOCK_WINDOW_MINUTES = 10  # how long after the target ORB time we'll still lock

LOGS.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(LOGS / "tri-city-level-lock.log")],
)
logger = logging.getLogger("level_lock")


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


def find_tv_target() -> dict | None:
    try:
        resp = requests.get(f"http://{CDP_HOST}:{CDP_PORT}/json/list", timeout=5)
        for t in resp.json():
            if t.get("type") == "page" and "tradingview.com/chart" in t.get("url", "").lower():
                return t
    except Exception as e:
        logger.warning(f"CDP target list failed: {e}")
    return None


def _drain_until_id(ws, target_id: int, max_msgs: int = 100) -> dict | None:
    for _ in range(max_msgs):
        try:
            msg = json.loads(ws.recv())
            if msg.get("id") == target_id:
                return msg
        except Exception:
            break
    return None


def cdp_evaluate(ws_url: str, expression: str, timeout: int = 20) -> object:
    ws = ws_client.create_connection(
        ws_url, timeout=timeout, host=f"{CDP_HOST}:{CDP_PORT}", suppress_origin=True,
    )
    ws.settimeout(timeout)
    try:
        ws.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": False},
        }))
        msg = _drain_until_id(ws, 1)
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


def process_pine_tables(raw: list) -> list[str] | None:
    if not raw:
        return None
    for study in raw:
        tables: dict = {}
        for item in study.get("items", []):
            v = item["raw"]
            tid, row, col, text = v.get("tid", 0), v.get("row", 0), v.get("col", 0), v.get("t", "")
            tables.setdefault(tid, {}).setdefault(row, {})[col] = text
        for tid in sorted(tables):
            rows = tables[tid]
            formatted = []
            for rn in sorted(rows):
                cols = rows[rn]
                row_str = " | ".join(cols[cn] for cn in sorted(cols) if cols[cn])
                if row_str:
                    formatted.append(row_str)
            if formatted:
                return formatted
    return None


def is_scanner_table(rows: list[str] | None) -> bool:
    if not rows:
        return False
    header = rows[0].upper()
    return header.startswith("SYMBOL") and "PRICE" in header and "RSI" in header


def _orh_orl(s: str) -> tuple[float, float]:
    try:
        a, b = s.split("/")
        return float(a.strip()), float(b.strip())
    except Exception:
        return 0.0, 0.0


def in_lock_window(orb_minutes: int, now: datetime) -> bool:
    target = now.replace(hour=MARKET_OPEN_H, minute=MARKET_OPEN_M, second=0, microsecond=0) + timedelta(minutes=orb_minutes)
    return target <= now <= target + timedelta(minutes=LOCK_WINDOW_MINUTES)


def already_locked_today(now: date) -> bool:
    if not LEVELS_FILE.exists():
        return False
    try:
        data = json.loads(LEVELS_FILE.read_text())
        return data.get("_date") == now.isoformat() and len(data) > 1
    except Exception:
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Skip time-window and already-locked checks")
    args = parser.parse_args()

    if load_dotenv:
        load_dotenv(WORKSPACE / ".env")

    import os
    orb_minutes = int(os.environ.get("ORB_MINUTES", "15"))
    now = datetime.now(CT)

    if not args.force:
        if now.weekday() > 4:
            logger.info("Weekend — skipping")
            return
        if already_locked_today(now.date()):
            logger.info("Levels already locked for today — skipping")
            return
        if not in_lock_window(orb_minutes, now):
            logger.info(f"Outside lock window for ORB_MINUTES={orb_minutes} (now={now.strftime('%H:%M')}) — skipping")
            return

    target = find_tv_target()
    if not target:
        logger.warning("No TradingView chart target found on CDP port 9222 — cannot lock levels")
        return

    raw = cdp_evaluate(target["webSocketDebuggerUrl"], _PINE_TABLE_JS)
    rows = process_pine_tables(raw) if raw else None
    if not is_scanner_table(rows):
        logger.warning("Pine table read failed or wrong table — cannot lock levels")
        return

    levels = {"_date": now.date().isoformat()}
    for row in rows[1:]:  # skip header
        cols = [c.strip() for c in row.split("|")]
        if len(cols) < 6:
            continue
        symbol = cols[0]
        if symbol in ("---", "", "SYMBOL"):
            continue
        orh, orl = _orh_orl(cols[5])
        if orh > 0 and orl > 0:
            levels[symbol] = {"orh": orh, "orl": orl}

    LEVELS_FILE.write_text(json.dumps(levels, indent=2))
    logger.info(f"Locked levels for {len(levels) - 1} symbols (ORB_MINUTES={orb_minutes}) -> {LEVELS_FILE}")

    try:
        subprocess.run(["bash", str(SCRIPTS / "start_poller.sh")], check=False)
        logger.info("Poller launch requested")
    except Exception as e:
        logger.warning(f"Failed to launch poller: {e}")


if __name__ == "__main__":
    main()
