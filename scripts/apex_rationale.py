#!/usr/bin/env python3
"""
APEX Layer 6 — Trade Rationale capture.

On every entry, persist a complete decision snapshot answering "why this stock, why now."
This single record powers (1) the rich Telegram alert, (2) the dashboard "Why" page, and
(3) Layer 4's per-setup/per-regime expectancy analysis. Foundational, not cosmetic.

See docs/STRATEGY_V2_DESIGN.md (Layer 6).
"""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from apex_config import RATIONALE_LOG

ET = ZoneInfo("America/New_York")


def build_rationale(signal, rs_pct, score, regime, entry_price, stop, atr, qty) -> dict:
    """Assemble the full decision snapshot + a plain-language 'why'."""
    why_bits = [
        f"RS leader (universe pct {rs_pct:.0f})",
        f"{signal.trigger} trigger @ {signal.bar_time}",
        f"RVOL {signal.rvol:.1f}x on breakout",
        "above VWAP" if entry_price >= signal.vwap else "below VWAP",
        f"regime: {regime}",
    ]
    return {
        "timestamp": datetime.now(ET).isoformat(),
        "symbol": signal.symbol,
        "trigger": signal.trigger,
        "composite_score": score,
        "rs_pct": round(rs_pct, 1),
        "regime": regime,
        "entry": round(entry_price, 4),
        "stop": round(stop, 4),
        "atr": round(atr, 4),
        "qty": qty,
        "risk_dollars": round(abs(entry_price - stop) * qty, 2),
        "orb_high": round(signal.orb_high, 4),
        "orb_low": round(signal.orb_low, 4),
        "vwap_at_entry": round(signal.vwap, 4),
        "rvol": signal.rvol,
        "bar_time": signal.bar_time,
        "context": signal.context,
        "why": " | ".join(why_bits),
        # health-at-entry baseline for Layer 3 (filled when health monitor exists)
        "health_at_entry": 100,
    }


def send_telegram(msg: str) -> None:
    """Shared HTML Telegram sender (entry, exit, health, carry alerts). No-op if unconfigured."""
    from apex_config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import urllib.parse, urllib.request as _ur
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
        ).encode()
        _ur.urlopen(_ur.Request(url, data=data), timeout=5)
    except Exception:
        pass


def log_rationale(rationale: dict) -> None:
    RATIONALE_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(RATIONALE_LOG.read_text()) if RATIONALE_LOG.exists() else []
    except Exception:
        existing = []
    existing.append(rationale)
    RATIONALE_LOG.write_text(json.dumps(existing, indent=2))


def _fmt_levels(levels: list, n: int = 3) -> str:
    levels = [l for l in (levels or []) if l.get("price") is not None][:n]
    return " · ".join(f"{l['label']} ${l['price']:g}" for l in levels) or "—"


def _fmt_float(f) -> str:
    if not f:
        return "—"
    try:
        f = float(f)
        return f"{f/1e9:.1f}B" if f >= 1e9 else f"{f/1e6:.0f}M"
    except Exception:
        return "—"


