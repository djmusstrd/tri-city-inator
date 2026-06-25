#!/bin/bash
# APEX trading-session bootstrap — idempotent. Brings the whole system up, ready to trade:
#   1. TradingView with CDP (the real-time hybrid quote feed)
#   2. today's RS leader watchlist (Layer 1) — built only if stale
#   3. the poller (detect → execute → Layer 3 manage)
#
# Usage:  apex_session_start.sh [--dry-run]
#   default     → LIVE PAPER (places real paper orders via Alpaca; ALPACA_PAPER must be true)
#   --dry-run   → log + Telegram only, no orders

TC="$HOME/tri-city-inator"
CDP_PORT=9222
TV_BIN="/Applications/TradingView.app/Contents/MacOS/TradingView"

DRY=""
[ "${1:-}" = "--dry-run" ] && DRY="--dry-run"

PYTHON=$(command -v python 2>/dev/null || command -v python3 2>/dev/null)
[ -x "$HOME/opt/anaconda3/bin/python" ] && PYTHON="$HOME/opt/anaconda3/bin/python"

echo "════════ APEX session start ════════"
if [ -n "$DRY" ]; then echo "MODE: DRY-RUN — no orders, log + Telegram only"
else echo "MODE: LIVE PAPER — places paper orders (ALPACA_PAPER)"; fi

# ── 1. TradingView + CDP ───────────────────────────────────────────────────────
cdp_up() { curl -s "http://localhost:$CDP_PORT/json/list" 2>/dev/null | grep -qi "tradingview.com/chart"; }
if cdp_up; then
    echo "✓ TradingView CDP already live (port $CDP_PORT)"
elif [ -x "$TV_BIN" ]; then
    echo "• launching TradingView with CDP…"
    kill "$(pgrep -x TradingView)" 2>/dev/null; sleep 2
    nohup "$TV_BIN" --remote-debugging-port=$CDP_PORT >/dev/null 2>&1 &
    for _ in $(seq 1 12); do sleep 4; cdp_up && break; done
    if cdp_up; then echo "✓ TradingView CDP ready"
    else echo "⚠ CDP not confirmed — APEX will run on the delayed Alpaca fallback"; fi
else
    echo "⚠ TradingView app not found — APEX will run on the delayed Alpaca fallback"
fi

# ── 1b. Dedicated quote-tab guard ───────────────────────────────────────────────
# The poller pins its real-time quote stream to the saved "APEX Feed" layout
# (APEX_QUOTE_CHART_ID) so the streaming load stays OFF your interactive chart. TV blocks
# programmatic tab creation (verified: /json/new and Target.createTarget both refused), so this
# can only CHECK the tab is open — TV restores it from the last session if you leave it open.
if cdp_up; then
    QCID=$(grep -E '^APEX_QUOTE_CHART_ID=' "$TC/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]')
    QCID=${QCID:-8mjaVwrx}
    if curl -s "http://localhost:$CDP_PORT/json/list" 2>/dev/null | grep -q "/chart/$QCID"; then
        echo "✓ APEX Feed quote tab ($QCID) present — quote stream isolated from your chart"
    else
        echo "⚠ APEX Feed quote tab ($QCID) NOT open — open that saved layout in a 2nd tab,"
        echo "  else the poller streams on your interactive chart and it WILL lag on symbol-cycling"
    fi
fi

# ── 2. Today's leaders (Layer 1) ───────────────────────────────────────────────
TODAY=$(date +%Y-%m-%d)
LEAD_DATE=$("$PYTHON" -c "import json;print(json.load(open('$TC/shared/apex-leaders.json'))['date'])" 2>/dev/null)
if [ "$LEAD_DATE" = "$TODAY" ]; then
    N=$("$PYTHON" -c "import json;print(len(json.load(open('$TC/shared/apex-leaders.json'))['leaders']))" 2>/dev/null)
    echo "✓ leaders ready for $TODAY ($N names)"
else
    echo "• building today's leader watchlist (scans the full universe, ~1 min)…"
    "$PYTHON" -W ignore "$TC/scripts/apex_daily_filter.py" 2>&1 | tail -2
fi

# ── 3. Poller ──────────────────────────────────────────────────────────────────
echo "• starting APEX poller…"
bash "$TC/scripts/start_apex_poller.sh" $DRY

# ── 4. Verify the system actually came up — heal once, else alert LOUD ───────────
# Why: a silent half-up bootstrap (TV without CDP, a poller that started then crashed, stale
# leaders) used to look like success and need a manual re-run. This block re-checks each piece,
# heals once, and if anything is still down it fails loud (stderr banner + macOS notification)
# instead of pretending it's ready.
echo "──────── verifying bootstrap ────────"
FAIL=""

# CDP / real-time feed — one retry if it never came up
if ! cdp_up && [ -x "$TV_BIN" ]; then
    echo "⚠ CDP down after launch — retrying TradingView once…"
    kill "$(pgrep -x TradingView)" 2>/dev/null; sleep 2
    nohup "$TV_BIN" --remote-debugging-port=$CDP_PORT >/dev/null 2>&1 &
    for _ in $(seq 1 12); do sleep 4; cdp_up && break; done
fi
if cdp_up; then echo "✓ CDP up"
else echo "✗ CDP DOWN — poller falls back to delayed Alpaca bars"; FAIL="$FAIL CDP"; fi

# Leaders — rebuild once if not today's
LEAD_DATE=$("$PYTHON" -c "import json;print(json.load(open('$TC/shared/apex-leaders.json'))['date'])" 2>/dev/null)
if [ "$LEAD_DATE" != "$TODAY" ]; then
    echo "⚠ leaders stale ($LEAD_DATE) — rebuilding…"
    "$PYTHON" -W ignore "$TC/scripts/apex_daily_filter.py" 2>&1 | tail -1
    LEAD_DATE=$("$PYTHON" -c "import json;print(json.load(open('$TC/shared/apex-leaders.json'))['date'])" 2>/dev/null)
fi
if [ "$LEAD_DATE" = "$TODAY" ]; then
    N=$("$PYTHON" -c "import json;print(len(json.load(open('$TC/shared/apex-leaders.json'))['leaders']))" 2>/dev/null)
    echo "✓ leaders fresh ($N for $TODAY)"
else echo "✗ leaders NOT built for today — entries impossible"; FAIL="$FAIL leaders"; fi

# Poller — must be alive; restart once if dead, then confirm it stayed up (catches start-then-crash)
poller_alive() { local p; p=$(cat "$TC/shared/apex-poller.pid" 2>/dev/null); [ -n "$p" ] && kill -0 "$p" 2>/dev/null; }
if ! poller_alive; then
    echo "⚠ poller not alive — (re)starting…"
    bash "$TC/scripts/start_apex_poller.sh" $DRY
    sleep 3
fi
if poller_alive; then echo "✓ poller alive (PID $(cat "$TC/shared/apex-poller.pid"))"
else echo "✗ poller DEAD — no trading"; FAIL="$FAIL poller"; fi

# Verdict
if [ -z "$FAIL" ]; then
    echo "════════ APEX ready to trade ✓ ════════"
else
    echo "════════ ⚠ APEX BOOTSTRAP INCOMPLETE —$FAIL — needs attention ════════" >&2
    osascript -e "display notification \"down:$FAIL\" with title \"⚠ APEX bootstrap incomplete\"" 2>/dev/null || true
fi
