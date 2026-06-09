#!/bin/bash
# Start the Phase 3 TV poller + alert monitor as background daemons.
# Called by Claude at session start (after level lock time is known).
# Usage: start_poller.sh [HH:MM]   (optional start-after time CT)

WORKSPACE="$HOME/tri-city-inator"
PID_FILE="$WORKSPACE/shared/tri-city-poller.pid"
ALERT_PID_FILE="$WORKSPACE/shared/tri-city-alert-monitor.pid"
LOG_FILE="$WORKSPACE/logs/tri-city-tv-poller.log"
ALERT_LOG_FILE="$WORKSPACE/logs/tri-city-alert-monitor.log"

# Kill any existing poller
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing poller PID $OLD_PID"
        kill "$OLD_PID"
        sleep 1
    fi
    rm -f "$PID_FILE"
fi

# Kill any existing alert monitor
if [ -f "$ALERT_PID_FILE" ]; then
    OLD_PID=$(cat "$ALERT_PID_FILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Stopping existing alert monitor PID $OLD_PID"
        kill "$OLD_PID"
        sleep 1
    fi
    rm -f "$ALERT_PID_FILE"
fi

START_AFTER="${1:-}"
if [ -n "$START_AFTER" ]; then
    EXTRA_ARGS="--start-after $START_AFTER"
else
    EXTRA_ARGS=""
fi

# Resolve python: prefer conda env, fallback to system python3
PYTHON=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
# If conda is available, ensure we use its python
if [ -f "$HOME/opt/anaconda3/bin/python" ]; then
    PYTHON="$HOME/opt/anaconda3/bin/python"
elif [ -f "$HOME/anaconda3/bin/python" ]; then
    PYTHON="$HOME/anaconda3/bin/python"
elif [ -f "$HOME/miniconda3/bin/python" ]; then
    PYTHON="$HOME/miniconda3/bin/python"
fi

# Launch TV poller in background; stdout suppressed (logs to file via FileHandler only)
nohup "$PYTHON" -W ignore "$WORKSPACE/scripts/tri_city_tv_poller.py" \
    --poll-interval 180 \
    $EXTRA_ARGS \
    > /dev/null 2>&1 &

POLLER_PID=$!
echo "TRI-CITY TV POLLER started    | PID $POLLER_PID | log: $LOG_FILE"
echo "Stop with: kill $POLLER_PID  or  kill \$(cat $PID_FILE)"

# Launch alert monitor in background (poll every 60s, independent of poller)
nohup "$PYTHON" -W ignore "$WORKSPACE/scripts/tri_city_alert_monitor.py" \
    --poll-interval 60 \
    > /dev/null 2>&1 &

ALERT_PID=$!
echo "TRI-CITY ALERT MONITOR started | PID $ALERT_PID | log: $ALERT_LOG_FILE"
echo "Stop with: kill $ALERT_PID  or  kill \$(cat $ALERT_PID_FILE)"
