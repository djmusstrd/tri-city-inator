# Tri-City Inator — Cheat Sheet

## Three Systems, One Account

| | System 1 — Tri-City Inator | System 3 — Sandbox |
|---|---|---|
| **Style** | Intraday momentum, gap-and-go | Intraday strategy mirroring |
| **Universe** | 20 gap-up fresh listings / day | QQQ · SPY · IWM (fixed) |
| **Signal** | BREAKOUT / CONTINUATION / PULLBACK / EMA20_PULLBACK / SUPERTREND_FLIP / CONSOL_BREAK | Super Scalper (QQQ) · Supertrend (SPY / IWM) |
| **Holds** | Intraday — closed by 2:45 PM CT | Intraday — closed by 2:45 PM ET |
| **Positions** | 3–6 max concurrent | 2 max concurrent |
| **Exit** | T1 +5% → breakeven · T2 +12% → lock · T3 trail EMA20/VWAP | Stop ATR×2 · Target ATR×5 (SS) / ATR×4 (ST) |
| **Capital** | Main paper account | $5,000 sandbox allocation |
| **Signals from** | TradingView Pine scanner (Kbzkkm) | TradingView SANDBOX layout (CDP) |

> System 2 (Confirmed Continuation) is designed but not yet built. See `strategies/confirmed_continuation_prd.md`.

---

## Daily Schedule (Central Time)

| Time (CT) | Event | Who runs it |
|---|---|---|
| 7:30 AM | Premarket scanner fires | Claude cron — `tri_city_scanner.py` |
| 8:00 AM | Symbol swap — top 20 pushed to Pine slots | Claude cron — inline |
| 8:15 AM | Re-scan + health check | Claude cron — `tri_city_health_check.py` |
| 8:30 AM | Market opens | — |
| 8:36 / 8:46 / 9:01 AM | Level lock + poller start | launchd — `tri_city_level_lock.py` |
| 9:00 AM+ | Symbol push every 30 min | Claude cron — inline |
| 9:30 AM | Intraday scanner starts (inside poller) | Poller internal — `tri_city_intraday_scanner.py` |
| 2:45 PM CT | EOD close — all positions closed | Poller internal — `tri_city_position_manager.py` |
| 3:05 PM CT | TV poller auto-stops | `tri_city_tv_poller.py` |

> ORB window: set `ORB_MINUTES` in `.env` (5 / 15 / 30, default 15).
> Level lock fires at 8:30 + ORB_MINUTES. launchd fires at 8:36, 8:46, and 9:01 AM to cover all three settings.

---

## Session Startup

```bash
# Full session (opens TradingView + wires all crons automatically)
tricity
```

Claude does everything on session start:
1. Checks if today already initialized (candidates.json date + levels.json)
2. Connects TradingView via CDP — launches if needed, switches to TRI CITY INATOR III layout
3. Reads ORB_MINUTES from .env
4. Registers 4 crons: 7:30 AM scanner · 8:00 AM swap · 8:15 AM re-scan · 9:00 AM+ symbol push
5. Level lock + poller are handled by launchd (not Claude crons)

---

## Signal Types (System 1)

| Signal | Entry condition | Auto-executed |
|---|---|---|
| BREAKOUT | Price > ORH · RSI > 50 · EMA dev > 0 | ✅ |
| CONTINUATION | Above ORH · EMA dev 0–1.5% · RSI 50–65 · MACD bullish | ✅ |
| PULLBACK | Pine PULLBACK signal · EMA dev 0–1.2% · RSI 38–65 | ✅ |
| EMA20_PULLBACK | At EMA20 after ≥5% run · above VWAP · RSI 45–70 · RVOL ≥0.8x | ✅ |
| SUPERTREND_FLIP | Supertrend direction changes bullish · stop = ST band | ✅ |
| CONSOL_BREAK | Base breakout · premarket/AH base included | ✅ |
| --- | No signal | Silent skip |

CUP = YES adds `--cup` flag for high-conviction log only (does not change execution).

---

## Entry Guards (System 1, in order)

| # | Guard | Default | Notes |
|---|---|---|---|
| 0 | Pre-ORB block | 8:30 + ORB_MINUTES | No entries before opening range closes |
| 1 | Already executed today | — | No duplicate setups per symbol |
| 2 | Already in position | — | No duplicate symbols |
| 3 | Max positions | 6 | Set via `MAX_POSITIONS` in .env |
| 4 | Daily loss limit | -$2,500 | Set via `MAX_DAILY_LOSS` in .env |
| 5 | Market regime (SPY) | -1.5% | Blocks LONGs in bear market |
| 6 | RVOL floor | 0.0x | Floor removed; Pine RVOL differs from actual |
| 7 | BREAKOUT extension | EMA dev >12% · RSI >82 | Skip parabolic chasing |

