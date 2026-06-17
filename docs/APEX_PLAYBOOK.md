# APEX — Live-Paper Operating Playbook

The complete workflow for running APEX (System 4) on the Alpaca **paper** account.
APEX = daily RS-leader scan → real-time ORB15 / VWAP_PB entries → Layer 3 health-managed exits.
**Architecture:** Alpaca = daily scan + intraday levels + execution · TradingView quote session
(CDP) = real-time trigger price + the chart · Telegram = alerts · dashboard = your eyes.

> Money is **paper only** (`ALPACA_PAPER=true`). Never real funds in this phase.

---

## TL;DR — the daily rhythm (3 commands)

```bash
apex            # morning: brings up TV+CDP, leaders, poller (LIVE PAPER) + opens Claude
                # …APEX trades itself all day; you watch the dashboard / Telegram…
end session     # type this in Claude at the end → flatten + day summary
```

That's the whole loop. Everything below is detail, monitoring, and troubleshooting.

---

## Pre-flight checklist

**One-time / rarely changes**
- [ ] `.env` has Alpaca **paper** keys and `ALPACA_PAPER=true`  ✓ confirmed
- [ ] Telegram bot token + chat ID in `.env`  ✓ confirmed (alerts fire to your phone)
- [ ] TradingView Desktop is **logged in** with a **real-time** US-equity data feed
      (the hybrid needs real-time quotes; a delayed TV plan would defeat the purpose)
- [ ] **System 1 is OFF** so it doesn't trade the same paper account and muddy results:
      ```bash
      launchctl list | grep level-lock        # expect: no output (not loaded) ✓
      # if it IS loaded, disable it:
      launchctl unload ~/Library/LaunchAgents/com.starks-labs.tricity-level-lock.plist
      ```

**Each morning (handled by `apex`, but good to know)**
- TradingView running with CDP on port 9222
- Today's leader watchlist built (`shared/apex-leaders.json` dated today)
- Poller running (`shared/apex-poller.pid` alive)

---

## Daily workflow

### 1. Start — type `apex`
Best **before 8:30 AM CT** (market open) so it captures the morning, but it works any time —
the poller idles until the open and the opening range. `apex` runs `apex_session_start.sh`:
1. Brings up **TradingView + CDP** (or detects it's already up)
2. Builds **today's leaders** (or detects today's are ready)
3. Starts the **poller in LIVE PAPER mode**
4. Opens **Claude** to monitor + handle "end session"

> Log-only instead? `apex --dry-run` (no orders placed).

Confirm the banner ends with `════ APEX ready to trade ════` and Claude reports one line:
mode / poller PID / CDP up / leader count.

### 2. The trading day (automatic)
You don't touch anything. Each cycle (~60s in the first hour, slower after 10:30 ET) the poller:
1. Pulls delayed bars (levels) + **real-time TV quotes** for positions + breakout-zone leaders
2. **Entries** — ORB15 fires when the live price crosses the ORB high; VWAP_PB on a live VWAP
   reclaim. Enters at the **live price**. Passes the 6 guards → places a paper bracket (entry + stop).
3. **Layer 3 management** — recomputes each position's health on the live price; proactive-exits
   on breakdown (health < 40), warns on decay (< 60), and near the close either carries a healthy
   runner overnight or flattens the weak.
Every entry and exit pings **Telegram** and lands in the dashboard + journal.

### 3. End — type `end session` (in Claude)
Runs `apex_session_end.sh`: stops the poller, **flattens all open positions** (paper-liquidated
via Alpaca), and prints the day's summary. TradingView is left running (close it manually if you
want). No-Claude alternative: `apex-end`.

---

## How to monitor (3 channels)

| Channel | How | Shows |
|---|---|---|
| **Telegram** | your phone | Real-time entry / exit / health-decay / carry alerts with the "why" |
| **Dashboard** | `apex-dash` → http://localhost:8533 | The real **Alpaca paper account** (equity, day P&L, buying power) + everything below |
| **Log** | `tail -f ~/tri-city-inator/logs/apex-poller.log` | Each pass: `live quotes N/45`, `K open, M done, X new, Y managed`, every ENTRY/EXIT |

**Dashboard pages** (every ticker is a clickable link → its TradingView chart):
- **Live Positions** — real Alpaca fills + unrealized P&L merged with Layer 3 health/status/stop;
  account strip up top. Inspect any position: candlestick with entry/stop/VWAP/ORB + the **live
  price line** + a health-score timeline.
- **Chart a Leader** — pick any leader → its chart + what APEX *would* do (hypothetical entry + health).
- **Entries — Why** — the Layer 6 rationale card for each fill ("why this stock, why now").
- **Closed Trades** — journal with a **Live paper / Dry-run / All** filter (keeps the live record clean).
- **Leaders** — the universe APEX trades from, with **open / current / chg %** per name to spot movers.
- **Playbook** — this document, in-app.

