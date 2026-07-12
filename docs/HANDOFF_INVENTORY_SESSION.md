# Handoff — Dashboard Feature-Inventory Session

> Session goal: execute the FIRST deliverable of `docs/DASHBOARD_TEMPLATE_BRIEF.md` — a full,
> complete inventory of every dashboard, with no jumping straight to a build.

## What was asked
Take a **full and complete inventory of each dashboard** (no glossing over details, no premature
building) as the parity/no-feature-left-behind spec for the reusable dashboard + Telegram template.

## What was produced
**`docs/FEATURE_INVENTORY_MATRIX.md`** (500 lines) — the brief's first deliverable, in two parts:

- **Part 1 — cross-system matrix (§1–12):** every feature as a row, the four systems as columns,
  has/lacks/variant per cell. Sections: architecture · page union → template page set ·
  live-positions breakdown · in-dashboard charting · analytics catalog · candidates/scanner ·
  **interaction layer** (7a Telegram / 7b manual-override / 7c TV-control) · cross-cutting infra ·
  config flags · journal schema · per-system parity checklist · template superset.
- **Part 2 — per-system appendix (A–D):** each dashboard audited standalone, page by page in render
  order, every widget enumerated.

Read directly from source (not from memory): `apex_dashboard.py` (801) · Tri-City `dashboard.py`
(1238) · Compounder `dashboard.py` (1315) · Dark City `dashboard.py` (1435) · APEX interaction layer
`apex_telegram.py` / `apex_actions.py` / `apex_rationale.py`.

## Key findings
1. **Interaction layer is almost entirely APEX-only.** Telegram (5 alert types + inbound commands +
   `/positions` + button callbacks), manual override (close / trim½ / close+block, confirm,
   kill-switch, never-naked, own-book journaling), and TV control are ✅ APEX, — for the other three.
   Biggest build chunk, as the brief predicted.
2. **No single system has the full analytics suite.** Tri-City & Dark City share the R-based suite
   (Sharpe/Calmar/histograms/scatters/drawdown); APEX has none of it; Compounder has its own (RS
   bars, scanner funnel) but no R-analytics. Template must union all three.
3. **Two unique pages worth preserving:** Dark City's **Regime & Edge** (edge-score calibration +
   risk-controls table) and **Session cockpit** (chips + dual feeds + ticker strip); APEX's
   **in-dashboard charting** + **Trade Journal thesis cards**.
4. **Journal-schema split is bidirectional (§10):** APEX journals `peak_gain`/`health`/`partial`
   but lacks the R-analysis block; Tri-City/Dark City journal `r_multiple`/`risk_dollars`/`stop`/
   `targets` but lack APEX's. Enriched template record needs both — confirms the brief's
   "APEX-enriched" decision.
5. **Compounder is correctly the hardest case (§11):** 3 Alpaca accounts, position groups,
   per-strategy pages, hard-coded 5-tab Playbook. Adapter must model `sub_strategy` + multi-account
   from day one.

## Git state (as of this session)
- Branch: `apex-manual-override`.
- Commit `50c392c` — "Add dashboard feature-inventory matrix (template spec, no-build)" (inventory
  doc only; 1 file, 500 insertions).
- Pushed to `origin/apex-manual-override`.
- **PR #1** opened → `main`: "APEX manual override + dashboard feature-inventory matrix"
  (https://github.com/djmusstrd/tri-city-inator/pull/1). Covers the full manual-override feature
  (10 commits) + the inventory doc.
- `docs/DASHBOARD_TEMPLATE_BRIEF.md` left **untracked/local** (scoped to committing the inventory only).

## Next steps (per the brief's sequencing)
1. Finish APEX: equity-validate the manual override, flip `APEX_MANUAL_OVERRIDE` on, merge.
2. Build the template (`tradingkit` editable-install repo) against this matrix: interaction layer ←
   APEX, adapter shape ← Compounder needs, analytics ← the union.
3. Rebuild Compounder greenfield on the template (n=2 that proves the abstraction).
4. Retrofit Tri-City + Dark City with parity checks against the matrix.

## Reference
- Brief: `docs/DASHBOARD_TEMPLATE_BRIEF.md`
- Deliverable: `docs/FEATURE_INVENTORY_MATRIX.md`
- Interaction-layer reference impl: `apex_dashboard.py`, `apex_telegram.py`, `apex_actions.py`,
  `apex_rationale.py`, `docs/PRD_APEX_MANUAL_OVERRIDE.md`
