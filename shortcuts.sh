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
# Resume the APEX build where we left off (reads the design doc + apex memory).
apex() { cd "$_TC" && claude 'Resume the APEX build. Read docs/STRATEGY_V2_DESIGN.md and the project_strategy_v2_apex memory, then tell me the exact resume point (the >>> RESUME HERE steps) before doing anything. We are at Phase 2b go-live wiring; next is the Layer 3 health monitor.'; }
# Layer 1 daily filter: rank the liquid universe by RS, write shared/apex-leaders.json
apex-leaders()  { cd "$_TC" && python -W ignore scripts/apex_daily_filter.py "$@"; }
# Walk-forward: do RS leaders prospectively beat SPY?
apex-validate() { cd "$_TC" && python -W ignore scripts/apex_daily_filter.py --validate "$@"; }
# Phase 0 concept backtest (RS + ribbon, --no-tp for let-it-run)
apex-backtest() { cd "$_TC" && python -W ignore scripts/apex_phase0_backtest.py "$@"; }
# Phase 2 entry-timing backtest (OPEN vs ORB15 vs VWAP_PB on leaders)
apex-entry()    { cd "$_TC" && python -W ignore scripts/apex_phase2_entry_backtest.py "$@"; }

echo "Tri-City shortcuts loaded:"
echo "  Session  : tricity  tricity-poller  tricity-health"
echo "  Scan     : tricity-scan  tricity-intraday"
echo "  Positions: tricity-status  tricity-eod"
echo "  Reports  : tricity-report  tricity-all  tricity-recap"
echo "  Research : tricity-backtest  tricity-walkforward  tricity-cheatsheet"
echo "  Dashboard: tricity-dash"
echo "  APEX     : apex (resume build)  apex-leaders  apex-validate  apex-backtest  apex-entry"
