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

echo "════════ APEX ready to trade ════════"
