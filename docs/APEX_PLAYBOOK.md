# APEX — Live-Paper Operating Playbook

The complete workflow for running APEX (System 4) on the Alpaca **paper** account.
APEX = daily RS-leader scan → real-time ORB15 / VWAP_PB entries → Layer 3 health-managed exits →
operator-steered overnight swings. Autonomous within standing operator flags.

> **Architecture:** Alpaca = daily scan + intraday levels + execution · TradingView quote session
> (CDP) = real-time trigger price + the chart · Telegram = alerts + carry control · dashboard =
> your cockpit. Money is **paper only** (`ALPACA_PAPER=true`).

---

## TL;DR — the daily rhythm

```bash
apex            # morning: TV+CDP, leaders, poller (LIVE PAPER) + Telegram listener + Claude
                # …APEX trades itself; you steer with flags + approve/deny carries…
end session     # type in Claude → stop poller + flatten intraday (swings stay)
```

Dashboard: **https://apex.clawbotinator.trade** (phone + desktop, behind Cloudflare). Carry control
from your phone: reply **`deny SYM`** / **`keep SYM`** to the Telegram bot.

---

## Pre-flight checklist

**One-time / rarely changes**
- [ ] `.env` has Alpaca **paper** keys + `ALPACA_PAPER=true`; Telegram bot token + chat ID
- [ ] TradingView Desktop logged in with a **real-time** US-equity feed (the hybrid needs it)
- [ ] **System 1 OFF** so it doesn't trade the same paper account:
      `launchctl list | grep level-lock` → expect no output
- [ ] Cloudflare: `apex.clawbotinator.trade` is gated by your **Cloudflare Access** policy (this
      dashboard places/closes trades — never leave it open)

**Each morning (handled by `apex`)** — TV+CDP up · today's leaders built · poller running ·
Telegram reply-listener active (inside the poller).

---

## Daily workflow

### 1. Start — `apex`
Best before the 8:30 CT open. Runs `apex_session_start.sh`: brings up TV+CDP, builds today's
leaders, starts the poller in **LIVE PAPER** mode (`apex --dry-run` for log-only), opens Claude to
monitor. The poller also runs the Telegram listener and clears any stale quote subscriptions.

### 2. The trading day (autonomous, you steer)
Each cycle (fast first hour + fast again in the EOD window, slow midday) the poller:
1. Pulls delayed bars (levels) + **real-time TV quotes** for positions + prioritized + breakout candidates
2. **Entries** — ORB15 fires when the live price crosses the ORB high; VWAP_PB on a live VWAP reclaim.
   Enters at the live price, runs the guards + your flags, places a paper bracket (entry + stop).
3. **Layer 3 management** — live-price health; proactive exit < 40; decay warnings; at EOD proposes
   healthy runners as overnight carries.
4. **Telegram** — rich alert on every entry/exit/health/carry; **reads your `deny/keep` replies**.

### 3. End — `end session` (in Claude)
Runs `apex_session_end.sh`: stops the poller, **flattens intraday positions only** — swing/
multi-week holdings stay open (durable). Prints the day summary. `apex-flatten` closes *everything*.

---

## Operator controls (you stay autonomous, but in charge)

