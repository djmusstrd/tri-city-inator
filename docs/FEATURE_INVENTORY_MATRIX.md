# Dashboard + Telegram — Feature Inventory Matrix

> **The no-feature-left-behind spec.** Exhaustive inventory of every page, chart, table, metric,
> widget, alert, button, command, flag, and data source across all four systems — read directly
> from source on 2026-06-24. Rows = features; columns = APEX / Tri-City / Compounder / Dark City;
> cells = **✅ has** · **—  lacks** · **▲ variant** (has it, but a different shape).
>
> This is the companion to `DASHBOARD_TEMPLATE_BRIEF.md`. The brief says *what* and *why*; this is
> *exactly what exists today*. The template = the **union** of every ✅ below; per-system parity =
> every ✅ in that system's column must still render after migration.

**Sources inventoried**
| System | File | Lines | Interaction layer |
|--------|------|------:|-------------------|
| APEX | `tri-city-inator/scripts/apex_dashboard.py` | 801 | `apex_telegram.py` · `apex_actions.py` · `apex_rationale.py` |
| Tri-City | `tri-city-inator/scripts/dashboard.py` | 1238 | — |
| Compounder | `compounder-inator/scripts/dashboard.py` | 1315 | — |
| Dark City | `dark-city-inator/scripts/dashboard.py` | 1435 | — |

---

## 1. System architecture (shape of each book)

| Trait | APEX | Tri-City | Compounder | Dark City |
|-------|------|----------|------------|-----------|
| Book model | single book, intraday→swing graduate | single intraday book | **3 sub-strategies** (Weinstein / Runner / Momentum) | single intraday book |
| Alpaca accounts | 1 | 1 | **3** (`ALPACA_*`, `RUNNER_ALPACA_*`, `MOMENTUM_ALPACA_*`) | 1 |
| Engine state source | `shared/apex-state.json` (live positions) | Alpaca + `executions.json` | per-strategy `*-portfolio.json` | Alpaca + `executions.json` |
| Closed-trade journal | `logs/apex-journal.json` (APEX schema) | `logs/tri-city-journal.json` (R-schema) | 3× `*-journal.json` (BUY/SELL log) | `logs/dark-city-journal.json` (R-schema) |
| Entry/signal log | `logs/apex-rationale.json` (Layer-6 thesis) | `logs/tri-city-executions.json` | (none separate) | `logs/dark-city-executions.json` |
| Real-time chart | ✅ in-dashboard candlesticks (Alpaca bars + TV quote) | — (TV desktop is the chart) | — | — |
| Live market feed beyond Alpaca | ✅ TV CDP quote (hybrid) | — | — | ▲ yfinance fallback for ticker strip |
| Regime model surfaced | ▲ regime string on cards | ▲ spy_regime in exec log | — | ✅ dedicated regime file + page |

---

## 2. Page inventory (the union → template page set)

Rows = every distinct page that exists in any system. This union is the template's page menu.

| Page (union) | APEX | Tri-City | Compounder | Dark City | Notes |
|--------------|:----:|:--------:|:----------:|:---------:|-------|
| **Live Positions** | ✅ `Live Positions` | ✅ `Positions` | ▲ `Positions` (3-acct tabs) | ▲ inside `Session` | every system has it, 4 different shapes |
| **Session / Cockpit** | ▲ (in Live Positions) | — | — | ✅ `Session` (chips + feeds + ticker) | Dark City's hero page |
| **Overview** (equity curve + outcomes) | — | ✅ | ▲ (per-bot in Trade Log) | ✅ | APEX has no aggregate analytics |
| **Trade Log** | ▲ `Closed Trades` | ✅ `Trade Log` | ✅ `Trade Log` (3-bot) | ✅ `Trade Log` | |
| **Trade Journal / Why** (thesis cards) | ✅ `Trade Journal` | — | — | — | APEX-only; the Layer-6 rationale view |
| **Signal Analysis** | — | ✅ | — | ✅ | win-rate/R by setup, indicator histos |
| **Risk & Sizing** | — | ✅ | — | ✅ | R-dist, streaks, drawdown, risk scatter |
| **Regime & Edge** | — | — | — | ✅ | regime + edge-score calibration |
| **Chart a Leader** | ✅ | — | — | — | hypothetical-entry chart on any leader |
| **Leaders / Watchlist** | ✅ `Leaders` (editable flags) | — | — | — | RS leaders + avoid/prioritize/watch editor |
| **Candidates / Scanner** | — | ✅ | ▲ (per-bot candidate tables) | ✅ | premarket ranked + research queue |
| **Universe & Rankings** | — | — | ✅ | — | RS distribution + scanner funnel |
| **Per-strategy pages** | — | — | ✅ `Weinstein` `Runner` `Momentum` | — | one page per sub-strategy |
| **Playbook** | ✅ (md file) | ✅ (CHEATSHEET.md) | ✅ (5 hard-coded tabs) | ✅ (CHEATSHEET.md) | 4 systems, 2 delivery styles |

