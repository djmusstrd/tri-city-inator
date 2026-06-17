# Strategy V2 — Design & Build Plan

> Name: **APEX** (trades market *leaders*). Confirmed 2026-06-16.
> Status: **PLANNING** — no code written yet. This doc is the source of truth so the
> logic is never lost. Update it as decisions change.
> Created 2026-06-16. Stable checkpoint of the old system: git tag `build-2026-06-16-stable`.

---

## 1. Goal & Honest Framing

**Target:** $300–500/day, ideally more.

**Reality check:** Daily P&L = edge × size × frequency. The current Tri-City system's edge
is *negative* (16.4% win rate, 89% drawdown from peak). No architecture conjures profit from
a negative edge. What V2 builds is the machinery that:
1. Only takes trades with a measured statistical edge (quality filter)
2. Gets in on time (timing engine — fixes "we miss the morning move")
3. Cuts losers the moment the thesis breaks (health-based exits)
4. Measures itself and stops doing what isn't working (self-improvement loop)
5. Knows the market regime and only deploys what fits it (durability)

The daily target *follows* a real edge — it cannot lead it. We build the machine, then let
data prove the edge before scaling size toward the target.

**Do NOT scrap the existing system.** It is paused, not deleted. V2 runs as a separate book.

---

## 2. Root-Cause Diagnosis (from the operator)

The operator's complaints about the current system, mapped to root causes:

| Complaint | Root cause | V2 layer that fixes it |
|---|---|---|
| "We miss the morning setup almost every day" | Level lock waits until 8:45 (ORB close); 3-min poll cadence; watchlist discovered too late | Layer 1 (pre-open watchlist) + Layer 2 (fast morning timing) |
| "3-min crons miss market context" | Single coarse cadence, no higher-timeframe context | Multi-timeframe split (daily context + intraday timing) |
| "Filters are too strict to take trades" | Hard-coded guard thresholds, not calibrated to results | Layer 4 auto-tunes thresholds from expectancy |
| "When we're in trades, they aren't managed properly" | Mechanical 50-25-25 brackets, no thesis awareness | Layer 3 trade health monitor |
| "Needs to be self-improving and durable, hold up in all markets" | No feedback loop, no regime awareness | Layer 4 + Layer 5 |
| "I need to understand why a stock was purchased + good Telegram/dashboard visibility" | No decision-context capture; thin alerts | Layer 6 (rationale capture + rich Telegram + dashboard "Why" page) |

---

## 3. Load-Bearing Decision: Multi-Timeframe Split  ✅ DECIDED

The operator's complaints all stem from one single-timeframe system doing two jobs at once.
V2 splits the timeframes:

- **DAILY = "what am I allowed to trade today?"**
  Universe-ranked Relative Strength percentile + EMA-ribbon trend alignment, computed on
  daily bars, produces a *focused, ranked watchlist of true market leaders*, built **pre-open**.

- **INTRADAY = "when do I enter, and am I still in the move?"**
  A faster, morning-ready execution engine that only trades names already blessed by the daily
  filter, with active thesis-aware management.

This is the classic multi-timeframe approach: higher timeframe sets *direction/eligibility*,
lower timeframe sets *timing/management*.

---

## 4. The Five Layers

### Layer 1 — Daily Filter ("what to trade")
- **Dynamic, broad universe — NEVER static.** Relative strength is only meaningful ranked
  across the widest possible universe (the whole premise of IBD RS). A static or price-capped
  list structurally cannot surface a theme leader early (the "$2 → $80 over 6 months" trade).
  → Screen the **full liquid US equity universe daily**, rank *everything* by RS percentile,
  then apply the ribbon/trend filter so the leader watchlist *falls out of the ranking* rather
  than being hand-picked.
- **No price floor or ceiling.** A $2 theme leader must be visible.
- **Liquidity floor instead of a price filter:** minimum average dollar volume so a position
  can enter/exit with acceptable slippage. Keeps illiquid traps out *without* excluding
  low-priced names. **The floor scales with account size** (a $5K position needs far less
  liquidity than a $50K one).
- **Relative Strength, done right:** true *universe percentile* (rank each name's IBD-style
  weighted return vs. the entire universe), NOT the fake single-symbol `50 + rs_value` from the
  source Pine script.
- **Trend alignment:** EMA ribbon (8/13/21/34/55) stacked and expanding on the daily.
- **Output:** ranked leader watchlist written pre-open (night before + pre-market refresh),
  so the intraday engine is ready at the bell — no 8:45 discovery lag.