All blocked signals logged to `logs/tri-city-blocked-signals.json` with reason.

---

## Position Structure — System 1 (50-25-25)

```
Entry
  → T1 (+T1_PCT%):  sell 50% → move stop to breakeven
  → T2 (+T2_PCT%):  sell 25% → lock stop at T2 level
  → T3 trail 25%:   close on EMA20 + VWAP breach — or EOD at 2:45 PM CT
  → Stop (−5% or ATR): all shares exit

Defaults: T1=5%  T2=12%  Stop=5%  (set in .env)
```

Quick-lock: if unrealized ≥ $100 and held ≥ 3 min → stop → breakeven (before T1 fires).
Free-ride:  if unrealized ≥ max($300, 1R) and price ≥ entry+2% → bank 50% → stop → entry−$0.05.

---

## System 3 — Sandbox (QQQ / SPY / IWM)

### How it works

```
Every 2 minutes (9:30 AM – 2:50 PM ET):

  1. Read filledOrders from _reportData on each chart widget via CDP
       QQQ → Super Scalper - 5 Min 15 Min
       SPY → Supertrend Strategy (10, 3)
       IWM → Supertrend Strategy (10, 3)

  2. New order with tm > last_seen_tm?
       b=True  + e=True  → LONG signal
       b=False + e=True  → EXIT signal (Supertrend flipped short)

  3. LONG signal → 6 guards → ATR-sized bracket order (Alpaca)
  4. EXIT signal → cancel bracket legs → market close
  5. EOD 2:45 PM ET → force-flat all sandbox positions
```

### Sandbox Guards (in order)

| # | Guard | Default |
|---|---|---|
| 1 | Market hours | 9:35 AM – 2:30 PM ET |
| 2 | Max positions | 2 |
| 3 | Already executed today | — |
| 4 | Stopped out today | No same-day re-entry |
| 5 | Daily loss lock | -$150 |
| 6 | Already in position | — |

### Sandbox Sizing

```
risk_dollars  = $5,000 × 0.5%  = $25
atr_stop_dist = ATR(14, 5-min) × 2.0
qty           = max(1, floor(risk_dollars / atr_stop_dist))
qty           = min(qty, floor($5,000 / price))   ← cash cap

Stop:    entry − ATR×2
Target:  entry + ATR×5  (Super Scalper)
         entry + ATR×4  (Supertrend)
```

### Start / Stop Sandbox

```bash
# Dry-run (log signals, no real orders)
bash ~/tri-city-inator/scripts/start_sandbox_poller.sh --dry-run

# Live (real paper orders)
kill $(cat ~/tri-city-inator/shared/sandbox-poller.pid)
bash ~/tri-city-inator/scripts/start_sandbox_poller.sh

# Stop
kill $(cat ~/tri-city-inator/shared/sandbox-poller.pid)
```

**TV requirement:** SANDBOX layout must be open in TradingView (3-pane QQQ/SPY/IWM on 5-min, Super Scalper on QQQ, Supertrend Strategy on SPY + IWM).

---

## Key Scripts

| Script | Triggered by | Does |
|---|---|---|
| `tri_city_scanner.py` | 7:30 AM + 8:15 AM crons | Gap-up candidates ranked by score; saves to `tri-city-candidates.json` |
| `tri_city_health_check.py` | 8:15 AM cron | Verifies .env, candidates, levels, Alpaca, poller, errors |
| `tri_city_level_lock.py` | launchd 8:36/8:46/9:01 AM | Reads ORH/ORL from Pine table → saves to `tri-city-levels.json` → starts poller |
| `tri_city_tv_poller.py` | `start_poller.sh` (at level lock) | CDP daemon: reads Pine table → detect → execute → manage every 3 min |
| `tri_city_intraday_scanner.py` | Inside poller every 30 min after 9:30 AM | Re-scores intraday movers; updates `tv_symbols` |
| `tri_city_signal_detector.py` | Called by poller | Detects BREAKOUT/CONT/PULLBACK/EMA20_PULLBACK + RVOL spikes |
| `tri_city_execute.py` | Called by poller | 7-guard gate → 50-25-25 bracket → logs to `tri-city-executions.json` |
| `tri_city_position_manager.py` | Called by poller | T1 breakeven · quick-lock · free-ride · EOD close · journal |
| `tri_city_alert_monitor.py` | `start_poller.sh` | Watches Alpaca/TV alerts → routes to execution |
| `start_poller.sh` | `tri_city_level_lock.py` | Kills old poller; starts `tri_city_tv_poller.py` + alert monitor via nohup |
| `sandbox_signal_reader.py` | Sandbox poller | Reads `_reportData.filledOrders` from SANDBOX chart widgets via CDP |
| `sandbox_execute.py` | Sandbox poller | 6 guards → ATR bracket order (Alpaca paper) |
| `sandbox_position_manager.py` | Sandbox poller | Exit-signal close · EOD force-flat · journal |
| `sandbox_poller.py` | `start_sandbox_poller.sh` | 2-min loop: read → execute → manage |
| `journal_report.py` | Manual / EOD | Performance report from trade journal |
| `dashboard.py` | Manual | Streamlit dashboard (6 pages) |
| `tri_city_backtest.py` | Manual | Historical simulation |