**Template page set (superset):** Session/Cockpit · Live Positions · Overview · Trade Log ·
Trade Journal (Why) · Signal Analysis · Risk & Sizing · Regime & Edge · Chart a Symbol · Leaders/Watchlist ·
Candidates · Universe & Rankings · **{strategy-specific pages}** (adapter-supplied) · Playbook.

---

## 3. Live-positions feature breakdown

Every element that appears on the positions surface, per system.

| Element | APEX | Tri-City | Compounder | Dark City |
|---------|:----:|:--------:|:----------:|:---------:|
| Account strip: Equity | ✅ | ✅ | ✅ (×3 accts) | ✅ (chip) |
| Account strip: Day P&L (+%) | ✅ | ✅ | ✅ (×3) | ✅ (chip) |
| Account strip: Buying power | ✅ | ✅ | — | ✅ (chip) |
| Account strip: Cash | ✅ (sidebar) | — | — | — |
| Account strip: Open-position count | ✅ | ✅ | ✅ (×3) | ✅ (chip) |
| Market open/closed indicator | — | — | — | ✅ (chip, Alpaca clock) |
| Detected-regime chip | — | — | — | ✅ |
| Total unrealized-P&L banner | — | ✅ | ✅ (per tab) | ✅ |
| Positions table | ✅ | ✅ | ✅ (per tab) | ✅ |
| — col: setup/trigger | ✅ trigger | ✅ setup | — | ✅ setup |
| — col: health score | ✅ | — | — | — |
| — col: status (intraday/swing) | ✅ | — | — | — |
| — col: peak % (MFE) | ✅ | — | — | — |
| — col: stop | ✅ | ✅ | ✅ (Runner) | ✅ |
| — col: T1/T2/T3 targets | — | ✅ | ▲ target (Momentum) | ✅ |
| — col: market value / notional | ✅ | — | ✅ (detail) | — |
| — col: src (alpaca vs engine-only) | ✅ | — | — | — |
| Union of broker + engine positions | ✅ | — | — | — |
| Per-position detail cards | ▲ (chart) | ✅ (Levels/Targets/Risk) | ✅ (Runner+Momentum) | — |
| — risk-to-stop % | ✅ | ✅ | ✅ | — |
| — days-held | ✅ | — | ✅ (Runner) | — |
| — stop ratchet (orig→current) | — | — | ✅ (Runner) | — |
| — trailing-active flag | — | — | ✅ (Runner) | — |
| — ORB levels (ORH/ORL/range) | ✅ (chart) | — | ✅ (Momentum) | — |
| — signal context (RSI/EMA dev) | ✅ (readout) | ✅ | — | — |
| Inspect-position → live chart | ✅ | — | — | — |
| Today's signal feed (fired today) | — | — | ▲ (phase pills) | ✅ |

---

## 4. In-dashboard charting (APEX-unique, becomes a template page)

The `render_symbol()` builder — none of the other three have any of this (they delegate to TV desktop):

| Chart element | APEX |
|---------------|:----:|
| 5-min candlestick (today, Alpaca bars) | ✅ |
| VWAP overlay | ✅ |
| Live price h-line (TV CDP quote) | ✅ |
| ORB high/low h-lines | ✅ |
| Entry h-line | ✅ |
| Stop h-line | ✅ |
| Layer-3 health timeline (replayed `compute_health`) | ✅ |
| Health exit/carry threshold lines | ✅ |
| Live readout metrics (Live/Bar/VWAP/Health/Gain) | ✅ |
| Thesis-intact / decay reasons caption | ✅ |
| Hypothetical-entry detection on any leader | ✅ (`detect_entry`) |

---

## 5. Analytics charts & tables (the union the template must absorb)

