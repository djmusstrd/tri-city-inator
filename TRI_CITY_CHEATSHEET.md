# Tri-City Inator — Cheatsheet
_Last updated: 2026-05-19_

---

## Daily Session Flow (all times CT)

| Time | What Happens | Who Does It |
|------|-------------|-------------|
| Before 7:30 AM | Open TradingView → TRI CITY INATOR III layout | You |
| Before 7:30 AM | Run `tricity` in terminal to register crons | You |
| **7:30 AM** | Premarket scanner runs, ranks gap-up candidates | Auto |
| **8:30 AM** | Top 15 symbols pushed into TradingView scanner | Auto |
| **8:45 AM** | ORH/ORL lock, signal monitor starts (15-min ORB) | Auto |
| Every 3 min | Signal check + RVol spike watch + position manager | Auto |
| **9:30 AM** | Intraday scan #1 — catches flat-open breakouts, merges pool, re-pushes TV slots | Auto |
| **11:30 AM** | Intraday scan #2 — catches mid-morning continuation movers, re-pushes TV slots | Auto |
| 3:45 PM | EOD close all open positions | Auto |

> ORB timeframe is set by `ORB_MINUTES` in `.env` (default: 15). Lock time = 8:30 + ORB_MINUTES.

---

## Terminal Commands

```bash
tricity              # Start session: open TV + Claude, register all crons
tricity-scan         # Run premarket scanner manually
tricity-status       # Check open positions
tricity              # Today's trade report (P&L, R-multiples)
tricity-all          # All-time performance report

# Run intraday scanners manually
python -W ignore ~/tri-city-inator/scripts/tri_city_intraday_scanner.py --source intraday_930
python -W ignore ~/tri-city-inator/scripts/tri_city_intraday_scanner.py --source intraday_1130
```

---

## Manual Script Commands

```bash
# Dry-run a signal without executing
python -W ignore ~/tri-city-inator/scripts/tri_city_execute.py \
  --symbol NVDA --price 225.00 --orh 223.16 --orl 218.37 \
  --rsi 55.0 --ema_dev 0.85 --signal "BREAKOUT" --setup BREAKOUT --cup --dry-run

# Force EOD close all positions now
python -W ignore ~/tri-city-inator/scripts/tri_city_position_manager.py --eod

# Backtest
python -W ignore ~/tri-city-inator/scripts/tri_city_backtest.py \
  --symbols NVDA TSLA --start 2024-01-01
```

---

## Signal Types & Entry Rules

| Signal | Conditions | Stop |
|--------|-----------|------|
| **BREAKOUT** | SIGNAL=BREAKOUT · Price > ORH · RSI > 50 · EMA Dev% > 0 | 13¢ below ORH |
| **CONTINUATION** | SIGNAL=CONTINUATION · Price > ORH · EMA Dev% 0–1.0% | 13¢ below ORH |
| **PULLBACK** | SIGNAL=PULLBACK · EMA Dev% 0–+0.8% · RSI 38–55 | EMA20 – 30¢ (widened stop) |

Add `--cup` flag if CUP column = YES (high-conviction log).

---

## Position Structure (50-25-25)

```
Entry
  → T1 (+10%): sell 50%  → stop moves to breakeven
  → T2 (+20%): sell 25%  → stop moves to T2 price
  → T3 (+30%): trail 25% → exits on EMA20+VWAP breach or EOD
  → Stop (-5%): all shares exit
```

---

## Entry Guards (in order)

| # | Guard | Default |
|---|-------|---------|
| 1 | Already executed today (no duplicates) | — |
| 2 | Already in position | — |
| 3 | Max concurrent positions | 3 |
| 4 | Daily loss limit | -$300 |
| 5 | Time cutoff | 1:00 PM CT |
| 6 | SPY regime (blocks LONGs in bear market) | -1.5% |
| 7 | Relative volume floor | BREAKOUT 2.0x · CONT 1.75x · PULLBACK 1.5x |
|   | ↳ Afternoon tightening (after noon CT) | max(setup floor, 2.0x) |

---

## RVOL Features

