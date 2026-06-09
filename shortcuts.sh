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

echo "Tri-City shortcuts loaded:"
echo "  Session  : tricity  tricity-poller  tricity-health"
echo "  Scan     : tricity-scan  tricity-intraday"
echo "  Positions: tricity-status  tricity-eod"
echo "  Reports  : tricity-report  tricity-all  tricity-recap"
echo "  Research : tricity-backtest  tricity-walkforward  tricity-cheatsheet"
echo "  Dashboard: tricity-dash"