| Chart / table | APEX | Tri-City | Compounder | Dark City |
|---------------|:----:|:--------:|:----------:|:---------:|
| Cumulative P&L (equity curve) | — | ✅ line | ✅ per-bot lines | ✅ filled area |
| Daily P&L bars | — | ✅ | — | ✅ |
| Outcome donut (win/partial/loss/scratch) | — | ✅ | — | ✅ |
| KPI row: Total P&L / WinRate / AvgR / PF / Trades | ▲ (Closed) | ✅ | ▲ | ✅ |
| KPI: Sharpe (R-based) | — | ✅ | — | ✅ |
| KPI: Calmar | — | ✅ | — | ✅ |
| KPI: Avg gain % | ✅ (Closed) | — | — | — |
| Win-rate by setup (bar) | — | ✅ | — | ✅ |
| Avg-R by setup (bar) | — | ✅ | — | ✅ |
| Setup summary table | — | ✅ | — | ✅ |
| RVOL histogram (win vs loss) | — | ✅ | — | ✅ |
| RSI histogram (win vs loss) | — | ✅ | — | ✅ |
| EMA-Dev% histogram (win vs loss) | — | ✅ | — | ✅ |
| Cup-pattern win-rate bar | — | ✅ | — | ✅ |
| BB-squeeze win-rate bar | — | ✅ | — | ✅ |
| P&L by entry hour (CT) bar | — | ✅ | — | ✅ |
| R-multiple distribution histogram | — | ✅ | — | ✅ |
| Best/Worst/Avg-win/Avg-loss R metrics | — | ✅ | — | ✅ |
| Position-notional vs R scatter | — | ✅ | — | ✅ |
| Streak analysis (max consec W/L) | — | ✅ | — | ✅ |
| Rolling 20-day drawdown | — | ✅ | — | ✅ |
| Risk-$ vs realized-P&L scatter | — | ✅ | — | ✅ |
| Late-entry evaluation table | ✅ | — | — | — |
| **Edge-score vs realized-R scatter + trendline** | — | — | — | ✅ |
| **Win-rate & Avg-R by edge bucket** | — | — | — | ✅ |
| **Risk-controls table (daily/weekly/DD/max-risk vs limits)** | — | — | — | ✅ |
| **Today's-regime panel (regime/size-mult/day-types/SPY/VIX)** | — | — | — | ✅ |
| RS-score bar (top 30) | — | — | ✅ (Weinstein) | — |
| RS-score distribution histogram | — | — | ✅ (Universe) | — |
| Scanner funnel (universe→filtered→setups) bar | — | — | ✅ (Runner/Universe) | — |
| Scan-history table | — | — | ✅ (×3 bots) | — |
| Phase-status pills (scan/trade/eod) | — | — | ✅ (Momentum) | — |
| Live ticker strip (SPY/QQQ/NVDA/META/AAPL) | — | — | — | ✅ (fixed bottom) |

---

## 6. Candidates / scanner surface

| Element | APEX | Tri-City | Compounder | Dark City |
|---------|:----:|:--------:|:----------:|:---------:|
| Ranked candidate table | ▲ (Leaders) | ✅ | ✅ (per-bot) | ✅ |
| Score-component breakdown columns | — | ✅ | — | ✅ |
| Score-weight reference expander | — | ✅ | — | ✅ |
| Filters: float tier / min-score / parabolic / earnings | — | ✅ | — | ✅ |
| Flags column (E/52H/PARA/HTF/SQZ/TRD) | — | ✅ | — | ✅ |
| Clickable news links | — | ✅ | — | ✅ |
| Research / decision queue (Watch/Take/Skip + notes) | — | ✅ | ✅ (shared `render_research_panel`) | ✅ |
| Decision log expander | — | ✅ | ✅ | ✅ |
| Shared `research-notes.json` across systems | — | ✅ | ✅ (multi-source) | ✅ (`source` tag) |
| RS-leader watchlist with editable flags | ✅ | — | — | — |
| avoid / prioritize / watch checkboxes + Apply | ✅ | — | — | — |

---

## 7. Interaction layer (APEX is the sole reference — everything else is net-new)

### 7a. Telegram (`apex_telegram.py` + `apex_rationale.send_telegram`)

