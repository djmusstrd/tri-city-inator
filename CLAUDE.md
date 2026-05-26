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
8:30 AM CT         Symbol swap        →  top 20 candidates pushed into Tri-City scanner inputs
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

**Step 0 — Session resumption check**
Read `~/tri-city-inator/shared/tri-city-candidates.json`. If the `"date"` field matches today AND `~/tri-city-inator/shared/tri-city-levels.json` exists with at least one non-zero ORH value, this session was already initialized today. Skip the 7:30 AM, 8:30 AM, and level-lock crons (they already ran). Register only the signal monitor and intraday scan crons that haven't fired yet. Announce: "Resuming session — candidates and levels already loaded from earlier today."

**Step 1 — Read ORB_MINUTES from .env**
Read the file `~/tri-city-inator/.env`. Look for a line like `ORB_MINUTES=15`.
- If found, use that value.
- If not found, default to `15`.

Calculate the level lock time: market open (8:30 AM CT) + ORB_MINUTES.
Examples: ORB_MINUTES=5 → 8:35 AM, ORB_MINUTES=15 → 8:45 AM, ORB_MINUTES=30 → 9:00 AM.

**Step 2 — Register seven crons silently**

1. Premarket scanner — weekdays at 7:30 AM CT:
   /loop 7:30am weekdays Run the Tri-City premarket scanner: execute `python -W ignore ~/tri-city-inator/scripts/tri_city_scanner.py` via Bash and report the full output including ranked candidate table and any parabolic warnings. Then read the "TV WATCHLIST" line at the bottom of the output and add each symbol to the TradingView watchlist using watchlist_add (one call per symbol).

2. Symbol swap — weekdays at 8:30 AM CT:
   /loop 8:30am weekdays Read ~/tri-city-inator/shared/tri-city-candidates.json. Extract the "tv_symbols" array (up to 20 exchange-prefixed symbols, e.g. "NASDAQ:RKLB"). Build an inputs dict mapping in_7 through in_26 to those symbols (in_7=sym[0], in_8=sym[1], ..., in_26=sym[19]; omit keys for positions beyond the available count). Call indicator_set_inputs with entity_id="Kbzkkm" and that inputs dict. Report: how many symbols were pushed and list them.

3. Health check — weekdays at 8:31 AM CT:
   /loop 8:31am weekdays Run `python -W ignore ~/tri-city-inator/scripts/tri_city_health_check.py` via Bash and print the full output. If any item shows FAIL, alert the user immediately. If all PASS or WARN, report the summary line only.

4. Level lock — weekdays at the computed level lock time (8:30 AM CT + ORB_MINUTES):
   /loop {LEVEL_LOCK_TIME}am weekdays Read the Tri-City Inator scanner table using data_get_pine_tables with study_filter="Tri-City". Use the first study (20 symbols). Extract ORH and ORL for every non-blank symbol from the "ORH/ORL" column and save to shared/tri-city-levels.json as {"SYMBOL": {"orh": float, "orl": float}, ...}. Report symbols loaded.

5. Intraday scan #1 — weekdays at 9:30 AM CT:
   /loop 9:30am weekdays Run the Tri-City intraday scanner: execute `python -W ignore ~/tri-city-inator/scripts/tri_city_intraday_scanner.py --source intraday_930` via Bash and report the full output including any new additions and re-scored symbols. Then read ~/tri-city-inator/shared/tri-city-candidates.json. Push the "tv_symbols" array (top-20 combined) into the main indicator: build an inputs dict mapping in_7 through in_26 and call indicator_set_inputs with entity_id="Kbzkkm". Push the "intraday_symbols" array (top-5 NEW intraday-only movers) into the intraday watcher: build a second inputs dict mapping in_7 through in_11 and call indicator_set_inputs with entity_id="Vk6rtV". If "intraday_symbols" is empty, leave Vk6rtV unchanged. Also add any new symbols to the TradingView watchlist using watchlist_add (one call per new symbol). Report: how many new symbols were added to the pool, how many were pushed to Kbzkkm, and which symbols went to Vk6rtV.

6. Intraday scan #2 — weekdays at 11:30 AM CT:
   /loop 11:30am weekdays Run the Tri-City intraday scanner: execute `python -W ignore ~/tri-city-inator/scripts/tri_city_intraday_scanner.py --source intraday_1130` via Bash and report the full output including any new additions and re-scored symbols. Then read ~/tri-city-inator/shared/tri-city-candidates.json. Push the "tv_symbols" array (top-20 combined) into the main indicator: build an inputs dict mapping in_7 through in_26 and call indicator_set_inputs with entity_id="Kbzkkm". Push the "intraday_symbols" array (top-5 NEW intraday-only movers) into the intraday watcher: build a second inputs dict mapping in_7 through in_11 and call indicator_set_inputs with entity_id="Vk6rtV". If "intraday_symbols" is empty, leave Vk6rtV unchanged. Also add any new symbols to the TradingView watchlist using watchlist_add (one call per new symbol). Report: how many new symbols were added to the pool, how many were pushed to Kbzkkm, and which symbols went to Vk6rtV.

