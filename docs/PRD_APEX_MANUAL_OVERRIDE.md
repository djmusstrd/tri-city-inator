# PRD — APEX Manual Override (Telegram + Dashboard close/trim buttons + TV chart link)

- **Status:** DRAFT / not implemented (planning only)
- **Date:** 2026-06-24
- **Baseline to preserve:** git tag `apex-baseline-2026-06-24-stable` (commit `1193669`)
- **Motivation:** today's research proved the *mechanized* fade exit loses (see PRD_APEX_DYNAMIC_EXIT
  §9b — two gate failures); the human discretionary read beat it. The right tool is therefore a
  **fast manual override**, not another rule: surface the fade, get eyes on the chart, let the
  operator react — and if they don't, run the existing rules unchanged.

---

## 0. Guiding principle — ADDITIVE, SAFE-BY-DEFAULT

The engine is untouched. This feature only ever *adds* an optional early manual exit. **If the
operator doesn't tap — or Telegram is down, or the bot can't receive the tap — the existing health
/ stop / carry rules run exactly as they do today.** Worst case, the button does nothing. It cannot
make tomorrow worse than today.

---

## 1. The loop
```
health-decay alert fires  ->  operator glances (taps [📈 Chart] for eyes-on)  ->  decides
   -> taps [Close] / [Close+block] / [Trim ½]   => actioned at CURRENT market in ~30s
   -> no tap                                     => existing rules run, untouched
```
Persistent affordance, **no timed window** (rules never pause). Also reachable on-demand via a
`/positions` command listing every open trade with the same buttons.

---

## 2. Design decisions (locked 2026-06-24)

### 2.1 Latency — Option A (tight inner poll)
Keep ONE Telegram updates consumer (the poller — `getUpdates` is single-consumer per bot). Add a
tight inner poll (~20–30s) for Telegram updates *between* the 3–5 min trade passes, so a tap is
actioned in ~30s without a webhook or a second consumer. Justified by the "research-then-react"
flow: operator think-time dominates; ~30s tap-reaction feels instant.

### 2.2 Shared `close_now(symbol, fraction=1.0, block=False)`
ONE function, called by both Telegram and the dashboard. Does the exact validated manual sequence:
1. cancel the symbol's open protective stop (shares are reserved by it — `qty_available 0`),
2. market sell `fraction` of the position,
3. **if partial (`fraction<1`): re-place a stop for the REMAINING qty** (cf. memory
   "Stop Re-placement on TP Change" — never leave the remainder unprotected),
4. if `block`: add symbol to today's `apex-flags.json` avoid list (no re-entry),
5. let the poller reconcile + journal (proven clean today on 7 closes).
Idempotent: checks the position still exists; guards double-tap.

### 2.3 Buttons
`[Close]` · `[Close + block today]` · `[Trim ½]` · `[📈 Chart]`
- **[Close]** → `close_now(sym, 1.0)`.
- **[Close + block today]** → `close_now(sym, 1.0, block=True)` (prevents poller re-entry, same as
  the strict-mode lever used manually today).
- **[Trim ½]** → `close_now(sym, 0.5)` — bank half, re-stop the runner. This is the untested
  scale-out lever; v1 includes it so live data can finally evaluate it.
- **[📈 Chart]** → inline URL button to TradingView (see §2.5).

### 2.4 Confirm guard
Build a confirm path now (tap → "tap again to confirm"), **default OFF for paper** (single-tap),
toggle ON for live. Prevents fat-finger closes when real money is involved.

### 2.5 TV chart link
Inline URL button → `https://www.tradingview.com/chart/?symbol=<EXCH>:<SYM>`. Exchange prefix comes
from Alpaca position data (`exchange` field). Interval pinned to `ORB_MINUTES` so it opens on the
trading timeframe.

### 2.6 Alert content (decision-at-a-glance)
Enrich the health-decay alert so the give-back is visible before opening the chart, e.g.:
`RXT health 41 · +1.2% now (peak +8.6%) · gave back 86% · below VWAP, lower highs` + buttons.

---

## 3. PRESERVATION & SAFEGUARDS
1. **Additive only** — no edits to existing health/stop/reconcile/carry/entry paths. The override
   is a new handler + a new shared close function called only on an explicit operator tap.
2. **Flag** `APEX_MANUAL_OVERRIDE` (default ON after test) + live kill-switch in `apex-flags.json`
   (`"manual_override"`), read each pass → disable action handling mid-session, no restart. When
   off, alerts revert to plain informational messages (today's behavior).
3. **Single `getUpdates` consumer preserved** — the inner poll lives inside the poller process;
   no second listener. Must update `allowed_updates` to include `callback_query`.
4. **Broker stop integrity** — full close cancels then closes; partial close cancels then re-places
   a stop for the remaining qty. A position is never left unprotected.
5. **Idempotency / race** — `close_now` re-checks live position before acting; double-tap and
   "tapped while a rule also fired" both resolve to a single close via the reconcile path.
6. **Dashboard** — close/trim on the Positions page calls the same `close_now`; confirm dialog;
   idempotent across Streamlit reruns (guard so a rerun can't re-fire the order).

---

## 4. Regression checklist (before live)
- [ ] Flag OFF → alerts informational only; zero behavior change vs baseline.
- [ ] No tap → health/stop/carry rules fire identically to baseline.
- [ ] [Close] → cancel-stop → full close → reconciled + journaled once.
- [ ] [Trim ½] → half closed, **remaining half still has a broker stop**, journaled correctly.
- [ ] [Close + block] → symbol added to avoid; poller does not re-enter it that day.
- [ ] Double-tap / stale-alert tap → exactly one close, at current market.
- [ ] Only one `getUpdates` consumer; trade-pass cadence unaffected by the inner poll.
- [ ] TV link opens the correct symbol+exchange at the ORB interval.

## 5. Validation / rollout
1. Paper: exercise each button on a live-paper position; verify §4 checklist.
2. Run a session with it enabled; confirm taps action in ~30s and non-taps change nothing.
3. Keep confirm OFF for paper; flip ON before any live use.

## 6. Open items
- `[Trim ½]` rounding on odd share counts (e.g., 5 → 2 or 3?) — define rounding rule.
- Dashboard auth (anyone on the LAN can hit the button) — acceptable for local use; note for later.
- Telegram callback for a position already closed (rule beat the tap) → respond "already flat", no-op.

## 7. Rollback
- **Behavior:** `APEX_MANUAL_OVERRIDE=false` (or `apex-flags.json` `"manual_override": false`) →
  alerts revert to informational, no code change, no restart.
- **Code:** `git checkout apex-baseline-2026-06-24-stable`.
- Safe-by-default means even a silent failure of the inbound path degrades to today's behavior.