| Capability | APEX | Tri-City | Compounder | Dark City |
|------------|:----:|:--------:|:----------:|:---------:|
| Outbound alerts (sendMessage, HTML) | ✅ | — | — | — |
| Entry alert (full thesis card) | ✅ `telegram_entry_message` | — | — | — |
| Exit alert | ✅ `telegram_exit_message` | — | — | — |
| Overnight-carry proposal | ✅ `telegram_carry_message` | — | — | — |
| Health/decay warning (with give-back from peak) | ✅ `telegram_health_message` | — | — | — |
| Swing-manager daily alert | ✅ `telegram_swing_message` | — | — | — |
| Inline keyboard on alerts | ✅ `override_keyboard` | — | — | — |
| Inbound replies (`getUpdates` poll) | ✅ `poll_replies` | — | — | — |
| Text commands `deny/keep/approve SYM`, `… all` | ✅ | — | — | — |
| `/positions` command (list + buttons) | ✅ `_send_positions` | — | — | — |
| Button-tap callback handling | ✅ `_handle_callback_update` | — | — | — |
| Chat-id allow-listing | ✅ | — | — | — |
| Update-offset persistence | ✅ `apex-telegram-offset.json` | — | — | — |
| Callback ack (clear spinner) | ✅ `_answer_callback` | — | — | — |

### 7b. Manual override (`apex_actions.py`) — same `close_now()` for Telegram **and** dashboard

| Capability | APEX | Tri-City | Compounder | Dark City |
|------------|:----:|:--------:|:----------:|:---------:|
| Close (full) | ✅ | — | — | ▲ emergency code snippet only |
| Trim ½ (partial + journal + re-stop remainder) | ✅ | — | — | — |
| Close + block (avoid re-entry today) | ✅ | — | — | — |
| Flag-gated (`APEX_MANUAL_OVERRIDE`) | ✅ | — | — | — |
| Live kill-switch (`apex-flags.json`) | ✅ | — | — | — |
| Two-tap confirm (`APEX_OVERRIDE_CONFIRM`) | ✅ | — | — | — |
| Idempotent (already-flat → `gone` no-op) | ✅ | — | — | — |
| Re-protect on partial-sell failure (never naked) | ✅ | — | — | — |
| Journals to **its own** book (anti-double-journal) | ✅ | — | — | — |
| TV chart deep-link in alert/keyboard | ✅ `tv_chart_url` | — | — | — |

### 7c. TradingView control (dashboard-side, APEX-only)

| Capability | APEX | Tri-City | Compounder | Dark City |
|------------|:----:|:--------:|:----------:|:---------:|
| Web TV link vs desktop-CDP-drive toggle | ✅ | — | — | — |
| Click row → drive desktop TV to symbol | ✅ `symbol_table` | — | — | — |
| Add symbols to TV watchlist | ✅ `add_to_watchlist` | — | — | — |
| Live quote via CDP (hybrid feed) | ✅ `live_quote` | — | — | — |
| Strict-mode toggle (trade only ⭐ names) | ✅ | — | — | — |
| Carry approve/deny buttons (in-dashboard) | ✅ | — | — | — |

> **Telegram is net-new for Tri-City, Compounder, Dark City.** Manual override + TV control are
> APEX-only. These three blocks (7a/7b/7c) are the single biggest chunk of template work.

---

## 8. Cross-cutting infrastructure

| Feature | APEX | Tri-City | Compounder | Dark City |
|---------|:----:|:--------:|:----------:|:---------:|
| Streamlit wide layout + page config | ✅ | ✅ | ✅ | ✅ |
| Password access gate (`DASH_PASSWORD`) | — | ✅ | ✅ | ✅ |
| Bookmarkable `?k=` token login | — | ✅ | ✅ | ✅ |
| Custom CSS metric styling | ▲ | ✅ | ✅ | ✅ (full terminal theme) |
| Plotly dark theme, `graph_objects` only | ✅ | ✅ | ✅ | ✅ |
| Refresh button (clear cache + rerun) | ✅ | ✅ | ✅ | ✅ |
| `@st.cache_data` TTL loaders | ✅ (10–60s) | ✅ (token) | ✅ | ✅ (60s) |
| Date-range filter (sidebar) | — | ✅ | — | ✅ |
| Setup filter (sidebar multiselect) | — | ✅ | — | ✅ |
| CT-timezone handling | ✅ | ▲ | ▲ | ✅ (pytz) |
| Sidebar account/state summary | ✅ | ✅ | ✅ | ▲ (chips on page) |
| Fixed bottom ticker strip | — | — | — | ✅ |
| Shared generic research panel helper | — | ▲ inline | ✅ `render_research_panel` | ▲ inline |
| Loaded-at timestamp | ✅ | — | — | ✅ |

---

## 9. Config / flags surfaced in the UI

