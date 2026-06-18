#!/usr/bin/env python3
"""
APEX Dashboard — visual management for the headless (Alpaca-native, no-chart) APEX engine.

Because APEX has no TradingView chart to babysit, this dashboard IS the chart: per-position
intraday candlesticks with entry / stop / VWAP / ORB overlays, a live Layer 3 health-score
timeline (replayed from the same compute_health() the poller uses), the Layer 6 "why" behind
every entry, the closed-trade journal, and today's RS leader watchlist.

Run:  python3 -m streamlit run ~/tri-city-inator/scripts/apex_dashboard.py
      (terminal shortcut: apex-dash)

Reads:
  shared/apex-state.json      live open positions (entry, stop, health, status, days_held)
  shared/apex-leaders.json    today's RS-ranked leader watchlist
  logs/apex-rationale.json    Layer 6 entry rationale ("why this stock, why now")
  logs/apex-journal.json      closed trades (Layer 3 exits: P&L, reason, peak gain)
  Alpaca market data          today's 5-min bars for the live candlestick + health replay
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

WORKSPACE = Path.home() / "tri-city-inator"
sys.path.insert(0, str(WORKSPACE / "scripts"))

import apex_config as cfg                       # noqa: E402  (loads .env centrally)
from apex_entry_engine import detect_entry      # noqa: E402
from apex_health import compute_health, record_carry_decision  # noqa: E402
import apex_tv_quotes                            # noqa: E402  (real-time TV quote, hybrid feed)
import apex_tv_control                           # noqa: E402  (drive desktop chart + watchlist add)
import apex_flags                                # noqa: E402  (operator avoid/prioritize/strict flags)

ET = ZoneInfo("America/New_York")
SESS_OPEN, SESS_CLOSE = dtime(9, 30), dtime(16, 0)

st.set_page_config(page_title="APEX", page_icon="🚀", layout="wide",
                   initial_sidebar_state="expanded")


# ── data loaders ─────────────────────────────────────────────────────────────
def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return default
    return default


def load_state() -> dict:
    return _load_json(cfg.STATE_FILE, {"date": "", "daily_pnl": 0.0,
                                       "positions": {}, "executed_today": []})


def load_leaders() -> dict:
    return _load_json(cfg.LEADERS_FILE, {"leaders": []})


@st.cache_data(ttl=60, show_spinner=False)
def leader_prices(symbols: tuple) -> dict:
    """Today's open + latest price per symbol from the daily bar (one batched Alpaca call,
    cached 60s). {sym: (open, current)}. Current is ~15-min delayed on a basic plan."""
    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec or not symbols:
        return {}
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame
        dc = StockHistoricalDataClient(key, sec)
        out = {}
        syms = list(symbols)
        for i in range(0, len(syms), 200):
            batch = syms[i:i + 200]
            df = dc.get_stock_bars(StockBarsRequest(
                symbol_or_symbols=batch, timeframe=TimeFrame.Day,
                start=datetime.utcnow() - timedelta(days=5), feed=cfg.DATA_FEED)).df
            if df.empty:
                continue
            for s in batch:
                try:
                    last = df.xs(s, level="symbol").iloc[-1]
                    out[s] = (float(last["open"]), float(last["close"]))
                except Exception:
                    pass
        return out
    except Exception:
        return {}


@st.cache_data(ttl=60, show_spinner=False)
def fetch_bars(symbol: str, day_iso: str) -> pd.DataFrame:
    """Today's regular-session 5-min bars for one symbol (cached 60s). Empty df if none."""
    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec:
        return pd.DataFrame()
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        day = datetime.fromisoformat(day_iso).date()
        dc = StockHistoricalDataClient(key, sec)
        # Delayed-SIP guard: cap end at now − SIP_DELAY_MIN (basic plan rejects recent SIP)
        end = min(datetime.combine(day + timedelta(days=1), dtime(0, 0)),
                  datetime.utcnow() - timedelta(minutes=cfg.SIP_DELAY_MIN))
        df = dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=datetime.combine(day, dtime(0, 0)),
            end=end, feed=cfg.DATA_FEED, adjustment="raw")).df
        if df.empty:
            return df
        sub = df.xs(symbol, level="symbol").copy()
        et_idx = sub.index.tz_convert(ET)
        sub["tod"] = et_idx.time
        sub["et"] = et_idx
        return sub[(sub["tod"] >= SESS_OPEN) & (sub["tod"] < SESS_CLOSE)]
    except Exception as e:
        st.warning(f"bar fetch failed for {symbol}: {e}")
        return pd.DataFrame()


