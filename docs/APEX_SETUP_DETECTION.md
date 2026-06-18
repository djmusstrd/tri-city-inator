# APEX — Setup-Detection & Early-Promotion Upgrade

**Goal:** catch moves *early* (on their common precursor signature) instead of *late* (after they
hit our 25-slot warm set or the 30-min rescan). Maximize the captured move **without bogging TV
or diluting the RS-leader edge.** The system already finds + manages winners (e.g. 2026-06-18:
+$105 realized, MUU/SNDK/MVLL the drivers) — this is **additive**, not a rewrite.

## Core principle — scan wide on cheap data, stream narrow on expensive data
Two layers, decoupled:
- **FIND (cheap, broad, no streaming):** snapshots / movers polled on a tight cadence flag names
  building a setup.
- **CATCH (expensive, narrow, real-time):** only names showing the precursor signature get promoted
  into the TV real-time warm stream (dedicated APEX Feed tab) — so we're positioned *before* the
  trigger, not reacting after.

We never real-time-stream the universe. We real-time-stream the *promotion shortlist*.

## Common setup characteristics (the precursor signature — "else we're late")
Score each candidate on cheap snapshot/bar data; promote when the composite crosses a threshold:
1. **RVOL surge** — volume vs the 20-day average-at-this-time-of-day rising (#1 precursor).
2. **Range compression → expansion** — tightening intraday range / Bollinger squeeze resolving up.
3. **Level reclaim / approach** — pressing ORH, prior-day high, VWAP reclaim, round number.
4. **Catalyst** — fresh news (`get_news`) or a corporate action (earnings/guidance).
5. **RS context** — already in / near the daily RS-leader set (keeps the edge; see scope below).

A name lighting up 3+ of these is "setting up" — promote it before it triggers.

## Scope: RS-leader set, NOT the whole market
APEX's edge is *relative-strength leaders*. The radar scans **movers ∩ most-active ∩ RS leaders**
(~200 names), not the entire market — broad enough to catch emergent movers, narrow enough to keep
the edge and the load sane. Chasing market-wide explosions = a different (worse) strategy.

## Alpaca endpoints to wire (audited 2026-06-18 — currently UNUSED)
- `get_market_movers` — market-wide top gainers/losers in one call (broad cheap radar).
- `get_most_active_stocks` — volume leaders (RVOL precursor, direct).
- `get_corporate_actions` / `get_corporate_action_announcements` — earnings/catalyst awareness.
Already used: bars, snapshots (incl. batch), latest quotes, news, account/orders/positions.

## Warm real-time set: target 100 (pending a live test)
The dedicated APEX Feed tab isolates streaming load from the user's chart (verified: 100 syms
streamed kept the user chart at ~3ms). So a bigger warm set is plausible. **But coverage at scale
is unproven** — cold 100-sym read was only 31%; warm/persistent is ~92% at 25. **Action: market-open
scaled test** — ramp warm subscriptions 25→50→75→100 over several minutes, measure warm coverage +
user-chart latency + TV CPU. That number sizes the promotion shortlist. (Can't test after hours —
no ticks/load.)

## Locked decisions (2026-06-18)
- **Price band stays $2–100** (`APEX_PRICE_MAX=100`). High-priced leaders move (today they won), but
  the operator does **not** want capital tied up in expensive single-share holds. No fractional /
  expensive-stock work. The band is the deliberate choice, not a sizing gap to fix.
- **Whole-share risk sizing unchanged**; `raw_qty<1 → skip` already shipped.
- **Fresh-entry churn guard** shipped+staged (aed02b5) — activates next poller restart.

## Build sequence
1. `apex_radar.py` — pull movers + most-active, intersect with RS leaders, emit a candidate list.
2. `apex_setup_score.py` — score each candidate on the 5 precursors (snapshot/bar data, no stream).
3. Promotion hook — feed the top setups into the poller's warm real-time set (replaces/augments the
   static top-25 push); keep the warm cap at the tested ceiling.
4. Tighten the intraday rescan cadence (30 min → ~5–10 min) for the cheap FIND layer.
5. **Live 100-sym warm-coverage test** (next market open) → set the warm cap.
6. Backtest the promotion criteria (did promoted names actually trigger + run?) before trusting it.

> Build #1–#2 are pure additive modules (no live-poller risk). #3 touches the poller (restart).
> #5 needs market-open. Nothing here changes the working entry/exit/management core.
