# Tri-City Inator — Claude Session Instructions

## Before Starting

1. **TradingView Desktop must be running** with the Tri-City Inator ENHANCED indicator
   visible on the active chart before 8:00 AM CT
2. `.env` must contain valid Alpaca API keys
3. Start in paper trading mode until you have validated signals live

## Start a Session

```bash
tricity   # opens TradingView Desktop + Claude in one command
```

The `.mcp.json` in this directory auto-connects the TradingView MCP server.

---

## Full Session Pipeline

> All times are Central (CT). Eastern Time users add 1 hour.

```
7:00 AM CT   Premarket scanner  →  gap-up candidates ranked by score
8:00 AM CT   Level lock         →  reads Tri-City indicator, locks today's entry levels
8:30 AM CT   Signal monitor     →  every 3 min, checks all watchlist symbols
8:30 AM CT+  Position manager   →  T1 breakeven, T2 lock, trailing stop
2:45 PM CT   EOD close          →  all positions closed automatically
             Journal            →  every exit auto-logged (P&L, R, outcome)
```

---

## SESSION START — Auto-setup (do this automatically, no user action needed)

When a session starts, immediately and silently register these three crons without asking the user to paste anything. Just confirm with a single short status line when done.

Register all three in order:

1. Premarket scanner — weekdays at 7:00 AM CT:
   /loop 7am weekdays Run the Tri-City premarket scanner: execute `python -W ignore ~/tri-city-inator/scripts/tri_city_scanner.py` via Bash and report the full output including ranked candidate table. Then read the "TV WATCHLIST" line at the bottom of the output and add each of those symbols to the TradingView watchlist using watchlist_add (one call per symbol). The active watchlist in TradingView's right sidebar is where they will appear — the user can click any symbol there to pull up the chart and see more detail.

2. Level lock — weekdays at 8:00 AM CT:
   /loop 8am weekdays Read the Tri-City Inator ENHANCED table using data_get_pine_tables with study_filter="Tri-City". Extract entry levels for all symbols and save to shared/tri-city-levels.json. Report symbols loaded.

3. Signal monitor — weekdays at 8:30 AM CT:
   /loop 8:30am weekdays Execute `python -W ignore ~/tri-city-inator/scripts/tri_city_monitor.py` via Bash. If there is output, print it. If there is no output, stay silent.

After registering, print one line only:
"Session ready. Scanner fires 7:00 AM, levels lock 8:00 AM, monitor starts 8:30 AM (all CT)."

Do not show the /loop commands to the user. Do not ask them to paste anything.

The monitor automatically:
- Checks all watchlist + scanner candidate symbols for ENTER/CONV/SETUP signals
- Calls `tri_city_execute.py` for any qualifying ENTER or CONV signal
- Runs `tri_city_position_manager.py` to manage open positions
- Silences itself after 4:00 PM CT (market close)

---

## Entry Guards (tri_city_execute.py — in order)

| # | Guard | Default | Effect |
|---|-------|---------|--------|
| 1 | Already executed today | — | No duplicate signals per symbol |
| 2 | Already in position | — | No duplicate symbols |
| 3 | Max positions | 3 | No more than 3 concurrent trades |
| 4 | Daily loss limit | -$300 | Circuit breaker |
| 5 | Time window | 1:00 PM CT | No new entries after cutoff |
| 6 | Market regime (SPY) | -1.5% | Blocks LONGs in bear market |
| 7 | Relative volume | 1.5x | Requires elevated volume |

Override any default by editing the values in `.env`.

---

## Signal Types

| Signal | Action | Execution |
|--------|--------|-----------|
| 🚀 ENTER | All conditions met | Auto-execute |
| 💎 CONV | MA convergence breakout | Auto-execute |
| ⚠️ SETUP | Almost ready | Print alert only |
| 🔵 ACCUM | Base forming | Print alert only |
| 🟢 RE-ENTRY | Pullback opportunity | Auto-execute (half size) |
| ⛔ EXIT | Position manager fires | Close triggered |

---

## Position Structure (50-25-25)

```
Entry → T1 (+10%): sell 50% → move stop to breakeven
       → T2 (+20%): sell 25% → move stop to T2 price
       → T3 (+30%): trail 25% → exits on EMA20 + VWAP breach or EOD
       → Stop (-5%): all shares exit
```

---

## Manual Commands

```bash
# Today's trade report
tricity

# All-time performance
tricity-all

# Check open positions
tricity-status

# Run premarket scanner
tricity-scan

# Dry-run a signal without executing
python -W ignore ~/tri-city-inator/scripts/tri_city_execute.py \
  --symbol NVDA --price 142.50 --rsi 62 --rvol 2.1 \
  --signal ENTER --setup ENTER --dry-run

# Backtest a symbol
python -W ignore ~/tri-city-inator/scripts/tri_city_backtest.py \
  --symbols NVDA TSLA --start 2024-01-01

# EOD close all positions now
python -W ignore ~/tri-city-inator/scripts/tri_city_position_manager.py --eod
```

---

## What Each Script Does

| Script | Trigger | Action |
|--------|---------|--------|
| `tri_city_scanner.py` | 7:00 AM cron | Gap-up candidates ranked by score |
| `tri_city_monitor.py` | 3-min cron | Alpaca snapshot scan → signals → execute |
| `tri_city_execute.py` | Called by monitor | 7-guard gate → 3-bracket Alpaca orders |
| `tri_city_position_manager.py` | Called by monitor | T1/T2/T3 management + EOD close |
| `tri_city_backtest.py` | Manual | Historical simulation with P&L report |
| `journal_report.py` | Manual | Performance report from trade journal |

---

## TradingView Indicator

The Tri-City Inator ENHANCED Pine Script should be visible on the active chart.
It provides visual confirmation of signals and the Trend Strength score.

The Python monitor handles all auto-execution — TradingView is for visualization only.

If you want to check the live indicator dashboard, use:
```
Read the Tri-City Inator table using data_get_pine_tables with study_filter="Tri-City"
```