@st.cache_data(ttl=60, show_spinner=False)
def session_context(symbol: str, date_iso: str) -> dict:
    """Day open/close + volume, plus pre-market (high/gap/last) and after-hours (% → price) for a
    trade's date. {} on failure; in-progress days return what exists (close/after-hrs blank)."""
    key, sec = os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY")
    if not key or not sec or not date_iso:
        return {}
    try:
        from datetime import date as _date
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockBarsRequest
        from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
        day = _date.fromisoformat(date_iso[:10])
        dc = StockHistoricalDataClient(key, sec)
        # daily bars → today's open/close/vol + prior close
        dd = dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame.Day,
            start=datetime.combine(day - timedelta(days=8), dtime(0, 0)),
            end=datetime.combine(day + timedelta(days=1), dtime(0, 0)), feed=cfg.DATA_FEED)).df
        if dd.empty:
            return {}
        ds = dd.xs(symbol, level="symbol").sort_index()
        ds = ds.assign(d=[ts.date() for ts in ds.index.tz_convert(ET)])
        rows = ds[ds["d"] <= day]
        if rows.empty:
            return {}
        today = rows.iloc[-1]
        prior_close = float(rows.iloc[-2]["close"]) if len(rows) >= 2 else None
        out = {"open": round(float(today["open"]), 2), "close": round(float(today["close"]), 2),
               "vol": int(today["volume"])}
        if prior_close:
            out["gap_pct"] = round((out["open"] - prior_close) / prior_close * 100, 1)
        # extended-hours 5-min bars → pre-market + after-hours
        end = min(datetime.combine(day + timedelta(days=1), dtime(0, 0)),
                  datetime.utcnow() - timedelta(minutes=cfg.SIP_DELAY_MIN))
        m = dc.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=[symbol], timeframe=TimeFrame(5, TimeFrameUnit.Minute),
            start=datetime.combine(day, dtime(0, 0)), end=end,
            feed=cfg.DATA_FEED, adjustment="raw")).df
        if not m.empty:
            ms = m.xs(symbol, level="symbol").copy()
            ms["tod"] = [t.time() for t in ms.index.tz_convert(ET)]
            pre = ms[(ms["tod"] >= dtime(4, 0)) & (ms["tod"] < dtime(9, 30))]
            post = ms[(ms["tod"] >= dtime(16, 0)) & (ms["tod"] < dtime(20, 0))]
            if not pre.empty and prior_close:
                out["premkt_high_pct"] = round((float(pre["high"].max()) - prior_close) / prior_close * 100, 1)
                out["premkt_last"] = round(float(pre.iloc[-1]["close"]), 2)
            if not post.empty:
                ah = float(post.iloc[-1]["close"])
                out["afterhrs_last"] = round(ah, 2)
                out["afterhrs_pct"] = round((ah - out["close"]) / out["close"] * 100, 1)
        return out
    except Exception:
        return {}


@st.cache_data(ttl=15, show_spinner=False)
def live_quote(symbol: str) -> dict | None:
    """Real-time TV quote for one symbol (cached 15s). None if TV/CDP down or no tick."""
    if not cfg.USE_TV_QUOTES:
        return None
    try:
        return apex_tv_quotes.get_quotes([symbol], wait_ms=1500).get(symbol)
    except Exception:
        return None


def _vwap(g: pd.DataFrame) -> pd.Series:
    tp = (g["high"] + g["low"] + g["close"]) / 3
    return (tp * g["volume"]).cumsum() / g["volume"].cumsum()


