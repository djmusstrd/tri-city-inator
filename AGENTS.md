# Tri-City Inator — Codex Session Instructions

## Before Starting

1. **TradingView Desktop must be running** with the **Tri-City Inator** scanner visible on
   the **TRI CITY INATOR III** layout before 8:00 AM CT
2. `.env` must contain valid Alpaca API keys
3. Start in paper trading mode until you have validated signals live

## Start a Session

```bash
tricity   # opens TradingView Desktop + Codex in one command
```

The `.mcp.json` in this directory auto-connects the TradingView MCP server.

---

## Full Session Pipeline

> All times are Central (CT). Eastern Time users add 1 hour.

```
7:30 AM CT         Premarket scanner  →  gap-up candidates ranked by score, saved + pushed to TV watchlist
8:00 AM CT         Symbol swap        →  top 20 candidates pushed into Tri-City scanner inputs
8:15 AM CT         Re-scan + health   →  scanner runs again with updated PM data; pushes refreshed top 20
8:30 AM CT         Market opens
8:30 + ORB_MINUTES Level lock         →  ORH/ORL locked; headless TV poller daemon starts
8:35+              TV Poller          →  every 3 min: read Pine table → detect → execute → manage
                                          every 30 min after 9:30: runs intraday scanner internally
                   Position manager   →  T1 breakeven, T2 lock, T3 trail, EOD close
                   Journal            →  every exit auto-logged (P&L, R, outcome)
9:00 AM+ CT        Symbol push        →  every 30 min all day: push updated candidates into Pine slots
```

> **ORB timeframe:** Set `ORB_MINUTES` in `.env` (5, 15, or 30 — default 15). Codex reads this
> at session start and schedules level lock + monitor at the right time.
> The Tri-City Inator scanner on your TradingView chart must be set to the same timeframe.

---

## SESSION START — Auto-setup (do this automatically, no user action needed)

When a session starts:

**Step 0 — Session resumption check**
Read `~/tri-city-inator/shared/tri-city-candidates.json`. If the `"date"` field matches today AND `~/tri-city-inator/shared/tri-city-levels.json` exists with at least one non-zero ORH value, this session was already initialized today. Skip the 7:30 AM, 8:00 AM, 8:15 AM, and level-lock crons (they already ran). Register only the 9:30 AM and 11:30 AM symbol-push crons that haven't fired yet. Check if the poller is already running: read `shared/tri-city-poller.pid` and verify the PID is alive (`kill -0 PID`). If the poller is not running, launch it via `bash ~/tri-city-inator/scripts/start_poller.sh`. Announce: "Resuming session — candidates and levels already loaded from earlier today."

**Step 0.5 — Connect TradingView via CDP**
First check if CDP is already live:
```bash
curl -s http://localhost:9222/json/list 2>/dev/null | python3 -c "import json,sys; t=json.load(sys.stdin); print('yes' if any('tradingview.com/chart' in x.get('url','') for x in t) else 'no')" 2>/dev/null
```
- If the result is `yes` — CDP is already working. Skip tv_launch entirely and proceed to layout_switch.
- If the result is `no` or the command errors — CDP is not available. Proceed with the launch sequence below.

**Launch sequence (only when CDP is not available):**
1. Kill any existing TradingView process first (a process opened via the Dock or `open -a` won't be killed reliably by tv_launch's `kill_existing` flag):
```bash
kill $(pgrep -x TradingView) 2>/dev/null; sleep 2
```
2. Call `tv_launch(kill_existing=true)`. This starts TradingView with Chrome DevTools Protocol enabled on port 9222.