---

## State Files

| File | Written by | Contains |
|---|---|---|
| `shared/tri-city-candidates.json` | Scanner (7:30 AM) | Top-100 gap-up candidates, scores, news, flags |
| `shared/tri-city-flags.json` | Scanner / intraday scanner | Cup/BB/earnings/news flags per symbol |
| `shared/tri-city-levels.json` | `tri_city_level_lock.py` | ORH / ORL per symbol for today |
| `shared/tri-city-table.json` | TV poller (each cycle) | Live Pine scanner table snapshot |
| `shared/tri-city-signals.json` | Signal detector | Latest detected signals |
| `shared/tri-city-summary.json` | TV poller (each cycle) | Last cycle result + open positions |
| `shared/tri-city-poller.pid` | TV poller on start | PID of running poller |
| `shared/sandbox-state.json` | Sandbox poller | Date · last_order_tms · positions · daily_pnl |
| `shared/sandbox-summary.json` | Sandbox poller (each cycle) | Cycle count · open positions · last signals |
| `logs/tri-city-executions.json` | `tri_city_execute.py` | Entry log: symbol · setup · price · qty · guards |
| `logs/tri-city-journal.json` | `tri_city_position_manager.py` | Exit log: P&L · R-multiple · outcome · duration |
| `logs/tri-city-blocked-signals.json` | All guards | Every blocked signal with reason |
| `logs/sandbox-executions.json` | `sandbox_execute.py` | Sandbox entry log |
| `logs/sandbox-journal.json` | `sandbox_position_manager.py` | Sandbox exit log |

---

## Monitoring

```bash
# System 1 — live poller activity
tail -f ~/tri-city-inator/logs/tri-city-tv-poller.log

# System 1 — execution + blocked signal log
tail -f ~/tri-city-inator/logs/tri-city-execute.log

# System 1 — current poller cycle summary
cat ~/tri-city-inator/shared/tri-city-summary.json

# Sandbox — live poller activity
tail -f ~/tri-city-inator/logs/sandbox-poller.log

# Sandbox — current state (positions, daily P&L, signal tms)
cat ~/tri-city-inator/shared/sandbox-state.json

# Check pollers are running
kill -0 $(cat ~/tri-city-inator/shared/tri-city-poller.pid) && echo "S1 running" || echo "S1 STOPPED"
kill -0 $(cat ~/tri-city-inator/shared/sandbox-poller.pid)  && echo "SB running" || echo "SB STOPPED"
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

# Health check
python -W ignore ~/tri-city-inator/scripts/tri_city_health_check.py

# Dry-run a signal (no order placed)
python -W ignore ~/tri-city-inator/scripts/tri_city_execute.py \
  --symbol TSEM --price 270.00 --orh 267.42 --orl 252.70 \
  --rsi 55.0 --ema_dev 0.35 --signal "BREAKOUT" --setup BREAKOUT --dry-run

# EOD close all System 1 positions now
python -W ignore ~/tri-city-inator/scripts/tri_city_position_manager.py --eod

# Backtest a symbol
python -W ignore ~/tri-city-inator/scripts/tri_city_backtest.py \
  --symbols NVDA TSLA --start 2024-01-01

# Today's journal report
python -W ignore ~/tri-city-inator/scripts/journal_report.py

# Launch dashboard
python3 -m streamlit run ~/tri-city-inator/scripts/dashboard.py
```

---

## Dashboard (6 Pages)

```bash
python3 -m streamlit run ~/tri-city-inator/scripts/dashboard.py
```

| Page | Shows |
|---|---|
| Positions | Live open trades: real-time P&L · shares · entry · stop · T1/T2/T3 |
| Overview | Cumulative P&L · daily bars · outcome donut · Sharpe (R) · Calmar |
| Trade Log | Sortable table + per-trade expander · signal/fill/slippage columns |
| Signal Analysis | Win rate / Avg R by setup · RVOL+RSI+EMA Dev histograms · P&L by hour |
| Risk & Sizing | R distribution · streak analysis · rolling drawdown |
| Candidates | Top-100 premarket ranked list · score breakdown · news links |