def _orb_levels(g: pd.DataFrame, orb_minutes: int):
    end_min = SESS_OPEN.hour * 60 + SESS_OPEN.minute + orb_minutes
    orb = g[g["tod"] < dtime(end_min // 60, end_min % 60)]
    if orb.empty:
        return None, None
    return float(orb["high"].max()), float(orb["low"].min())


def health_timeline(entry: float, bars: pd.DataFrame, start_tod: str | None):
    """Replay compute_health() over accumulating bars from start_tod → (times, healths)."""
    g = bars.sort_values("tod").reset_index(drop=True)
    pos = {"entry": float(entry)}
    start = 0
    if start_tod:
        idx = g.index[g["tod"].astype(str) == str(start_tod)]
        start = int(idx[0]) if len(idx) else 0
    times, healths = [], []
    for i in range(start, len(g)):
        h = compute_health(pos, g.iloc[: i + 1])
        if not h:
            continue
        times.append(g.iloc[i]["et"])
        healths.append(h["health"])
    return times, healths


def now_ct():
    return datetime.now(ZoneInfo("America/Chicago")).strftime("%-I:%M %p CT")


def tv_url(symbol: str) -> str:
    """Web link to this symbol in your last-used TV layout. (TV chart URLs use a short slug, not
    the numeric layout id — the numeric id 404s — so we don't force a layout here; opens your
    default/last layout. Set APEX as your default in TradingView for consistency.)"""
    return f"https://www.tradingview.com/chart/?symbol={symbol}"


def desktop_send(symbol: str) -> None:
    """Drive the running desktop TV to `symbol` (deduped per session so it fires once)."""
    if st.session_state.get("_last_sent") == symbol:
        return
    st.session_state["_last_sent"] = symbol
    if apex_tv_control.set_chart_symbol(symbol):
        st.toast(f"📺 Sent {symbol} to desktop TV")
    else:
        st.toast(f"⚠ Couldn't reach desktop TV (CDP down?)")


def add_to_watchlist(symbols: list[str]) -> None:
    added = [s for s in symbols if apex_tv_control.watchlist_add(s)]
    if added:
        st.success(f"Added to APEX watchlist: {', '.join(added)}")
    else:
        st.warning("Nothing added (TV/CDP down, or already present).")


def is_desktop_mode() -> bool:
    return st.session_state.get("link_mode", "Web (APEX layout)").startswith("Desktop")


def symbol_table(df: pd.DataFrame, symcol: str, key: str, column_config: dict | None = None):
    """Render a table whose ticker honors the link toggle: web = clickable TV link;
    desktop = select a row to drive the running desktop TV chart to that symbol."""
    column_config = dict(column_config or {})
    if is_desktop_mode():
        event = st.dataframe(df, width="stretch", hide_index=True, column_config=column_config,
                             on_select="rerun", selection_mode="single-row", key=key)
        rows = []
        try:
            rows = event.selection.rows
        except Exception:
            try:
                rows = event["selection"]["rows"]
            except Exception:
                rows = []
        if rows:
            sym = str(df.iloc[rows[0]][symcol])
            sel_key = f"{key}_lastsel"
            # Only drive the chart on a NEW selection (an actual click) — never on a passive
            # rerun, so we don't fight your manual TV navigation.
            if st.session_state.get(sel_key) != sym:
                st.session_state[sel_key] = sym
                desktop_send(sym)
        st.caption("🖱️ click a row → sent to your desktop TV")
    else:
        d = df.copy()
        d[symcol] = d[symcol].map(tv_url)
        column_config[symcol] = st.column_config.LinkColumn(symcol, display_text=r"symbol=(.+)$")
        st.dataframe(d, width="stretch", hide_index=True, column_config=column_config)


def _entry_tod(p: dict):
    """Floor a position's ISO entry_time to its 5-min bar 'HH:MM:SS' (for health replay start)."""
    et = p.get("entry_time")
    if not et:
        return None
    try:
        dt = datetime.fromisoformat(et).astimezone(ET)
        dt = dt.replace(minute=(dt.minute // 5) * 5, second=0, microsecond=0)
        return str(dt.time())
    except Exception:
        return None


# ── Alpaca (live paper account: equity, day P&L, real positions) ───────────────
def _trading_client():
    from alpaca.trading.client import TradingClient
    return TradingClient(os.getenv("ALPACA_API_KEY"), os.getenv("ALPACA_SECRET_KEY"),
                         paper=os.getenv("ALPACA_PAPER", "true").lower() == "true")


@st.cache_data(ttl=10, show_spinner=False)
def alpaca_account() -> dict | None:
    try:
        a = _trading_client().get_account()
        eq, last = float(a.equity), float(a.last_equity)
        return {"equity": eq, "last_equity": last, "day_pnl": eq - last,
                "buying_power": float(a.buying_power), "cash": float(a.cash)}
    except Exception:
        return None


@st.cache_data(ttl=10, show_spinner=False)
def alpaca_positions() -> dict:
    """{symbol: {qty, avg_entry, current, unrealized_pl, unrealized_plpc, market_value}}."""
    try:
        out = {}
        for p in _trading_client().get_all_positions():
            out[p.symbol] = {"qty": float(p.qty), "avg_entry": float(p.avg_entry_price),
                             "current": float(p.current_price),
                             "unrealized_pl": float(p.unrealized_pl),
                             "unrealized_plpc": float(p.unrealized_plpc) * 100,
                             "market_value": float(p.market_value)}
        return out
    except Exception:
        return {}


IS_PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"


# ── sidebar ──────────────────────────────────────────────────────────────────
state = load_state()
leaders_doc = load_leaders()
leaders = leaders_doc.get("leaders", [])
positions = state.get("positions", {})
acct = alpaca_account()
apos = alpaca_positions()

with st.sidebar:
    st.title("🚀 APEX")
    st.caption(f"hybrid feed · Layer 3 · {'PAPER' if IS_PAPER else 'LIVE 💰'} account")
    page = st.radio("View", ["Live Positions", "Chart a Leader",
                             "Trade Journal", "Closed Trades", "Leaders", "Playbook"])
    st.radio("Ticker click →", ["Web (APEX layout)", "Desktop TV (CDP)"], key="link_mode",
             help="Web: opens the APEX layout in a browser tab. Desktop: drives your running "
                  "TradingView app to that symbol via CDP.")
    _flags = apex_flags.load_flags()
    _strict = st.toggle("🎯 Strict mode", value=bool(_flags.get("strict")),
                        help="When ON, APEX trades ONLY ⭐ prioritized names (your curated test "
                             "universe). With no prioritized names, it trades nothing.")
    if _strict != bool(_flags.get("strict")):
        apex_flags.set_strict(_strict)
        st.rerun()
    if _strict:
        n = len(_flags.get("prioritize", {}))
        st.caption(f"🎯 STRICT: only {n} prioritized name(s)" + (" — ⚠ none, nothing will trade!" if not n else ""))
    st.divider()
    if acct:
        st.metric("Equity", f"${acct['equity']:,.2f}",
                  f"{acct['day_pnl']:+,.2f} today"
                  + (f" ({acct['day_pnl']/acct['last_equity']*100:+.2f}%)" if acct['last_equity'] else ""))
        st.metric("Open positions", len(apos))
        st.caption(f"buying power ${acct['buying_power']:,.0f} · cash ${acct['cash']:,.0f}")
    else:
        st.metric("Open positions", len(apos) or len(positions))
        st.caption("⚠ Alpaca account unavailable — showing engine state")
    st.caption(f"realized day P&L (engine): ${state.get('daily_pnl', 0.0):,.2f}")
    st.caption(f"leaders: {len(leaders)} · {leaders_doc.get('date', '—')}")
    if st.button("↻ Refresh"):
        st.cache_data.clear()
        st.rerun()
    st.caption(f"loaded {now_ct()}")

day_iso = state.get("date") or leaders_doc.get("date") or datetime.now(ET).date().isoformat()
ORB_MIN = cfg.ORB_MINUTES


# ── shared chart builder ─────────────────────────────────────────────────────
def render_symbol(symbol: str, entry: float | None, stop: float | None,
                  start_tod: str | None, trigger: str | None):
    if is_desktop_mode():
        c1, c2 = st.columns([1, 6])
        c1.markdown(f"### {symbol}")
        if c2.button(f"📺 Open {symbol} on desktop TV", key=f"send_{symbol}"):
            st.session_state["_last_sent"] = None
            desktop_send(symbol)
    else:
        st.markdown(f"### [{symbol} ↗]({tv_url(symbol)})", help="Open in the APEX layout")
    bars = fetch_bars(symbol, day_iso)
    if bars.empty:
        st.info(f"No intraday bars for {symbol} yet (market may be pre-open).")
        return
    g = bars.sort_values("tod").reset_index(drop=True)
    g = g.assign(vwap=_vwap(g).values)
    orb_h, orb_l = _orb_levels(g, ORB_MIN)
    x = g["et"]
    lq = live_quote(symbol)
    lp = lq.get("last") if lq else None

    fig = go.Figure()
    fig.add_trace(go.Candlestick(x=x, open=g["open"], high=g["high"], low=g["low"],
                                 close=g["close"], name=symbol,
                                 increasing_line_color="#26a69a",
                                 decreasing_line_color="#ef5350"))
    fig.add_trace(go.Scatter(x=x, y=g["vwap"], name="VWAP", mode="lines",
                             line=dict(color="#ab47bc", width=1.5, dash="dot")))
    if lp is not None:
        fig.add_hline(y=lp, line=dict(color="#ffca28", width=1.5),
                      annotation_text=f"● LIVE {lp:.2f}", annotation_position="right")
    if orb_h is not None:
        fig.add_hline(y=orb_h, line=dict(color="#90a4ae", width=1, dash="dash"),
                      annotation_text=f"ORB H {orb_h:.2f}", annotation_position="right")
        fig.add_hline(y=orb_l, line=dict(color="#90a4ae", width=1, dash="dash"),
                      annotation_text=f"ORB L {orb_l:.2f}", annotation_position="right")
    if entry is not None:
        fig.add_hline(y=entry, line=dict(color="#26a69a", width=1.5),
                      annotation_text=f"Entry {entry:.2f}", annotation_position="left")
    if stop is not None:
        fig.add_hline(y=stop, line=dict(color="#ef5350", width=1.5, dash="dash"),
                      annotation_text=f"Stop {stop:.2f}", annotation_position="left")
    fig.update_layout(height=430, margin=dict(l=10, r=10, t=30, b=10),
                      xaxis_rangeslider_visible=False, template="plotly_dark",
                      title=f"{symbol} · 5-min" + (f" · {trigger}" if trigger else ""),
                      legend=dict(orientation="h", y=1.02, x=0))
    st.plotly_chart(fig, use_container_width=True)

    # health timeline (only meaningful with an entry reference)
    if entry is not None:
        times, healths = health_timeline(entry, g, start_tod)
        if times:
            hfig = go.Figure()
            hfig.add_trace(go.Scatter(x=times, y=healths, mode="lines",
                                      line=dict(color="#42a5f5", width=2),
                                      fill="tozeroy", fillcolor="rgba(66,165,245,0.12)",
                                      name="health"))
            hfig.add_hline(y=cfg.EXIT_HEALTH, line=dict(color="#ef5350", width=1, dash="dash"),
                           annotation_text=f"exit < {int(cfg.EXIT_HEALTH)}")
            hfig.add_hline(y=cfg.CARRY_HEALTH, line=dict(color="#26a69a", width=1, dash="dot"),
                           annotation_text=f"carry ≥ {int(cfg.CARRY_HEALTH)}")
            hfig.update_layout(height=200, margin=dict(l=10, r=10, t=10, b=10),
                               template="plotly_dark", yaxis=dict(range=[0, 105]),
                               title="Layer 3 health", showlegend=False)
            st.plotly_chart(hfig, use_container_width=True)

    # live read-out — health computed on the live price when available (hybrid)
    last = g.iloc[-1]
    bar_close = float(last["close"])
    h = compute_health({"entry": entry}, g, live_price=lp) if entry is not None else {}
    cols = st.columns(5)
    if lp is not None:
        cols[0].metric("Live", f"${lp:.2f}", f"{lp - bar_close:+.2f} vs bar")
    else:
        cols[0].metric("Live", "—", "TV/CDP down")
    cols[1].metric("Bar (delayed)", f"${bar_close:.2f}")
    cols[2].metric("VWAP", f"${float(last['vwap']):.2f}")
    if h:
        cols[3].metric("Health", h["health"])
        cols[4].metric("Gain", f"{h['gain_pct']:+.1f}%")
    if h and h.get("reasons"):
        st.caption("⚠️ " + " · ".join(h["reasons"]))
    elif h:
        st.caption("✅ thesis intact — above entry, VWAP, EMA9; momentum/structure OK")


def _levels_md(levels: list, fallback_label: str, fallback_price) -> str:
    levels = [l for l in (levels or []) if l.get("price") is not None]
    if not levels and fallback_price is not None:
        levels = [{"label": fallback_label, "price": fallback_price}]
    return "  \n".join(f"{l['label']} **\\${l['price']:g}**" for l in levels) or "—"


def trade_card(r: dict, outcome: dict | None):
    """Render one trade as a full thesis card (entry plan) + outcome (once closed)."""
    sym = r.get("symbol")
    th = r.get("thesis") or {}
    ts = str(r.get("timestamp", ""))[:16].replace("T", " ")
    with st.container(border=True):
        hdr = st.columns([3, 1, 1, 1])
        hdr[0].markdown(f"### [{sym} ↗]({tv_url(sym)}) · {r.get('trigger')} · LONG")
        hdr[1].metric("Conviction", r.get("composite_score"))
        hdr[2].metric("RS pct", r.get("rs_pct"))
        hdr[3].metric("RVOL", f"{r.get('rvol')}x")
        late = " · ⏰ LATE entry (last hour)" if r.get("entry_window") == "late" else ""
        st.caption(f"taken {ts} CT-ET · regime {r.get('regime')}"
                   f"{' · DRY-RUN' if r.get('dry_run') else ' · LIVE'}{late}")

        st.markdown(f"**Thesis** — {th.get('thesis') or r.get('why', '')}")
        cat = th.get("catalyst") or []
        if cat:
            bits = [f"[{c['headline'][:72]}]({c['url']})" if c.get("url") else c["headline"][:72]
                    for c in cat[:2]]
            st.markdown("**Catalyst** — " + " · ".join(bits))

        col = st.columns(3)
        inval = th.get("invalidation", r.get("stop"))
        col[0].markdown(
            f"**Entry** \\${r.get('entry')}  \n"
            f"**Stop / invalidation** \\${inval}  \n"
            f"**Risk** \\${r.get('risk_dollars')} · qty {r.get('qty')}")
        col[1].markdown("**Support**  \n" + _levels_md(th.get("support"), "ORB low", r.get("orb_low")))
        col[2].markdown("**Resistance**  \n" + _levels_md(th.get("resistance"), "ORB high", r.get("orb_high")))

        # Session context — day open/close + volume, pre-market & after-hours moves
        sc = session_context(sym, str(r.get("timestamp", "")))
        if sc:
            v = sc.get("vol")
            vol_s = (f"{v/1e6:.1f}M" if v and v >= 1e6 else f"{v/1e3:.0f}K") if v else "—"
            st.markdown(f"📊 **Session** — open \\${sc.get('open','—')} · "
                        f"close \\${sc.get('close','—')} · vol {vol_s}")
            pre = []
            if sc.get("premkt_high_pct") is not None:
                pre.append(f"high {sc['premkt_high_pct']:+.1f}%")
            if sc.get("gap_pct") is not None:
                pre.append(f"gap {sc['gap_pct']:+.1f}%")
            if sc.get("premkt_last") is not None:
                pre.append(f"(last \\${sc['premkt_last']})")
            detail = []
            if pre:
                detail.append("pre-mkt: " + " · ".join(pre))
            if sc.get("afterhrs_pct") is not None:
                detail.append(f"after-hrs: {sc['afterhrs_pct']:+.1f}% → \\${sc['afterhrs_last']}")
            if detail:
                st.caption("  \n".join(detail))

        st.caption("**Plan** — " + (th.get("planned_exit") or "Layer 3 health-managed (let winners run)."))

        if outcome:
            pnl = outcome.get("pnl", 0)
            emoji = "🟩" if pnl >= 0 else "🟥"
            st.markdown(
                f"{emoji} **Outcome** — exit \\${outcome.get('exit')} · "
                f"P&L \\${pnl} ({outcome.get('gain_pct', 0):+.1f}%) · peak {outcome.get('peak_gain')}% · "
                f"health@exit {outcome.get('health_at_exit')} · _{outcome.get('reason', '')[:60]}_")
        else:
            st.caption("⏳ open")


# ── pages ────────────────────────────────────────────────────────────────────
if page == "Live Positions":
    st.title("Live Positions")
    st.caption(f"Real Alpaca {'paper ' if IS_PAPER else ''}fills + Layer 3 health · "
               f"exit < {int(cfg.EXIT_HEALTH)} · carry ≥ {int(cfg.CARRY_HEALTH)}")

    # account strip
    if acct:
        a = st.columns(4)
        a[0].metric("Equity", f"${acct['equity']:,.2f}")
        a[1].metric("Day P&L", f"${acct['day_pnl']:+,.2f}",
                    f"{acct['day_pnl']/acct['last_equity']*100:+.2f}%" if acct['last_equity'] else None)
        a[2].metric("Buying power", f"${acct['buying_power']:,.0f}")
        a[3].metric("Open positions", len(apos))
        st.divider()

    # Pending overnight-carry proposals — carries by default; approve to lock, deny to flatten.
    pending = [s for s, p in positions.items() if p.get("pending_deny")]
    if pending:
        st.warning(f"🌙 **Overnight carry proposed** — these carry overnight by default. "
                   f"**Deny before 3:00 CT to flatten instead.**")
        for s in pending:
            p = positions[s]
            cc = st.columns([3, 1, 1])
            cc[0].markdown(f"**{s}** · health {p.get('health')} · {p.get('gain_pct', 0):+.1f}% "
                           f"→ carry as swing?")
            if cc[1].button("✅ Keep", key=f"appr_{s}"):
                record_carry_decision(s, "approve")
                st.toast(f"{s}: carry approved")
                st.rerun()
            if cc[2].button("🚫 Deny", key=f"deny_{s}"):
                record_carry_decision(s, "deny")
                st.toast(f"{s}: will flatten")
                st.rerun()
        st.divider()

    # union of what Alpaca actually holds and what the engine tracks
    syms = list(dict.fromkeys(list(apos.keys()) + list(positions.keys())))
    if not syms:
        st.info("No open positions yet. The poller places entries when a leader triggers "
                "ORB15/VWAP_PB and clears the guards. Use **Chart a Leader** to watch the board.")
    else:
        rows = []
        for sym in syms:
            ap = apos.get(sym, {})
            p = positions.get(sym, {})
            rows.append({
                "symbol": sym, "status": p.get("status", "intraday"),
                "trigger": p.get("trigger"), "health": p.get("health"),
                "qty": ap.get("qty", p.get("qty")),
                "avg entry": ap.get("avg_entry", p.get("entry")),
                "current": ap.get("current", p.get("last_price")),
                "unreal $": ap.get("unrealized_pl"),
                "unreal %": ap.get("unrealized_plpc", p.get("gain_pct")),
                "peak %": p.get("peak_gain"),
                "mkt value": ap.get("market_value"),
                "stop": p.get("stop"),
                "src": "alpaca" if sym in apos else "engine-only",
            })
        symbol_table(pd.DataFrame(rows), "symbol", key="pos_table")
        if any(r["src"] == "engine-only" for r in rows):
            st.caption("⚠ 'engine-only' = tracked by APEX but not yet confirmed in the Alpaca "
                       "account (order pending/unfilled).")
        st.divider()
        sym = st.selectbox("Inspect position", syms)
        p = positions.get(sym, {})
        entry = apos.get(sym, {}).get("avg_entry", p.get("entry"))
        render_symbol(sym, entry, p.get("stop"), _entry_tod(p), p.get("trigger"))

elif page == "Chart a Leader":
    st.title("Chart a Leader")
    st.caption("Pick any leader to see its intraday action + what APEX would do "
               "(hypothetical ORB15/VWAP_PB entry + health).")
    if not leaders:
        st.info("No leaders file yet — run `apex-leaders`.")
    else:
        syms = [l["symbol"] for l in leaders]
        sym = st.selectbox("Leader", syms)
        bars = fetch_bars(sym, day_iso)
        entry = stop = start_tod = trig = None
        if not bars.empty:
            sig = detect_entry(sym, bars, orb_minutes=ORB_MIN)
            if sig is not None:
                entry, start_tod, trig = float(sig.price), str(sig.bar_time), sig.trigger
                st.success(f"Hypothetical {trig} entry @ ${entry:.2f} ({start_tod})")
            else:
                st.caption("No ORB15/VWAP_PB trigger detected yet today.")
        render_symbol(sym, entry, stop, start_tod, trig)

elif page == "Trade Journal":
    st.title("Trade Journal")
    st.caption("The full thesis for every trade — setup · catalyst · entry/stop · support/"
               "resistance · plan — plus the outcome once it closes.")
    rats = _load_json(cfg.RATIONALE_LOG, [])
    journal = _load_json(cfg.APEX_JOURNAL, [])
    out_by_oid = {j["order_id"]: j for j in journal
                  if j.get("order_id") and j.get("order_id") != "DRY-RUN"}
    out_by_sym = {}
    for j in journal:
        out_by_sym[j.get("symbol")] = j   # latest close per symbol (chronological log)
    scope = st.radio("Show", ["Today", "All"], horizontal=True, key="tj_scope")
    rows = rats if scope == "All" else [r for r in rats
                                        if str(r.get("timestamp", "")).startswith(day_iso)]
    if not rows:
        st.info("No trades logged yet.")
    for r in reversed(rows[-50:]):
        oid = r.get("order_id")
        outcome = out_by_oid.get(oid) if oid and oid != "DRY-RUN" else out_by_sym.get(r.get("symbol"))
        trade_card(r, outcome)

elif page == "Closed Trades":
    st.title("Closed Trades")
    journal = _load_json(cfg.APEX_JOURNAL, [])
    if not journal:
        st.info("No closed APEX trades yet. Layer 3 logs exits here "
                "(logs/apex-journal.json).")
    else:
        jdf = pd.DataFrame(journal)
        if "dry_run" not in jdf.columns:
            jdf["dry_run"] = False
        scope = st.radio("Show", ["Live paper", "Dry-run", "All"], horizontal=True)
        if scope == "Live paper":
            jdf = jdf[~jdf["dry_run"].fillna(False)]
        elif scope == "Dry-run":
            jdf = jdf[jdf["dry_run"].fillna(False)]
        if jdf.empty:
            st.info(f"No {scope.lower()} trades yet.")
            st.stop()
        jdf = jdf.assign(mode=jdf["dry_run"].map(lambda d: "DRY" if d else "LIVE"))
        wins = (jdf["pnl"] > 0).sum()
        k = st.columns(4)
        k[0].metric("Trades", len(jdf))
        k[1].metric("Net P&L", f"${jdf['pnl'].sum():,.2f}")
        k[2].metric("Win rate", f"{wins / len(jdf) * 100:.0f}%")
        k[3].metric("Avg gain", f"{jdf['gain_pct'].mean():+.1f}%")

        # Late-entry evaluation (last-hour entries vs the rest) — informs the future gate
        if "entry_window" in jdf.columns:
            ew = jdf.groupby(jdf["entry_window"].fillna("normal")).agg(
                trades=("pnl", "size"), net=("pnl", "sum"),
                win_rate=("pnl", lambda s: (s > 0).mean() * 100), avg_gain=("gain_pct", "mean"))
            with st.expander("⏰ Late-entry evaluation (last-hour entries vs normal)", expanded=False):
                st.dataframe(ew.style.format({"net": "${:,.2f}", "win_rate": "{:.0f}%",
                                              "avg_gain": "{:+.1f}%"}), width="stretch")
                st.caption("Tracking whether last-hour entries fade intraday or become overnight "
                           "runners — to design a smart gate rather than a blunt cutoff.")
        show = ["timestamp", "symbol", "trigger", "mode", "entry_window", "status_at_exit",
                "entry", "exit", "gain_pct", "peak_gain", "pnl", "health_at_exit", "reason"]
        show = [c for c in show if c in jdf.columns]
        tdf = jdf[show].iloc[::-1].copy()
        symbol_table(tdf, "symbol", key="closed_table")

elif page == "Leaders":
    st.title(f"Today's Leaders — {leaders_doc.get('date', '—')}")
    st.caption(f"The universe APEX trades from. RS≥{int(cfg.RS_MIN)} & ribbon-bull, from "
               f"{leaders_doc.get('liquid_size', '—')} liquid names "
               f"(≥${leaders_doc.get('min_dollar_vol', 0):,.0f}/day). Click a ticker to open it on TradingView.")
    st.caption("**🚫 avoid** (never trade) · **⭐ prioritize** (relaxed threshold + always real-time) "
               "· **➕ watch** (add to TV watchlist). Set them, then **Apply**. Previously-flagged "
               "names show pre-checked. Avoid wins over prioritize.")
    flags = apex_flags.load_flags()
    avoid_now, prefer_now = set(flags.get("avoid", {})), set(flags.get("prioritize", {}))
    if avoid_now or prefer_now:
        flagged_here = [l["symbol"] for l in leaders if l["symbol"] in avoid_now | prefer_now]
        if flagged_here:
            st.info("⚑ Flagged names in today's leaders (reminder): "
                    + " · ".join(f"{s} ({apex_flags.flag_label(s, flags)})" for s in flagged_here))
    if not leaders:
        st.info("No leaders file yet — run `apex-leaders`.")
    else:
        ldf = pd.DataFrame(leaders)
        prices = leader_prices(tuple(ldf["symbol"]))
        bare = ldf["symbol"].tolist()
        ldf["open"] = ldf["symbol"].map(lambda s: prices.get(s, (None, None))[0])
        ldf["current"] = ldf["symbol"].map(lambda s: prices.get(s, (None, None))[1])
        ldf["chg %"] = (ldf["current"] - ldf["open"]) / ldf["open"] * 100
        ldf.insert(0, "➕ watch", False)
        ldf.insert(0, "⭐ prioritize", ldf["symbol"].isin(prefer_now))
        ldf.insert(0, "🚫 avoid", ldf["symbol"].isin(avoid_now))
        ldf["symbol"] = ldf["symbol"].map(tv_url)
        editable = ["🚫 avoid", "⭐ prioritize", "➕ watch"]
        cols = editable + [c for c in ["symbol", "rs_pct", "open", "current", "chg %"] if c in ldf.columns]
        edited = st.data_editor(
            ldf[cols], width="stretch", hide_index=True,
            disabled=[c for c in cols if c not in editable], key="leaders_editor",
            column_config={
                "🚫 avoid": st.column_config.CheckboxColumn("🚫 avoid", default=False),
                "⭐ prioritize": st.column_config.CheckboxColumn("⭐ prioritize", default=False),
                "➕ watch": st.column_config.CheckboxColumn("➕ watch", default=False),
                "symbol": st.column_config.LinkColumn("symbol", display_text=r"symbol=(.+)$"),
                "rs_pct": st.column_config.NumberColumn("RS pct", format="%.1f"),
                "open": st.column_config.NumberColumn("open price", format="$%.2f"),
                "current": st.column_config.NumberColumn("current price", format="$%.2f"),
                "chg %": st.column_config.NumberColumn("chg %", format="%+.1f%%"),
            })
        av = edited["🚫 avoid"].tolist()
        pr = edited["⭐ prioritize"].tolist()
        wl = edited["➕ watch"].tolist()
        if st.button("✓ Apply flags + watchlist"):
            changed = 0
            for i, s in enumerate(bare):
                was_av, was_pr = s in avoid_now, s in prefer_now
                if av[i] and not was_av:
                    apex_flags.set_flag("avoid", s); changed += 1
                elif not av[i] and was_av:
                    apex_flags.clear_flag("avoid", s); changed += 1
                if pr[i] and not was_pr and not av[i]:
                    apex_flags.set_flag("prioritize", s); changed += 1
                elif not pr[i] and was_pr:
                    apex_flags.clear_flag("prioritize", s); changed += 1
            watch_syms = [bare[i] for i, v in enumerate(wl) if v]
            if watch_syms:
                add_to_watchlist(watch_syms)
            st.success(f"Applied {changed} flag change(s)" + (f", {len(watch_syms)} to watchlist" if watch_syms else ""))
            st.rerun()

elif page == "Playbook":
    st.title("APEX Playbook")
    pb = WORKSPACE / "docs" / "APEX_PLAYBOOK.md"
    if pb.exists():
        st.markdown(pb.read_text())
    else:
        st.info("Playbook not found at docs/APEX_PLAYBOOK.md")
