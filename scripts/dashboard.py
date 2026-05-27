"""
Tri-City Inator — Streamlit Trading Dashboard
Run with: streamlit run ~/tri-city-inator/scripts/dashboard.py
"""

import json
import math
from pathlib import Path
from datetime import datetime, date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

# ─── Paths ────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.parent
JOURNAL_PATH = BASE_DIR / "logs" / "tri-city-journal.json"
EXECUTIONS_PATH = BASE_DIR / "logs" / "tri-city-executions.json"

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
                "profit_factor": 0, "total_trades": 0}

    total = len(df)
    wins = df["outcome"].isin(["full_win", "partial_win"]).sum() if "outcome" in df.columns else 0

    gross_profit = df.loc[df["realized_pnl"] > 0, "realized_pnl"].sum()
    gross_loss = abs(df.loc[df["realized_pnl"] < 0, "realized_pnl"].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")

    return {
        "total_pnl": df["realized_pnl"].sum(),
        "win_rate": (wins / total * 100) if total > 0 else 0,
        "avg_r": df["r_multiple"].mean() if "r_multiple" in df.columns else 0,
        "profit_factor": profit_factor,
        "total_trades": total,
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
    ["Overview", "Trade Log", "Signal Analysis", "Risk & Sizing"],
    label_visibility="collapsed",
)

# ──────────────────────────────────────────────────────────────────────────────
# PAGE 1 — OVERVIEW
# ──────────────────────────────────────────────────────────────────────────────
if page == "Overview":
    st.title("Overview")

    fdf = apply_filters(merged if not merged.empty else jdf)
    m = calc_metrics(fdf)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total P&L", f"${'−' if m['total_pnl'] < 0 else ''}{abs(m['total_pnl']):,.2f}")
    c2.metric("Win Rate", f"{m['win_rate']:.1f}%")
    c3.metric("Avg R", f"{m['avg_r']:.2f}R" if not math.isnan(m["avg_r"]) else "—")
    c4.metric("Profit Factor", f"{m['profit_factor']:.2f}" if m['profit_factor'] != float('inf') else "∞")
    c5.metric("Total Trades", m["total_trades"])

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

        display_cols = {
            "date": "Date", "symbol": "Symbol", "setup": "Setup",
            "entry_price": "Entry", "exit_price": "Exit",
            "realized_pnl": "P&L ($)", "r_multiple": "R",
            "duration_min": "Duration (min)", "outcome": "Outcome",
        }

        available = [c for c in display_cols if c in fdf.columns]
        tbl = fdf[available].copy().sort_values("date", ascending=False)
        tbl = tbl.rename(columns=display_cols)

        if "Date" in tbl.columns:
            tbl["Date"] = tbl["Date"].dt.strftime("%Y-%m-%d")
        for col in ["Entry", "Exit"]:
            if col in tbl.columns:
                tbl[col] = tbl[col].apply(lambda v: f"${v:.2f}" if pd.notna(v) else "—")
        if "P&L ($)" in tbl.columns:
            tbl["P&L ($)"] = tbl["P&L ($)"].apply(lambda v: f"${v:+,.2f}" if pd.notna(v) else "—")
        if "R" in tbl.columns:
            tbl["R"] = tbl["R"].apply(lambda v: f"{v:+.2f}R" if pd.notna(v) else "—")
        if "Duration (min)" in tbl.columns:
            tbl["Duration (min)"] = tbl["Duration (min)"].apply(
                lambda v: f"{v:.0f}" if pd.notna(v) else "—"
            )

        def row_bg(outcome: str) -> str:
            mapping = {
                "full_win": "rgba(38,166,154,0.15)",
                "partial_win": "rgba(255,167,38,0.15)",
                "loss": "rgba(239,83,80,0.15)",
                "scratch": "rgba(120,144,156,0.10)",
            }
            return mapping.get(outcome, "")

        if "Outcome" in tbl.columns:
            raw_outcomes = fdf.sort_values("date", ascending=False)["outcome"].values
        else:
            raw_outcomes = [""] * len(tbl)

        styled = tbl.style.apply(
            lambda _: [f"background-color: {row_bg(raw_outcomes[i])}" for i in range(len(tbl))],
            axis=None,
        )

        st.dataframe(styled, use_container_width=True, height=420)

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
