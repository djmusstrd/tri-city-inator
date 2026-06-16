# Sandbox: 3-Strategy Parallel System — PRD & Build TODO
**Created:** 2026-06-15  
**Status:** Pre-build — approved, not started  
**Trade date:** 2026-06-16 (tomorrow)

---

## Overview

Run 3 TradingView strategies simultaneously in paper mode. TradingView generates
the signals. Python handles clean execution, risk management, and journaling.
No Tri-City system is modified. Completely isolated.

```
TradingView SANDBOX layout (3 panes)
    Pane 1: QQQ 5-min  → Super Scalper 5/15 Min
    Pane 2: SPY 5-min  → Context Engine Bot v1
    Pane 3: IWM 5-min  → Supertrend (built-in, Factor=3 ATR=10)
         ↓
sandbox_poller.py  (reads signals every 2 min via TV MCP CDP)
         ↓
sandbox_execute.py (guards → Alpaca bracket orders)
         ↓
sandbox_position_manager.py (breakeven, trail, EOD close)
         ↓
logs/sandbox-journal.json + logs/sandbox-executions.json
```

---

## Decisions Locked

| Parameter | Value |
|---|---|
| Instruments | QQQ (S1), SPY (S2), IWM (S3) — one each, no conflicts |
| Direction | Long-only (all 3 strategies) |
| Capital per strategy | $5,000 each = $15,000 total sandbox |
| Risk per trade | 1% = $50 per trade per strategy |
| TradingView layout | New layout: "SANDBOX" |
| Supertrend source | TV built-in (Factor=3, ATR=10) |
| Tri-City system | Untouched — runs in parallel independently |

---

## Strategy Specs

### Strategy 1: Super Scalper → QQQ

**Pine logic (entry):**
- `longCondition = open < lower_band` (open below ATR band)
- `when = RSILong` (RSI25 > RSI100)
- Plots: `plotshape` label "Buy" on long entry

**Signal reading:**
- Method: `data_get_pine_labels(study_filter="Super Scalper")`
- Trigger: new label with text containing "Buy" on latest bar
- Dedup: track `last_buy_bar_index` in state file