| Feature | Detail |
|---------|--------|
| **Size boost** | RVOL > 1.5x scales position up linearly to +25% at 3.0x |
| **Setup floors** | BREAKOUT 2.0x · CONTINUATION 1.75x · PULLBACK 1.5x |
| **Collapse exit** | After T1 hit (breakeven set), exits if RVOL drops below 1.0x |
| **Candle type** | PULLBACK: HAMMER = full size · NEUTRAL/BEARISH/DOJI = -25% size (auto-detected) |

Override in `.env`: `RVOL_SIZE_BOOST_MAX`, `RVOL_SIZE_BOOST_THRESH`, `RVOL_EXIT_FLOOR`

## Bulkowski Fixes (2026-05-19)

| # | Finding | Change |
|---|---------|--------|
| 1 | Fix B buffer | Requires 0.5% below entry before exit fires (was any penny) |
| 2 | Entry timing | PULLBACK only when EMA Dev% ≥ 0% (price at or above EMA) |
| 3 | EMA stop | PULLBACK stop = EMA20 – 30¢ (widened from 10¢) |
| 4 | Last bar close | Fix B uses last completed bar close, not live tick |
| 5 | Re-entry | One re-entry allowed after Fix B scratch < $0.50/share |
| 6 | Candle type | PULLBACK non-hammer entries get -25% position size |

Override: `PULLBACK_FAIL_BUFFER`, `EMA_STOP_BUFFER`, `RE_ENTRY_MAX_LOSS` in `.env`

---

## TradingView Scanner

| Item | Value |
|------|-------|
| Layout | TRI CITY INATOR III (ID 168250176) |
| Indicator | Tri-City Inator (Pine v5, shorttitle: "Tri-City") |
| Entity ID | `YcTiy2` |
| Symbol inputs | `in_7` through `in_21` (15 slots) |
| Timeframe | 15-min (matches ORB_MINUTES=15) |
| Columns | SYMBOL · PRICE · RSI · EMA DEV% · **RVOL** · ORH/ORL · CUP · SIGNAL |

**RVOL color coding:**
- 🔴 Red = < 1.5x (weak)
- 🟡 Yellow = 1.5x–2.5x (moderate)
- 🟢 Green = ≥ 2.5x (strong)

---

## .env Reference

```bash
ALPACA_API_KEY=...
ALPACA_SECRET_KEY=...
ALPACA_PAPER=true          # Switch to false for live trading

# Scanner
MIN_GAP_PCT=1.5            # Minimum gap % to qualify

# Position structure
FREE_RIDE_PCT=3            # Profit % to lock free-ride stop
T3_TRAIL_PCT=5             # T3 trailing stop %

# RVOL (all have defaults — only set to override)
# BREAKOUT_MIN_RVOL=2.0
# CONTINUATION_MIN_RVOL=1.75
# PULLBACK_MIN_RVOL=1.5
# RVOL_SIZE_BOOST_MAX=1.25
# RVOL_SIZE_BOOST_THRESH=3.0
# RVOL_EXIT_FLOOR=1.0
# PM_MIN_RVOL=2.0          # Afternoon floor (after noon CT)

# Session
# ORB_MINUTES=15           # 5, 15, or 30
# MAX_POSITIONS=3
# DAILY_LOSS_LIMIT=-300
```

---

## Repo

```
github.com/djmusstrd/tri-city-inator

scripts/
  tri_city_scanner.py          ← 7:30 AM premarket scan (gap-up candidates)
  tri_city_intraday_scanner.py ← 9:30 AM + 11:30 AM intraday scan (flat-open breakouts)
  tri_city_execute.py          ← signal execution + guards
  tri_city_position_manager.py ← T1/T2/T3 targets, EOD close
  tri_city_backtest.py         ← historical simulation
  journal_report.py            ← performance report
pine/
  tri_city_inator.pine         ← TradingView Pine v5 source
shared/
  tri-city-candidates.json     ← scanner output (auto-updated, gap + intraday merged)
  tri-city-levels.json         ← ORH/ORL after lock (auto-updated)
  tri-city-rvol-state.json     ← RVol spike watch state (updated each monitor cycle)
logs/                          ← executions, journal, daily reports
```