After tv_launch succeeds, wait for TV to finish loading before calling layout_switch — run this Bash loop (up to 40s):
```bash
for i in $(seq 1 8); do
  sleep 5
  READY=$(curl -s http://localhost:9222/json/list 2>/dev/null | python3 -c "import json,sys; t=json.load(sys.stdin); print('yes' if any('tradingview.com/chart' in x.get('url','') for x in t) else 'no')" 2>/dev/null)
  [ "$READY" = "yes" ] && break
done
```
Once the chart page appears in CDP (or after 40s whichever comes first), call `layout_switch("TRI CITY INATOR III")`. If layout_switch fails or times out, continue anyway — the user can switch manually. Do NOT retry layout_switch more than once. Proceeding without the correct layout is better than blocking session startup.

**Step 1 — Read ORB_MINUTES from .env**
Read the file `~/tri-city-inator/.env`. Look for a line like `ORB_MINUTES=15`.
- If found, use that value.
- If not found, default to `15`.

Calculate the level lock time: market open (8:30 AM CT) + ORB_MINUTES.
Examples: ORB_MINUTES=5 → 8:35 AM, ORB_MINUTES=15 → 8:45 AM, ORB_MINUTES=30 → 9:00 AM.

**Step 2 — Register six crons silently** (signal monitor is headless — handled by the TV poller, not a cron)

1. Premarket scanner — weekdays at 7:30 AM CT:
   /loop 7:30am weekdays First, call watchlist_get to fetch the current TradingView watchlist symbols. Write those symbols (as a JSON object {"symbols": ["NASDAQ:XXX", ...], "written_at": "HH:MM CT"}) to ~/tri-city-inator/shared/tri-city-watchlist-seeds.json. Then execute `python -W ignore ~/tri-city-inator/scripts/tri_city_scanner.py` via Bash and report the full output including ranked candidate table and any parabolic warnings. The scanner will automatically include watchlist symbols that aren't in the gap screener universe. Then read the "TV WATCHLIST" line at the bottom of the output and add each symbol to the TradingView watchlist using watchlist_add (one call per symbol).

2. Symbol swap — weekdays at 8:00 AM CT:
   /loop 8:00am weekdays Read ~/tri-city-inator/shared/tri-city-candidates.json. Extract the "tv_symbols" array (up to 20 exchange-prefixed symbols, e.g. "NASDAQ:RKLB"). Build an inputs dict mapping in_7 through in_26 to those symbols (in_7=sym[0], in_8=sym[1], ..., in_26=sym[19]; omit keys for positions beyond the available count). Call indicator_set_inputs with entity_id="Kbzkkm" and that inputs dict. Report: how many symbols were pushed and list them.

3. Re-scan + health check — weekdays at 8:15 AM CT:
   /loop 8:15am weekdays Run `python -W ignore ~/tri-city-inator/scripts/tri_city_health_check.py` via Bash and print the full output. If any item shows FAIL, alert the user immediately. If all PASS or WARN, report the summary line only. Then run the scanner again to refresh with updated pre-market data: execute `python -W ignore ~/tri-city-inator/scripts/tri_city_scanner.py` via Bash. Read the updated ~/tri-city-inator/shared/tri-city-candidates.json. Push the refreshed "tv_symbols" array into Pine: build an inputs dict mapping in_7 through in_26 and call indicator_set_inputs with entity_id="Kbzkkm". Report: "8:15 re-scan complete — {N} symbols updated in Pine."