**Our stop/target (overrides Pine's unused values):**
- Stop: `entry_price - (ATR14 * 2.0)`
- Target: `entry_price + (ATR14 * 5.0)`  ← matches Pine's intent
- Position size: `floor($50 / stop_distance)`

**Exit triggers (whichever first):**
1. Alpaca bracket stop hit
2. Alpaca bracket target hit
3. Pine "Sell" label appears → market close
4. Force flat 3:45 PM ET

---

### Strategy 2: Context Engine Bot v1 → SPY

**Pine logic (entry):**
- contextScore >= 70 AND (pullbackLong OR vwapReclaimLong OR compressionBreakoutLong)
- Trade window: 9:35–11:30 ET (built-in)
- `strategy.entry("LONG", strategy.long, qty=qty)`
- `strategy.exit("LONG EXIT", "LONG", stop=longStop, limit=longTarget)`

**Signal reading:**
- Method: `data_get_trades(study_filter="Context Engine")` or `data_get_strategy_results`
- Trigger: new open trade appears in strategy results (strategy.position_size goes 0 → positive)
- Dedup: compare current open trade entry bar vs `last_entry_bar` in state file

**Our stop/target:**
- Stop: `entry_price - (ATR14 * 1.2)`   ← matches Pine's `atrStopMult=1.2`
- Target: `entry_price + (risk_per_share * 2.0)` (base R) or `* 3.0` (strong, score>=85)
- Position size: `floor($50 / stop_distance)`

**Exit triggers (whichever first):**
1. Alpaca bracket stop hit
2. Alpaca bracket target hit
3. Pine strategy position_size → 0 (strategy closed it) → market close
4. Force flat 3:45 PM ET

---

### Strategy 3: Supertrend → IWM

**Pine logic (direction):**
- TV built-in Supertrend: Factor=3.0, ATR Length=10
- Output: `direction` series — `1` = bullish, `-1` = bearish
- Bullish flip: direction changes -1 → 1 = BUY signal
- Bearish flip: direction changes 1 → -1 = CLOSE signal (long-only, no short)

**Signal reading:**
- Method: `data_get_study_values(study_filter="Supertrend")`
- Read "direction" output value on latest bar
- Trigger: direction transitions from -1 to 1 since last poll
- Dedup: track `last_direction` in state file

**Our stop/target:**
- Stop: `entry_price - (ATR14 * 2.0)` (wider, trend-following)
- Target: `entry_price + (ATR14 * 4.0)` (4R — trend can run)
- Position size: `floor($50 / stop_distance)`

**Exit triggers (whichever first):**
1. Alpaca bracket stop hit
2. Alpaca bracket target hit
3. Pine direction flips -1 (bearish) → market close
4. Force flat 3:45 PM ET

---

## Execution Guards (all 3 strategies share these)

Applied in order before any order is placed:

| # | Guard | Value | Applies to |
|---|---|---|---|
| 0 | Long-only lock | Hard-coded | All 3 |
| 1 | Trade window | 9:35 AM – 2:30 PM ET | All 3 (override each Pine window) |
| 2 | Already in position | 1 max per strategy | All 3 |
| 3 | Daily loss lock | -$150 per strategy (-3%) | All 3 |
| 4 | No re-entry same symbol | After stop-out, blocked rest of day | All 3 |
| 5 | Minimum qty | qty >= 1 | All 3 |
| 6 | Stale signal | Signal bar > 2 bars old → skip | All 3 |

---

## Position Management

Same structure for all 3 strategies:

```
Entry → bracket order placed (stop + limit via Alpaca OCO)
T1 hit (limit): close full position (no partial scaling — simpler for sandbox)
Stop hit:       full position closed
Force flat:     market close at 3:45 PM ET regardless
Pine exit signal: market close if Alpaca bracket not yet hit
```

No partial scaling in sandbox (50-25-25 is Tri-City's approach — sandbox keeps
it simple with full-exit at target or stop to maximize clarity of results).

---

## State File: `shared/sandbox-state.json`

```json
{
  "date": "2026-06-16",
  "strategies": {
    "super_scalper": {
      "symbol": "QQQ",
      "last_buy_bar": null,
      "position": null,
      "entry_price": null,
      "stop": null,
      "target": null,
      "qty": 0,
      "order_id": null,
      "daily_pnl": 0.0,
      "stopped_today": false
    },
    "context_engine": {
      "symbol": "SPY",
      "last_entry_bar": null,
      "last_direction": null,
      "position": null,
      "entry_price": null,
      "stop": null,
      "target": null,
      "qty": 0,
      "order_id": null,
      "daily_pnl": 0.0,
      "stopped_today": false
    },
    "supertrend": {
      "symbol": "IWM",
      "last_direction": null,
      "position": null,
      "entry_price": null,
      "stop": null,
      "target": null,
      "qty": 0,
      "order_id": null,
      "daily_pnl": 0.0,
      "stopped_today": false
    }
  }
}
```

---

## File Map

### New files to create

```
scripts/
  sandbox_poller.py          Main loop: reads TV → detect → execute → manage
  sandbox_signal_reader.py   TV MCP signal reading for all 3 strategies
  sandbox_execute.py         Guards + Alpaca bracket order placement
  sandbox_position_manager.py Breakeven, force-flat, Alpaca position sync
  start_sandbox_poller.sh    Launch script (nohup, writes PID)
  stop_sandbox_poller.sh     Kill script

pine/
  super_scalper_fixed.pine   Original + strategy.exit added (for TV load)
  context_engine_v1.pine     Context Engine (already have, for TV load)

shared/
  sandbox-state.json         Runtime state (auto-created on first run)

logs/
  sandbox-journal.json       One entry per closed trade
  sandbox-executions.json    One entry per entry order
  sandbox-poller.log         stdout/stderr from poller

strategies/
  sandbox_prd.md             This file
```

### Unchanged files (Tri-City system — do not touch)

```
scripts/tri_city_tv_poller.py
scripts/tri_city_monitor.py
scripts/tri_city_signal_detector.py
scripts/tri_city_execute.py
scripts/tri_city_position_manager.py
shared/tri-city-levels.json
shared/tri-city-candidates.json
CLAUDE.md
```

---

## Build TODO (ordered — do not skip steps)

### PHASE 0 — Pine setup on TradingView (tonight, before build)
- [ ] 0.1  Launch TradingView Desktop via `tv_launch` + CDP verification
- [ ] 0.2  Create new layout: `layout_switch` or via TV UI — name it "SANDBOX"
- [ ] 0.3  Set Pane 1: symbol=QQQ, timeframe=5min
- [ ] 0.4  Set Pane 2: symbol=SPY, timeframe=5min
- [ ] 0.5  Set Pane 3: symbol=IWM, timeframe=5min
- [ ] 0.6  Load Super Scalper onto Pane 1 via `pine_new` + `pine_set_source` + `pine_save`
           then `chart_manage_indicator` to add to QQQ pane
- [ ] 0.7  Load Context Engine Bot v1 onto Pane 2 same way
- [ ] 0.8  Add built-in Supertrend to Pane 3: `chart_manage_indicator("Supertrend")`
           Set inputs: Factor=3.0, ATR Length=10
- [ ] 0.9  Verify all 3 are visible and not showing errors (`pine_get_errors` for S1/S2)
- [ ] 0.10 Save layout as "SANDBOX"
- [ ] 0.11 Screenshot to confirm layout looks correct (`capture_screenshot`)

### PHASE 1 — Signal reader (`sandbox_signal_reader.py`)
- [ ] 1.1  `read_super_scalper_signal(state)` 
           Call `data_get_pine_labels(study_filter="Super Scalper")`
           Find labels with text "Buy" on the most recent bar
           Return signal dict if new (bar_index > state.last_buy_bar), else None
- [ ] 1.2  `read_context_engine_signal(state)`
           Call `data_get_strategy_results` or `data_get_trades(study_filter="Context Engine")`
           Check if open trade exists with entry bar > state.last_entry_bar
           Also check if open trade closed (position_size → 0) → return CLOSE signal
           Return signal dict if new entry detected, CLOSE dict if exit detected, else None
- [ ] 1.3  `read_supertrend_signal(state)`
           Call `data_get_study_values(study_filter="Supertrend")`
           Read current `direction` value
           If direction == 1 and state.last_direction == -1: return BUY signal
           If direction == -1 and state.last_direction == 1: return CLOSE signal
           Always update state.last_direction
           Return signal dict or None
- [ ] 1.4  `fetch_current_atr(symbol)` 
           Fetch last 20 5-min bars from Alpaca for symbol
           Compute ATR14 (rma of true range)
           Return ATR value (used for stop/target calc by all 3 strategies)
- [ ] 1.5  Unit test: dry-run all 3 readers against current TV chart state, print output

### PHASE 2 — Execution layer (`sandbox_execute.py`)
- [ ] 2.1  `load_state()` / `save_state()` — reads/writes `shared/sandbox-state.json`
           Auto-creates file with defaults if missing or date mismatch (new day)
- [ ] 2.2  `check_guards(strategy_name, state, current_time_et)` — returns (ok, reason)
           Guard 0: direction == "long" (hard-coded, always pass for now)
           Guard 1: 9:35 AM <= current_time_et <= 2:30 PM
           Guard 2: state[strategy].position is None (flat)
           Guard 3: state[strategy].daily_pnl >= -150
           Guard 4: not state[strategy].stopped_today
           Guard 5: signal bar age <= 2 bars (signal.bar_index >= chart_last_bar - 2)
- [ ] 2.3  `calc_sizing(entry_price, atr, strategy_name)` — returns (stop, target, qty)
           Per-strategy stop/target multipliers (S1: ATR*2/ATR*5, S2: ATR*1.2/2R, S3: ATR*2/ATR*4)
           qty = floor($50 / stop_distance), minimum 1
- [ ] 2.4  `place_entry(symbol, qty, stop, target)` — returns order_id
           alpaca-py: submit market order, then bracket (OCO stop + limit)
           Log to `logs/sandbox-executions.json`
           Update state: position="long", entry_price, stop, target, qty, order_id
- [ ] 2.5  `close_position(symbol, strategy_name, reason)` — returns exit_price
           alpaca-py: cancel open orders on symbol, submit market close
           Log realized P&L to `logs/sandbox-journal.json`
           Update state: position=None, daily_pnl += realized_pnl
           Set stopped_today=True if reason=="stop"
- [ ] 2.6  `sync_positions_from_alpaca(state)` — reconciliation
           On each poll: fetch Alpaca open positions, compare to state
           If Alpaca shows flat but state shows long: position was closed by bracket
           → write journal exit, update state accordingly
           This is the key reliability mechanism (bracket orders close without our code)

### PHASE 3 — Position manager (`sandbox_position_manager.py`)
- [ ] 3.1  `check_force_flat(state, current_time_et)` 
           If time >= 3:45 PM ET and any strategy has open position: close it
- [ ] 3.2  `check_pine_close_signals(state)` 
           Read close signals from TV (Super Scalper "Sell" label, CE position=0, ST direction=-1)
           If close signal for a strategy that has open position: call close_position()
- [ ] 3.3  `check_daily_reset(state)` 
           If state.date != today: reset all daily_pnl, stopped_today, clear positions

### PHASE 4 — Main poller (`sandbox_poller.py`)
- [ ] 4.1  Main loop (every 120 seconds):
           ```
           while True:
               sync_positions_from_alpaca(state)   # reconcile first
               check_daily_reset(state)
               check_force_flat(state, now_et)
               check_pine_close_signals(state)      # check exits before entries
               
               for strategy in [super_scalper, context_engine, supertrend]:
                   signal = read_signal(strategy, state)
                   if signal and signal.type == "BUY":
                       ok, reason = check_guards(strategy, state, now_et)
                       if ok:
                           atr = fetch_current_atr(state[strategy].symbol)
                           stop, target, qty = calc_sizing(signal.price, atr, strategy)
                           place_entry(state[strategy].symbol, qty, stop, target)
                       else:
                           log_blocked(strategy, reason)
               
               save_state(state)
               sleep(120)
           ```
- [ ] 4.2  Startup: verify CDP live (curl localhost:9222), verify SANDBOX layout active
           If layout wrong: call `layout_switch("SANDBOX")`
           Log startup state to sandbox-poller.log
- [ ] 4.3  Graceful shutdown: SIGTERM handler → close all positions → save state → exit
- [ ] 4.4  macOS desktop notification on every entry/exit (same as Tri-City poller)
- [ ] 4.5  Auto-stop at 4:00 PM ET (same pattern as tri_city_tv_poller.py)

### PHASE 5 — Launch script (`start_sandbox_poller.sh`)
- [ ] 5.1  Kill existing sandbox poller if running (`shared/sandbox-poller.pid`)
- [ ] 5.2  Launch `sandbox_poller.py` with nohup, redirect to `logs/sandbox-poller.log`
- [ ] 5.3  Write PID to `shared/sandbox-poller.pid`
- [ ] 5.4  Print "Sandbox poller started (PID XXXX)"

### PHASE 6 — Dry-run test (before market open tomorrow)
- [ ] 6.1  Run `python -W ignore scripts/sandbox_poller.py --dry-run`
           --dry-run mode: reads signals, prints what it WOULD do, places NO orders
- [ ] 6.2  Verify signal reader returns valid data for all 3 strategies
- [ ] 6.3  Verify guard logic triggers correctly (test: manually set daily_pnl=-200, confirm G3 blocks)
- [ ] 6.4  Verify position sync works (manually open a small QQQ position in Alpaca paper, 
           confirm poller detects and logs it)
- [ ] 6.5  Verify force-flat fires at 3:45 PM in dry-run

### PHASE 7 — Dashboard tab (`scripts/dashboard.py`)
- [ ] 7.1  Add "Sandbox" page to sidebar
- [ ] 7.2  Three columns: one per strategy (QQQ/SPY/IWM)
           Each column shows: win rate, P&L, trades today, current position status
- [ ] 7.3  Trade log table: symbol | strategy | entry | exit | P&L | R | outcome
- [ ] 7.4  Side-by-side comparison: Sandbox total vs Tri-City total for same day

---

## Tomorrow Morning Sequence

```
8:30 AM CT   TradingView Desktop opens (user)
             Claude session starts → verifies SANDBOX layout active
             bash ~/tri-city-inator/scripts/start_sandbox_poller.sh

8:35 AM CT   Tri-City level-lock fires (launchd — unchanged)
             Sandbox poller already reading signals (no ORB dependency)

9:35 AM CT   Trade window opens for all 3 sandbox strategies
             First signals eligible for execution

3:45 PM ET   Force-flat fires for all 3 sandbox positions

4:00 PM ET   Sandbox poller auto-stops
```

---

## What the User Needs to Do

**Tonight:** Nothing — Claude handles all Pine loading and TV layout setup.

**Tomorrow:**
1. Open TradingView Desktop (or it will be launched at session start)
2. Run `bash ~/tri-city-inator/scripts/start_sandbox_poller.sh`
3. Check `logs/sandbox-poller.log` to confirm it's reading signals
4. Watch the dashboard Sandbox tab throughout the day

That's it. Everything else is automated.

---

## Success Criteria (Day 1)

| Check | Pass condition |
|---|---|
| Signal reading | All 3 strategies produce at least 1 readable signal before 10 AM |
| Guard logic | No duplicate entries, no trades outside window |
| Order placement | All entries confirmed in Alpaca paper |
| Position sync | Bracket exits auto-detected within 1 poll cycle (2 min) |
| Force flat | All positions closed by 3:45 PM ET |
| Journal | All trades logged with entry/exit/P&L |
| Tri-City | Unaffected — still running independently |

---

## Risk / What Could Go Wrong

| Risk | Mitigation |
|---|---|
| TV MCP signal format unknown until runtime | Phase 1.5 dry-run test verifies before market open |
| Alpaca bracket order not filling (thin IWM premarket) | All entries at market open only (guard G1: window starts 9:35 AM) |
| Super Scalper RSI100 slow to initialize | QQQ has years of 5-min history — no warmup issue |
| State file corruption | JSON load wrapped in try/except → reset to defaults |
| Network blip during poll | Retry once, then skip cycle, log warning |
| Both poller + Tri-City reading TV simultaneously | TV MCP is read-only — safe for concurrent reads |
| Pine signal on wrong bar (repainting) | Guard G5: reject signals older than 2 bars |