| Flag / config | APEX | Tri-City | Compounder | Dark City |
|---------------|:----:|:--------:|:----------:|:---------:|
| `ALPACA_PAPER` (paper/live badge) | ✅ | ✅ | ✅ | ✅ |
| `APEX_MANUAL_OVERRIDE` | ✅ | — | — | — |
| `APEX_OVERRIDE_CONFIRM` | ✅ | — | — | — |
| `APEX_USE_TV_QUOTES` | ✅ | — | — | — |
| Strict mode (`apex-flags.json`) | ✅ | — | — | — |
| avoid/prioritize/watch flags | ✅ | — | — | — |
| `EXIT_HEALTH` / `CARRY_HEALTH` / `RS_MIN` / `ORB_MINUTES` | ✅ | ▲ ORB | — | — |
| `T1_PCT`/`T2_PCT`/`T3_PCT` | — | ✅ | — | — |
| `MAX_DAILY_LOSS` / `MAX_RISK` | — | ▲ guards | — | ✅ (risk-controls table) |
| Per-bot Alpaca key routing | — | — | ✅ | — |
| `DASH_PASSWORD` | — | ✅ | ✅ | ✅ |

---

## 10. Closed-trade journal schema (drives the template trade record)

Template standardizes on the **APEX exit journal, enriched** (per the brief). What each system writes today:

| Field | APEX | Tri-City / Dark City | Compounder |
|-------|:----:|:--------------------:|:----------:|
| timestamp / date | ✅ | ✅ `date` | ✅ `date` |
| symbol / ticker | ✅ | ✅ | ✅ `ticker` |
| trigger / setup | ✅ `trigger` | ✅ `setup` | ▲ (per-bot) |
| entry / exit price | ✅ | ✅ `entry_price`/`exit_price` | ✅ |
| qty / position_size | ✅ | ✅ | ▲ `notional` |
| pnl | ✅ | ✅ `realized_pnl` | ✅ `pnl` |
| gain_pct | ✅ | ▲ (derive) | ✅ `pnl_pct` |
| peak_gain (MFE) | ✅ | — | — |
| health_at_exit | ✅ | — | — |
| status_at_exit | ✅ | ▲ `status` | — |
| days_held | ✅ | — | ✅ |
| entry_window (late/normal) | ✅ | — | — |
| reason / exit_reason | ✅ | ✅ `exit_reason` | ✅ `reason` |
| entry_time / exit_time | ✅ | ✅ | — |
| order_id | ✅ | — | — |
| partial / remaining_qty | ✅ | — | — |
| **r_multiple** | — | ✅ | — |
| **risk_dollars / risk_per_share** | — | ✅ | — |
| **stop_loss / target_1·2·3** | — | ✅ | ▲ stop/target |
| **duration_min** | — | ✅ | — |
| signal_price (→ slippage) | — | ✅ (optional) | — |
| rsi / ema_dev / rvol / cup / bb_squeeze | — | ✅ (exec log) | — |
| edge_score / edge_win_rate / confirmations | — | ▲ Dark City | — |

**Template enrichment to add (brief §"trade record"):** `strategy`/`sub_strategy`, `trade_id`,
`initial_stop`+`initial_risk`+`r_multiple`, `mae` (pair with `peak_gain` MFE), `slippage`, `fees`.
Capture points: at-entry (stop/risk/signal-price), in-trade (MAE), at-fill (slippage). Note APEX
*lacks* the entire R-analysis block that Tri-City/Dark City already journal — and they lack APEX's
`peak_gain`/`health`/`partial` block. The template needs **both**.

---

## 11. Per-system parity checklist (what must not regress on migration)

- **APEX** — keep: in-dashboard candlestick+health charts, Trade Journal thesis cards, Leaders flag
  editor, TV web/desktop toggle + watchlist add, Strict mode, carry approve/deny, all of §7
  (Telegram + manual override). *Gains from template:* Overview, Signal Analysis, Risk & Sizing,
  Regime & Edge, R-multiple analytics.
- **Tri-City** — keep: full analytics suite (§5), Candidates + research queue, Positions detail
  cards, password gate. *Gains:* §7 Telegram + manual override, in-dashboard charting, Trade
  Journal cards, Regime & Edge.
- **Compounder (hardest)** — keep: 3-account routing, per-strategy pages (Weinstein/Runner/
  Momentum), 3-tab positions, per-bot detail cards (trailing/ratchet/ORB), Universe funnel + RS
  charts, 5-tab hard-coded Playbook, shared research panel. *Gains:* §7 ×3 books, unified
  cross-strategy analytics, Sharpe/Calmar, Risk & Sizing. **Adapter must model position groups +
  sub_strategy from day one — this is the case the abstraction is designed against.**