---

## Risk & guards (live values)

| Parameter | Value | Meaning |
|---|---|---|
| Account equity | $5,000 | Paper; sizing base (compounds, no leverage) |
| Risk per trade | 1% (~$50), hard cap $150 | Position size = risk ÷ stop distance |
| Max concurrent positions | 5 | Guard #3 |
| Daily loss circuit breaker | −$250 | Guard #4 — no new entries past this |
| Entry composite threshold | 65 | RS + trigger + volume + VWAP confluence gate |
| ATR stop | 2× ATR, **capped at 10%** | Protective stop on every entry |
| Proactive exit | health < 40 | Layer 3 cuts the fade before the hard stop |
| Carry overnight | health ≥ 70, green, > VWAP | Graduates intraday → swing → multi-week (5d) |

Override any of these in `.env` (e.g. `APEX_MAX_POSITIONS`, `APEX_DAILY_LOSS`, `APEX_RISK_PCT`,
`APEX_EXIT_HEALTH`). Restart the poller to apply.

---

## The hybrid feed & fallback (what to expect)

- **Real-time** comes from your TV quote session over CDP — the *trigger price* and *position
  health* are live. Coverage shows as `live quotes N/45` in the log (N warms up over cycles).
- If **TV/CDP goes down** (you close TradingView, it crashes), APEX **auto-falls back to delayed
  Alpaca bars** per symbol — it keeps trading, just 15 min behind, no crash. The dashboard shows
  "TV/CDP down" on the live read-out. Bring TV back with `apex` (idempotent) to restore real-time.
- Kill the hybrid entirely with `APEX_USE_TV_QUOTES=false` (pure delayed Alpaca).

---

## Daily review (during the validation period)

After `end session`, review:
1. **The printed summary** — entries, exits, net P&L, win rate.
2. **Dashboard → Closed Trades** — per-trade P&L, exit reason, peak gain vs realized.
3. Ask the key questions:
   - Did **Layer 3** add value? (proactive exits beating the −5%/−10% stop; runners held through chop)
   - Did **real-time entries** fill meaningfully better than the delayed bar price?
   - Did any **guard** misfire (blocked a good trade / let a bad one through)?
   - **peak_gain vs realized** — are we giving back too much? (→ tune toward a peak-aware trail)

Logs to mine: `logs/apex-journal.json` (closed trades), `logs/apex-executions.json` (entries),
`logs/apex-rationale.json` (why), `logs/apex-blocked-signals.json` if present.

---

## Validation goals — when is it "working"?

Run live-paper for a **meaningful sample** (aim ~2–4 weeks / 30–50+ trades across different
market days) before judging. Watch for:
- **Positive expectancy** (avg R > 0) that holds across up/down/chop days
- **Win rate × avg win** vs **loss rate × avg loss** favorable
- Layer 3 demonstrably better than a dumb fixed stop
- No recurring guard/plumbing failures

Only after that — and as a separate decision — consider real money with **reduced** risk caps.
Until then: **paper only.**

---

## Troubleshooting

| Symptom | Check / Fix |
|---|---|
| `apex` says "CDP not confirmed" | TV didn't start with the debug port. Re-run `apex`; or check `curl -s localhost:9222/json/list`. APEX still runs on delayed fallback meanwhile. |
| `live quotes 0/45` in log | TV closed or not logged in / not real-time. Reopen via `apex`. Falls back to delayed automatically. |
| No entries firing | Normal pre-open and before ~10:00 ET (opening range + data). Check regime/log. Verify leaders built (`apex-leaders` count). |
| Poller not running | `cat shared/apex-poller.pid; kill -0 <pid>`. Restart: `bash scripts/start_apex_poller.sh` (add `--dry-run` for log-only). |
| Two pollers / stuck | `apex-end` then `apex` — start/stop scripts wait-and-kill duplicates. |
| Want to stop NOW, keep positions | `kill $(cat shared/apex-poller.pid)` (stops trading; does **not** flatten). |
| Flatten everything immediately | `apex-end` (stops + flattens + summary). |

---

## Command reference

```bash
apex                 # START a live-paper session (TV+CDP, leaders, poller) + open Claude
apex --dry-run       # same, but log-only (no orders)
end session          # (in Claude) stop + flatten + summary
apex-end             # same shutdown, without Claude
apex-dash            # open the dashboard (localhost:8533)
apex-quotes WDC NUAI # spot-check the real-time TV feed
apex-leaders         # rebuild today's leader watchlist manually
apex-health --self-test [YYYY-MM-DD]   # replay a session's health trajectory
apex-build           # resume DEV/design work on APEX (not a trading session)
```

Full design + rationale: `docs/STRATEGY_V2_DESIGN.md`. Quick architecture: `CHEATSHEET.md`,
CLAUDE.md "APEX SESSION".
