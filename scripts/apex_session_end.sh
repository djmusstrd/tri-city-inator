#!/bin/bash
# APEX trading-session shutdown — stop the intraday poller, flatten INTRADAY positions, print the
# day's summary. Swing / multi-week holdings are LEFT OPEN (durable — they exit on swing rules via
# the swing manager, not on session end). Use `apex-flatten` to close everything explicitly.
# Called when you type "end session" (or run directly via the `apex-end` shortcut).
# TradingView is left running; close it manually if you want.

TC="$HOME/tri-city-inator"
PID_FILE="$TC/shared/apex-poller.pid"

PYTHON=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -x "$HOME/opt/anaconda3/bin/python" ] && PYTHON="$HOME/opt/anaconda3/bin/python"

echo "════════ APEX session end ════════"

# ── 1. Stop the poller (wait for exit, escalate to -9) ─────────────────────────
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        echo "• stopping poller PID $PID…"
        kill "$PID" 2>/dev/null
        for _ in $(seq 1 8); do kill -0 "$PID" 2>/dev/null || break; sleep 1; done
        kill -0 "$PID" 2>/dev/null && kill -9 "$PID" 2>/dev/null
    fi
fi
for pid in $(pgrep -f apex_poller.py 2>/dev/null); do kill -9 "$pid" 2>/dev/null; done
rm -f "$PID_FILE"
echo "✓ poller stopped"

# ── 2. Flatten open positions + print summary ──────────────────────────────────
"$PYTHON" -W ignore "$TC/scripts/apex_eod.py"

echo "════════ APEX session ended ════════"
