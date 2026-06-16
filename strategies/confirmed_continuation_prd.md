# System 2: Confirmed Continuation — PRD & Build Todo

**Status:** Prototype / Pre-build  
**Goal:** 60-70% win rate on Tri-City's existing 20-symbol universe  
**Capital allocation:** $2,500 of paper account (separate from Tri-City System 1)  
**Runs alongside:** Tri-City Inator (System 1, unchanged) + Context Engine QQQ (System 3)

---

## Why This Exists

Tri-City System 1 enters on the FIRST sign of momentum — the initial ORH breakout bar.
Win rate is ~13-18% with 2:1 R:R. The edge is that occasional big winners (WBI +$850)
carry the account. The problem: 8 of 15 losses today were immediate reversals (stopped
within 15-30 min), meaning entries hit exhaustion points, not continuation.

Confirmed Continuation fixes this by requiring proof-of-hold before entry:
price must break ORH, pull back to test it, then reclaim it on volume. That
pullback-and-reclaim pattern filters out fakeouts and chases, targeting the
second entry opportunity that is statistically higher probability.

---

## Entry Logic (all conditions required)

```
1. ORH_BROKEN     — price already traded above ORH at least 1 bar earlier today
2. PULLBACK       — after the break, close came back within 1.5% of ORH (tested the level)
3. RECLAIM        — current bar: close > ORH and close > open (green reclaim candle)
4. VOLUME         — RVOL >= 1.5x (strong volume on reclaim, not a dead-cat bounce)
5. RSI RANGE      — RSI(14) between 40-68 (not overbought on re-entry)
6. EMA STACK      — close > EMA20 (still in uptrend structure)
7. TIME WINDOW    — 8:45 AM–11:00 AM CT (after ORB settles, before noon chop)
8. COOLDOWN       — symbol not stopped out earlier today (no same-day re-entry on a loser)
9. FLAT           — no open position in this symbol (System 1 or System 2)
```

---

## Exit Structure (smaller R, higher win rate)

```
Entry price = close of reclaim bar

Stop:    entry - (ATR14 * 1.5)  [wider than System 1's flat 5%, ATR-sized to actual noise]
T1:      entry + (risk_per_share * 2.0)   → sell 50%  [+5-8% typically]
T2:      entry + (risk_per_share * 3.5)   → sell 25%  [+9-14% typically]
T3:      trail 25% on EMA20 breach below VWAP (same as System 1)
EOD:     force-flat 2:45 PM CT (same as System 1)
```

At 2:1 first target / ATR stop: need 34% win rate to break even.
At 60% target win rate: PF ≈ 3.0+

---

## Position Sizing

```python
risk_dollars = system2_capital * 0.01   # 1% risk per trade (vs System 1's ~5-10%)
atr_stop_dist = atr14 * 1.5
qty = floor(risk_dollars / atr_stop_dist)
max_positions = 2                        # vs System 1's 3
daily_loss_lock = -$150                  # -6% of $2,500 allocation
```

---

## What Changes vs System 1

| | System 1 (Tri-City) | System 2 (Confirmed Continuation) |
|---|---|---|
| Entry trigger | First close above ORH | Pullback-and-reclaim of ORH |
| Min RVOL | 0.0x (removed) | 1.5x |
| RSI range | 50-82 | 40-68 |
| Stop | Fixed 5% or ST band | ATR14 × 1.5 (dynamic) |
| T1 target | +10% (2:1) | ~+6% (2:1 on ATR stop) |
| Max positions | 3 | 2 |
| Re-entry same symbol | Allowed | Blocked same day after stop |
| Trade window | 8:30+ORB to 2:45 PM | 8:45 AM–11:00 AM CT only |
| Capital | Main allocation | $2,500 sub-allocation |

---

## Signals System 1 Keeps (unchanged)

- BREAKOUT, CONTINUATION, EMA20_PULLBACK, SUPERTREND_FLIP, CONSOL_BREAK
- All existing guards (guard 0-7) remain
- Existing poller, level-lock, journal, position manager — untouched

System 2 runs as a separate detector+executor pair that reads the same
`tri-city-levels.json` (for ORH/ORL) but uses its own execution and journal.

---

## Key Finding From Prototype (2026-06-15)

The pullback state-machine approach got zero signals on day-1 fresh listings —
these names don't produce ORH-support pullback patterns (they gap straight up
or reverse hard, no clean test-and-hold). BUT the entry distance analysis
revealed a simpler, higher-impact finding:

| Entry distance from ORH | Win rate | Avg P&L/trade |
|---|---|---|
| ≤2% of ORH | 2/5 = 40% | +$35.81 |
| 2–5% above ORH | 0/4 = 0% | -$211.04 |
| >5% above ORH | 0/3 = 0% | -$198.56 |

**Action: highest-impact single change is an ORH proximity guard on System 1.**
System 2 as a standalone system should run on ESTABLISHED stocks (multi-day
history, defined support/resistance) rather than day-1 fresh listings.

---

## Revised Build Plan