- **Dark City** — keep: Session cockpit (chips + dual feeds + fixed ticker strip), Regime & Edge
  page (edge calibration + risk controls), terminal theme, market-clock chip, full analytics. *Gains:*
  §7 Telegram + manual override, in-dashboard charting, Trade Journal cards.

---

## 12. Template superset (what "build once" must contain)

Distilled from every ✅ above — the kit ships all of this; each adapter switches pages/fields on/off:

1. **Pages:** Session/Cockpit · Live Positions · Overview · Trade Log · Trade Journal (Why) ·
   Signal Analysis · Risk & Sizing · Regime & Edge · Chart a Symbol · Leaders/Watchlist ·
   Candidates · Universe & Rankings · {strategy pages} · Playbook.
2. **Charting:** candlestick + VWAP/ORB/entry/stop/live overlays · health/score timeline ·
   equity curve · daily bars · outcome donut · indicator histograms · R-distribution · scatters ·
   drawdown · edge calibration · RS bars/distribution · scanner funnel · ticker strip.
3. **Interaction:** Telegram out (entry/exit/carry/health/swing) + in (commands, `/positions`,
   button callbacks) · manual override (close/trim/close+block, confirm, kill-switch, idempotent,
   never-naked, own-book journaling) · TV control (web link/desktop drive/watchlist/live quote) ·
   research/decision queue.
4. **Infra:** password gate + `?k=` token · cache TTL loaders · date/setup filters · paper/live
   badge · dark Plotly (`graph_objects` only) · refresh · CT tz.
5. **Normalization shim:** map each system's positions + journal + candidates → one view model;
   support **position groups** (Compounder) and **multi-account** routing; enrich journal to the
   template trade record (§10).
6. **Per-system isolation:** own Alpaca account + own Telegram bot; "my positions only" filter for
   any shared account.

---

*Generated 2026-06-24 from a full read of all four dashboards + the APEX interaction layer. Next
step per the brief: finish APEX (equity-validate override, flip flag, merge), then build the kit
against this matrix, proving the abstraction by rebuilding Compounder greenfield.*

---

# Appendix — Per-system complete inventory

Each dashboard documented in render order, every widget enumerated. The matrix above is the
comparison; this is the standalone audit of each file so nothing is lost in migration.

## A. APEX — `apex_dashboard.py` (801 ln)

**Header:** page config (🚀, wide, sidebar expanded). No password gate. Imports the live engine
(`apex_config`, `apex_entry_engine.detect_entry`, `apex_health.compute_health`/`record_carry_decision`,
`apex_tv_quotes`, `apex_tv_control`, `apex_flags`) — the dashboard reuses the poller's own functions.

**Data loaders:** `load_state` (apex-state.json), `load_leaders` (apex-leaders.json),
`leader_prices` (Alpaca daily bars, batched 200, 60s cache), `fetch_bars` (5-min today bars,
delayed-SIP guarded, 60s), `session_context` (open/close/vol + premkt high/gap/last + afterhrs
%→price, 60s), `live_quote` (TV CDP quote, 15s), `alpaca_account` (equity/last_equity/day_pnl/
buying_power/cash, 10s), `alpaca_positions` (qty/avg_entry/current/unreal $+%/mkt_value, 10s).

**Sidebar:** title + "hybrid feed · Layer 3 · PAPER|LIVE"; **View radio** (Live Positions / Chart a
Leader / Trade Journal / Closed Trades / Leaders / Playbook); **Ticker-click radio** (Web APEX layout
/ Desktop TV CDP); **Strict-mode toggle** (writes apex-flags); Equity metric (+day Δ +%), Open-
positions metric, buying-power/cash caption, realized-day-P&L caption, leaders count+date, Refresh
button, loaded-at timestamp.

**Shared builders:** `symbol_table` (web LinkColumn vs desktop click-to-drive), `tv_url`,
`desktop_send` (CDP drive, deduped), `add_to_watchlist`, `render_symbol` (candlestick + VWAP +
live-price line + ORB H/L + entry + stop; health timeline w/ exit/carry lines; 5-metric live
readout + reasons), `trade_card` (thesis/catalyst/entry-stop-risk/support/resistance/session-
context/plan/outcome), `_levels_md`, `_entry_tod`.

