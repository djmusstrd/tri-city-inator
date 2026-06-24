"""
Tri-City Inator — Streamlit Trading Dashboard
Run with: streamlit run ~/tri-city-inator/scripts/dashboard.py
"""

import json
import math
import os
from pathlib import Path
from datetime import datetime, date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

try:
    from dotenv import load_dotenv
    _env = Path(__file__).parent.parent / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
JOURNAL_PATH = BASE_DIR / "logs" / "tri-city-journal.json"
EXECUTIONS_PATH = BASE_DIR / "logs" / "tri-city-executions.json"
CANDIDATES_PATH = BASE_DIR / "shared" / "tri-city-candidates.json"
RESEARCH_NOTES_PATH = BASE_DIR / "shared" / "research-notes.json"

# ─── Theme constants ──────────────────────────────────────────────────────────
PLOTLY_THEME = "plotly_dark"
COLOR_WIN = "#26a69a"
COLOR_LOSS = "#ef5350"
COLOR_SCRATCH = "#78909c"
COLOR_PARTIAL = "#ffa726"
OUTCOME_COLORS = {
    "full_win": COLOR_WIN,
    "win": COLOR_WIN,
    "partial_win": COLOR_PARTIAL,
    "loss": COLOR_LOSS,
    "scratch": COLOR_SCRATCH,
}
SETUP_COLORS = {
    "BREAKOUT": "#ab47bc",
    "CONTINUATION": "#42a5f5",
    "PULLBACK": "#26a69a",
}

# ─── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Tri-City Inator Dashboard",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─── Access gate (shared password; traffic is HTTPS via the Cloudflare tunnel) ──
import hmac as _hmac
def _require_password():
    _pw = os.getenv("DASH_PASSWORD", "")
    if not _pw:
        return  # no password set → open (local use)
    # Bookmarkable token: append ?k=<password> to the URL to auto-log-in.
    # Save that link / "Add to Home Screen" on your phone and you never type again.
    try:
        _tok = st.query_params.get("k", "")
    except Exception:
        try:
            _tok = st.experimental_get_query_params().get("k", [""])[0]
        except Exception:
            _tok = ""
    if _tok and _hmac.compare_digest(_tok, _pw):
        st.session_state["_auth_ok"] = True
    if st.session_state.get("_auth_ok"):
        return
    def _verify():
        if _hmac.compare_digest(st.session_state.get("_pw_input", ""), _pw):
            st.session_state["_auth_ok"] = True
            st.session_state.pop("_pw_input", None)
        else:
            st.session_state["_auth_ok"] = False
    st.text_input("🔒 Password", type="password", key="_pw_input", on_change=_verify)
    if st.session_state.get("_auth_ok") is False:
        st.error("Incorrect password")
    st.stop()
_require_password()