### Layer 2 — Timing Engine ("when to enter")
- **Faster cadence in the open:** 1-minute cadence for the first hour (where the moves are),
  relax to slower cadence midday.
- **Morning-ready:** no waiting for ORB lock to build the list; the list exists pre-open.
- **Entry setups (intraday) on eligible leaders only:** gap-and-go, first-pullback, ORB —
  with a composite confluence score and a *tunable* entry threshold (not hard-coded strict).
- **Data-quality gate:** never enter on RSI=0 / EMA_dev=0 (today's HITI/SPCB failure mode).

### Layer 3 — Trade Health Monitor ("am I still in the move?")  ← the "self-aware trade"
Each open position gets a **health score** recomputed every cycle from:
- Entry thesis still valid? (still above breakout level, RS still strong, ribbon still stacked)
- Momentum sustaining or decaying? (price velocity, volume trend, higher-high structure)
- Holding key levels? (VWAP, ribbon support)
- Time-in-trade vs. expected hold
When health crosses *down* through a threshold → **proactive exit now**, do not wait for the
hard -5% stop. (Would have cut HITI/SPCB far earlier.)

**Position lifecycle — "never sell without a reason" (the $2 → $80 principle):**
The current system force-closes everything at 2:45pm EOD — that rule would have killed a
$2 → $80 runner on day one. Layer 3 must instead let a winner **graduate** as long as health
stays strong:
- **intraday day-trade → overnight swing → multi-week position.**
- Sell only on a *reason*: momentum decay, thesis break, or a hard risk rule. Never on the clock.
- **Conditional EOD close:** carry healthy, still-trending leaders overnight (margin account,
  no PDT → overnight holds are fine); only force-close trades whose intraday thesis has broken
  or that are scratch/weak. Don't carry junk overnight, but never cut a runner because it's 2:45.
- This is how one system delivers *both* the daily cash-flow target *and* the rare monster.

### Layer 4 — Self-Improvement Loop ("evaluate myself to stay profitable")  ← AUTO-ADJUST, guardrailed
A periodic job reads the journal, computes per-setup / per-regime expectancy on **rolling
windows** (walk-forward style), and **auto-adjusts the live config within hard guardrails**.
Rules-based, NOT black-box ML (ML overfits; you'd never trust it in a drawdown).

**What it MAY change automatically (within caps):**
- **Disable a setup** if rolling expectancy < 0 over the lookback window (e.g., ≥20 trades or
  10 trading days). (SUPERTREND_FLIP at 0% would auto-disable.)
- **Re-enable** a disabled setup if a shadow/paper-tracked expectancy turns positive again.
- **Scale position sizing** within a band, e.g. **0.5×–1.5×** base, by rolling performance.
- **Tune the entry composite threshold** within a band, e.g. **60–75**, to balance
  selectivity vs. trade count.

**Hard guardrails it may NEVER cross (human-set):**
- Max risk per trade ($ cap)
- Max daily loss (circuit breaker)
- Max concurrent positions
- Sizing band floor/ceiling and threshold band floor/ceiling above

**Safety requirements:**
- Every automated change is **logged with reason + timestamp** to a config-history file.
- All changes **reversible**; a one-command **revert-to-baseline** + kill switch.
- Changes apply only at session boundaries (not mid-trade), unless it's a guardrail trip.

### Layer 5 — Regime Awareness ("hold up in all markets")
- Classify market regime each day: SPY trend, VIX level, breadth.
- Tag every journal trade with the regime it occurred in.
- Layer 4 evaluates expectancy **per regime**; Layer 1/2 only deploy setups proven in the
  *current* regime. When no setup fits the regime → stand down (circuit breaker).
- Durability = knowing the regime + deploying only what fits, NOT a magic all-weather signal.

### Layer 6 — Observability & Explainability ("show me why, in real time")
The operator must always be able to see *what* the system is doing and *why*. This is not
cosmetic — the same decision-context capture feeds Layer 4's per-setup/per-regime analysis,
so it is **foundational** and built early.

**Trade Rationale capture (cross-cutting — every entry records its full "why"):**
On each entry, persist a complete decision snapshot:
- Setup type + the composite confluence score and its component breakdown
- RS percentile (and rank vs. universe), EMA-ribbon state (stacked? expanding?)
- Which filters/guards passed, and the values at the moment of entry
- Market regime tag (Layer 5)
- VWAP / level context, RVOL, price vs. ORB
- Health score at entry (Layer 3 baseline)
This snapshot is the single record that powers both the Telegram alert and the dashboard
"Why" page, and is what Layer 4 mines later.

**Telegram alerts (rich, not just "bought X"):**
- **Entry:** symbol, setup, composite score + top 2–3 reasons it qualified, RS percentile,
  regime, entry/stop/target/size.
- **Health/management:** proactive-exit warnings ("MOMENTUM DECAY: SYM health 72→38, exiting"),
  T1/T2/T3 hits, stop moves.
- **Exit:** outcome, R, P&L, hold time, exit reason.
- **Self-improvement events:** when Layer 4 changes config ("auto-disabled SETUP: expectancy
  -0.3R over 22 trades"), so the operator is never surprised by autonomous changes.
- **Regime/circuit-breaker:** regime shifts and stand-down events.

**Dashboard — new "Why / Trade Rationale" page:**
- For each trade (open and closed): render the full entry rationale snapshot in plain language —
  *why this stock, why now*: the score breakdown, RS percentile, ribbon state, regime, the
  setup, and the levels. Make the decision auditable at a glance.
- Live health-score timeline for open positions (Layer 3) so the operator sees momentum
  decaying in real time.
- Surface Layer 4's current config + recent autonomous changes with their reasons.
- Complements existing dashboard pages (Positions, Overview, Trade Log, Signal Analysis,
  Risk & Sizing, Candidates).

---

## 5. Source Pine Script — What We Keep / Fix / Drop

From the operator's "Multi-Indicator Strategy: IBD RS + EMA Ribbon + RSI Divergence":
- **KEEP** the concept: RS + EMA-ribbon trend + confluence scoring.
- **FIX** IBD RS → true universe percentile in the Python scanner (source version is a fake
  single-symbol `50 + rs_value`).
- **DROP (for now)** the RSI divergence component — its detection loop is broken
  (`last_pivot_bar` init 0 → `bar_index[i] == last_pivot_bar` never matches real divergences),
  and it's 25% of a score that never fires correctly. Rebuild properly later if it adds alpha.
- **DROP** shorting → **long-only** (small-account growth goal; shorting small caps impractical).
- **FIX** sizing muddle (default_qty 100% equity vs explicit qty; pyramiding 3 + risk sizing
  can over-leverage).

---

## 6. Phased Build Plan (the to-do list)

### Phase 0 — Validate the concept ✅ DONE (2026-06-16) — GREENLIGHT
- [x] Clean Pine script (`pine/apex_phase0.pine`): RSI divergence dropped, RS marked as
      single-symbol proxy, sizing fixed, long-only.
- [x] Pivoted from single-symbol TV backtest to a **multi-symbol Python backtest**
      (`scripts/apex_phase0_backtest.py`, Alpaca daily bars) — more rigorous aggregate
      expectancy, zero risk to the live TV tabs. TV tab handling was flaky + single-symbol
      can't test a relative-strength concept.
- [x] Results (3y daily, R-multiple expectancy):
      - **Take-profit capped:** 547 trades, 35.6% win, **+0.214R**, PF 1.66.
      - **Let-it-run (no TP):** 442 trades, 32.6% win, **+0.424R**, PF 2.32, best +21.85R.
        → *Letting winners run ~doubles expectancy* (bias-free, same basket). Validates the
        "never sell without a reason" core. Edge = fat right tail, not high win rate.
      - **Neutral/laggard basket (robustness):** **+0.146R**, PF 1.44 — still positive; RS
        filter self-throttled (2–5 trades on laggards vs 20–35 on trenders).
- [x] **Verdict: GREENLIGHT.** Positive expectancy across winner + laggard baskets; let-it-run
      is the key driver; RS filter discriminates.
- Caveats (NOT yet proven): daily-close fills, no slippage/commission in R, single-symbol RS
  proxy (not the real universe percentile), no walk-forward. The core Phase 1 question —
  *can RS ranking pick leaders prospectively?* — remains to be tested.

### Phase 1 — Daily Filter (Layer 1) in Python ✅ CORE DONE (2026-06-16) — EDGE CONFIRMED
Implemented in `scripts/apex_daily_filter.py` (two-stage fetch: cheap recent window →
liquidity filter → full history on survivors).
- [x] Universe RS percentile (rank IBD-weighted return vs. the full liquid universe).
- [x] Daily EMA-ribbon alignment gate (8/13/21/34/55 stacked).
- [x] Pre-open ranked leader watchlist artifact → `shared/apex-leaders.json`.
- [x] Live run: 12,251 tradable non-OTC → 3,736 liquid (≥$10M/day) → 192 leaders.
      Top cluster = memory/storage theme (WDC/MU/STX/SNDK) — filter correctly surfaced the
      dominant leadership theme (prices verified real, not data artifacts).
- [x] **Walk-forward validation (71 windows, 3.5y, 20d forward):** RS leaders **+1.48%** vs
      SPY **+0.76%** → **+0.72% excess per 20 days (~9%/yr)**, 58% of windows beat SPY, 48%
      per-name hit. **Prospective edge confirmed** (the question Phase 0 couldn't answer).
- Caveats: validation applies today's liquid universe retroactively (mild survivorship bias);
  equal-weight leader mean driven by fat tail (reinforces let-it-run / Layer 3).
- TODO (Layer 1 polish, later): point-in-time universe to kill survivorship bias; ribbon
  "expanding" refinement; liquidity floor scaling with account equity; pre-open scheduling.

### Phase 2 — Timing Engine (Layer 2) + Rationale Capture (Layer 6 foundation)
**Phase 2a — entry-timing validation ✅ DONE (2026-06-16).**
`scripts/apex_phase2_entry_backtest.py` compared entry triggers on the same leaders
(bias-free). 40 leaders, 24 days, forward returns on daily closes:
| Trigger | N | Close% | +2D% | Win% |
|---|---|---|---|---|
| OPEN (baseline) | 949 | +0.89 | +4.77 | 53% |
| **ORB15 (vol-confirmed)** | 445 | **+1.51** | **+6.38** | **66%** |
| VWAP_PB | 734 | +0.97 | +5.84 | 57% |
→ **ORB15 (volume-confirmed opening-range breakout) is the primary entry** (best R/R + 66%
win, fires on ~47% of leader-days = self-filters to names with real intraday momentum).
**VWAP_PB = secondary entry.** Naive open-buy is worst — timing matters. Caveats: short
24-day strong-tape sample (relative ordering robust, absolute won't hold in chop); ORB fills
modeled at breakout level.

**Phase 2b — live engine SKELETON ✅ BUILT (2026-06-16, untested live — validated via self-test).**
Alpaca-native (NO TradingView/CDP dependency — removes the fragility class that broke the old
system). Modules:
- `apex_config.py` — central tunables + Layer 4 guardrail bands; loads .env centrally.
- `apex_entry_engine.py` — ORB15 (primary) / VWAP_PB (secondary) detector mirroring the
  Phase 2a backtest; data-quality gate; composite confluence score.
- `apex_execute.py` — guards (positions/dup/daily-loss/score), risk-based sizing (no leverage,
  cash-capped), ATR stop **capped at MAX_STOP_PCT (10%)**, entry+stop order (NO TP cap —
  Layer 3 owns the exit), exec log + rationale + Telegram.
- `apex_rationale.py` — Layer 6 snapshot ("why this stock, why now") + rich Telegram message.
- `apex_poller.py` — orchestration loop, fast cadence first hour, `--self-test [DATE]` replays
  a past session with no live market, `--dry-run`. Layer 5 regime = SPY/50-SMA stub.
- `start_apex_poller.sh` — launcher with orphan-kill safety net (sandbox-zombie lesson).
- Self-test (replay 2026-06-15, 192 leaders): full chain fired 5 entries, guards + capped
  stops + rationale + Telegram all correct. Max-stop cap added after self-test exposed 60%
  ATR stops on volatile leaders.
- [ ] **Remaining for go-live (next live session):** wire into a live paper session, watch a
      real ORB15 entry fire, confirm Telegram delivery + rationale persistence intraday.
- TODO (small-account practicality): high-priced leaders ($2k+ names) only afford 1 share on
  $5K — consider fractional shares or account-aware price ceiling in Layer 1.

### Phase 3 — Trade Health Monitor (Layer 3) ✅ BUILT (2026-06-17, dry-run validated)
`apex_health.py`, wired into the poller at every pass; closed trades → `logs/apex-journal.json`.
- [x] Per-position health-score function (0-100): thesis (above entry/breakout), VWAP, 5-min
      EMA9 ribbon proxy, last-3-bar momentum, higher-high structure. Transparent v1 weights.
- [x] Proactive exit when health < `EXIT_HEALTH` (40) — cuts fades before the hard stop.
- [x] Conditional EOD: carry a healthy runner overnight (health ≥ `CARRY_HEALTH` 70, green,
      above VWAP) — positions graduate intraday→swing→multi-week (`GRAD_DAYS` 5); force-close
      the scratch/thesis-broken at the bell. Poller date-reset now preserves carries + ages them.
- [x] Telegram exit / health-decay / carry / graduation alerts; `apex_health.py --self-test`
      replays a past session showing the full health trajectory + where it would exit.
- Validated on 2026-06-15: QH faded from +13%→health 34→proactive exit -2.7% (beat the stop);
  MUU chopped 53–100 health all day, never tripped 40, rode to +5.6% as a runner.
- [ ] TODO (data-tune in Phase 3.5 / Layer 4): exit gave back QH's +13% peak — add a
      peak-gain-aware trail; derive EXIT/CARRY thresholds + weights from the journal once it fills.

### Phase 4 — Regime Awareness (Layer 5)
- [ ] Daily regime classifier (SPY trend, VIX, breadth).
- [ ] Tag every trade with regime in the journal (and in the rationale snapshot).

### Phase 4.5 — Dashboard "Why / Trade Rationale" page (Layer 6) ✅ CORE BUILT (2026-06-17)
Standalone `apex_dashboard.py` (Streamlit, `apex-dash`) — APEX is headless/no-chart, so this
dashboard IS the chart. AppTest-validated, all pages render clean.
- [x] Live Positions: health / gain% / status / reasons table + per-position intraday
      candlestick (entry/stop/VWAP/ORB overlays) + Layer 3 health-score timeline (replays the
      poller's compute_health over the day — no extra logging needed).
- [x] "Chart a Leader": chart any leader + the hypothetical ORB15/VWAP_PB entry & health APEX
      would assign (visual management pre-trade).
- [x] Entries — Why: Layer 6 rationale cards. Closed Trades: journal P&L. Leaders: RS watchlist.
- [ ] Surface Layer 4 current config + recent autonomous changes with reasons (after Phase 5).

### Phase 5 — Self-Improvement Loop (Layer 4, auto-adjust + guardrails)
- [ ] Rolling-window per-setup / per-regime expectancy calculator.
- [ ] Auto-adjust config within guardrails (disable/enable, sizing band, threshold band).
- [ ] Config-history log (change + reason + timestamp), revert-to-baseline, kill switch.
- [ ] Telegram alert on every autonomous config change (never surprise the operator).

### Phase 6 — Live paper → validate → small live
- [ ] Run V2 in the sandbox-style paper harness.
- [ ] Validate expectancy holds out-of-sample across regimes.
- [ ] Then, and only then, small live with reduced risk caps.

---

## 7. Decisions Log
- **2026-06-16:** PDT rule eliminated (2026-06-04) — no longer a constraint; the "swing dodges
  PDT" rationale is moot. V2's case rests on edge quality. See memory
  `reference_pdt_rule_eliminated_2026`.
- **2026-06-16:** Architecture = **multi-timeframe split** (daily filter → intraday execution). ✅
- **2026-06-16:** Self-improvement loop = **auto-adjust within guardrails** (not suggest-only,
  not read-only). ✅
- **2026-06-16:** Old system **paused, not scrapped**; V2 is a separate book.
- **2026-06-16:** **Universe = dynamic & broad, RS-ranked daily.** No static list, no price
  floor/ceiling; liquidity floor (scaling with account size) replaces price filtering. ✅
- **2026-06-16:** **Position lifecycle graduates** (intraday → swing → position) on health;
  EOD close is conditional, never cut a healthy runner on the clock. ✅
- **2026-06-16:** **Account = $5K**, funded only after V2 proves profitable on paper; **scale
  by compounding daily profits, NOT leverage.** Use a **margin account self-restricted to 1:1
  (no borrowing)** — captures post-PDT mechanics (unlimited day trades, instant buying-power
  reuse, no T+1 cash-settlement good-faith violations) without leverage risk. ✅

## 8. Open Questions (still to decide)
- Exact liquidity-floor thresholds and how they scale with account equity (Layer 1) — set in Phase 1.
- Exact health-score formula weights and graduation thresholds (Layer 3) — derive from data in Phase 3.
- Exact guardrail caps (Layer 4 bands) — set before Phase 5.