### Track A — System 1 Guard (highest immediate impact, low effort)
- [ ] Add ORH proximity guard to `scripts/tri_city_execute.py`
  - [ ] Block entry if `(signal_price - orh) / orh > 0.03` (>3% above ORH)
  - [ ] Log as blocked signal to `tri-city-blocked-signals.json` with reason "ORH_EXTENSION"
  - [ ] Tune threshold (2% vs 3%) after 2 weeks of live data
  - [ ] Exclude SUPERTREND_FLIP and EMA20_PULLBACK (these don't use ORH)

### Track B — System 2 on Established Stocks (new parallel system)

System 2 runs on **established large-cap/mid-cap momentum names** (RVOL spike
on stocks with 20+ days of trading history) where pullback-to-support patterns
actually form. The scanner is separate from Tri-City's fresh-listing universe.

#### Phase 1 — Scanner
- [ ] `scripts/system2_scanner.py`
  - [ ] Scan SPY/QQQ components + Russell 1000 for intraday RVOL spike >= 2x
  - [ ] Filter: >20 days trading history (eliminates day-1 IPOs)
  - [ ] Filter: gap up >= 3% from prior close
  - [ ] Output: `shared/system2-candidates.json` (top 10 symbols)
  - [ ] Run at 7:30 AM CT alongside existing Tri-City scanner

#### Phase 2 — Signal Detector
- [ ] `scripts/system2_signal_detector.py`
  - [ ] For each candidate: compute ORH from 9:30-9:45 AM CT bars
  - [ ] Track state: ORH_BROKEN → PULLED_BACK (within 2% of ORH) → RECLAIMED
  - [ ] Reclaim candle: close > ORH, close > open, RVOL >= 1.5x
  - [ ] RSI 40-68 on reclaim bar
  - [ ] Output: `shared/system2-signals.json`

#### Phase 3 — Entry Guards
- [ ] `scripts/system2_execute.py`
  - [ ] Guard: within 2% of ORH at signal time
  - [ ] Guard: close > EMA20
  - [ ] Guard: time window 8:45–11:00 AM CT
  - [ ] Guard: symbol not stopped today (no re-entry)
  - [ ] Guard: no open S2 position (max 2 concurrent)
  - [ ] Guard: daily loss lock (-$150 on $2,500 allocation)
  - [ ] ATR14 × 1.5 stop, T1 at 2R, T2 at 3.5R
  - [ ] Bracket orders via Alpaca
  - [ ] Log to `logs/system2-executions.json`

#### Phase 4 — Position Manager
- [ ] `scripts/system2_position_manager.py`
  - [ ] T1 breakeven promotion
  - [ ] T2 stop lock
  - [ ] T3 EMA20+VWAP trail
  - [ ] EOD close 2:45 PM CT
  - [ ] Log exits to `logs/system2-journal.json`

#### Phase 5 — Poller
- [ ] `scripts/system2_poller.py`
  - [ ] Runs every 3 min via `scripts/start_system2_poller.sh`
  - [ ] PID: `shared/system2-poller.pid`

#### Phase 6 — Dashboard
- [ ] Add "System 2" tab to `scripts/dashboard.py`
  - [ ] Win rate / PF / trade log for System 2
  - [ ] Side-by-side vs System 1: win rate, PF, P&L

#### Phase 7 — Backtest
- [ ] `scripts/system2_backtest.py`
  - [ ] Simulate pullback-reclaim on established names, last 60 days
  - [ ] Target: >50% win rate before enabling live/paper orders

#### Phase 8 — Session Integration
- [ ] Add System 2 scanner to 7:30 AM cron
- [ ] Add System 2 poller start to level-lock sequence
- [ ] Add System 2 to health check
- [ ] Add System 2 shutdown to end-session sequence in CLAUDE.md

### Track C — Context Engine QQQ (lowest effort, separate capital)
- [ ] `scripts/context_engine_poller.py` — port Context Engine Bot v1 to Python
  - [ ] Compute EMA20/ATR/VWAP/volMA/30m-EMA/daily-EMA on QQQ 5-min feed
  - [ ] Pull/push/compression setups with scoreMin=70
  - [ ] 9:35–11:30 ET trade window, 1:1.5% risk, bracket orders
  - [ ] Separate capital: $2,500 allocation, max 1 position, daily loss lock $50
  - [ ] Log to `logs/context-engine-journal.json`
- [ ] Add Context Engine tab to dashboard

---

## Success Criteria (paper trading, first 30 days)

| Metric | Minimum | Target |
|---|---|---|
| Win rate | > 45% | > 60% |
| Profit factor | > 1.5 | > 2.5 |
| Max daily DD | < -6% of $2,500 | < -3% |
| Trades/day | 1-3 | 1-2 |
| Avg holding time | 20-90 min | 30-60 min |

If minimum not met after 30 days / 30+ trades: pause System 2, analyze losing setups,
adjust PULLBACK tolerance (currently 1.5%) or RVOL floor (currently 1.5x).

---

## Parallel System Map

```
Tri-City Paper Account ($10K total)
├── System 1: Tri-City Inator         $7,500  (existing, unchanged)
│   └── poller: tri_city_tv_poller.py
├── System 2: Confirmed Continuation  $2,500  (this build)
│   └── poller: system2_poller.py
└── System 3: Context Engine QQQ      separate capital, separate script (future)
    └── poller: context_engine_poller.py
```

Systems 1 and 2 share:
- tri-city-levels.json (ORH/ORL source of truth)
- tri-city-table.json (live Pine scanner output)
- Alpaca account (position-aware, no double-counting)

Systems 1 and 2 do NOT share:
- Execution scripts (fully separate)
- Journals (separate log files)
- Capital allocation (tracked separately)
- Signal logic (completely different entry conditions)
