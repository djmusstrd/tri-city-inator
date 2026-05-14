# Tri-City Inator — Claude Session Instructions

## Before Starting

1. **TradingView Desktop must be running** with the **Tri-City Inator** scanner visible on
   the **TRI CITY INATOR III** layout before 8:00 AM CT
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
7:30 AM CT         Premarket scanner  →  gap-up candidates ranked by score, saved + pushed to TV watchlist
8:30 AM CT         Symbol swap        →  top 15 candidates pushed into Tri-City scanner inputs
8:30 + ORB_MINUTES Level lock         →  ORH/ORL finalize once opening range closes
8:30 + ORB_MINUTES Signal monitor     →  every 3 min, checks all symbols for BREAKOUT/CONT/PULLBACK
                   Position manager   →  T1 breakeven, T2 lock, T3 trail, EOD close
                   Journal            →  every exit auto-logged (P&L, R, outcome)
```

> **ORB timeframe:** Set `ORB_MINUTES` in `.env` (5, 15, or 30 — default 15). Claude reads this
> at session start and schedules level lock + monitor at the right time.
> The Tri-City Inator scanner on your TradingView chart must be set to the same timeframe.

---

## SESSION START — Auto-setup (do this automatically, no user action needed)

When a session starts:

**Step 1 — Read ORB_MINUTES from .env**
Read the file `~/tri-city-inator/.env`. Look for a line like `ORB_MINUTES=15`.
- If found, use that value.
- If not found, default to `15`.

Calculate the level lock time: market open (8:30 AM CT) + ORB_MINUTES.
Examples: ORB_MINUTES=5 → 8:35 AM, ORB_MINUTES=15 → 8:45 AM, ORB_MINUTES=30 → 9:00 AM.

**Step 2 — Register four crons silently**

1. Premarket scanner — weekdays at 7:30 AM CT:
   /loop 7:30am weekdays Run the Tri-City premarket scanner: execute `python -W ignore ~/tri-city-inator/scripts/tri_city_scanner.py` via Bash and report the full output including ranked candidate table and any parabolic warnings. Then read the "TV WATCHLIST" line at the bottom of the output and add each symbol to the TradingView watchlist using watchlist_add (one call per symbol).

2. Symbol swap — weekdays at 8:30 AM CT:
   /loop 8:30am weekdays Read ~/tri-city-inator/shared/tri-city-candidates.json. Extract the "tv_symbols" array (up to 15 exchange-prefixed symbols, e.g. "NASDAQ:RKLB"). Build an inputs dict mapping in_7 through in_21 to those symbols (in_7=sym[0], in_8=sym[1], ..., in_21=sym[14]; omit keys for positions beyond the available count). Call indicator_set_inputs with entity_id="r4D8kP" and that inputs dict. Report: how many symbols were pushed and list them.

3. Level lock — weekdays at the computed level lock time (8:30 AM CT + ORB_MINUTES):
   /loop {LEVEL_LOCK_TIME}am weekdays Read the Tri-City Inator scanner table using data_get_pine_tables with study_filter="Tri-City". Extract ORH and ORL for every symbol from the "ORH/ORL" column and save to shared/tri-city-levels.json. Report symbols loaded.

4. Signal monitor — weekdays every 3 minutes (starts at level lock time):
   /loop 3m Read the Tri-City Inator scanner table using data_get_pine_tables with study_filter="Tri-City". Use the ORH and ORL values from the table directly for each symbol — do NOT use hardcoded levels. Check every symbol for THREE setup types. For each qualifying setup: (1) report it, (2) immediately execute via Bash: `python -W ignore ~/tri-city-inator/scripts/tri_city_execute.py --symbol {SYMBOL} --price {PRICE} --orh {ORH} --orl {ORL} --rsi {RSI} --ema_dev {EMA_DEV} --signal "{SIGNAL}" --setup {SETUP_TYPE} {--cup if CUP=YES}`. If nothing qualifies, stay silent.

   --- SETUP 1: BREAKOUT (--setup BREAKOUT) ---
   All 4 required:
   1. SIGNAL = "BREAKOUT"
   2. Price above ORH
   3. RSI > 50
   4. EMA Dev% > 0
   Stop: 13 cents below ORH. Add --cup if CUP column = "YES". Confidence: HIGH

   --- SETUP 2: CONTINUATION (--setup CONTINUATION) ---
   All 3 required:
   1. SIGNAL = "CONTINUATION"
   2. Price above ORH
   3. EMA Dev% between 0% and +1.0%
   Stop: 13 cents below ORH. Add --cup if CUP column = "YES". Confidence: HIGH

   --- SETUP 3: PULLBACK (--setup PULLBACK) ---
   All 3 required:
   1. SIGNAL = "PULLBACK"
   2. EMA Dev% between -0.5% and +0.8%
   3. RSI between 38 and 55
   Stop: 13 cents below ORH (if price within 2% above ORH), else 5% below entry. Add --cup if CUP column = "YES". Confidence: MEDIUM

   If nothing qualifies AND no POST_CUTOFF_SIGNAL output, stay silent.

   POST_CUTOFF_SIGNAL handling: If the execute output contains "POST_CUTOFF_SIGNAL", do NOT stay silent. Parse the output and alert the user: "@user — [SYMBOL] met [SETUP_TYPE] conditions at $[PRICE] after the [CUTOFF] entry cutoff. RSI [RSI], EMA Dev% [EMA_DEV], ORH $[ORH], Stop $[STOP], Risk/share $[RISK_PER_SHARE], Size [SHARES] shares. Cup: [YES/NO]. Take the trade? Reply 'yes [SYMBOL]' to execute with --override-cutoff."

   Run `python -W ignore ~/tri-city-inator/scripts/tri_city_position_manager.py` via Bash and print any output. If there is no output, stay silent.

**Step 3 — Confirm with one line only:**
"Session ready. Scanner 7:30 AM, symbol swap 8:30 AM, levels lock + monitor start {LEVEL_LOCK_TIME} AM ({ORB_MINUTES}-min ORB), all CT."

Do not show the /loop commands to the user. Do not ask them to paste anything.

---

## TradingView Scanner

| Item | Value |
|------|-------|
| Layout | **TRI CITY INATOR III** (ID 168250176) |
| Indicator | **Tri-City Inator** (Pine shorttitle: "Tri-City") |
| Entity ID | `r4D8kP` |
| Symbol inputs | `in_7` through `in_21` (15 slots) |
| Table columns | SYMBOL · PRICE · RSI · EMA DEV% · ORH/ORL · CUP · SIGNAL |

To read the live scanner table:
```
data_get_pine_tables with study_filter="Tri-City"
```

---

## Entry Guards (tri_city_execute.py — in order)

| # | Guard | Default | Effect |
|---|-------|---------|--------|
| 1 | Already executed today | — | No duplicate setups per symbol |
| 2 | Already in position | — | No duplicate symbols |
| 3 | Max positions | 3 | No more than 3 concurrent trades |
| 4 | Daily loss limit | -$300 | Circuit breaker |
| 5 | Time window | 1:00 PM CT | No new entries after cutoff |
| 6 | Market regime (SPY) | -1.5% | Blocks LONGs in bear market |
| 7 | Relative volume | 1.5x | Requires elevated volume |

Override any default by editing the values in `.env`.

---

## Signal Types

| Signal | Action | Notes |
|--------|--------|-------|
| BREAKOUT | Auto-execute | Price above ORH, high vol, RSI > 50, EMA Dev > 0 |
| CONTINUATION | Auto-execute | Above ORH pullback, EMA dev 0–1%, RSI 50–65 |
| PULLBACK | Auto-execute | Above EMA, dev -0.5–0.8%, RSI 38–55 |
| --- | Silent | No signal — skip |
| CUP = YES | +cup flag | Add --cup to execute call for high-conviction log |

---

## Position Structure (50-25-25)

```
Entry → T1 (+10%): sell 50% → move stop to breakeven
       → T2 (+20%): sell 25% → move stop to T2 price
       → T3 (+30%): trail 25% → exits on EMA20 + VWAP breach or EOD
       → Stop (-5%):  all shares exit
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
  --symbol TSEM --price 270.00 --orh 267.42 --orl 252.70 \
  --rsi 55.0 --ema_dev 0.35 --signal "BREAKOUT" --setup BREAKOUT --cup --dry-run

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
| `tri_city_scanner.py` | 7:30 AM cron | Gap-up candidates ranked by score, saved to tri-city-candidates.json |
| Symbol swap (inline) | 8:30 AM cron | Reads tv_symbols → `indicator_set_inputs` on r4D8kP (in_7–in_21) |
| Level lock (inline) | ORB_MINUTES cron | Reads Tri-City table → saves ORH/ORL to tri-city-levels.json |
| `tri_city_execute.py` | Signal monitor | 7-guard gate → 50-25-25 bracket orders via Alpaca → logs to tri-city-executions.json |
| `tri_city_position_manager.py` | Signal monitor | T1 hit → breakeven stop; 3:45 PM → EOD close all |
| `tri_city_backtest.py` | Manual | Historical simulation with P&L report |
| `journal_report.py` | Manual | Performance report from trade journal |

---

## TradingView Indicator

The **Tri-City Inator** scanner must be visible on the **TRI CITY INATOR III** layout.
It shows a live table with BREAKOUT / CONTINUATION / PULLBACK signals and cup detection for all 15 symbols.

The Python monitor handles all auto-execution — TradingView is for visualization and signal generation only.

To switch to the Tri-City layout:
```
layout_switch("TRI CITY INATOR III")
```
