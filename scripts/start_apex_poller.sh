#!/bin/bash
# Start the APEX intraday poller as a background daemon.
# Usage: start_apex_poller.sh [--dry-run]

WORKSPACE="$HOME/tri-city-inator"
PID_FILE="$WORKSPACE/shared/apex-poller.pid"
LOG_FILE="$WORKSPACE/logs/apex-poller.log"

# Kill existing by PID file, then by name as a safety net (lesson from the sandbox zombies)
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing APEX poller PID $OLD_PID"
        kill "$OLD_PID"; sleep 1
    fi
    rm -f "$PID_FILE"
fi
ORPHANS=$(pgrep -f "apex_poller.py" 2>/dev/null)
if [ -n "$ORPHANS" ]; then
    echo "Killing orphaned APEX poller(s): $ORPHANS"
    kill $ORPHANS 2>/dev/null; sleep 1
fi

PYTHON=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -f "$HOME/opt/anaconda3/bin/python" ] && PYTHON="$HOME/opt/anaconda3/bin/python"

DRY=""
[ "$1" = "--dry-run" ] && DRY="--dry-run" && echo "DRY-RUN: no live orders"

nohup "$PYTHON" -W ignore "$WORKSPACE/scripts/apex_poller.py" $DRY > /dev/null 2>&1 &
echo "APEX POLLER started | PID $! | log: $LOG_FILE"