7. Signal monitor — weekdays every 3 minutes (starts at level lock time):
   /loop 3m
   (a) Call data_get_pine_tables with study_filter="Tri-City". Use the FIRST study (20-symbol main scanner, not the 5-slot intraday watcher). Extract its rows array.
   (b) Write those rows to ~/tri-city-inator/shared/tri-city-table.json as a JSON array of strings.
   (c) Run `python -W ignore ~/tri-city-inator/scripts/tri_city_signal_detector.py` via Bash.
   (d) Read ~/tri-city-inator/shared/tri-city-signals.json.
   (e) For each entry in "signals": report it, then execute via Bash: `python -W ignore ~/tri-city-inator/scripts/tri_city_execute.py --symbol {symbol} --price {price} --orh {orh} --orl {orl} --rsi {rsi} --ema_dev {ema_dev} --signal "{setup}" --setup {setup} {--cup if cup=true} {--htf if htf=true} --quiet`
   (f) If execute output contains "POST_CUTOFF_SIGNAL": alert "@user — {symbol} met {setup} conditions at ${price} after the {cutoff} cutoff. RSI {rsi}, EMA Dev% {ema_dev:+.2f}%, ORH ${orh}, Stop ${stop}, Risk/share ${risk_per_share}, Size {shares} shares. Cup: {YES/NO}. Reply 'yes {symbol}' to execute with --override-cutoff."
   (g) For each entry in "rvol_spikes": alert "⚡ RVOL SPIKE: {symbol} {prev:.1f}x → {now:.1f}x"
   (h) If a symbol in "signals" has resistance=true: note "⚠ {symbol} near 52-wk high" (caution only, do NOT block)
   (i) Run `python -W ignore ~/tri-city-inator/scripts/tri_city_position_manager.py` via Bash and print any output.
   If "signals" is empty AND no POST_CUTOFF_SIGNAL AND "rvol_spikes" is empty, stay silent.

**Step 3 — Confirm with one line only:**
"Session ready. Scanner 7:30 AM, swap 8:30 AM, health check 8:31 AM, levels lock + monitor {LEVEL_LOCK_TIME} AM ({ORB_MINUTES}-min ORB), intraday scans 9:30 AM + 11:30 AM, all CT."

Do not show the /loop commands to the user. Do not ask them to paste anything.

---

## TradingView Scanner

| Item | Value |
|------|-------|
| Layout | **TRI CITY INATOR III** (ID 168250176) |
| Indicator | **Tri-City Inator** (Pine shorttitle: "Tri-City") |
| Entity ID | `Kbzkkm` |
| Symbol inputs | `in_7` through `in_26` (20 slots) |
| Table columns | SYMBOL · PRICE · RSI · EMA DEV% · RVOL · ORH/ORL · CUP · SMA↑ · SIGNAL |
| Intraday Watcher Entity ID | `Vk6rtV` |
| Intraday Watcher inputs | `in_7` through `in_11` (5 slots, same Tri-City code) |

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
| PULLBACK | Auto-execute | At/above EMA, dev 0–+0.8%, RSI 38–55 |
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
| `tri_city_scanner.py` | 7:30 AM cron | Gap-up candidates ranked by score, saved to tri-city-candidates.json + tri-city-flags.json |
| Symbol swap (inline) | 8:30 AM cron | Reads tv_symbols → `indicator_set_inputs` on Kbzkkm (in_7–in_26, 20 slots) |
| `tri_city_health_check.py` | 8:31 AM cron | Session health verifier: .env keys, candidates, flags, levels, Alpaca, execution errors |
| Level lock (inline) | ORB_MINUTES cron | Reads Tri-City table → saves ORH/ORL to tri-city-levels.json |
| `tri_city_intraday_scanner.py` | 9:30 AM + 11:30 AM crons | Re-scores intraday movers; writes tv_symbols + intraday_symbols + tri-city-flags.json |
| `tri_city_signal_detector.py` | Signal monitor (sub-step) | Reads tri-city-table.json; detects BREAKOUT/CONT/PULLBACK + RVOL spikes; writes tri-city-signals.json |
| `tri_city_execute.py` | Signal monitor | 7-guard gate → 50-25-25 bracket orders via Alpaca → logs to tri-city-executions.json |
| `tri_city_position_manager.py` | Signal monitor | T1 hit → breakeven stop; 3:45 PM → EOD close all; logs exits to journal |
| `tri_city_backtest.py` | Manual | Historical simulation with P&L report |
| `journal_report.py` | Manual | Performance report from trade journal |

---

## TradingView Indicator

The **Tri-City Inator** scanner must be visible on the **TRI CITY INATOR III** layout.
It shows a live table with BREAKOUT / CONTINUATION / PULLBACK signals and cup detection for all 20 symbols.

The Python monitor handles all auto-execution — TradingView is for visualization and signal generation only.

To switch to the Tri-City layout:
```
layout_switch("TRI CITY INATOR III")
```