st.markdown(
    """
    <style>
    [data-testid="stMetricValue"] { font-size: 2rem; font-weight: 700; }
    div[data-testid="metric-container"] {
        background: #1e1e2e; border-radius: 8px; padding: 12px 16px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ─── Data loading ─────────────────────────────────────────────────────────────
@st.cache_data(show_spinner=False)
def load_data(refresh_token: int):
    journal_raw: list = []
    if JOURNAL_PATH.exists():
        try:
            journal_raw = json.loads(JOURNAL_PATH.read_text()) or []
        except Exception:
            pass

    jdf = pd.DataFrame(journal_raw) if journal_raw else pd.DataFrame()

    if not jdf.empty:
        jdf["date"] = pd.to_datetime(jdf["date"], errors="coerce")
        for col in ["realized_pnl", "r_multiple", "entry_price", "exit_price",
                    "risk_dollars", "risk_per_share", "duration_min",
                    "stop_loss", "target_1", "target_2", "target_3", "position_size"]:
            if col in jdf.columns:
                jdf[col] = pd.to_numeric(jdf[col], errors="coerce")
        if "outcome" in jdf.columns:
            jdf["outcome"] = jdf["outcome"].replace("win", "full_win")
        jdf = jdf[jdf["status"] == "closed"] if "status" in jdf.columns else jdf

    exec_raw: list = []
    if EXECUTIONS_PATH.exists():
        try:
            exec_raw = json.loads(EXECUTIONS_PATH.read_text()) or []
        except Exception:
            pass

    edf = pd.DataFrame(exec_raw) if exec_raw else pd.DataFrame()

    if not edf.empty:
        edf["date"] = pd.to_datetime(edf["date"], errors="coerce")
        for col in ["rsi", "ema_dev", "rvol", "spy_change", "pnl_per_share",
                    "entry_price", "orh", "orl"]:
            if col in edf.columns:
                edf[col] = pd.to_numeric(edf[col], errors="coerce")

    merged = jdf.copy()
    if not jdf.empty and not edf.empty:
        exec_key = edf[["date", "symbol", "rsi", "ema_dev", "rvol", "cup",
                         "bb_squeeze", "orh", "orl", "spy_regime", "spy_change",
                         "candle_type"]].copy() if all(
            c in edf.columns for c in ["rsi", "ema_dev", "rvol"]
        ) else edf[["date", "symbol"]].copy()
        merged = jdf.merge(exec_key, on=["date", "symbol"], how="left", suffixes=("", "_exec"))

    return jdf, edf, merged


def load_candidates() -> dict:
    """Load tri-city-candidates.json. Not cached — refreshes each scan cycle."""
    if not CANDIDATES_PATH.exists():
        return {}
    try:
        return json.loads(CANDIDATES_PATH.read_text()) or {}
    except Exception:
        return {}


def load_research_notes() -> dict:
    """Load research-notes.json: {symbol: {decision, notes, updated_at, scan_date}}."""
    if not RESEARCH_NOTES_PATH.exists():
        return {}
    try:
        return json.loads(RESEARCH_NOTES_PATH.read_text()) or {}
    except Exception:
        return {}


def save_research_note(symbol: str, decision: str, notes: str, scan_date: str) -> None:
    data = load_research_notes()
    data[symbol] = {
        "decision": decision,
        "notes": notes,
        "scan_date": scan_date,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    RESEARCH_NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESEARCH_NOTES_PATH.write_text(json.dumps(data, indent=2))


# ─── Sidebar ──────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("Tri-City Inator")
    st.caption("Algo Trading Dashboard")
    st.divider()

    if st.button("🔄 Refresh Data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    if "refresh_token" not in st.session_state:
        st.session_state["refresh_token"] = 0

    jdf, edf, merged = load_data(st.session_state["refresh_token"])

    st.divider()
    st.subheader("Date Range")

    if not jdf.empty and "date" in jdf.columns:
        min_date = jdf["date"].min().date()
        max_date = jdf["date"].max().date()
    else:
        min_date = max_date = date.today()

    date_from = st.date_input("From", value=min_date, min_value=min_date, max_value=max_date)
    date_to = st.date_input("To", value=max_date, min_value=min_date, max_value=max_date)

    st.divider()
    st.subheader("Setup Filter")
    setups_available = sorted(jdf["setup"].dropna().unique().tolist()) if not jdf.empty and "setup" in jdf.columns else []
    selected_setups = st.multiselect("Setup", options=setups_available, default=setups_available)

    st.divider()
    st.caption(f"Journal: {len(jdf)} closed trades")
    st.caption(f"Executions: {len(edf)} entries")


# ─── Filter helper ────────────────────────────────────────────────────────────
def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    if "date" in df.columns:
        df = df[
            (df["date"].dt.date >= date_from) &
            (df["date"].dt.date <= date_to)
        ]
    if "setup" in df.columns and selected_setups:
        df = df[df["setup"].isin(selected_setups)]
    return df.copy()


# ─── Metric helpers ───────────────────────────────────────────────────────────
def calc_metrics(df: pd.DataFrame) -> dict:
    if df.empty or "realized_pnl" not in df.columns:
        return {"total_pnl": 0, "win_rate": 0, "avg_r": 0,
                "profit_factor": 0, "total_trades": 0,
                "sharpe_r": None, "calmar": None}

    total = len(df)
    wins = df["outcome"].isin(["full_win", "partial_win"]).sum() if "outcome" in df.columns else 0

    gross_profit = df.loc[df["realized_pnl"] > 0, "realized_pnl"].sum()
    gross_loss = abs(df.loc[df["realized_pnl"] < 0, "realized_pnl"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    # Sharpe (R-based, trade-level)
    sharpe_r = None
    if "r_multiple" in df.columns:
        r_vals = df["r_multiple"].dropna().tolist()
        if len(r_vals) >= 3:
            mean_r = sum(r_vals) / len(r_vals)
            std_r  = math.sqrt(sum((x - mean_r) ** 2 for x in r_vals) / (len(r_vals) - 1))
            if std_r > 0:
                dates = df["date"].dropna().dt.date.nunique() if "date" in df.columns else 1
                avg_per_day = len(r_vals) / max(dates, 1)
                sharpe_r = round((mean_r / std_r) * math.sqrt(252 / max(avg_per_day, 0.01)), 2)

    # Calmar ratio: total P&L / abs(max drawdown)
    calmar = None
    if "date" in df.columns:
        daily = df.groupby(df["date"].dt.date)["realized_pnl"].sum().sort_index()
        cum   = daily.cumsum()
        max_dd = (cum - cum.cummax()).min()
        total_pnl = df["realized_pnl"].sum()
        if max_dd < 0 and total_pnl != 0:
            calmar = round(total_pnl / abs(max_dd), 2)

    return {
        "total_pnl":    df["realized_pnl"].sum(),
        "win_rate":     (wins / total * 100) if total > 0 else 0,
        "avg_r":        df["r_multiple"].mean() if "r_multiple" in df.columns else 0,
        "profit_factor": profit_factor,
        "total_trades": total,
        "sharpe_r":     sharpe_r,
        "calmar":       calmar,
    }


def no_data_msg(label: str = "No trades in selected range"):
    st.info(f"📭 {label}")


def _base_layout(fig, title: str, height: int = 320, **kwargs):
    kwargs.setdefault("margin", dict(t=40, b=20))
    fig.update_layout(
        title=title,
        template=PLOTLY_THEME,
        height=height,
        **kwargs,
    )
    return fig


# ═══════════════════════════════════════════════════════════════════════════════
# PAGES
# ═══════════════════════════════════════════════════════════════════════════════
page = st.sidebar.radio(
    "Navigation",
    ["Positions", "Overview", "Trade Log", "Signal Analysis", "Risk & Sizing", "Candidates", "Playbook"],
    label_visibility="collapsed",
)

# ──────────────────────────────────────────────────────────────────────────────
# PAGE 0 — POSITIONS (live open trades)
# ──────────────────────────────────────────────────────────────────────────────
if page == "Positions":
    st.title("Open Positions")

    api_key    = os.getenv("ALPACA_API_KEY")
    secret_key = os.getenv("ALPACA_SECRET_KEY")
    paper      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

    col_refresh, _ = st.columns([1, 5])
    with col_refresh:
        if st.button("🔄 Refresh", use_container_width=True):
            st.rerun()

    if not api_key or not secret_key:
        st.error("Alpaca API keys not found in .env")
        st.stop()

    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(api_key, secret_key, paper=paper)
        account = client.get_account()
        positions = client.get_all_positions()
    except Exception as e:
        st.error(f"Alpaca connection failed: {e}")
        st.stop()

    # Account summary strip
    equity     = float(account.equity)
    last_eq    = float(account.last_equity)
    day_pnl    = equity - last_eq
    buying_pwr = float(account.buying_power)

    ac1, ac2, ac3, ac4 = st.columns(4)
    ac1.metric("Equity",       f"${equity:,.2f}")
    ac2.metric("Day P&L",      f"${'−' if day_pnl < 0 else ''}{abs(day_pnl):,.2f}",
               delta=f"{day_pnl / last_eq * 100:+.2f}%" if last_eq else None)
    ac3.metric("Buying Power", f"${buying_pwr:,.2f}")
    ac4.metric("Positions",    len(positions))

    st.divider()

    if not positions:
        st.info("No open positions.")
        st.stop()

    # Load executions log to get stop/target levels
    exec_map: dict[str, dict] = {}
    if EXECUTIONS_PATH.exists():
        try:
            exec_log = json.loads(EXECUTIONS_PATH.read_text()) or []
            today_str = datetime.now().strftime("%Y-%m-%d")
            for e in reversed(exec_log):
                sym = e.get("symbol", "")
                if sym and sym not in exec_map and e.get("date") == today_str:
                    exec_map[sym] = e
        except Exception:
            pass

    rows = []
    for pos in positions:
        sym        = pos.symbol
        qty        = float(pos.qty)
        entry      = float(pos.avg_entry_price)
        curr       = float(pos.current_price)
        unreal_pnl = float(pos.unrealized_pl)
        unreal_pct = float(pos.unrealized_plpc) * 100

        ex = exec_map.get(sym, {})
        stop   = ex.get("stop_loss")
        t1     = ex.get("target_1")
        t2     = ex.get("target_2")
        t3     = ex.get("target_3")
        setup  = ex.get("setup", "—")

        # Risk to stop
        risk_to_stop = round((curr - stop) / entry * 100, 2) if stop else None

        rows.append({
            "Symbol":    sym,
            "Setup":     setup,
            "Shares":    int(qty),
            "Entry $":   f"${entry:.2f}",
            "Current $": f"${curr:.2f}",
            "P&L $":     f"${'−' if unreal_pnl < 0 else ''}{abs(unreal_pnl):,.2f}",
            "P&L %":     f"{unreal_pct:+.2f}%",
            "Stop $":    f"${stop:.2f}" if stop else "—",
            "T1 $":      f"${t1:.2f}"  if t1   else "—",
            "T2 $":      f"${t2:.2f}"  if t2   else "—",
            "T3 $":      f"${t3:.2f}"  if t3   else "—",
            "_pnl":      unreal_pnl,
            "_sym":      sym,
        })

    pos_df = pd.DataFrame(rows)

    # Total unrealized P&L banner
    total_unreal = sum(r["_pnl"] for r in rows)
    banner_color = "#26a69a" if total_unreal >= 0 else "#ef5350"
    st.markdown(
        f'<div style="background:{banner_color}22;border-left:4px solid {banner_color};'
        f'padding:10px 16px;border-radius:6px;margin-bottom:12px;">'
        f'<b>Total Unrealized P&L: '
        f'{"−" if total_unreal < 0 else ""}${abs(total_unreal):,.2f}</b></div>',
        unsafe_allow_html=True,
    )

    # Display table (hide helper columns)
    display_cols = ["Symbol", "Setup", "Shares", "Entry $", "Current $",
                    "P&L $", "P&L %", "Stop $", "T1 $", "T2 $", "T3 $"]
    st.dataframe(
        pos_df[display_cols],
        use_container_width=True,
        hide_index=True,
    )

    # ── Manual override — close / trim a live position (same close_now() as the Telegram buttons) ──
    import sys as _sys
    _sys.path.insert(0, str(Path(__file__).parent))
    try:
        import apex_actions as _AA
        _override_on = _AA.manual_override_enabled()
    except Exception as _e:
        _AA, _override_on = None, False

    with st.expander("⚡ Manual actions — close / trim", expanded=False):
        if _AA is None:
            st.caption("apex_actions unavailable.")
        elif not _override_on:
            st.caption("Manual override is OFF — set APEX_MANUAL_OVERRIDE=true to enable.")
        else:
            for _r in rows:
                _sym = _r["_sym"]
                cA, cB, cC, cD = st.columns([2, 1, 1, 1.3])
                cA.write(f"**{_sym}** · {_r['Shares']} sh · {_r['P&L %']}")
                if cB.button("❌ Close", key=f"mo_close_{_sym}"):
                    st.session_state[f"mo_res_{_sym}"] = _AA.close_now(_sym, 1.0)
                if cC.button("✂️ Trim ½", key=f"mo_trim_{_sym}"):
                    st.session_state[f"mo_res_{_sym}"] = _AA.close_now(_sym, 0.5)
                if cD.button("🚫 Close+block", key=f"mo_block_{_sym}"):
                    st.session_state[f"mo_res_{_sym}"] = _AA.close_now(_sym, 1.0, block=True)
                if st.session_state.get(f"mo_res_{_sym}"):
                    st.success(str(st.session_state[f"mo_res_{_sym}"]))

    st.divider()

    # Per-position detail cards
    st.subheader("Position Detail")
    for r in rows:
        sym  = r["_sym"]
        pnl  = r["_pnl"]
        ex   = exec_map.get(sym, {})
        card_color = "#26a69a" if pnl >= 0 else "#ef5350"

        with st.expander(f"{sym}  ·  {r['Setup']}  ·  {r['P&L $']}  ({r['P&L %']})", expanded=True):
            d1, d2, d3 = st.columns(3)
            with d1:
                st.markdown("**Levels**")
                st.write(f"Entry:  {r['Entry $']}")
                st.write(f"Current: {r['Current $']}")
                st.write(f"Stop:   {r['Stop $']}")
            with d2:
                st.markdown("**Targets**")
                st.write(f"T1 (+{os.getenv('T1_PCT','10')}%): {r['T1 $']}")
                st.write(f"T2 (+{os.getenv('T2_PCT','20')}%): {r['T2 $']}")
                st.write(f"T3 (+{os.getenv('T3_PCT','30')}%): {r['T3 $']}")
            with d3:
                st.markdown("**Risk**")
                risk_d = ex.get("risk_dollars")
                rps    = ex.get("risk_per_share")
                st.write(f"Shares:     {r['Shares']}")
                st.write(f"Risk $:     ${risk_d:.2f}" if risk_d else "Risk $:  —")
                st.write(f"Risk/share: ${rps:.2f}" if rps else "Risk/share: —")
                st.write(f"RSI:        {ex.get('rsi', '—')}")
                st.write(f"EMA Dev:    {ex.get('ema_dev', '—')}")


# ──────────────────────────────────────────────────────────────────────────────
# PAGE 1 — OVERVIEW
# ──────────────────────────────────────────────────────────────────────────────
elif page == "Overview":
    st.title("Overview")

    fdf = apply_filters(merged if not merged.empty else jdf)
    m = calc_metrics(fdf)

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Total P&L", f"${'−' if m['total_pnl'] < 0 else ''}{abs(m['total_pnl']):,.2f}")
    c2.metric("Win Rate", f"{m['win_rate']:.1f}%")
    c3.metric("Avg R", f"{m['avg_r']:.2f}R" if not math.isnan(m["avg_r"]) else "—")
    c4.metric("Profit Factor", f"{m['profit_factor']:.2f}" if m['profit_factor'] != float('inf') else "∞")
    c5.metric("Total Trades", m["total_trades"])
    c6.metric("Sharpe (R)", f"{m['sharpe_r']:+.2f}" if m["sharpe_r"] is not None else "—",
              help="Trade-level Sharpe: mean(R)/std(R)×√(252/avg_trades_per_day). Preferred metric. ≥1.0 = good.")
    c7.metric("Calmar", f"{m['calmar']:+.2f}" if m["calmar"] is not None else "—",
              help="Total P&L / max drawdown. Higher = better drawdown-adjusted return.")

    st.divider()

    if fdf.empty or "realized_pnl" not in fdf.columns:
        no_data_msg()
    else:
        daily = fdf.groupby(fdf["date"].dt.date)["realized_pnl"].sum().reset_index()
        daily.columns = ["date", "pnl"]
        daily = daily.sort_values("date")
        daily["cumulative_pnl"] = daily["pnl"].cumsum()

        fig_cum = go.Figure([go.Scatter(
            x=daily["date"].astype(str),
            y=daily["cumulative_pnl"],
            mode="lines",
            line=dict(color=COLOR_WIN, width=2),
            name="Cumulative P&L",
        )])
        fig_cum.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
        _base_layout(fig_cum, "Cumulative P&L", height=320,
                     xaxis_title="Date", yaxis_title="Cumulative P&L ($)")
        st.plotly_chart(fig_cum, use_container_width=True)

        col_left, col_right = st.columns([3, 2])

        with col_left:
            bar_colors = [COLOR_WIN if v >= 0 else COLOR_LOSS for v in daily["pnl"]]
            fig_daily = go.Figure([go.Bar(
                x=daily["date"].astype(str),
                y=daily["pnl"],
                marker_color=bar_colors,
                name="Daily P&L",
            )])
            fig_daily.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.4)
            _base_layout(fig_daily, "P&L by Day", height=300,
                         xaxis_title="Date", yaxis_title="P&L ($)")
            st.plotly_chart(fig_daily, use_container_width=True)

        with col_right:
            if "outcome" in fdf.columns:
                outcome_counts = fdf["outcome"].value_counts().reset_index()
                outcome_counts.columns = ["outcome", "count"]
                labels = outcome_counts["outcome"].replace({
                    "full_win": "Full Win", "partial_win": "Partial Win",
                    "loss": "Loss", "scratch": "Scratch",
                })
                pie_colors = [OUTCOME_COLORS.get(o, "#888") for o in outcome_counts["outcome"]]
                fig_pie = go.Figure([go.Pie(
                    labels=labels,
                    values=outcome_counts["count"],
                    marker_colors=pie_colors,
                    hole=0.45,
                    textinfo="label+percent",
                )])
                _base_layout(fig_pie, "Outcome Breakdown", height=300,
                             showlegend=False, margin=dict(t=40, b=20, l=0, r=0))
                st.plotly_chart(fig_pie, use_container_width=True)


# ──────────────────────────────────────────────────────────────────────────────
# PAGE 2 — TRADE LOG
# ──────────────────────────────────────────────────────────────────────────────
elif page == "Trade Log":
    st.title("Trade Log")

    fdf = apply_filters(merged if not merged.empty else jdf)

    if fdf.empty or "realized_pnl" not in fdf.columns:
        no_data_msg()
    else:
        m = calc_metrics(fdf)
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("P&L", f"${'−' if m['total_pnl'] < 0 else ''}{abs(m['total_pnl']):,.2f}")
        c2.metric("Trades", m["total_trades"])
        c3.metric("Win Rate", f"{m['win_rate']:.1f}%")
        c4.metric("Avg R", f"{m['avg_r']:.2f}R" if not math.isnan(m["avg_r"]) else "—")

        st.divider()

        # Compute slippage if signal_price was logged alongside entry_price
        if "signal_price" in fdf.columns and "entry_price" in fdf.columns:
            fdf = fdf.copy()
            fdf["slippage"] = (fdf["entry_price"] - fdf["signal_price"]).round(4)

        display_cols = {
            "date": "Date", "symbol": "Symbol", "setup": "Setup",
            "signal_price": "Signal $", "entry_price": "Fill $",
            "slippage": "Slip", "exit_price": "Exit",
            "realized_pnl": "P&L ($)", "r_multiple": "R",
            "duration_min": "Duration (min)", "outcome": "Outcome",
        }

        available = [c for c in display_cols if c in fdf.columns]
        tbl = fdf[available].copy().sort_values("date", ascending=False)
        tbl = tbl.rename(columns=display_cols)

        if "Date" in tbl.columns:
            tbl["Date"] = tbl["Date"].dt.strftime("%Y-%m-%d")
        for col in ["Signal $", "Fill $", "Exit"]:
            if col in tbl.columns:
                tbl[col] = tbl[col].apply(lambda v: f"${v:.2f}" if pd.notna(v) else "—")
        if "Slip" in tbl.columns:
            tbl["Slip"] = tbl["Slip"].apply(lambda v: f"{v:+.4f}" if pd.notna(v) else "—")
        if "P&L ($)" in tbl.columns:
            tbl["P&L ($)"] = tbl["P&L ($)"].apply(lambda v: f"${v:+,.2f}" if pd.notna(v) else "—")
        if "R" in tbl.columns:
            tbl["R"] = tbl["R"].apply(lambda v: f"{v:+.2f}R" if pd.notna(v) else "—")
        if "Duration (min)" in tbl.columns:
            tbl["Duration (min)"] = tbl["Duration (min)"].apply(
                lambda v: f"{v:.0f}" if pd.notna(v) else "—"
            )

        st.dataframe(tbl, use_container_width=True, height=420)

        st.subheader("Trade Detail")
        symbols_available = sorted(fdf["symbol"].dropna().unique().tolist()) if "symbol" in fdf.columns else []
        selected_sym = st.selectbox("Select symbol to inspect", options=["—"] + symbols_available)

        if selected_sym != "—":
            rows = fdf[fdf["symbol"] == selected_sym].sort_values("date", ascending=False)
            for _, row in rows.iterrows():
                label = (
                    f"{row.get('date', pd.NaT).strftime('%Y-%m-%d') if pd.notna(row.get('date')) else '?'}"
                    f" · {row.get('setup', '?')} · {row.get('outcome', '?').upper()}"
                )
                with st.expander(label, expanded=False):
                    d1, d2, d3 = st.columns(3)
                    with d1:
                        st.markdown("**Entry / Exit**")
                        st.write(f"Entry: ${row.get('entry_price', 0):.2f} @ {row.get('entry_time', '?')}")
                        st.write(f"Exit:  ${row.get('exit_price', 0):.2f} @ {row.get('exit_time', '?')}")
                        st.write(f"Reason: {row.get('exit_reason', '?')}")
                    with d2:
                        st.markdown("**Levels**")
                        st.write(f"Stop:  ${row.get('stop_loss', 0):.2f}")
                        st.write(f"T1:    ${row.get('target_1', 0):.2f}")
                        st.write(f"T2:    ${row.get('target_2', 0):.2f}")
                        st.write(f"T3:    ${row.get('target_3', 0):.2f}")
                    with d3:
                        st.markdown("**Risk / P&L**")
                        st.write(f"Size:  {row.get('position_size', '?')} shares")
                        st.write(f"Risk:  ${row.get('risk_dollars', 0):.2f} (${row.get('risk_per_share', 0):.2f}/sh)")
                        st.write(f"P&L:   ${row.get('realized_pnl', 0):+,.2f}")
                        st.write(f"R:     {row.get('r_multiple', 0):+.2f}R")
                        st.write(f"Duration: {row.get('duration_min', 0):.0f} min")
                    sig_fields = ["rsi", "ema_dev", "rvol", "cup", "bb_squeeze", "orh", "orl"]
                    present = {k: row[k] for k in sig_fields if k in row.index and pd.notna(row[k])}
                    if present:
                        st.markdown("**Signal Context**")
                        sig_cols = st.columns(len(present))
                        for idx, (k, v) in enumerate(present.items()):
                            sig_cols[idx].metric(k.upper(), f"{v:.2f}" if isinstance(v, float) else str(v))


# ──────────────────────────────────────────────────────────────────────────────
# PAGE 3 — SIGNAL ANALYSIS
# ──────────────────────────────────────────────────────────────────────────────
elif page == "Signal Analysis":
    st.title("Signal Analysis")

    fdf = apply_filters(merged if not merged.empty else jdf)

    if fdf.empty or "realized_pnl" not in fdf.columns:
        no_data_msg()
        st.stop()

    is_win = fdf["outcome"].isin(["full_win", "partial_win"]) if "outcome" in fdf.columns else pd.Series(False, index=fdf.index)

    # ── Win rate & Avg R by setup ──
    if "setup" in fdf.columns:
        st.subheader("By Setup Type")
        setup_grp = fdf.groupby("setup").agg(
            total=("realized_pnl", "count"),
            wins=("outcome", lambda x: x.isin(["full_win", "partial_win"]).sum()),
            avg_r=("r_multiple", "mean"),
            total_pnl=("realized_pnl", "sum"),
        ).reset_index()
        setup_grp["win_rate"] = (setup_grp["wins"] / setup_grp["total"] * 100).round(1)
        setup_grp["avg_r"] = setup_grp["avg_r"].round(2)
        setup_grp["total_pnl"] = setup_grp["total_pnl"].round(2)

        col_a, col_b = st.columns(2)
        with col_a:
            fig_wr = go.Figure([go.Bar(
                x=setup_grp["setup"],
                y=setup_grp["win_rate"],
                marker_color=[SETUP_COLORS.get(s, "#888") for s in setup_grp["setup"]],
                text=[f"{v:.1f}%" for v in setup_grp["win_rate"]],
                textposition="outside",
                name="Win Rate",
            )])
            _base_layout(fig_wr, "Win Rate by Setup (%)", height=300,
                         showlegend=False, yaxis_range=[0, 100])
            st.plotly_chart(fig_wr, use_container_width=True)

        with col_b:
            fig_r = go.Figure([go.Bar(
                x=setup_grp["setup"],
                y=setup_grp["avg_r"],
                marker_color=[SETUP_COLORS.get(s, "#888") for s in setup_grp["setup"]],
                text=[f"{v:.2f}R" for v in setup_grp["avg_r"]],
                textposition="outside",
                name="Avg R",
            )])
            fig_r.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
            _base_layout(fig_r, "Avg R by Setup", height=300, showlegend=False)
            st.plotly_chart(fig_r, use_container_width=True)

        st.dataframe(
            setup_grp.rename(columns={
                "setup": "Setup", "total": "Trades", "wins": "Wins",
                "win_rate": "Win %", "avg_r": "Avg R", "total_pnl": "Total P&L ($)"
            }),
            use_container_width=True, hide_index=True,
        )

    st.divider()

    # ── RVOL distribution ──
    if "rvol" in fdf.columns and fdf["rvol"].notna().any():
        st.subheader("RVOL at Entry — Winners vs Losers")
        rvol_df = fdf[fdf["rvol"].notna()].copy()
        wins_mask = is_win.reindex(rvol_df.index)
        fig_rvol = go.Figure()
        fig_rvol.add_trace(go.Histogram(
            x=rvol_df.loc[wins_mask, "rvol"],
            name="Win", marker_color=COLOR_WIN, opacity=0.7, nbinsx=20,
        ))
        fig_rvol.add_trace(go.Histogram(
            x=rvol_df.loc[~wins_mask, "rvol"],
            name="Loss", marker_color=COLOR_LOSS, opacity=0.7, nbinsx=20,
        ))
        fig_rvol.update_layout(barmode="overlay")
        _base_layout(fig_rvol, "RVOL Distribution", height=300, xaxis_title="RVOL")
        st.plotly_chart(fig_rvol, use_container_width=True)

    # ── RSI at entry ──
    if "rsi" in fdf.columns and fdf["rsi"].notna().any():
        st.subheader("RSI at Entry")
        rsi_df = fdf[fdf["rsi"].notna()].copy()
        wins_mask = is_win.reindex(rsi_df.index)
        fig_rsi = go.Figure()
        fig_rsi.add_trace(go.Histogram(
            x=rsi_df.loc[wins_mask, "rsi"],
            name="Win", marker_color=COLOR_WIN, opacity=0.7, nbinsx=20,
        ))
        fig_rsi.add_trace(go.Histogram(
            x=rsi_df.loc[~wins_mask, "rsi"],
            name="Loss", marker_color=COLOR_LOSS, opacity=0.7, nbinsx=20,
        ))
        fig_rsi.update_layout(barmode="overlay")
        _base_layout(fig_rsi, "RSI at Entry Distribution", height=300, xaxis_title="RSI")
        st.plotly_chart(fig_rsi, use_container_width=True)

    # ── EMA Dev% at entry ──
    if "ema_dev" in fdf.columns and fdf["ema_dev"].notna().any():
        st.subheader("EMA Dev% at Entry — Winners vs Losers")
        ema_df = fdf[fdf["ema_dev"].notna()].copy()
        wins_mask = is_win.reindex(ema_df.index)
        fig_ema = go.Figure()
        fig_ema.add_trace(go.Histogram(
            x=ema_df.loc[wins_mask, "ema_dev"],
            name="Win", marker_color=COLOR_WIN, opacity=0.7, nbinsx=20,
        ))
        fig_ema.add_trace(go.Histogram(
            x=ema_df.loc[~wins_mask, "ema_dev"],
            name="Loss/Scratch", marker_color=COLOR_LOSS, opacity=0.7, nbinsx=20,
        ))
        fig_ema.update_layout(barmode="overlay")
        fig_ema.add_vline(x=0, line_dash="dash", line_color="gray", opacity=0.6,
                          annotation_text="EMA", annotation_position="top right")
        _base_layout(fig_ema, "EMA Dev% at Entry Distribution", height=300,
                     xaxis_title="EMA Dev%")
        st.plotly_chart(fig_ema, use_container_width=True)

    st.divider()
    col_cup, col_htf = st.columns(2)

    with col_cup:
        st.subheader("Cup Pattern")
        if "cup" in fdf.columns and fdf["cup"].notna().any():
            cup_grp = fdf.groupby("cup").apply(
                lambda g: pd.Series({
                    "Trades": len(g),
                    "Win Rate %": g["outcome"].isin(["full_win", "partial_win"]).mean() * 100 if "outcome" in g else 0,
                    "Avg R": g["r_multiple"].mean() if "r_multiple" in g.columns else 0,
                })
            ).reset_index()
            cup_labels = cup_grp["cup"].map({True: "Cup=YES", False: "Cup=NO"})
            cup_clrs = [COLOR_WIN if "YES" in str(l) else COLOR_SCRATCH for l in cup_labels]
            fig_cup = go.Figure([go.Bar(
                x=cup_labels,
                y=cup_grp["Win Rate %"],
                marker_color=cup_clrs,
                text=[f"{v:.1f}%" for v in cup_grp["Win Rate %"]],
                textposition="outside",
            )])
            _base_layout(fig_cup, "Win Rate: Cup Pattern", height=300,
                         showlegend=False, yaxis_range=[0, 100])
            st.plotly_chart(fig_cup, use_container_width=True)
        else:
            st.info("Cup data not available in this date range.")

    with col_htf:
        st.subheader("BB Squeeze")
        if "bb_squeeze" in fdf.columns and fdf["bb_squeeze"].notna().any():
            bb_grp = fdf.groupby("bb_squeeze").apply(
                lambda g: pd.Series({
                    "Trades": len(g),
                    "Win Rate %": g["outcome"].isin(["full_win", "partial_win"]).mean() * 100 if "outcome" in g else 0,
                    "Avg R": g["r_multiple"].mean() if "r_multiple" in g.columns else 0,
                })
            ).reset_index()
            bb_labels = bb_grp["bb_squeeze"].map({True: "Squeeze=YES", False: "Squeeze=NO"})
            bb_clrs = ["#ab47bc" if "YES" in str(l) else COLOR_SCRATCH for l in bb_labels]
            fig_bb = go.Figure([go.Bar(
                x=bb_labels,
                y=bb_grp["Win Rate %"],
                marker_color=bb_clrs,
                text=[f"{v:.1f}%" for v in bb_grp["Win Rate %"]],
                textposition="outside",
            )])
            _base_layout(fig_bb, "Win Rate: BB Squeeze", height=300,
                         showlegend=False, yaxis_range=[0, 100])
            st.plotly_chart(fig_bb, use_container_width=True)
        else:
            st.info("BB Squeeze data not available in this date range.")

    st.divider()

    st.subheader("P&L by Entry Hour (CT)")
    if "entry_time" in fdf.columns and fdf["entry_time"].notna().any():
        def parse_hour(t):
            if pd.isna(t):
                return None
            try:
                return int(str(t).split(":")[0])
            except Exception:
                return None

        fdf_tod = fdf.copy()
        fdf_tod["entry_hour"] = fdf_tod["entry_time"].apply(parse_hour)
        fdf_tod = fdf_tod[fdf_tod["entry_hour"].notna()]

        if not fdf_tod.empty:
            hour_grp = fdf_tod.groupby("entry_hour").agg(
                total_pnl=("realized_pnl", "sum"),
                trades=("realized_pnl", "count"),
                win_rate=("outcome", lambda x: x.isin(["full_win", "partial_win"]).mean() * 100),
            ).reset_index()
            hour_grp["entry_hour_label"] = hour_grp["entry_hour"].apply(lambda h: f"{h}:00")
            bar_clrs = [COLOR_WIN if v >= 0 else COLOR_LOSS for v in hour_grp["total_pnl"]]

            fig_tod = go.Figure([go.Bar(
                x=hour_grp["entry_hour_label"],
                y=hour_grp["total_pnl"],
                marker_color=bar_clrs,
                text=hour_grp["total_pnl"].apply(lambda v: f"${v:+,.0f}"),
                textposition="outside",
                name="P&L ($)",
            )])
            fig_tod.add_hline(y=0, line_dash="dot", line_color="gray", opacity=0.4)
            _base_layout(fig_tod, "Total P&L by Entry Hour (CT)", height=320,
                         xaxis_title="Hour (CT)", yaxis_title="P&L ($)")
            st.plotly_chart(fig_tod, use_container_width=True)
        else:
            no_data_msg("No entry time data available.")
    else:
        no_data_msg("No entry time data available.")


# ──────────────────────────────────────────────────────────────────────────────
# PAGE 4 — RISK & SIZING
# ──────────────────────────────────────────────────────────────────────────────
elif page == "Risk & Sizing":
    st.title("Risk & Sizing")

    fdf = apply_filters(merged if not merged.empty else jdf)

    if fdf.empty or "realized_pnl" not in fdf.columns:
        no_data_msg()
        st.stop()

    is_win = fdf["outcome"].isin(["full_win", "partial_win"]) if "outcome" in fdf.columns else pd.Series(False, index=fdf.index)

    # ── R distribution ──
    st.subheader("R-Multiple Distribution")
    if "r_multiple" in fdf.columns and fdf["r_multiple"].notna().any():
        r_df = fdf[fdf["r_multiple"].notna()].copy()
        wins_mask = is_win.reindex(r_df.index)
        fig_r_hist = go.Figure()
        fig_r_hist.add_trace(go.Histogram(
            x=r_df.loc[wins_mask, "r_multiple"],
            name="Win/Partial", marker_color=COLOR_WIN, opacity=0.7, nbinsx=30,
        ))
        fig_r_hist.add_trace(go.Histogram(
            x=r_df.loc[~wins_mask, "r_multiple"],
            name="Loss/Scratch", marker_color=COLOR_LOSS, opacity=0.7, nbinsx=30,
        ))
        fig_r_hist.update_layout(barmode="overlay")
        fig_r_hist.add_vline(x=0, line_dash="dash", line_color="gray", opacity=0.6)
        _base_layout(fig_r_hist, "R-Multiple Distribution (all trades)", height=320,
                     xaxis_title="R-Multiple")
        st.plotly_chart(fig_r_hist, use_container_width=True)

        sc1, sc2, sc3, sc4 = st.columns(4)
        sc1.metric("Best Trade", f"{r_df['r_multiple'].max():+.2f}R")
        sc2.metric("Worst Trade", f"{r_df['r_multiple'].min():+.2f}R")
        sc3.metric("Avg R (wins)", f"{r_df.loc[wins_mask,'r_multiple'].mean():+.2f}R" if wins_mask.any() else "—")
        sc4.metric("Avg R (losses)", f"{r_df.loc[~wins_mask,'r_multiple'].mean():+.2f}R" if (~wins_mask).any() else "—")

    # ── Position size vs R ──
    if "position_size" in fdf.columns and "r_multiple" in fdf.columns:
        ps_df = fdf[fdf["position_size"].notna() & fdf["r_multiple"].notna()].copy()
        if not ps_df.empty and "entry_price" in ps_df.columns:
            ps_df["notional"] = ps_df["position_size"] * ps_df["entry_price"].fillna(0)
            wins_mask_ps = is_win.reindex(ps_df.index)
            ps_df["outcome_label"] = ps_df["outcome"].replace({
                "full_win": "Full Win", "partial_win": "Partial Win",
                "loss": "Loss", "scratch": "Scratch",
            }) if "outcome" in ps_df.columns else "Unknown"
            color_map_ps = {"Full Win": COLOR_WIN, "Partial Win": COLOR_PARTIAL,
                            "Loss": COLOR_LOSS, "Scratch": COLOR_SCRATCH}
            fig_ps = go.Figure()
            for label, grp in ps_df.groupby("outcome_label"):
                fig_ps.add_trace(go.Scatter(
                    x=grp["notional"],
                    y=grp["r_multiple"],
                    mode="markers",
                    name=label,
                    marker=dict(color=color_map_ps.get(label, "#888"), size=8, opacity=0.8),
                    text=grp["symbol"] if "symbol" in grp.columns else None,
                    hovertemplate="<b>%{text}</b><br>Notional: $%{x:,.0f}<br>R: %{y:+.2f}<extra></extra>",
                ))
            fig_ps.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
            _base_layout(fig_ps, "Position Notional ($) vs R-Multiple", height=320,
                         xaxis_title="Notional Value ($)", yaxis_title="R-Multiple")
            st.plotly_chart(fig_ps, use_container_width=True)

    st.divider()

    # ── Streak analysis ──
    st.subheader("Streak Analysis")
    if "outcome" in fdf.columns:
        outcomes_sorted = fdf.sort_values("date")["outcome"].values
        max_consec_loss = 0
        max_consec_win = 0
        cur_loss = 0
        cur_win = 0
        for o in outcomes_sorted:
            if o == "loss":
                cur_loss += 1
                cur_win = 0
                max_consec_loss = max(max_consec_loss, cur_loss)
            elif o in ("full_win", "partial_win"):
                cur_win += 1
                cur_loss = 0
                max_consec_win = max(max_consec_win, cur_win)
            else:
                cur_loss = 0
                cur_win = 0

        col_s1, col_s2 = st.columns(2)
        col_s1.metric("Max Consecutive Losses", max_consec_loss)
        col_s2.metric("Max Consecutive Wins", max_consec_win)

    st.divider()

    # ── Rolling drawdown ──
    st.subheader("Rolling 20-Day Drawdown")
    if "realized_pnl" in fdf.columns:
        dd_df = fdf.groupby(fdf["date"].dt.date)["realized_pnl"].sum().reset_index()
        dd_df.columns = ["date", "pnl"]
        dd_df = dd_df.sort_values("date")
        dd_df["cum_pnl"] = dd_df["pnl"].cumsum()

        if len(dd_df) >= 2:
            rolling_max = dd_df["cum_pnl"].rolling(window=20, min_periods=1).max()
            dd_df["drawdown"] = dd_df["cum_pnl"] - rolling_max

            fig_dd = go.Figure([go.Scatter(
                x=dd_df["date"].astype(str),
                y=dd_df["drawdown"],
                fill="tozeroy",
                fillcolor="rgba(239,83,80,0.15)",
                line=dict(color=COLOR_LOSS, width=1.5),
                name="Drawdown ($)",
            )])
            fig_dd.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.4)
            _base_layout(fig_dd, "Rolling 20-Day Drawdown from Peak Equity", height=320,
                         xaxis_title="Date", yaxis_title="Drawdown ($)")
            st.plotly_chart(fig_dd, use_container_width=True)
            st.caption(f"Max drawdown in period: ${dd_df['drawdown'].min():,.2f}")
        else:
            st.info("Need at least 2 trading days for drawdown chart.")

    st.divider()

    # ── Risk scatter ──
    st.subheader("Risk per Trade vs Actual P&L")
    if "risk_dollars" in fdf.columns and "realized_pnl" in fdf.columns:
        scatter_df = fdf[fdf["risk_dollars"].notna() & fdf["realized_pnl"].notna()].copy()
        if "outcome" in scatter_df.columns:
            scatter_df["outcome_label"] = scatter_df["outcome"].replace({
                "full_win": "Full Win", "partial_win": "Partial Win",
                "loss": "Loss", "scratch": "Scratch",
            })
        else:
            scatter_df["outcome_label"] = "Unknown"

        scatter_df["symbol_label"] = scatter_df["symbol"] if "symbol" in scatter_df.columns else "?"

        color_map = {
            "Full Win": COLOR_WIN, "Partial Win": COLOR_PARTIAL,
            "Loss": COLOR_LOSS, "Scratch": COLOR_SCRATCH, "Unknown": "#888",
        }

        fig_scatter = go.Figure()
        for label, grp in scatter_df.groupby("outcome_label"):
            hover_text = grp["symbol_label"]
            if "setup" in grp.columns:
                hover_text = grp["symbol_label"] + " | " + grp["setup"].fillna("")
            fig_scatter.add_trace(go.Scatter(
                x=grp["risk_dollars"],
                y=grp["realized_pnl"],
                mode="markers",
                name=label,
                marker=dict(color=color_map.get(label, "#888"), size=8, opacity=0.8),
                text=hover_text,
                hovertemplate="<b>%{text}</b><br>Risk: $%{x:.2f}<br>P&L: $%{y:+.2f}<extra></extra>",
            ))

        fig_scatter.add_hline(y=0, line_dash="dash", line_color="gray", opacity=0.5)
        if not scatter_df["risk_dollars"].empty:
            avg_risk = scatter_df["risk_dollars"].mean()
            fig_scatter.add_vline(x=avg_risk, line_dash="dot", line_color="#78909c",
                                   opacity=0.5, annotation_text="Avg Risk",
                                   annotation_position="top right")
        _base_layout(fig_scatter, "Risk Dollars vs Realized P&L", height=380,
                     xaxis_title="Risk ($)", yaxis_title="P&L ($)")
        st.plotly_chart(fig_scatter, use_container_width=True)
    else:
        no_data_msg("Risk/sizing data not available.")


# ──────────────────────────────────────────────────────────────────────────────
# PAGE 5 — CANDIDATES
# ──────────────────────────────────────────────────────────────────────────────
elif page == "Candidates":
    st.title("Candidate Scanner")

    raw = load_candidates()
    if not raw or not raw.get("candidates"):
        st.info("No candidate data found. Run the premarket scanner first: `python -W ignore scripts/tri_city_scanner.py`")
        st.stop()

    # Metadata header
    mc1, mc2, mc3 = st.columns(3)
    mc1.metric("Scan Date", raw.get("date", "—"))
    mc2.metric("Scanned At", raw.get("scanned_at", "—"))
    mc3.metric("Total Candidates", raw.get("total", len(raw["candidates"])))

    if st.button("🔄 Refresh Candidates", use_container_width=False):
        st.rerun()

    st.divider()

    cdf = pd.DataFrame(raw["candidates"])

    # ── Filters ──
    fc1, fc2, fc3, fc4 = st.columns(4)
    with fc1:
        float_opts = ["All"] + sorted(cdf["float_cat"].dropna().unique().tolist()) if "float_cat" in cdf.columns else ["All"]
        float_filter = st.selectbox("Float Tier", float_opts)
    with fc2:
        min_score = st.slider("Min Score", 0.0, 1.0, 0.0, 0.05)
    with fc3:
        hide_parabolic = st.checkbox("Hide Parabolic (>10x RVol)", value=False)
    with fc4:
        earnings_only = st.checkbox("Earnings Only", value=False)

    filtered = cdf.copy()
    if float_filter != "All" and "float_cat" in filtered.columns:
        filtered = filtered[filtered["float_cat"] == float_filter]
    if "score" in filtered.columns:
        filtered = filtered[filtered["score"] >= min_score]
    if hide_parabolic and "parabolic" in filtered.columns:
        filtered = filtered[~filtered["parabolic"]]
    if earnings_only and "earnings_soon" in filtered.columns:
        filtered = filtered[filtered["earnings_soon"] == True]

    st.caption(f"Showing {len(filtered)} of {len(cdf)} candidates")

    # ── Build flags column ──
    def build_flags(row):
        flags = []
        if row.get("earnings_soon"):  flags.append("E")
        if row.get("near_52wk_high"): flags.append("⚠52H")
        if row.get("parabolic"):      flags.append("⚡PARA")
        if row.get("htf"):            flags.append("★HTF")
        if row.get("bb_squeeze"):     flags.append("SQZ")
        if row.get("park_trending"):  flags.append("TRD")
        return " ".join(flags) if flags else "—"

    filtered = filtered.copy()
    filtered["flags"] = filtered.apply(build_flags, axis=1)

    # ── Main table ──
    st.subheader("Ranked Candidates")

    # Score component columns (only show if present)
    sc_cols = ["sc_gap", "sc_rvol", "sc_stage", "sc_sma", "sc_catalyst",
               "sc_float", "sc_rsi", "sc_park", "sc_bb", "sc_penalty"]
    show_breakdown = st.checkbox("Show score breakdown columns", value=False)

    display_cols = ["rank", "symbol", "price", "gap_pct", "rvol", "rsi",
                    "float_cat", "float_shares", "score", "flags"]
    if show_breakdown:
        display_cols += [c for c in sc_cols if c in filtered.columns]
    display_cols += ["news_headline", "news_url"]

    # Keep only columns that exist
    display_cols = [c for c in display_cols if c in filtered.columns]

    tbl = filtered[display_cols].sort_values("rank").copy()

    # Format numeric columns
    for col, fmt in [("price", "${:.2f}"), ("gap_pct", "{:+.1f}%"),
                     ("rvol", "{:.1f}x"), ("rsi", "{:.1f}"),
                     ("score", "{:.4f}")]:
        if col in tbl.columns:
            tbl[col] = tbl[col].apply(
                lambda v, f=fmt: f.format(v) if pd.notna(v) else "—"
            )

    if "float_shares" in tbl.columns:
        def fmt_float(v):
            if pd.isna(v) or v == 0: return "—"
            if v >= 1e6: return f"{v/1e6:.1f}M"
            if v >= 1e3: return f"{v/1e3:.0f}K"
            return str(int(v))
        tbl["float_shares"] = tbl["float_shares"].apply(fmt_float)

    for sc in sc_cols:
        if sc in tbl.columns:
            tbl[sc] = tbl[sc].apply(lambda v: f"{v:+.4f}" if pd.notna(v) else "—")

    col_labels = {
        "rank": "Rank", "symbol": "Symbol", "price": "Price",
        "gap_pct": "Gap%", "rvol": "RVOL", "rsi": "RSI",
        "float_cat": "Float Tier", "float_shares": "Float Shares",
        "score": "Score", "flags": "Flags",
        "sc_gap": "sc:gap", "sc_rvol": "sc:rvol", "sc_stage": "sc:stage",
        "sc_sma": "sc:sma", "sc_catalyst": "sc:cat", "sc_float": "sc:float",
        "sc_rsi": "sc:rsi", "sc_park": "sc:park", "sc_bb": "sc:bb",
        "sc_penalty": "sc:penalty",
        "news_headline": "Headline", "news_url": "News Link",
    }
    tbl = tbl.rename(columns={k: v for k, v in col_labels.items() if k in tbl.columns})

    # Configure news URL column as clickable link
    col_config = {}
    if "News Link" in tbl.columns:
        col_config["News Link"] = st.column_config.LinkColumn(
            "News Link", display_text="Open", help="Click to open news article"
        )

    st.dataframe(tbl, use_container_width=True, height=520,
                 hide_index=True, column_config=col_config)

    st.divider()

    # ── Research / decision queue ──
    st.subheader("Research")

    notes = load_research_notes()
    symbols = filtered.sort_values("rank")["symbol"].tolist()
    if symbols:
        sel_symbol = st.selectbox("Symbol", symbols)
        srow = filtered[filtered["symbol"] == sel_symbol].iloc[0]

        rc1, rc2 = st.columns([1, 1])
        with rc1:
            st.metric("Score", f"{srow.get('score', 0):.4f}")
            st.metric("Gap %", f"{srow.get('gap_pct', 0):+.1f}%")
            st.metric("RVOL", f"{srow.get('rvol', 0):.1f}x")
            st.metric("RSI", f"{srow.get('rsi', 0):.1f}")
            st.write(f"**Float:** {srow.get('float_cat', '—')} ({srow.get('float_shares', '—')})")
            st.write(f"**Flags:** {srow.get('flags', '—')}")
        with rc2:
            headline = srow.get("news_headline")
            url = srow.get("news_url")
            if headline:
                if url:
                    st.write(f"**News:** [{headline}]({url})")
                else:
                    st.write(f"**News:** {headline}")
            else:
                st.write("**News:** —")

            for sc in sc_cols:
                if sc in srow.index and pd.notna(srow[sc]):
                    st.caption(f"{sc}: {srow[sc]:+.4f}")

        existing = notes.get(sel_symbol, {})
        decisions = ["No decision", "Watch", "Take", "Skip"]
        default_idx = decisions.index(existing["decision"]) if existing.get("decision") in decisions else 0

        with st.form(f"research_form_{sel_symbol}"):
            decision = st.radio("Decision", decisions, index=default_idx, horizontal=True)
            note_text = st.text_area("Notes", value=existing.get("notes", ""), height=100)
            submitted = st.form_submit_button("Save")
            if submitted:
                save_research_note(sel_symbol, decision, note_text, raw.get("date", "—"))
                st.success(f"Saved {decision} for {sel_symbol}")
                st.rerun()

        if existing.get("updated_at"):
            st.caption(f"Last updated {existing['updated_at']} (scan {existing.get('scan_date', '—')})")
    else:
        st.info("No candidates match the current filters.")

    # ── Decision log ──
    if notes:
        with st.expander("Decision log (all symbols)", expanded=False):
            log_df = pd.DataFrame([
                {"Symbol": sym, **vals} for sym, vals in notes.items()
            ]).sort_values("updated_at", ascending=False)
            st.dataframe(log_df, use_container_width=True, hide_index=True)

    # ── Score weight reference ──
    with st.expander("Score weight reference"):
        weights = {
            "Gap %":       ("sc:gap",     0.30, "Size of overnight move — capped at 20%"),
            "RVOL":        ("sc:rvol",    0.30, "Relative volume — institutional conviction; parabolic >10x gets 50% credit"),
            "Stage 2":     ("sc:stage",   0.10, "Price above SMA50 — Weinstein uptrend"),
            "SMA Slope":   ("sc:sma",     0.07, "SMA50 above SMA100 — rising trend"),
            "Catalyst":    ("sc:cat",     0.08, "Gap ≥5% treated as catalyst-driven"),
            "Float":       ("sc:float",   0.06, "Low float (<10M) = full; medium = half; large = 0"),
            "RSI":         ("sc:rsi",     0.05, "50–70 = full; 40–80 = half; outside = 0"),
            "Parkinson":   ("sc:park",    0.02, "Vol regime trending (H/L estimator)"),
            "BB Squeeze":  ("sc:bb",      0.02, "5-min BB compressed — coiled energy"),
            "52wk Penalty":("sc:penalty",-0.08, "Within 5% of 52-week high — resistance zone"),
        }
        wdf = pd.DataFrame([
            {"Factor": k, "Column": v[0], "Weight": f"{v[1]:+.2f}", "Description": v[2]}
            for k, v in weights.items()
        ])
        st.dataframe(wdf, use_container_width=True, hide_index=True)

# ──────────────────────────────────────────────────────────────────────────────
# PAGE 7 — PLAYBOOK
# ──────────────────────────────────────────────────────────────────────────────
elif page == "Playbook":
    st.title("Playbook")
    st.caption("Full operating instructions for System 1 (Tri-City) and the Sandbox.")

    cheatsheet = BASE_DIR / "CHEATSHEET.md"
    if cheatsheet.exists():
        st.markdown(cheatsheet.read_text())
    else:
        st.error("CHEATSHEET.md not found. Expected at: " + str(cheatsheet))
