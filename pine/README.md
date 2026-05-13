# Tri-City Inator Pine Script

## Installation

1. Open **TradingView Desktop**
2. Click **Pine Editor** (bottom of screen)
3. Click **Open** → **New blank indicator**
4. Delete all existing code
5. Paste the complete Tri-City Inator ENHANCED source code
6. Click **Save** → name it `Tri-City Inator ENHANCED`
7. Click **Add to Chart**

## Requirements

- TradingView **Pro or higher** (for real-time data)
- The indicator must be **visible on the active chart** during Claude sessions

## What the Indicator Provides

The Tri-City Inator displays a real-time dashboard on the chart with:

| Metric | Description |
|--------|-------------|
| Price | Current price with filter status |
| Rel Volume | RVol vs 20-day average |
| RSI | 14-period RSI |
| Gap % | From previous close |
| vs EMA20 | Price above/below EMA |
| vs VWAP | Price above/below VWAP |
| Trend Strength | 0-100% composite score |
| vs SPY | Relative performance |
| Market Regime | Bull/Bear/Choppy |
| Smart Money | Institutional activity |
| SIGNAL | Current trade signal |

## Signal Types

| Signal | Meaning | Action |
|--------|---------|--------|
| 🚀 ENTER | All conditions met | Execute immediately |
| 💎 CONV | MA convergence breakout | Execute immediately |
| ⚠️ SETUP | Almost ready | Watch closely |
| 🔵 ACCUM | Base forming | Add to watchlist |
| 🟢 RE-ENTRY | Pullback opportunity | Second entry |
| ⛔ EXIT | Close position | Exit now |

## Notes

- The indicator is for **visualization only** when used with this system
- Signal detection for auto-execution uses Alpaca real-time data (faster)
- The dashboard's **Trend Strength** score is useful for manual override decisions
