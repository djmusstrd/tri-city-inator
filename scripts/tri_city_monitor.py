#!/usr/bin/env python3
"""
TRI-CITY MONITOR — Standalone pipeline runner (Phase 2 bloat fix).

Called every 3–5 min by the signal-monitor cron (one Bash call).
Runs the full detect → execute → manage pipeline and writes
tri-city-summary.json with ONLY actionable events.

Produces ZERO stdout output — all logs go to logs/tri-city-monitor.log.

Claude's cron does only 3 things:
  (a) Read Pine table → write tri-city-table.json
  (b) Bash: python tri_city_monitor.py   (this script)
  (c) Read tri-city-summary.json → report if non-empty, else silent

Pipeline (inside this script):
  1. Validate tri-city-table.json is fresh (< 5 min old)
  2. Run tri_city_signal_detector.py
  3. Read tri-city-signals.json
  4. For each signal: run tri_city_execute.py --quiet, capture 1-line output
  5. Run tri_city_position_manager.py, capture output
  6. Write tri-city-summary.json

Summary schema:
{
  "timestamp":        "HH:MM CT",
  "executions":       [{"symbol", "setup", "cup", "htf", "raw"}],
  "post_cutoff":      [{"symbol", "setup", "price", "orh", "stop",
                        "risk_per_share", "shares", "cup", "cutoff", "raw"}],
  "rvol_spikes":      [{"symbol", "prev", "now"}],
  "position_events":  ["TSSI PULLBACK FAIL: ..."],
  "resistance_flags": ["ASTC"],
  "errors":           ["❌ SYMBOL EXECUTION FAILED: ..."]
}
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE     = Path.home() / "tri-city-inator"
SHARED        = WORKSPACE / "shared"
LOGS          = WORKSPACE / "logs"
SCRIPTS       = WORKSPACE / "scripts"

TABLE_FILE    = SHARED / "tri-city-table.json"
SIG_FILE      = SHARED / "tri-city-signals.json"
SUMMARY_FILE  = SHARED / "tri-city-summary.json"

CT = ZoneInfo("America/Chicago")

# ── Logging: file only, zero stdout ──────────────────────────────────────────
LOGS.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOGS / "tri-city-monitor.log")],
    force=True,
)
logger = logging.getLogger(__name__)

TABLE_MAX_AGE_SEC = 300  # table.json must be < 5 min old to be considered live


# ── Helpers ───────────────────────────────────────────────────────────────────

def run(cmd: list[str]) -> tuple[int, str]:
    """Run subprocess, return (returncode, stdout+stderr combined)."""
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def table_is_fresh() -> bool:
    if not TABLE_FILE.exists():
        logger.warning("tri-city-table.json missing — skipping cycle")
        return False
    age = datetime.now().timestamp() - TABLE_FILE.stat().st_mtime
    if age > TABLE_MAX_AGE_SEC:
        logger.warning(f"tri-city-table.json is {age:.0f}s old — stale, skipping")
        return False
    return True


def parse_post_cutoff(line: str) -> dict:
    """
    Parse: POST_CUTOFF_SIGNAL | SYM | SETUP | price=X | orh=Y | ... | stop=Z | ...
    into a structured dict.
    """
    parts = [p.strip() for p in line.split("|")]
    result: dict = {"raw": line}
    if len(parts) >= 3:
        result["symbol"] = parts[1]
        result["setup"]  = parts[2]
    for part in parts[3:]:
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in ("price", "orh", "orl", "rsi", "ema_dev", "stop", "risk_per_share"):
            try:
                result[k] = float(v)
            except ValueError:
                result[k] = v
        elif k == "shares":
            try:
                result[k] = int(v)
            except ValueError:
                result[k] = v
        elif k == "cup":
            result[k] = v.lower() == "true"
        else:
            result[k] = v
    return result


def parse_execute_output(output: str, sig: dict) -> dict | None:
    """
    Parse one-line --quiet execute output into a structured event.
    Returns None if the symbol was silently skipped.
    """
    if not output:
        return None
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("✅"):
            # ✅ TSSI PULLBACK CUP 1060sh @$15.61 stop=$14.78 (slippage +0.05)
            return {
                "type":   "execution",
                "symbol": sig["symbol"],
                "setup":  sig["setup"],
                "cup":    sig.get("cup", False),
                "htf":    sig.get("htf", False),
                "raw":    line,
            }
        if line.startswith("❌"):
            logger.error(f"{sig['symbol']}: {line}")
            return {"type": "error", "symbol": sig["symbol"], "raw": line}
        if line.startswith("POST_CUTOFF_SIGNAL") and "price=" in line:
            return {"type": "post_cutoff", **parse_post_cutoff(line)}
    return None


def build_execute_cmd(sig: dict) -> list[str]:
    cmd = [
        "python", "-W", "ignore",
        str(SCRIPTS / "tri_city_execute.py"),
        "--symbol",  sig["symbol"],
        "--price",   str(sig["price"]),
        "--orh",     str(sig["orh"]),
        "--orl",     str(sig["orl"]),
        "--rsi",     str(sig["rsi"]),
        "--ema_dev", str(sig["ema_dev"]),
        "--rvol",    str(sig["rvol"]),
        "--signal",  sig["setup"],
        "--setup",   sig["setup"],
        "--quiet",
    ]
    if sig.get("cup"):        cmd.append("--cup")
    if sig.get("htf"):        cmd.append("--htf")
    if sig.get("bb_squeeze"): cmd.append("--bb_squeeze")
    if sig.get("earnings"):   cmd.append("--earnings")
    if sig.get("vwap") is not None:
        cmd += ["--vwap", str(sig["vwap"])]
    if sig.get("gap_pct") is not None:
        cmd += ["--gap", str(sig["gap_pct"])]
    if sig.get("st_band") is not None:
        cmd += ["--st_band", str(sig["st_band"])]
    if sig.get("er") is not None:
        cmd += ["--er", str(sig["er"])]
    return cmd


# ── Main pipeline ─────────────────────────────────────────────────────────────

def main() -> None:
    now = datetime.now(CT)
    logger.info(f"── Cycle {now.strftime('%H:%M CT')} ─────────────────────────")

    if not table_is_fresh():
        # Table stale — skip signal detection but still run position management
        rc, pm_out = run(["python", "-W", "ignore",
                           str(SCRIPTS / "tri_city_position_manager.py")])
        logger.info(f"PosMgr (no table): {pm_out[:200] or '(silent)'}")
        pos_events = [ln.strip() for ln in pm_out.splitlines() if ln.strip()] if pm_out else []
        _write_summary(now, [], [], pos_events, [])
        return

    # Step 1: signal detector
    rc, det_out = run(["python", "-W", "ignore",
                        str(SCRIPTS / "tri_city_signal_detector.py")])
    if rc != 0:
        logger.error(f"Detector rc={rc}: {det_out[:300]}")
    else:
        logger.info(f"Detector:\n{det_out}" if det_out else "Detector: no signals")

    # Step 2: read signals.json
    if not SIG_FILE.exists():
        logger.warning("tri-city-signals.json missing after detector")
        _write_summary(now, [], [], [], [])
        return

    try:
        data        = json.loads(SIG_FILE.read_text())
        signals     = data.get("signals", [])
        rvol_spikes = data.get("rvol_spikes", [])
    except Exception as e:
        logger.error(f"signals.json parse error: {e}")
        _write_summary(now, [], [], [], [])
        return

    # Step 3: execute each signal
    events: list[dict] = []
    for sig in signals:
        rc, out = run(build_execute_cmd(sig))
        logger.info(f"Execute {sig['symbol']}/{sig['setup']}: {out[:100] or '(silent)'}")
        event = parse_execute_output(out, sig)
        if event:
            events.append(event)
        if sig.get("resistance"):
            events.append({"type": "resistance", "symbol": sig["symbol"]})

    # Step 4: position manager
    rc, pm_out = run(["python", "-W", "ignore",
                       str(SCRIPTS / "tri_city_position_manager.py")])
    logger.info(f"PosMgr: {pm_out[:200] or '(silent)'}")
    pos_events = [ln.strip() for ln in pm_out.splitlines() if ln.strip()] if pm_out else []

    _write_summary(now, events, rvol_spikes, pos_events, [])

    logger.info(
        f"Done — {len([e for e in events if e['type']=='execution'])} exec | "
        f"{len([e for e in events if e['type']=='post_cutoff'])} post-cutoff | "
        f"{len(rvol_spikes)} spikes | {len(pos_events)} pos-events"
    )


def _write_summary(
    now: datetime,
    events: list[dict],
    rvol_spikes: list[dict],
    pos_events: list[str],
    _unused: list,
) -> None:
    summary = {
        "timestamp":        now.strftime("%H:%M CT"),
        "executions":       [e for e in events if e["type"] == "execution"],
        "post_cutoff":      [e for e in events if e["type"] == "post_cutoff"],
        "rvol_spikes":      rvol_spikes,
        "position_events":  pos_events,
        "resistance_flags": [e["symbol"] for e in events if e["type"] == "resistance"],
        "errors":           [e["raw"] for e in events if e["type"] == "error"],
    }
    SUMMARY_FILE.write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.exception(f"Monitor crashed: {e}")
        sys.exit(1)
