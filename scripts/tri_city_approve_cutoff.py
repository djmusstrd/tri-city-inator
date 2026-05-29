#!/usr/bin/env python3
"""
TRI-CITY APPROVE CUTOFF
Called by Claude when user types "yes SYMBOL" in response to a POST_CUTOFF notification.
Reads shared/tri-city-pending-approvals.json, finds the pending entry, and executes
tri_city_execute.py with --override-cutoff.

Usage:
  python tri_city_approve_cutoff.py SYMBOL
"""

import json
import subprocess
import sys
from pathlib import Path

WORKSPACE      = Path.home() / "tri-city-inator"
SHARED         = WORKSPACE / "shared"
SCRIPTS        = WORKSPACE / "scripts"
APPROVALS_FILE = SHARED / "tri-city-pending-approvals.json"


def main():
    if len(sys.argv) < 2:
        print("Usage: tri_city_approve_cutoff.py SYMBOL")
        sys.exit(1)

    symbol = sys.argv[1].upper().strip()

    if not APPROVALS_FILE.exists():
        print(f"No pending approvals file found.")
        sys.exit(1)

    try:
        pending = json.loads(APPROVALS_FILE.read_text())
    except Exception as e:
        print(f"Error reading pending approvals: {e}")
        sys.exit(1)

    entry = next((x for x in pending if x.get("symbol", "").upper() == symbol), None)
    if not entry:
        print(f"No pending approval found for {symbol}. Available: {[x.get('symbol') for x in pending]}")
        sys.exit(1)

    print(f"Approving POST_CUTOFF: {symbol} {entry.get('setup')} @${entry.get('price')} "
          f"stop=${entry.get('stop', 'N/A')} {entry.get('shares', 0)}sh")

    cmd = [
        "python", "-W", "ignore",
        str(SCRIPTS / "tri_city_execute.py"),
        "--symbol",  symbol,
        "--price",   str(entry["price"]),
        "--orh",     str(entry.get("orh", entry["price"])),
        "--orl",     str(entry.get("orl", entry["price"])),
        "--rsi",     str(entry.get("rsi", 50)),
        "--ema_dev", str(entry.get("ema_dev", 0)),
        "--rvol",    str(entry.get("rvol", 1.5)),
        "--signal",  entry["setup"],
        "--setup",   entry["setup"],
        "--override-cutoff",
    ]
    if entry.get("cup"):        cmd.append("--cup")
    if entry.get("htf"):        cmd.append("--htf")
    if entry.get("bb_squeeze"): cmd.append("--bb_squeeze")
    if entry.get("vwap"):
        cmd += ["--vwap", str(entry["vwap"])]

    result = subprocess.run(cmd, capture_output=True, text=True)
    output = (result.stdout + result.stderr).strip()
    if output:
        print(output)

    # Remove from pending
    updated = [x for x in pending if x.get("symbol", "").upper() != symbol]
    APPROVALS_FILE.write_text(json.dumps(updated, indent=2))
    print(f"Cleared {symbol} from pending approvals.")


if __name__ == "__main__":
    main()
