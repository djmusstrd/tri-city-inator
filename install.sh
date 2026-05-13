#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Tri-City Inator — One-shot install
#
# Usage:
#   bash install.sh
#
# What this does:
#   1. Checks Python 3.9+
#   2. Installs pip dependencies
#   3. Creates required directories
#   4. Creates .env from .env.example (if not present)
#   5. Adds shell aliases to ~/.zshrc
#
# Prerequisites (install before running this script):
#   - TradingView Desktop (Pro plan minimum — for real-time data)
#   - Claude Code: https://claude.ai/code
#   - Alpaca account: https://alpaca.markets (paper trading is free)
#   - The Tri-City Inator Pine Script added to TradingView
# ─────────────────────────────────────────────────────────────────────────────

set -e

WORKSPACE="$HOME/tri-city-inator"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✅ $1${NC}"; }
warn() { echo -e "${YELLOW}⚠️  $1${NC}"; }
fail() { echo -e "${RED}❌ $1${NC}"; exit 1; }

echo ""
echo "════════════════════════════════════════════"
echo "  Tri-City Inator — Setup"
echo "════════════════════════════════════════════"
echo ""

# ── 1. Python version ─────────────────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    fail "python3 not found. Install Python 3.9+ from https://python.org"
fi

PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
if python3 -c "import sys; sys.exit(0 if sys.version_info >= (3,9) else 1)"; then
    ok "Python $PY_VER"
else
    fail "Python 3.9+ required (found $PY_VER)"
fi

# ── 2. Dependencies ───────────────────────────────────────────────────────────
echo "Installing Python dependencies..."
pip3 install -q --upgrade pip
pip3 install -q alpaca-py python-dotenv requests pandas
ok "Dependencies installed"

# ── 3. Directories ────────────────────────────────────────────────────────────
mkdir -p "$WORKSPACE/logs"
mkdir -p "$WORKSPACE/shared"
ok "Directories ready"

# ── 4. .env ───────────────────────────────────────────────────────────────────
if [ ! -f "$WORKSPACE/.env" ]; then
    if [ -f "$WORKSPACE/.env.example" ]; then
        cp "$WORKSPACE/.env.example" "$WORKSPACE/.env"
        warn ".env created — edit $WORKSPACE/.env and add your Alpaca API keys"
    else
        warn ".env.example not found — create $WORKSPACE/.env manually (see README)"
    fi
else
    ok ".env already exists"
fi

# ── 5. Shell aliases ──────────────────────────────────────────────────────────
SHELL_RC="$HOME/.zshrc"
[ ! -f "$SHELL_RC" ] && SHELL_RC="$HOME/.bashrc"

ALIAS_MARKER="# Tri-City Inator aliases"
if ! grep -q "$ALIAS_MARKER" "$SHELL_RC" 2>/dev/null; then
    cat >> "$SHELL_RC" << 'ALIASES'

# Tri-City Inator aliases
alias tricity="cd ~/tri-city-inator && python -W ignore scripts/journal_report.py --today"
alias tricity-all="cd ~/tri-city-inator && python -W ignore scripts/journal_report.py"
alias tricity-scan="cd ~/tri-city-inator && python -W ignore scripts/tri_city_scanner.py"
alias tricity-status="cd ~/tri-city-inator && python -W ignore scripts/tri_city_position_manager.py --status"
ALIASES
    ok "Aliases added to $SHELL_RC"
else
    ok "Aliases already present"
fi

# ── 6. Claude Code check ──────────────────────────────────────────────────────
if ! command -v claude &>/dev/null; then
    warn "Claude Code not found. Install from: https://claude.ai/code"
else
    ok "Claude Code found"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════"
echo "  Setup complete!"
echo "════════════════════════════════════════════"
echo ""
echo "Next steps:"
echo ""
echo "  1. Add your Alpaca API keys:"
echo "     nano ~/tri-city-inator/.env"
echo ""
echo "  2. Add the Tri-City Inator indicator to TradingView:"
echo "     → Pine Editor → paste code → Save → Add to Chart"
echo "     (See pine/README.md for detailed instructions)"
echo ""
echo "  3. Copy .mcp.json.example → .mcp.json and update the path:"
echo "     cp ~/tri-city-inator/.mcp.json.example ~/tri-city-inator/.mcp.json"
echo ""
echo "  4. Reload your shell:"
echo "     source $SHELL_RC"
echo ""
echo "  5. Start a session:"
echo "     cd ~/tri-city-inator && claude"
echo ""
echo "  6. Inside Claude, run the morning scanner:"
echo "     tricity-scan"
echo ""
echo "Paper trading test (no money at risk):"
echo "  python -W ignore ~/tri-city-inator/scripts/tri_city_execute.py \\"
echo "    --symbol NVDA --price 142.50 --rsi 62 --rvol 2.1 \\"
echo "    --signal ENTER --setup ENTER --dry-run"
echo ""