---

## TradingView Layouts

| Layout | Used for | How to switch |
|---|---|---|
| `TRI CITY INATOR III` | System 1 — live scanner (entity ID: `Kbzkkm`) | `layout_switch("TRI CITY INATOR III")` |
| `SANDBOX` | System 3 — QQQ/SPY/IWM 5-min strategies | Open second TV tab manually |

```
TRI CITY INATOR III scanner inputs:
  in_7 → in_26 = 20 symbol slots (populated by Claude crons)
  Read live table: data_get_pine_tables(study_filter="Inator")
```

---

## .env Key Variables

```bash
# Core
ALPACA_PAPER=true
ORB_MINUTES=5          # 5 / 15 / 30 — must match Pine scanner timeframe
MAX_POSITIONS=6
MAX_DAILY_LOSS=-2500

# Targets
T1_PCT=5
T2_PCT=12
T3_TRAIL_PCT=3

# Signals
PULLBACK_ENABLED=false
MIN_RVOL=0.0
MIN_INTRADAY_RVOL=0.0

# ATR stops (Davey cheat codes)
ATR_STOP_MULT=1.0
ATR_T1_MULT=1.5
ATR_T2_MULT=3.0

# Sandbox
SANDBOX_CAPITAL=5000
SANDBOX_RISK_PCT=0.005
SANDBOX_ATR_STOP=2.0
SANDBOX_ATR_TARGET_SS=5.0
SANDBOX_ATR_TARGET_ST=4.0
SANDBOX_MAX_POSITIONS=2
SANDBOX_DAILY_LOSS=-150

# Notifications
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

---

## Emergency Stop

**Stop everything immediately:**

```bash
# Kill System 1 poller
kill $(cat ~/tri-city-inator/shared/tri-city-poller.pid) 2>/dev/null

# Kill Sandbox poller
kill $(cat ~/tri-city-inator/shared/sandbox-poller.pid) 2>/dev/null

# Close ALL open Alpaca positions
python3 -c "
from alpaca.trading.client import TradingClient
import os; from dotenv import load_dotenv; load_dotenv('$HOME/tri-city-inator/.env')
c = TradingClient(os.getenv('ALPACA_API_KEY'), os.getenv('ALPACA_SECRET_KEY'), paper=True)
c.cancel_orders()
c.close_all_positions(cancel_orders=True)
print('All positions closed.')
"
```

**Stop just the pollers (keep positions open):**
```bash
kill $(cat ~/tri-city-inator/shared/tri-city-poller.pid) 2>/dev/null
kill $(cat ~/tri-city-inator/shared/sandbox-poller.pid)  2>/dev/null
```

**EOD close System 1 only:**
```bash
python -W ignore ~/tri-city-inator/scripts/tri_city_position_manager.py --eod
```

---

## Repo

```
https://github.com/djmusstrd/tri-city-inator
```

```
scripts/
  tri_city_scanner.py           Gap-up candidate scanner
  tri_city_intraday_scanner.py  Intraday re-scorer (inside poller)
  tri_city_health_check.py      Session health verifier
  tri_city_level_lock.py        Standalone CDP ORH/ORL locker (launchd)
  tri_city_tv_poller.py         Headless CDP daemon — detect/execute/manage
  tri_city_signal_detector.py   BREAKOUT/CONT/PULLBACK/EMA20_PB/ST_FLIP/CONSOL detector
  tri_city_execute.py           7-guard gate + bracket orders
  tri_city_position_manager.py  T1/T2/T3 management + EOD close
  tri_city_alert_monitor.py     TV/Alpaca alert router
  tri_city_backtest.py          Historical simulation
  start_poller.sh               Starts TV poller + alert monitor
  sandbox_signal_reader.py      CDP reader for SANDBOX chart widgets
  sandbox_execute.py            Sandbox guards + ATR bracket orders
  sandbox_position_manager.py   Sandbox position management
  sandbox_poller.py             2-min sandbox loop
  start_sandbox_poller.sh       Starts sandbox poller
  journal_report.py             Performance report
  dashboard.py                  Streamlit dashboard

strategies/
  sandbox_prd.md                Sandbox build spec
  confirmed_continuation_prd.md System 2 build spec (future)

shared/                         Runtime state (JSON files)
logs/                           Execution + journal + poller logs
managers/
  trade_executor.py             Alpaca order placement
  trade_journal.py              Journal write helpers
```
