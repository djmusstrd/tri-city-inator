#!/usr/bin/env python3
"""
SANDBOX POLLER — Main orchestration loop for the 3-strategy sandbox.

Runs every POLL_INTERVAL seconds (default 120s = 2 min).
Active 9:30 AM – 2:50 PM ET on weekdays.

Loop:
  1. Read signals from SANDBOX TV tab via CDP
  2. For each new LONG signal: run sandbox_execute
  3. For each new EXIT signal + open positions: run sandbox_position_manager
  4. Write shared/sandbox-summary.json

PID: shared/sandbox-poller.pid
Log: logs/sandbox-poller.log
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path.home() / "tri-city-inator"
sys.path.insert(0, str(WORKSPACE))
LOGS = WORKSPACE / "logs"
LOGS.mkdir(parents=True, exist_ok=True)

try:
    from dotenv import load_dotenv
    _env = WORKSPACE / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

ET       = ZoneInfo("America/New_York")
PID_FILE = WORKSPACE / "shared" / "sandbox-poller.pid"
SUMMARY  = WORKSPACE / "shared" / "sandbox-summary.json"
STATE_FILE = WORKSPACE / "shared" / "sandbox-state.json"

DEFAULT_POLL_INTERVAL = 120  # 2 minutes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOGS / "sandbox-poller.log")],
    force=True,
)
logger = logging.getLogger(__name__)
for _lib in ("urllib3", "requests", "websocket"):
    logging.getLogger(_lib).setLevel(logging.CRITICAL)


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"date": "", "last_order_tms": {"QQQ": -1, "SPY": -1, "IWM": -1},
            "daily_pnl": 0.0, "positions": {},
            "executed_today": [], "stopped_today": []}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _is_market_hours() -> bool:
    now = datetime.now(ET)
    if now.weekday() >= 5:  # Saturday / Sunday
        return False
    open_  = now.replace(hour=9,  minute=30, second=0, microsecond=0)
    close_ = now.replace(hour=14, minute=50, second=0, microsecond=0)
    return open_ <= now <= close_


def _write_summary(state: dict, cycle: int, last_signals: list) -> None:
    try:
        SUMMARY.write_text(json.dumps({
            "ts":            datetime.now(ET).isoformat(),
            "cycle":         cycle,
            "daily_pnl":     state.get("daily_pnl", 0.0),
            "open_positions": list(state.get("positions", {}).keys()),
            "executed_today": state.get("executed_today", []),
            "last_signals":  [
                {"symbol": s.symbol, "direction": s.direction,
                 "strategy": s.strategy, "label": s.signal_id}
                for s in last_signals
            ],
        }, indent=2))
    except Exception:
        pass


def run(poll_interval: int = DEFAULT_POLL_INTERVAL, dry_run: bool = False) -> None:
    # Insert scripts dir so sibling imports resolve whether run as module or direct
    scripts_dir = str(WORKSPACE / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from sandbox_signal_reader import read_signals
    from sandbox_execute import execute_signal
    from sandbox_position_manager import manage

    # Write PID
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))
    logger.info(f"Sandbox poller started | PID {os.getpid()} | interval={poll_interval}s dry_run={dry_run}")

    # Graceful shutdown
    _running = [True]
    def _stop(sig, frame):
        logger.info("Shutdown signal received")
        _running[0] = False
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    state = _load_state()
    cycle = 0

    while _running[0]:
        cycle += 1
        now_et = datetime.now(ET)
        today  = now_et.strftime("%Y-%m-%d")

        # Idle outside market hours
        if not _is_market_hours():
            logger.info(f"[cycle {cycle}] Outside market hours — sleeping {poll_interval}s")
            time.sleep(poll_interval)
            continue

        try:
            # Reset state on new day
            if state.get("date") != today:
                state.update({"date": today, "daily_pnl": 0.0,
                              "last_order_tms": {"QQQ": -1, "SPY": -1, "IWM": -1},
                              "executed_today": [], "stopped_today": []})
                _save_state(state)
                logger.info("New day — state reset")

            last_order_tms = state.get("last_order_tms", {"QQQ": -1, "SPY": -1, "IWM": -1})

            # Read signals from TV
            signals, new_tms = read_signals(last_order_tms)
            state["last_order_tms"] = new_tms
            _save_state(state)

            if signals:
                logger.info(f"[cycle {cycle}] {len(signals)} signal(s): "
                            f"{[(s.symbol, s.direction, s.signal_id) for s in signals]}")
            else:
                logger.info(f"[cycle {cycle}] No new signals")

            # Execute new LONG signals
            long_signals = [s for s in signals if s.direction == "long"]
            for sig in long_signals:
                try:
                    execute_signal(sig, state, dry_run=dry_run)
                except Exception as e:
                    logger.error(f"execute_signal error for {sig.symbol}: {e}")

            # Manage positions (exits + EOD)
            exit_signals = [s for s in signals if s.direction == "exit"]
            try:
                state = manage(exit_signals, state, dry_run=dry_run)
            except Exception as e:
                logger.error(f"manage error: {e}")

            _write_summary(state, cycle, signals)

        except Exception as e:
            logger.error(f"[cycle {cycle}] Unhandled error: {e}", exc_info=True)

        time.sleep(poll_interval)

    # Cleanup
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    logger.info("Sandbox poller stopped")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--poll-interval", type=int, default=DEFAULT_POLL_INTERVAL)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(poll_interval=args.poll_interval, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
