# Dashboard + Telegram Template — Handoff Brief

> Working spec for the build that runs in a separate window. Captures the 2026-06-24 design
> discussion so it isn't re-derived. **Scope is functionality only — no strategy/engine logic changes.**

## Goal
Give every trading system the **APEX-style dashboard + Telegram cockpit** as a reusable, build-once
template. Each system keeps its **core idea/strategy 100% intact** — we only add a presentation +
interaction layer that reads its existing data and wires to its existing close/journal.

## The systems (all github/djmusstrd)
| System | Dashboard | Telegram today | Role |
|--------|-----------|----------------|------|
| **APEX** | `tri-city-inator/scripts/apex_dashboard.py` (800 ln) | ✅ `apex_telegram.py` | ⭐ reference for the **interaction layer** |
| **Tri-City** | `tri-city-inator/scripts/dashboard.py` (1237 ln) | — | retrofit |
| **Compounder-Inator** | `compounder-inator/scripts/dashboard.py` (1314 ln, multi-sub-strategy: Momentum/Runner/Weinstein) | — | rebuild greenfield on template (n=2 proving ground) |
| **Dark City** | `dark-city-inator/scripts/dashboard.py` (1434 ln) | — | retrofit |

**Telegram is net-new for 3 of 4 systems** — the biggest win and biggest chunk of work.

## Key design principles (settled in discussion)
1. **Template = SUPERSET, not an APEX clone.** APEX is small because it's *missing* analytics
   (Signal Analysis, Risk & Sizing, Regime & Edge, multi-strategy views, cheat sheet) the others
   have. The template absorbs everyone's best pages so nothing is lost.
2. **APEX is the reference ONLY for the interaction layer** — Telegram (alerts / buttons /
   `/positions`) + manual override (Close / Trim ½ / Close+block, TV link, give-back line) + Live
   Positions. Do NOT let APEX's single-book, intraday-graduate lifecycle define the analytics pages
   or the adapter shape.
3. **Design the adapter against the HARDEST case (Compounder, multi-sub-strategy / position groups),
   not the easiest.** Borrow the interaction layer from APEX; borrow analytics from the union.
4. **Thin adapter, no engine changes.** Per system the adapter supplies coordinates only:
   Alpaca creds, Telegram bot token/chat, journal/state/config file paths, and the strategy-specific
   pages. A normalization shim maps each system's journal/state → a common view model the generic
   pages render. The engine/strategy code is untouched.
5. **Clean per-strategy isolation** (operator decision): each system gets its **own Alpaca account +
   own Telegram bot** → no shared-account position-ownership problem, no single-consumer getUpdates
   conflict. (If any system keeps a shared account, that adapter adds a "my positions only" filter.)
6. **Safe + reversible**, like APEX: actions flag-gated (`*_MANUAL_OVERRIDE`), kill-switch, confirm
   option; migrations validated with **parity checks** (old vs new render identical data + actions).

## FIRST deliverable (do this before any code)
**An exhaustive feature-inventory matrix** — rows = every page, chart, table, alert type, button,
command, flag; columns = APEX / Tri-City / Compounder / Dark City; cells = has / lacks / variant.
This matrix IS the template spec and the no-feature-left-behind / parity checklist.

## Template packaging — DECIDED
**Dedicated `tradingkit` git repo, installed editable (`pip install -e ~/tradingkit`).** All repos
share one machine + the `(base)` env, so one editable install makes the kit importable across all
four systems; edit the kit once and every system picks it up live (the only real upside of a
submodule) — without submodule friction, plus real versioning when you want to pin. **Not a submodule.**

## Template trade record (journal schema) — DECIDED
Standardize on the **APEX exit journal, enriched**. The adapter maps each system's existing fields
onto it; the few new fields are added by small **exit-path instrumentation** (instrumentation, NOT
strategy logic — allowed under the no-core-change rule).

**Base (APEX today):** `timestamp, symbol, trigger, entry, exit, qty, pnl, gain_pct,
peak_gain (=MFE), health_at_exit, status_at_exit, days_held, entry_window, reason, entry_time,
order_id, partial, remaining_qty`.

**Add for the template:**
- `strategy` / `sub_strategy` — unified Closed-Trades view (Compounder = Momentum/Runner/Weinstein)
- `trade_id` — roll scale-out / Trim ½ legs into one trade (matters now that trims exist)
- `initial_stop` + `initial_risk` + `r_multiple` — self-contained R analysis comparable across
  strategies (the top gap) · *needs source capture at entry*
- `mae` (max adverse excursion) to pair with `peak_gain` (MFE) — stop/heat analysis · *needs in-trade capture*
- `slippage` (signal price vs fill) — APEX logs it but doesn't journal it · *capture at fill*
- `fees` — 0 on paper; field present for live

## Sequencing
1. Finish APEX (equity-validate manual override, flip on, merge) — extract from proven code.
2. Build template: interaction layer ← APEX; adapter shape ← Compounder needs; analytics ← union.
3. **Rebuild Compounder greenfield on the template** = n=2 that proves the abstraction.
4. Retrofit Tri-City + Dark City with parity checks.

## Hard constraints
- Never modify strategy/engine logic — dashboard + Telegram functionality only.
- Each system's actions journal/close to **its own** book (the APEX mis-journal trap).
- Reference implementations: `apex_dashboard.py`, `apex_telegram.py`, `apex_actions.py`,
  `apex_rationale.py` (message templates), and `docs/PRD_APEX_MANUAL_OVERRIDE.md`.