**Pages:**
1. **Live Positions** — 4-metric account strip; pending overnight-carry block (Keep/Deny per name);
   union positions table (symbol/status/trigger/health/qty/avg-entry/current/unreal$/unreal%/peak%/
   mkt-value/stop/src); engine-only caption; **Manual actions expander** (Close / Trim½ / Close+block
   per row, gated `APEX_MANUAL_OVERRIDE`); inspect-position selectbox → `render_symbol`.
2. **Chart a Leader** — leader selectbox; `detect_entry` hypothetical (success/none caption);
   `render_symbol`.
3. **Trade Journal** — Today/All scope; reversed last-50 rationale records as `trade_card`s; outcome
   matched by order_id then symbol.
4. **Closed Trades** — Live-paper/Dry-run/All scope; 4 metrics (Trades/Net P&L/Win rate/Avg gain);
   Late-entry evaluation expander (entry_window groupby); closed table via `symbol_table`.
5. **Leaders** — flagged-name reminder; `data_editor` (🚫avoid / ⭐prioritize / ➕watch checkboxes +
   rs_pct/open/current/chg%); Apply-flags+watchlist button (avoid wins over prioritize).
6. **Playbook** — renders `docs/APEX_PLAYBOOK.md`.

## B. Tri-City — `dashboard.py` (1238 ln)

**Header:** page config (📈); **password gate** (`DASH_PASSWORD` + `?k=` token); metric CSS.

**Data:** `load_data` (journal→closed-only R-schema; executions; merged on date+symbol; cached by
refresh token), `load_candidates`, `load_research_notes`/`save_research_note`. Helpers:
`apply_filters` (date + setup), `calc_metrics` (P&L/win-rate/avg-R/PF/Sharpe-R/Calmar), `no_data_msg`,
`_base_layout`.

**Sidebar:** title; Refresh Data; Date Range From/To; Setup multiselect; journal/exec counts;
**Navigation radio** (Positions / Overview / Trade Log / Signal Analysis / Risk & Sizing /
Candidates / Playbook).

**Pages:**
0. **Positions** — Refresh; live Alpaca (account+positions); 4-metric strip; exec-map for
   stop/T1/T2/T3/setup; total-unrealized banner; table (Symbol/Setup/Shares/Entry/Current/P&L$/
   P&L%/Stop/T1/T2/T3); per-position expanders (Levels / Targets w/ env T-pcts / Risk: shares/risk$/
   risk-per-share/RSI/EMA-Dev).
1. **Overview** — 7 KPIs (Total P&L/WinRate/AvgR/PF/Trades/Sharpe-R/Calmar); cumulative-P&L line;
   P&L-by-day bars; outcome donut.
2. **Trade Log** — 4 KPIs; slippage (fill−signal); table (Date/Symbol/Setup/Signal$/Fill$/Slip/Exit/
   P&L/R/Duration/Outcome); detail selectbox → expanders (Entry/Exit, Levels, Risk/P&L, Signal-
   context metrics).
3. **Signal Analysis** — By-setup (win-rate bar / avg-R bar / table); RVOL/RSI/EMA-Dev histograms
   (win vs loss); Cup + BB-squeeze win-rate bars; P&L-by-entry-hour bar.
4. **Risk & Sizing** — R-distribution histogram + best/worst/avg-win/avg-loss; notional-vs-R scatter;
   streak (max consec W/L); rolling-20d drawdown; risk-$ vs P&L scatter.
5. **Candidates** — metadata (date/scanned-at/total); filters (float/min-score/parabolic/earnings);
   flags column; ranked table (score breakdown toggle + news links); Research panel (metrics/news/
   score-components/decision form); Decision-log expander; Score-weight-reference expander.
6. **Playbook** — renders `CHEATSHEET.md`.

## C. Compounder — `dashboard.py` (1315 ln) — the hardest case

**Header:** page config; password gate; metric CSS. **3 Alpaca accounts** via
`alpaca_client(account)` routing (`ALPACA_*` / `RUNNER_ALPACA_*` / `MOMENTUM_ALPACA_*`).
**9 state files** (3 per strategy: portfolio / journal / rankings|setups|candidates).
Shared `render_research_panel` (generic decision widget reused by all sub-strategies).

**Sidebar:** title; Refresh; **Navigation** (Positions / Weinstein / Runner / Momentum / Trade Log /
Universe / Playbook); last-trade + last-scan captions.

**Pages:**
0. **Positions** — 3-column account summary (Weinstein/Runner/Momentum: equity + day-P&L + count);
   **3 tabs**: Weinstein (banner+table), Runner (banner+table + detail cards: Levels/Position/Status
   w/ trailing+ratchet), Momentum (banner+table + ORB detail: ORB-levels/Trade/Risk-Reward 2:1).
