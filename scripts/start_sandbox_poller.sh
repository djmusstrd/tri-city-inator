#!/bin/bash
# Start the sandbox poller as a background daemon.
# Usage: start_sandbox_poller.sh [--dry-run]

WORKSPACE="$HOME/tri-city-inator"
PID_FILE="$WORKSPACE/shared/sandbox-poller.pid"
LOG_FILE="$WORKSPACE/logs/sandbox-poller.log"

# Kill any existing sandbox poller
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing sandbox poller PID $OLD_PID"
        kill "$OLD_PID"
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# Resolve python (prefer conda)
PYTHON=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
if [ -f "$HOME/opt/anaconda3/bin/python" ]; then
    PYTHON="$HOME/opt/anaconda3/bin/python"
elif [ -f "$HOME/anaconda3/bin/python" ]; then
    PYTHON="$HOME/anaconda3/bin/python"
elif [ -f "$HOME/miniconda3/bin/python" ]; then
    PYTHON="$HOME/miniconda3/bin/python"
fi

DRY_RUN_FLAG=""
if [ "$1" = "--dry-run" ]; then
    DRY_RUN_FLAG="--dry-run"
    echo "DRY-RUN mode enabled — orders will be logged but NOT submitted"
fi

nohup "$PYTHON" -W ignore "$WORKSPACE/scripts/sandbox_poller.py" \
    --poll-interval 120 \
    $DRY_RUN_FLAG \
    > /dev/null 2>&1 &

POLLER_PID=$!
echo "SANDBOX POLLER started | PID $POLLER_PID | log: $LOG_FILE"
echo "Stop with: kill $POLLER_PID  or  kill \$(cat $PID_FILE)"
