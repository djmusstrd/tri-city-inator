#!/usr/bin/env python3
"""
DAILY RECAP (Tri-City) — EOD journal sync + performance report.

Run automatically at 3:50 PM CT (5 min after EOD close).

Steps:
  1. Pull today's FILL activities from Alpaca to find any unlogged entries
  2. Write synthetic execution log entries for fills missing from tri-city-executions.json
  3. Call log_exit() for entries with no matching journal record
  4. Compute day metrics, write markdown to logs/reports/daily-YYYY-MM-DD-tricty.md
  5. Print the report to stdout

Usage:
    python -W ignore scripts/daily_recap.py
    python -W ignore scripts/daily_recap.py --date 2026-05-14
    python -W ignore scripts/daily_recap.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

WORKSPACE = Path.home() / "tri-city-inator"
sys.path.insert(0, str(WORKSPACE))

try:
    from dotenv import load_dotenv
    _env = WORKSPACE / ".env"
    if _env.exists():
        load_dotenv(_env)
except ImportError:
    pass

CT = ZoneInfo("America/Chicago")
REPORTS_DIR = WORKSPACE / "logs" / "reports"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"


# ── Alpaca helpers ─────────────────────────────────────────────────────────────

def get_alpaca_client():
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.trading.client import TradingClient
        return TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
    except Exception as e:
        logger.warning(f"Alpaca client error: {e}")
        return None


def get_today_fills(date: str) -> list[dict]:
    """
    Pull FILL activities from Alpaca for the given date via REST API.
    Returns list of dicts with symbol, side, qty, price, time.
    """
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return []
    try:
        import requests as req
        base = "https://paper-api.alpaca.markets" if ALPACA_PAPER else "https://api.alpaca.markets"
        resp = req.get(
            f"{base}/v2/account/activities/FILL",
            headers={
                "APCA-API-KEY-ID":     ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            },
            params={"date": date, "page_size": 100},
            timeout=10,
        )
        resp.raise_for_status()
        fills = []
        for a in resp.json():
            activity_date = str(a.get("transaction_time", ""))[:10]
            if activity_date != date:
                continue
            fills.append({
                "symbol":   a.get("symbol", ""),
                "side":     a.get("side", ""),
                "qty":      float(a.get("qty", 0)),
                "price":    float(a.get("price", 0)),
                "time":     a.get("transaction_time", ""),
                "order_id": a.get("order_id", ""),
            })
        return fills
    except Exception as e:
        logger.warning(f"get_today_fills error: {e}")
        return []


def get_account_equity() -> float | None:
    client = get_alpaca_client()
    if not client:
        return None
    try:
        return float(client.get_account().equity)
    except Exception:
        return None


# ── Sync logic ─────────────────────────────────────────────────────────────────

def get_tricty_universe(date: str) -> set[str]:
    """
    Return the set of symbols Tri-City is allowed to trade.
    Uses symbols already in the execution log for today, plus any morning watchlist.
    """
    known: set[str] = set()
    exec_log = WORKSPACE / "logs" / "tri-city-executions.json"
    try:
        entries = json.loads(exec_log.read_text()) if exec_log.exists() else []
        known.update(e["symbol"] for e in entries if e.get("date") == date)
    except Exception:
        pass
    watchlist = WORKSPACE / "shared" / "morning-watchlist.json"
    if watchlist.exists():
        try:
            data = json.loads(watchlist.read_text())
            syms = data if isinstance(data, list) else data.get("symbols", [])
            known.update(s.split(":")[-1] for s in syms)
        except Exception:
            pass
    return known


def sync_missing_entries(fills: list[dict], date: str, dry_run: bool) -> list[str]:
    """
    Flag any Tri-City-universe symbols with BUY fills today but no execution log entry.
    Does NOT auto-write synthetic entries (shared account — fills are ambiguous between agents).
    Use write_synthetic_entry() manually if needed.
    """
    exec_log = WORKSPACE / "logs" / "tri-city-executions.json"
    try:
        existing = json.loads(exec_log.read_text()) if exec_log.exists() else []
    except Exception:
        existing = []

    logged_symbols = {
        e["symbol"] for e in existing
        if e.get("date") == date and e.get("success")
    }

    universe = get_tricty_universe(date)
    if not universe:
        return []

    buy_symbols = {
        f["symbol"] for f in fills
        if f["side"].lower() in ("buy", "buy_to_open") and f["symbol"] in universe
    }

    actions = []
    for sym in sorted(buy_symbols - logged_symbols):
        actions.append(
            f"WARNING: {sym} has Alpaca buy fills today but no tri-city-executions.json entry — "
            f"possible logging gap; use write_synthetic_entry() to reconstruct manually"
        )

    return actions


def sync_missing_exits(fills: list[dict], date: str, dry_run: bool) -> list[str]:
    """
    For any successful execution log entry with no journal record, attempt to log the exit
    using today's sell fills. Skips symbols with no sell fill today (still open).
    """
    from managers.trade_journal import get_open_entries, log_exit
    open_entries = get_open_entries(date)
    if not open_entries:
        return []

    # Build avg sell price from today's fills per symbol
    sell_prices: dict[str, list[float]] = {}
    for f in fills:
        if f["side"].lower() in ("sell", "sell_to_close") and f["price"] > 0:
            sell_prices.setdefault(f["symbol"], []).append(f["price"])
    avg_sells = {sym: round(sum(ps) / len(ps), 4) for sym, ps in sell_prices.items()}

    actions = []
    for e in open_entries:
        sym   = e["symbol"]
        setup = e.get("setup", "UNKNOWN")

        if sym not in avg_sells:
            actions.append(f"SKIP EXIT: {sym} — no sell fill on {date} (may still be open)")
            continue

        exit_price = avg_sells[sym]
        msg = f"LOGGING EXIT: {sym} {setup} @ ${exit_price:.2f} (reconstructed from today's fills)"
        actions.append(msg)

        if not dry_run:
            log_exit(
                symbol=sym,
                setup=setup,
                date=date,
                exit_price=exit_price,
                exit_reason="Reconstructed from Alpaca fill (recap sync)",
                shares=e.get("position_size", 0),
            )

    return actions


# ── Report generation ──────────────────────────────────────────────────────────

def build_report(date: str, sync_actions: list[str]) -> str:
    from managers.trade_journal import get_all_trades, compute_metrics

    all_trades = get_all_trades()
    today_trades = [t for t in all_trades if t.get("date") == date and t.get("status") == "closed"]
    metrics = compute_metrics(today_trades) if today_trades else {"total": 0}

    lines = [
        f"# Tri-City — Daily Recap: {date}",
        f"",
        f"Generated: {datetime.now(CT).strftime('%Y-%m-%d %H:%M CT')}",
        f"",
    ]

    equity = get_account_equity()
    if equity:
        lines += [f"**Account Equity:** ${equity:,.2f}", ""]

    if sync_actions:
        lines += ["## Sync Actions", ""]
        for a in sync_actions:
            lines.append(f"- {a}")
        lines.append("")

    if not today_trades:
        lines += ["## Trades", "", "No closed trades recorded today.", ""]
    else:
        lines += [
            "## Trades",
            "",
            "| # | Symbol | Setup | Cup | Entry | Exit | Shares | P&L | R | Outcome | Duration |",
            "|---|--------|-------|-----|-------|------|--------|-----|---|---------|----------|",
        ]
        for i, t in enumerate(today_trades, 1):
            cup = "Y" if t.get("cup") else "-"
            lines.append(
                f"| {i} | {t['symbol']} | {t['setup']} | {cup} "
                f"| ${t['entry_price']:.2f} | ${t['exit_price']:.2f} "
                f"| {t['position_size']} "
                f"| ${t['realized_pnl']:+.2f} | {t['r_multiple']:+.2f}R "
                f"| {t['outcome']} | {t.get('duration_min', '?')} min |"
            )
        lines.append("")

    if metrics.get("total", 0) > 0:
        lines += [
            "## Day Metrics",
            "",
            f"- **Trades:** {metrics['total']}  "
            f"({metrics['wins']}W / {metrics['losses']}L / {metrics.get('scratches', 0)}S)",
            f"- **Win Rate:** {metrics['win_rate']:.0%}",
            f"- **Total P&L:** ${metrics['total_pnl']:+.2f}",
            f"- **Avg R:** {metrics['avg_r']:+.2f}R",
            f"- **Best R:** {metrics['best_r']:+.2f}R",
            f"- **Worst R:** {metrics['worst_r']:+.2f}R",
            f"- **Profit Factor:** {metrics['profit_factor']}",
            "",
        ]

        if metrics.get("by_setup"):
            lines += ["### By Setup", ""]
            for setup, sv in metrics["by_setup"].items():
                lines.append(
                    f"- **{setup}:** {sv['trades']} trades | "
                    f"P&L ${sv['pnl']:+.2f} | Avg R {sv['avg_r']:+.2f}R"
                )
            lines.append("")

    lines += ["## Notes / Improvement Flags", ""]
    flags = []
    for t in today_trades:
        if t["r_multiple"] < -0.8:
            flags.append(f"- {t['symbol']}: stopped out at {t['r_multiple']:+.2f}R — review entry timing")
        if t.get("duration_min") and t["duration_min"] < 5:
            flags.append(f"- {t['symbol']}: very fast exit ({t['duration_min']} min) — whipsaw risk")
        if t.get("exit_reason", "").startswith("Reconstructed"):
            flags.append(f"- {t['symbol']}: exit NOT auto-logged — execution log gap at entry")

    if not flags:
        flags = ["- No red flags today."]
    lines += flags
    lines.append("")

    return "\n".join(lines)


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City daily recap")
    parser.add_argument("--date",    default=datetime.now(CT).strftime("%Y-%m-%d"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    date = args.date
    print(f"\n{'='*60}")
    print(f"TRI-CITY DAILY RECAP — {date}")
    print(f"{'='*60}")

    fills = get_today_fills(date)
    print(f"Alpaca fills found: {len(fills)}")

    sync_actions = []
    sync_actions += sync_missing_entries(fills, date, args.dry_run)
    sync_actions += sync_missing_exits(fills, date, args.dry_run)

    for a in sync_actions:
        print(f"  {a}")

    report = build_report(date, sync_actions)

    if not args.dry_run:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report_path = REPORTS_DIR / f"daily-{date}-tricty.md"
        report_path.write_text(report)
        print(f"\nReport saved: {report_path}")

    print("\n" + report)


if __name__ == "__main__":
    main()
