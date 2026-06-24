# PRD — APEX Dynamic Give-Back Exit

- **Status:** DRAFT / not implemented (planning only)
- **Date:** 2026-06-24
- **Baseline to preserve:** git tag `apex-baseline-2026-06-24-stable` (commit `1193669`)
- **Author:** trading-session design discussion (operator + Claude)

---

## 0. Guiding principle — ADDITIVE, REVERSIBLE, DEFAULT-OFF

The current system works (validated live-paper 2026-06-24, +$64.24). This feature must
**never degrade the baseline.** Every requirement below is subordinate to that. The feature
ships behind a flag that is **OFF by default**; when off, APEX must behave **byte-for-byte** like
the tagged baseline. We do not edit existing exit/stop/reconcile code paths — we add a new,
optional term that the manage layer consults only when explicitly enabled.

> If anything here makes tomorrow worse than today, it has failed regardless of backtest numbers.

---

## 1. Background — the validated baseline (DO NOT REGRESS)

Behaviors confirmed working today; these are the regression invariants:

1. **Reconciliation** of manual/broker-side closes → journaled exactly once (7 closes today, all clean).
2. **GTC-on-carry** — overnight holds keep durable broker stops (5/5 confirmed at start).
3. **Health-based proactive exit** (`health < exit_health`, default 40) cuts faders (ERAS@29, TWST@25).
4. **Broker hard stop** (ATR-based) is the ultimate protection and is always present.
5. **Strict-mode entry gate** — `apex-flags.json` `strict:true` + empty `prioritize` blocks all
   auto-entry live, no restart; read live each pass.
6. **Operator approval flow** — free slot → read-only ORB15 scan → approve → `prioritize` → poller
   enters only that name → clear `prioritize` re-locks.
7. **Swing vs intraday split** — `manage_positions` skips `status != intraday`; swings owned by
   `apex_swing.py` (daily-close EMA10/invalidation), run ~15:10 CT via launchd.
8. Premarket idle gating, fast/slow cadence, CDP hybrid quote feed, daily leader rebuild.

---

## 2. Problem statement

Winners give back earned profit before exiting. Today RXT ran +8.6% then round-tripped to +1%
(~$18–24 left on the table); the health exit and our manual close both lagged the peak. A naive
fixed-% trailing stop is the **wrong primitive**:

- It ignores gain size — 3% gives back 75% of a +4% move but only 27% of a +11% move.
- It ignores volatility — 3% shakes out a $6 mover (RXT) and overstays a $90 quiet name.
- It ignores trade type and time — a day-trade fader near the close ≠ a swing breathing midday.
- It fires on noise — a flat-3% trail shook RLAY out at 17.60; it recovered to 18.10 the same day.

**Reframe:** protect a *fraction of peak unrealized profit*, scaled dynamically — not a fixed price %.

---

## 3. Goals / Non-goals

**Goals**
- Reduce give-back on positions that have *earned* a profit cushion, without churning winners.
- Be dynamic on: profit size, volatility (ATR), hold type (swing/day), recent action, time of day.
- Be fully reversible and observable before it ever acts on a live order.

**Non-goals**
- Not an entry fix. Positions that never ran (NUAI/ERAS/TWST) are out of scope — they're an
  entry-quality problem, handled separately.
- Not a replacement for the broker hard stop or the health exit — it's an *additional, tighter*
  give-back layer that only arms on winners.
- No change to swing daily-structure logic unless explicitly tested and flagged.

---

## 4. Proposed feature — dynamic give-back exit

### 4.1 Core rule
```
IF position is ARMED                      # has earned a cushion
AND give_back(peak_gain, current_gain) > allowed_give_back(context)
AND confirmation gate passes              # momentum actually rolling, not one-bar noise
THEN exit (else hold)
```

### 4.2 Arming (don't trail noise)
- Arm only after gain ≥ `arm_threshold` (default: **+1 ATR** above entry, or +5% fallback).
- Before armed: behavior identical to baseline (initial ATR/structure stop + health). **No change.**

### 4.3 Allowed give-back (the dynamic part) — expressed in **ATR / R units**, not %
A function of:
| Input | Source (exists today) | Effect |
|-------|----------------------|--------|
| Profit tier | `gain_pct` / R | <1R: not armed. 1–2R: protect ~50% of move. >3R: protect ~70–75%. |
| Volatility | ATR (`atr_from_daily`) | Give-back band = `k × ATR`; auto-normalizes across price. |
| Hold type (tier) | `status` | **Day-trades-that-graduate (resolved §8.1):** starts day → tight intraday band; only after graduating to swing → wide daily-structure band. Tier-keyed, not trigger-keyed. |
| Time of day | clock | Tighten band as close approaches for ungraduated (day-tier) names; flatten faders into the close. |

### 4.4 Confirmation gate (prevents RLAY-style shakeouts) — TUNABLE, default ON (resolved §8.2)
Only fire if the give-back is *confirmed* by deterioration — reuse the existing `health` signal
(lower highs / below VWAP / below EMA9) and, if available, **volume** (heavy-volume reversal =
distribution → fire; light-volume drift = consolidation → hold). Single down-tick never triggers.
Implemented as an on/off backtest variable; the backtest reports both ways, default ON.

### 4.5 EOD handling
Day-trade (`intraday`) names that are armed and fading into the close: flatten, never carry.
Swing names: defer to `apex_swing.py` daily-structure rule unchanged.

---

## 5. PRESERVATION & SAFEGUARDS (the part that matters most)

1. **Master flag, default OFF** — `APEX_DYNAMIC_EXIT=false` in `.env`. When off, the new code path
   is not entered; baseline behavior is identical. A regression test asserts this.