1. **Weinstein** — 4 metrics (Holdings/Equity/Last-Rebalance/Trades); holdings table (RS+weight);
   Stage-2 RS bar (top 30); rankings table; Research panel; Exit-history table.
2. **Runner** — 4 metrics; open-position expanders (Trade/Risk w/ ratchet/Setup); today's breakout
   setups table; Research panel; scan-history table.
3. **Momentum** — 5 metrics; **phase-status pills** (Scan/Trade/EOD done-today); gap-candidates
   table; Research panel; open-position ORB expanders; closed-trades metrics+table; scan history.
4. **Trade Log** — combined 3-bot; metrics (Buys/Sells/Realized-P&L/Win-rate); bot multiselect;
   per-bot cumulative-P&L lines; full trade table (Date/Bot/Action/Ticker/Notional/P&L/P&L%/Reason/
   Days).
5. **Universe** — 2 tabs: Weinstein Stage-2 (RS distribution histogram + table); Runner Setups
   History (scanner-funnel grouped bars + table).
6. **Playbook** — **5 hard-coded tabs** (not a markdown file): Bot Overview (3 expanders + cron
   schedule code), Workflows (per-bot step-by-step), Manual Triggers (shortcuts table + gh commands +
   python snippets), State Files (file table + secrets table), Emergency (Options A–D: disable
   workflow / cancel orders / close positions / force-EOD).

## D. Dark City — `dashboard.py` (1435 ln)

**Header:** page config (⚡); password gate; **heavy terminal-theme CSS** (chips, headers, ticker
strip, empty-states); CT-tz helpers (pytz). Extra state file: `dark-city-regime.json`.

**Data:** `load_data` (same R-schema as Tri-City), `load_candidates`, research notes (source-tagged
"darkcity"), `load_regime`; Alpaca via cached `get_alpaca` (account+positions+**clock**);
`load_quotes` (Alpaca snapshot → yfinance fallback) for ticker strip; `render_ticker_strip`.
Helpers: `apply_filters`, `calc_metrics` (Sharpe/Calmar), `no_data`, `_lay`.

**Sidebar:** branded title; Refresh; Date Range; Setup multiselect; journal/exec counts;
**Navigation** (Session / Overview / Trade Log / Signal Analysis / Risk & Sizing / Regime & Edge /
Candidates / Playbook).

**Pages:**
- **Session** (cockpit) — dc-header banner (regime/sizing/timestamp); **6 stat chips** (Detected
  Regime / Portfolio Value / Buying Power / Day P&L / Open Positions / Market OPEN|CLOSED); Refresh;
  **Live Positions feed** (table: Symbol/Setup/Shares/Entry/Current/P&L$/P&L%/Stop/T1/T2/T3 + banner);
  **Today's Signal Feed** (Time/Symbol/Setup/Edge/WR%/Confirms/Entry/Stop/T1/T2/T3/R-R); **fixed
  bottom ticker strip** (SPY/QQQ/NVDA/META/AAPL live).
- **Overview** — 7 KPIs; cumulative-P&L filled area (accent); daily bars; outcome donut.
- **Trade Log** — same structure as Tri-City (4 KPIs / slippage / table / detail expanders).
- **Signal Analysis** — by-setup (extra setups EMA20_PULLBACK, FADE); RVOL/RSI/EMA-Dev histograms;
  Cup + BB-squeeze; P&L-by-hour.
- **Risk & Sizing** — R-distribution + metrics; notional-vs-R scatter; streaks; rolling-20d
  drawdown; risk-vs-P&L scatter.
- **Regime & Edge** (unique) — Today's-regime panel (Regime/Size-Mult/Day-Types/SPY%/VIX + action
  banner); **Edge-score vs realized-R scatter + numpy trendline**; **win-rate & avg-R by edge
  bucket** (bar + secondary-axis line); **Risk-Controls table** (Daily/Weekly P&L, Peak Drawdown,
  Max-Risk vs limits w/ OK/LIMIT-HIT status).
- **Candidates** — same as Tri-City (filters / flags / ranked table / Research / Decision log /
  Score-weight reference).
- **Playbook** — renders `CHEATSHEET.md`.

---

*Appendix complete — every page and widget across the four dashboards is now enumerated. The
inventory (matrix + appendix) is the full template spec; no code has been changed.*