4. Level lock — weekdays at the computed level lock time (8:30 AM CT + ORB_MINUTES):
   /loop {LEVEL_LOCK_TIME}am weekdays Read the Tri-City Inator scanner table using data_get_pine_tables with study_filter="Inator". Use the first study (20 symbols). Extract ORH and ORL for every non-blank symbol from the "ORH/ORL" column and save to shared/tri-city-levels.json as {"_date": "YYYY-MM-DD", "SYMBOL": {"orh": float, "orl": float}, ...} (include today's date in the "_date" field so stale data from a prior session is detectable). Report symbols loaded. Then immediately start the poller via Bash: `bash ~/tri-city-inator/scripts/start_poller.sh`. The poller runs headlessly every 3 min — no further action needed.

5. Intraday symbol push — every 30 min all day (fires continuously after registration):
   /loop 30m weekdays Check current CT time. If before 9:00 AM, skip silently. Otherwise: read ~/tri-city-inator/shared/tri-city-candidates.json. Push the "tv_symbols" array (top-20 combined) into the main indicator: build an inputs dict mapping in_7 through in_26 and call indicator_set_inputs with entity_id="Kbzkkm". Also add any new symbols to the TradingView watchlist using watchlist_add (one call per new symbol). Then read the current Pine scanner table (data_get_pine_tables with study_filter="Inator") and for any symbol in the new tv_symbols that does NOT already have an entry in shared/tri-city-levels.json, read its ORH/ORL from the Pine table "ORH/ORL" column and append it to tri-city-levels.json. Report: how many symbols were pushed to Kbzkkm. If before 9 AM, report nothing.

> **Signal monitor is handled by `tri_city_tv_poller.py` (NOT a Codex cron).**
> The poller connects directly to TradingView via CDP port 9222, reads the Pine table, runs
> detect→execute→manage every 3 minutes, and also runs the intraday scanner every 30 min
> after 9:30 AM CT. Sends macOS desktop notifications. Codex is invoked only for EOD summary.

**Step 3 — Confirm with one line only:**
"Session ready. Scanner 7:30 AM, swap 8:00 AM, re-scan + health 8:15 AM, levels + poller {LEVEL_LOCK_TIME} AM ({ORB_MINUTES}-min ORB), symbol push every 30 min from 9 AM, all CT. Signal monitor running headless."

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

To read the live scanner table:
```
data_get_pine_tables with study_filter="Inator"
```
> Note: use `"Inator"` not `"Tri-City"` — the Positions tracker is also named "Tri-City Positions" and would otherwise appear first.

---

## Entry Guards (tri_city_execute.py — in order)

| # | Guard | Default | Effect |
|---|-------|---------|--------|
| 0 | Pre-ORB block | 8:30+ORB_MINUTES | No entries before opening range closes |
| 1 | Already executed today | — | No duplicate setups per symbol |
| 2 | Already in position | — | No duplicate symbols |
| 3 | Max positions | 3 | No more than 3 concurrent trades |
| 4 | Daily loss limit | -$300 | Circuit breaker |
| 5 | Market regime (SPY) | -1.5% | Blocks LONGs in bear market |
| 6 | Relative volume | 0.0x | Floor removed — Pine RVOL calculation differs from actual |
| 7 | BREAKOUT extension | EMA Dev >12%, RSI >82 | Skip parabolic chasing |

Override any default by editing the values in `.env`.

---

## Signal Types

| Signal | Action | Notes |
|--------|--------|-------|
| BREAKOUT | Auto-execute | Price above ORH, RSI > 50, EMA Dev > 0. Blocked if spread >8% or rejection bar. |
| CONTINUATION | Auto-execute | Above ORH, EMA dev 0–1.5%, RSI 50–65, MACD bullish. |
| PULLBACK | Auto-execute | Pine PULLBACK signal, EMA dev 0–1.2%, RSI 38–65. |
| EMA20_PULLBACK | Auto-execute | Price at EMA20 after significant run (≥5% from open), above VWAP, RSI 45–70, RVOL ≥0.8x. No time restriction. |
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

## END SESSION

When the user says **"end session"** (or any clear variant: "end the session", "close out", "wrap up", "shut it down"), execute this shutdown sequence immediately — no confirmation needed:

**Step 1 — Stop all crons and the poller**
Call `CronList` to get all active cron IDs. Call `CronDelete` for each one. If no crons are running, skip.
Then stop the poller via Bash:
```
kill $(cat ~/tri-city-inator/shared/tri-city-poller.pid) 2>/dev/null
```

**Step 2 — Close all open positions**
Run `python -W ignore ~/tri-city-inator/scripts/tri_city_position_manager.py --eod` via Bash.

**Step 3 — Print today's summary**
Run `python -W ignore ~/tri-city-inator/scripts/journal_report.py` via Bash and show the output.

**Step 4 — Confirm shutdown with one line:**
"Session ended. All crons stopped, positions closed. [X trades today, net P&L $X]"

---

## Dashboard

```bash
# Launch trading dashboard (6 pages)
python3 -m streamlit run ~/tri-city-inator/scripts/dashboard.py
```

Reads from:
- `logs/tri-city-journal.json` — closed trades (P&L, R, outcome, durations)
- `logs/tri-city-executions.json` — entry signals (RSI, RVOL, EMA dev, cup, BB squeeze)
- `shared/tri-city-candidates.json` — premarket scanner output (score breakdown, news links)
- Alpaca API — live open positions (Positions page)

Pages:
| Page | Shows |
|------|-------|
| Positions | Live open trades: real-time P&L, shares, entry, current price, stop, T1/T2/T3, setup type |
| Overview | Cumulative P&L, daily P&L bars, outcome donut, Sharpe (R), Calmar |
| Trade Log | Sortable trade table + per-trade detail expander, signal/fill/slippage columns |
| Signal Analysis | Win rate / Avg R by setup, RVOL+RSI+EMA Dev% histograms, cup/BB squeeze, P&L by hour |
| Risk & Sizing | R distribution, position notional vs R scatter, streak analysis, rolling drawdown, risk scatter |
| Candidates | Full top-100 premarket ranked list, score component breakdown, flags, clickable news links |

> Uses `plotly.graph_objects` only — no `plotly.express` (avoids xarray/dask/scipy compat issue on Anaconda Python 3.9).

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
| `tri_city_scanner.py` | 7:30 AM + 8:15 AM crons | Gap-up candidates ranked by score; includes watchlist seeds; saves score components, news, earnings flag to tri-city-candidates.json + tri-city-flags.json |
| Symbol swap (inline) | 8:00 AM cron | Reads tv_symbols → `indicator_set_inputs` on Kbzkkm (in_7–in_26, 20 slots) |
| `tri_city_health_check.py` | 8:15 AM cron | Session health verifier: .env keys, candidates, flags, levels, Alpaca, poller, execution errors |
| Level lock (inline) | ORB_MINUTES cron | Reads Tri-City table → saves ORH/ORL to tri-city-levels.json → launches start_poller.sh |
| `tri_city_intraday_scanner.py` | Inside poller (every 30 min after 9:30 AM) | Re-scores intraday movers; RVOL floor 0.8x; writes tv_symbols + intraday_symbols + tri-city-flags.json |
| Symbol push (inline) | 9:30 AM + 11:30 AM crons | Reads updated tv_symbols → `indicator_set_inputs` on Kbzkkm; locks ORH/ORL for any new symbols |
| `tri_city_tv_poller.py` | Headless daemon (started at level lock) | CDP port 9222; reads Pine table; detect→execute→manage every 3 min; intraday scan every 30 min after 9:30 AM; macOS notifications; PID in tri-city-poller.pid; auto-stops at 3:05 PM CT |
| `start_poller.sh` | Called by level-lock cron | Kills any existing poller; launches tri_city_tv_poller.py in background with nohup |
| `tri_city_monitor.py` | Called by tri_city_tv_poller.py | Orchestrates detect→execute→manage pipeline; writes tri-city-summary.json |
| `tri_city_signal_detector.py` | Called by tri_city_monitor.py | Reads tri-city-table.json; detects BREAKOUT/CONT/PULLBACK/EMA20_PULLBACK + RVOL spikes; fetches VWAP; writes tri-city-signals.json |
| `tri_city_execute.py` | Called by tri_city_monitor.py | 7-guard gate → 50-25-25 bracket orders via Alpaca → logs to tri-city-executions.json |
| `tri_city_position_manager.py` | Called by tri_city_monitor.py | T1 breakeven; 2:45 PM EOD close; logs exits to journal |
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