2. **Live kill-switch** — also gated by an `apex-flags.json` key (e.g. `"dynamic_exit": false`)
   read live each pass, so it can be disabled mid-session **without restarting the poller** (same
   mechanism that made strict-mode safe today).
3. **No edits to existing paths** — reconciliation, GTC carry, health exit, broker stop, strict
   gate, swing manager remain untouched. The give-back check is a **new, additive function** called
   after the existing checks, and only when both flags are on AND the position is armed.
4. **Broker hard stop always remains** — the give-back exit is tighter and optional; the ATR hard
   stop is the floor and is never removed or widened by this feature.
5. **Shadow mode first** — a `dynamic_exit: "shadow"` setting logs *what it would have done*
   (symbol, peak, give-back, would-exit price/PnL) each pass **without placing any order**, for
   ≥1 live session, so we see its decisions against reality before it can act.
6. **Backtest gate** — must run across ≥20 past sessions (clean multi-day highs, complete bars) and
   show P&L **and** win-rate **not worse** than baseline, plus reduced avg give-back, before live
   enable. Numbers logged in this doc.
7. **Rollback** — `git checkout apex-baseline-2026-06-24-stable` restores code; flag-off restores
   behavior without a code change. Documented restore step in the tag message.

---

## 6. Regression checklist (run before any live enable, flag ON and OFF)
- [ ] Flag OFF → exits/journal/stops identical to baseline on a replayed session (diff = 0).
- [ ] Manual close still reconciles + journals exactly once.
- [ ] GTC carry stop still placed at EOD for swing graduates.
- [ ] Health exit still fires at <40 independent of the new layer.
- [ ] Strict gate + approval flow unaffected.
- [ ] Broker hard stop present on every position at all times.
- [ ] Kill-switch in `apex-flags.json` disables the layer mid-session, no restart.

---

## 7. Validation plan
1. **Backtest** parameterized policy (`arm_threshold`, `give_back_k×ATR` per tier, confirmation
   on/off, swing/day widths, EOD-flatten) over ≥20 sessions. Report per-trade + aggregate vs baseline.
2. **Shadow mode** live for ≥1 session — compare logged would-do vs actual.
3. **Enable live-paper** with the tuned params only after 6 & shadow pass.

---

## 8. Open questions

**RESOLVED 2026-06-24 (operator):**
1. **Hold intent → day-trades-that-graduate.** Every entry starts as a *day trade* with the tight,
   intraday give-back band. It only *graduates* to the wide, daily-structure (swing) band after it
   earns it (e.g., closes strong / qualifies for the existing EOD carry). Until graduated, the
   tight band applies and faders are flattened into the close — never carried. This keys the
   swing/day width split (§4.3) and EOD handling (§4.5) to the position's *current* tier, not its
   trigger type.
2. **Confirmation gate → tunable.** Implement as an on/off backtest variable (§4.4), **default ON**
   (the RLAY shakeout argues for it), but the backtest must report results both ways so we choose
   on evidence.

**Still open:**
3. Volume signal availability/quality in the live feed for the confirmation gate.

---

## 9b. Validation tooling + RESULTS (2026-06-24)

**Tools built (analysis only, touch nothing live):**
- `scripts/apex_giveback_backtest.py` — n=67 live-trade sanity check (plumbing + hypothesis).
- `scripts/apex_giveback_validate.py` — decision-grade harness: historical ORB15-proxy replay →
  walk-forward (rolling train/test, OOS by construction) → Monte Carlo (block-bootstrap by day).

**Results:**
- **Sanity check (n=67, one regime):** confirmation gate ON >> OFF; best policy +$126 vs baseline.
  *Not decision-grade* — few correlated trades.
- **Decision-grade (1,148 trades, 38 sessions, 6 OOS folds):** give-back **UNDERPERFORMS baseline
  out-of-sample by −$256** (WF: +$1,077 vs +$1,333). Monte Carlo: baseline dominates (median
  $3,280 vs $2,289; p5 $1,369 vs $827). Give-back's only edge = slightly smaller maxDD
  (−$324 vs −$390). confirm=ON chosen 6/6 windows.

**GATE STATUS: FAILED (twice).** §7 requires "not worse than baseline OOS."
- *Distance trail* (`apex_giveback_validate.py`): −$256 OOS; fired too early, shaken out of runners.
- *Real fade detector* (`apex_fade_exit_validate.py`, health<40 armed): also loses — P&L $2,734 vs
  baseline $3,310; MC median $2,702 vs $3,294. **Peak-profit retained 17% vs baseline 18%** — the
  fade-exit protects NO more profit (confirmation lag: by the time health<40 confirms, the give-back
  already happened) AND clips runners that dipped-then-continued.

Root cause: tight exit = shaken out; confirmed exit = too late. Both lose to holding to stop/EOD.
**Does NOT proceed to live. The baseline exit is empirically better. Feature shelved.**

**Before reconsidering, would need to clear the negative — options:**
1. Compare against APEX's *actual* health-exit baseline (not ATR-stop/EOD proxy).
2. ATR-normalized bands instead of %.
3. Test **partial profit-taking** (scale out) vs full-trail — trailing the whole position is what
   killed expectancy; banking part + trailing a runner may behave differently.
4. Multiple regimes / longer window.
5. If none beat baseline OOS → **shelve**; the baseline (run to stop/EOD) is the better policy.

---

## 9. Rollback plan (explicit)
- **Behavior:** set `APEX_DYNAMIC_EXIT=false` (or `apex-flags.json` `"dynamic_exit": false`) → instant revert, no restart.
- **Code:** `git checkout apex-baseline-2026-06-24-stable` → restores validated baseline.
- **Trust:** if the feature is even suspected of misbehaving live, disable first, investigate after.
