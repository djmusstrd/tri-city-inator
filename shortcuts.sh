#!/usr/bin/env bash
# Tri-City Inator shortcuts
# Usage: source ~/tri-city-inator/shortcuts.sh
# Add to ~/.zshrc:  source ~/tri-city-inator/shortcuts.sh

_TC=~/tri-city-inator

# ── Session ───────────────────────────────────────────────────────────────────
tricity()         { open -a TradingView && cd "$_TC" && claude 'Session start'; }
tricity-poller()  { bash "$_TC/scripts/start_poller.sh"; }
tricity-health()  { cd "$_TC" && python -W ignore scripts/tri_city_health_check.py; }

# ── Scan ─────────────────────────────────────────────────────────────────────
tricity-scan()     { cd "$_TC" && python -W ignore scripts/tri_city_scanner.py; }
tricity-intraday() { cd "$_TC" && python -W ignore scripts/tri_city_intraday_scanner.py; }

# ── Positions ────────────────────────────────────────────────────────────────
tricity-status()  { cd "$_TC" && python -W ignore scripts/tri_city_position_manager.py --status; }
tricity-eod()     { cd "$_TC" && python -W ignore scripts/tri_city_position_manager.py --eod; }

# ── Reports ──────────────────────────────────────────────────────────────────
tricity-report()  { cd "$_TC" && python -W ignore scripts/journal_report.py --today; }
tricity-all()     { cd "$_TC" && python -W ignore scripts/journal_report.py; }
tricity-recap()   { cd "$_TC" && python -W ignore scripts/daily_recap.py; }

# ── Research ─────────────────────────────────────────────────────────────────
tricity-backtest()    { cd "$_TC" && python -W ignore scripts/tri_city_backtest.py "$@"; }
tricity-walkforward() { cd "$_TC" && python -W ignore scripts/tri_city_walkforward.py "$@"; }
tricity-cheatsheet()  { cd "$_TC" && python -W ignore scripts/generate_cheatsheet.py; }

# ── Dashboard ────────────────────────────────────────────────────────────────
tricity-dash() { launchctl kickstart -k gui/$(id -u)/com.starks-labs.tricity-dashboard 2>/dev/null; echo "Tri-City → https://tricity.clawbotinator.trade  pw: meadow-harbor-ember-44"; }

# ── APEX (Strategy V2 — see docs/STRATEGY_V2_DESIGN.md) ───────────────────────
# Start a full APEX trading session: brings up TV+CDP, today's leaders, and the poller
# (LIVE PAPER by default; pass --dry-run for log-only), then opens Claude to monitor.
apex() { cd "$_TC" && bash scripts/apex_session_start.sh "$@" && claude 'APEX trading session was just launched by apex_session_start.sh. Verify the poller is alive (shared/apex-poller.pid) and TV/CDP is up (curl -s localhost:9222/json/list), give me a one-line status, then monitor. When I type "end session", run scripts/apex_session_end.sh and show me the summary.'; }
# End the APEX session: stop poller + flatten INTRADAY positions; swings stay open (durable).
apex-end() { cd "$_TC" && bash scripts/apex_session_end.sh; }
# Flatten EVERYTHING now — intraday AND swing/position holdings (explicit full close).
apex-flatten() { cd "$_TC" && python -W ignore scripts/apex_eod.py --all; }
# Resume the APEX BUILD / design work (not a trading session).
apex-build() { cd "$_TC" && claude 'Resume the APEX build. Read docs/STRATEGY_V2_DESIGN.md and the project_strategy_v2_apex memory, then tell me the exact resume point (the >>> RESUME HERE steps) before doing anything.'; }
# Layer 1 daily filter: rank the liquid universe by RS, write shared/apex-leaders.json
apex-leaders()  { cd "$_TC" && python -W ignore scripts/apex_daily_filter.py "$@"; }
# Walk-forward: do RS leaders prospectively beat SPY?
apex-validate() { cd "$_TC" && python -W ignore scripts/apex_daily_filter.py --validate "$@"; }
# Phase 0 concept backtest (RS + ribbon, --no-tp for let-it-run)
apex-backtest() { cd "$_TC" && python -W ignore scripts/apex_phase0_backtest.py "$@"; }
# Phase 2 entry-timing backtest (OPEN vs ORB15 vs VWAP_PB on leaders)
apex-entry()    { cd "$_TC" && python -W ignore scripts/apex_phase2_entry_backtest.py "$@"; }
# Layer 3 health monitor self-test — replay a past session's health trajectory + exits
apex-health()   { cd "$_TC" && python -W ignore scripts/apex_health.py --self-test "$@"; }
# Swing-tier manager — daily-bar exit rules for carried positions (also runs daily via launchd)
apex-swing()    { cd "$_TC" && python -W ignore scripts/apex_swing.py; }
# Visual management dashboard (candles + health timeline + why + journal + leaders)
apex-dash()     { cd "$_TC" && python3 -m streamlit run scripts/apex_dashboard.py "$@"; }
# Test the real-time TV quote reader (hybrid feed) — needs TV running with CDP
apex-quotes()   { cd "$_TC" && python -W ignore scripts/apex_tv_quotes.py "$@"; }
# Open the live-paper operating playbook (full daily workflow)
apex-playbook() { ${PAGER:-less} "$_TC/docs/APEX_PLAYBOOK.md"; }

echo "Tri-City shortcuts loaded:"
echo "  Session  : tricity  tricity-poller  tricity-health"
echo "  Scan     : tricity-scan  tricity-intraday"
echo "  Positions: tricity-status  tricity-eod"
echo "  Reports  : tricity-report  tricity-all  tricity-recap"
echo "  Research : tricity-backtest  tricity-walkforward  tricity-cheatsheet"
echo "  Dashboard: tricity-dash"
echo "  APEX     : apex (START session)  apex-end (stop+flatten intraday)  apex-flatten (close ALL)  apex-build"
echo "           : apex-leaders  apex-validate  apex-backtest  apex-entry  apex-health"
echo "           : apex-dash (dashboard)  apex-quotes (live feed test)  apex-playbook (how-to)"