### Trade flags — Leaders page (✓ Apply) + sidebar
- **🚫 avoid** — never trade this name (blocks new entries; doesn't flatten a held one)
- **⭐ prioritize** — relaxed entry threshold + always kept real-time; alerts tagged ⭐
- **🎯 strict mode** (sidebar toggle) — trade **only** prioritized names (curated test universe)
- Avoid wins over prioritize. Flags persist; a flagged name shows pre-checked when it reappears.

### Overnight carry — confirm/deny (default = CARRY)
At the 2:45 EOD pass APEX **proposes** each healthy runner (health ≥ 70, green, > VWAP) as an
overnight swing — it **carries by default** unless you deny it before the 3:00 CT close:
- **Dashboard** → Live Positions → **✅ Keep** / **🚫 Deny**
- **Phone** → reply to the Telegram carry alert: **`deny SYM`**, **`keep SYM`**, **`deny all`**, **`keep all`**

Carried positions become swings, managed daily by the swing manager (not the intraday engine).
Set `APEX_ALLOW_OVERNIGHT_CARRY=false` to flatten everything at EOD (intraday-only validation).

---

## How to monitor (3 channels)

| Channel | How | Shows |
|---|---|---|
| **Telegram** | your phone | Rich entry/exit/health/carry/swing alerts (setup · catalyst · levels · R:R · VIX · float · Decision); **reply `deny/keep SYM` to steer carries** |
| **Dashboard** | **https://apex.clawbotinator.trade** (or `apex-dash` locally) | The real Alpaca paper account + everything below |
| **Log** | `tail -f ~/tri-city-inator/logs/apex-poller.log` | Each pass: `live quotes N/25`, open/done/new/managed, every ENTRY/EXIT, telegram replies |

**Dashboard pages** (every ticker → its TradingView chart; toggle Web-link vs Desktop-drive in the sidebar):
- **Live Positions** — real Alpaca fills + unrealized P&L + Layer 3 health/status/stop; account
  strip; **carry ✅Keep/🚫Deny** when pending; inspect any position (candles + entry/stop/VWAP/ORB +
  **live price line** + health timeline).
- **Chart a Leader** — any leader + the hypothetical entry/health APEX would assign.
- **Trade Journal** — full thesis card per trade (setup · catalyst · entry/stop · support/
  resistance · plan) + a **📊 Session** block (day open/close/vol · pre-market high/gap/last ·
  after-hours move→price) + outcome once closed; ⏰ badge on late entries.
- **Closed Trades** — journal with Live/Dry/All filter + a **late-vs-normal** performance eval.
- **Leaders** — the universe with **open/current/chg %** + the 🚫/⭐/➕ flag checkboxes.
- **Playbook** — this document, in-app.

---

## Risk & guards (live values)

| Parameter | Value |
|---|---|
| Account equity (sizing base) | $5,000 |
| Risk per trade | 1% (~$50), hard cap $150 |
| Max concurrent positions | 5 |
| Daily loss circuit breaker | −$250 |
| Entry composite threshold | 65 (−5 for ⭐ prioritized) |
| ATR stop | 2× ATR, capped 10% |
| Proactive exit / carry | health < 40 / health ≥ 70 |
| Swing exit (daily) | close < invalidation or < EMA10 trend |

Override any in `.env` (`APEX_*`). Restart the poller to apply.

---

## Hybrid feed & fallback
Real-time **trigger price + position health** come from your TV quote session over CDP (warm set
capped at `MAX_LIVE_QUOTES`=25 so it never lags the app). If TV/CDP goes down, APEX **falls back to
delayed Alpaca bars** per symbol — keeps trading, ~15 min behind. `apex-quotes release` clears the
feed if the desktop app ever feels heavy; `APEX_USE_TV_QUOTES=false` disables the hybrid entirely.

---

## Swing tier (overnight holds)
A carried position is owned by **`apex_swing.py`**, which runs **daily at 3:10 PM CT via launchd**
(independent of whether you ran `apex`). It exits only on swing rules — daily close below the
documented **invalidation** (the thesis support) or below the **EMA10 trend**. Overnight is covered
by the resting stop. Run it manually with `apex-swing`.

---

## Late-entry tracking
Entries in the last hour (after `LATE_ENTRY_ET` 15:00 ET) are tagged **late** — ⏰ on the Trade
Journal, and a late-vs-normal eval on Closed Trades. Not blocked yet; gathering data on whether
late entries fade intraday or become overnight runners, to design a smart gate.

---

## Daily review
After `end session`: read the printed summary, then **Dashboard → Closed Trades** (P&L, exit
reasons, peak vs realized, **late-entry eval**) and **Trade Journal** (the thesis behind each).
Ask: did Layer 3 add value? did real-time entries fill better? any guard/flag misfire? peak give-back?

Logs: `logs/apex-journal.json` (closed) · `apex-executions.json` (entries) · `apex-rationale.json`
(thesis) · `apex-swing.log` (swing decisions).

## Validation goals
Run ~2–4 weeks / 30–50+ trades across market types. Look for positive expectancy that holds, Layer
3 beating a fixed stop, no recurring failures. Only then — separate decision — small live with
reduced caps. Until then: **paper only.**

---

## Troubleshooting

| Symptom | Check / Fix |
|---|---|
| Dashboard URL down | tunnel: `pgrep -f 'run starks-dashboards'`; dashboard: `pgrep -f apex_dashboard`. Local fallback: `apex-dash` → localhost:8533 |
| Telegram replies ignored | the listener runs inside the poller — only active while the poller's up (i.e. during a session) |
| `live quotes 0/25` | TV closed / not real-time. Reopen via `apex`; falls back to delayed automatically |
| Desktop TV laggy | `apex-quotes release` (clears the quote feed) |
| No entries firing | normal pre-open / before ~10:00 ET; check strict mode isn't on with no ⭐ names; verify leaders built |
| Stop trading now, keep positions | `kill $(cat shared/apex-poller.pid)` |
| Flatten everything now | `apex-flatten` |

---

## Command reference

```bash
apex                 # START a live-paper session (TV+CDP, leaders, poller, listener) + Claude
apex --dry-run       # same, log-only (no orders)
end session          # (in Claude) stop poller + flatten intraday (swings stay)
apex-end             # same shutdown, without Claude
apex-flatten         # close EVERYTHING (intraday + swings)
apex-dash            # dashboard locally (also at https://apex.clawbotinator.trade)
apex-swing           # run the daily swing manager now
apex-quotes [SYMS]   # spot-check the real-time feed   ·   apex-quotes release  (clear the feed)
apex-leaders         # rebuild today's leader watchlist
apex-health --self-test [YYYY-MM-DD]   # replay a session's health trajectory
apex-build           # resume APEX DEV/design work (not a trading session)
```
Telegram (phone): `deny SYM` · `keep SYM` · `deny all` · `keep all` — steer overnight carries.

Full design: `docs/STRATEGY_V2_DESIGN.md`. Quick architecture: `CHEATSHEET.md`, CLAUDE.md "APEX SESSION".
