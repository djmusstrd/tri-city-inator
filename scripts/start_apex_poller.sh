#!/bin/bash
# Start the APEX intraday poller as a background daemon.
# Usage: start_apex_poller.sh [--dry-run]

WORKSPACE="$HOME/tri-city-inator"
PID_FILE="$WORKSPACE/shared/apex-poller.pid"
LOG_FILE="$WORKSPACE/logs/apex-poller.log"

# Kill existing by PID file, then by name as a safety net (lesson from the sandbox zombies).
# WAIT for each to actually exit (the poller may be mid-sleep) and escalate to -9, so we never
# leave a lingering duplicate that double-executes or deletes the new PID file on its way out.
kill_and_wait() {
    local pid="$1"
    [ -n "$pid" ] || return 0
    kill -0 "$pid" 2>/dev/null || return 0
    echo "Stopping APEX poller PID $pid"
    kill "$pid" 2>/dev/null
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
    done
    echo "  PID $pid still alive — SIGKILL"
    kill -9 "$pid" 2>/dev/null; sleep 1
}
[ -f "$PID_FILE" ] && kill_and_wait "$(cat "$PID_FILE")"
rm -f "$PID_FILE"
for pid in $(pgrep -f "apex_poller.py" 2>/dev/null); do
    kill_and_wait "$pid"
done

PYTHON=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -f "$HOME/opt/anaconda3/bin/python" ] && PYTHON="$HOME/opt/anaconda3/bin/python"

DRY=""
[ "$1" = "--dry-run" ] && DRY="--dry-run" && echo "DRY-RUN: no live orders"

nohup "$PYTHON" -W ignore "$WORKSPACE/scripts/apex_poller.py" $DRY > /dev/null 2>&1 &
echo "APEX POLLER started | PID $! | log: $LOG_FILE"