def telegram_entry_message(rationale: dict) -> str:
    """Full Trade-Journal-style entry alert (autonomous — Decision = what APEX did + why)."""
    from apex_thesis import get_vix, get_float
    r = rationale
    th = r.get("thesis") or {}
    entry, stop = float(r["entry"]), float(r["stop"])
    vwap = float(r.get("vwap_at_entry") or entry)
    open_px = (r.get("context") or {}).get("open") or th.get("context", {}).get("open")
    stop_pct = (entry - stop) / entry * 100 if entry else 0
    dist_vwap = (entry - vwap) / vwap * 100 if vwap else 0
    day_pct = (entry - open_px) / open_px * 100 if open_px else None
    flag = r.get("flag")        # ⭐ prioritized / etc., set by the engine when relevant

    res = [l for l in (th.get("resistance") or []) if l.get("price")]
    target = res[0]["price"] if res else None
    rr = ((target - entry) / (entry - stop)) if (target and entry > stop) else None

    vix = get_vix()
    flt = get_float(r["symbol"])
    cat = th.get("catalyst") or []
    lines = [
        f"🚀 <b>APEX ENTRY — {r['symbol']}</b>" + (f"  {flag}" if flag else ""),
        "",
        f"<b>Ticker:</b> {r['symbol']}",
        f"<b>Setup:</b> {r['trigger']}" + ("  (real-time)" if (r.get('context') or {}).get('feed') == 'tv_realtime' else ""),
        f"<b>Entry:</b> ${entry:g}",
        f"<b>Confidence:</b> {r.get('composite_score')}/100 · RS {r.get('rs_pct')} pct",
        "",
        "📊 <b>Market Context</b>",
        f"<b>Regime:</b> {r.get('regime')}" + (f" · <b>VIX:</b> {vix:.1f}" if vix else ""),
        f"<b>VWAP:</b> ${vwap:g} ({'ABOVE' if entry >= vwap else 'BELOW'})",
        "",
        "💡 <b>Levels</b>  <i>(let winners run — Layer 3 exit, no fixed TP)</i>",
        f"<b>Entry:</b> ${entry:g}",
        f"<b>Stop / invalidation:</b> ${stop:g} (−{stop_pct:.1f}% risk)",
        f"<b>Support:</b> {_fmt_levels(th.get('support'))}",
        f"<b>Resistance (targets):</b> {_fmt_levels(th.get('resistance'))}"
        + (f"  ·  R:R to 1st ≈ 1:{rr:.1f}" if rr else ""),
        "",
        "💰 <b>Position</b>",
        f"<b>Shares:</b> {r.get('qty')} · <b>Value:</b> ${r.get('qty', 0) * entry:,.0f} · "
        f"<b>Risk:</b> ${r.get('risk_dollars')}",
        "",
        "📈 <b>Stock Context</b>",
        f"<b>RVOL:</b> {r.get('rvol')}x · <b>Dist from VWAP:</b> {dist_vwap:+.1f}%"
        + (f" · <b>Day:</b> {day_pct:+.1f}%" if day_pct is not None else "")
        + (f" · <b>Float:</b> {_fmt_float(flt)}" if flt else ""),
    ]
    if cat:
        c = cat[0]
        lines += ["", "📰 <b>Catalyst</b>",
                  (f"<a href=\"{c['url']}\">{c['headline'][:90]}</a>" if c.get("url")
                   else c["headline"][:90]) + f"  ({c.get('date','')})"]
    lines += ["", "🤖 <b>Decision</b>",
              "EXECUTED (auto) — Layer 3 managed. Hold while healthy, "
              "carry overnight if strong."]
    return "\n".join(lines)


def telegram_exit_message(rec: dict) -> str:
    """Layer 3 / swing exit alert — Decision conveys why it closed."""
    pnl = rec.get("pnl", 0)
    emoji = "🟩" if pnl >= 0 else "🟥"
    reason = rec.get("reason", "")
    return (
        f"{emoji} <b>APEX EXIT — {rec.get('symbol')}</b>\n\n"
        f"<b>Exit:</b> ${rec.get('exit')} (entry ${rec.get('entry')})\n"
        f"<b>P&L:</b> ${pnl} ({rec.get('gain_pct', 0):+.1f}%) · "
        f"<b>peak:</b> {rec.get('peak_gain')}%\n"
        f"<b>Held:</b> {rec.get('status_at_exit', 'intraday')}"
        + (f" · {rec.get('days_held')}d" if rec.get('days_held') else "") + "\n\n"
        f"🤖 <b>Decision</b>\nEXITED — {reason}"
    )


def telegram_carry_message(sym: str, health, gain_pct, info: str = "") -> str:
    """Overnight-carry proposal — Decision conveys the swing-hold + the deny window."""
    return (
        f"🌙 <b>APEX CARRY — {sym} → SWING</b>\n\n"
        f"<b>Health:</b> {health} · <b>Gain:</b> {gain_pct:+.1f}%{(' · ' + info) if info else ''}\n\n"
        f"🤖 <b>Decision</b>\n"
        f"HOLD overnight as a swing (default). Managed daily by swing rules.\n"
        f"<i>Deny on the dashboard before 3:00 CT to flatten instead.</i>"
    )


def telegram_health_message(sym: str, health, reasons: list, gain_pct) -> str:
    """Momentum-decay warning — Decision = still holding, watching."""
    return (
        f"⚠️ <b>APEX HEALTH — {sym}</b>\n\n"
        f"<b>Health:</b> {health} ({'; '.join(reasons)}) · {gain_pct:+.1f}%\n\n"
        f"🤖 <b>Decision</b>\nHOLDING — watching; proactive exit if health &lt; 40."
    )


def telegram_swing_message(sym: str, decision: str, detail: str) -> str:
    """Daily swing-manager alert — Decision = HOLD or EXIT with the daily-level reasoning."""
    emoji = "✅" if decision == "HOLD" else "🟥"
    return (
        f"{emoji} <b>APEX SWING — {sym}</b>\n\n"
        f"🤖 <b>Decision</b>\n{decision} (swing) — {detail}"
    )
