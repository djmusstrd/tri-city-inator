#!/usr/bin/env python3
"""
TRADE JOURNAL — Records and measures every Tri-City trade.

Entry data comes from tri-city-executions.json (written by tri_city_execute.py).
Exit data written when close_position() is called.
Journal file: logs/tri-city-journal.json
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

WORKSPACE  = Path(__file__).parent.parent
JOURNAL    = WORKSPACE / "logs" / "tri-city-journal.json"
ENTRY_LOG  = WORKSPACE / "logs" / "tri-city-executions.json"
CT         = ZoneInfo("America/Chicago")

logger = logging.getLogger(__name__)


def _load(path: Path, default):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def _save(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str))


def _trade_id(symbol: str, setup: str, date: str) -> str:
    return f"{date}-{symbol}-{setup}"


def _outcome(r: float) -> str:
    if r >= 2.5:   return "full_win"
    if r >= 0.5:   return "partial_win"
    if r >= -0.3:  return "scratch"
    return "loss"


def log_exit(symbol: str, setup: str, date: str,
             exit_price: float, exit_reason: str, shares: int) -> dict | None:
    """Record an exit and compute P&L + R-multiple."""
    entries = _load(ENTRY_LOG, [])
    entry   = None
    for e in reversed(entries):
        if (e.get("symbol") == symbol and e.get("setup") == setup
                and e.get("date") == date and e.get("success")):
            entry = e
            break

    if not entry:
        logger.warning(f"journal.log_exit: no entry for {symbol}/{setup} on {date}")
        return None

    trade_id  = _trade_id(symbol, setup, date)
    journal   = _load(JOURNAL, [])
    existing  = next((t for t in journal if t["trade_id"] == trade_id), None)

    entry_price    = entry["entry_price"]
    stop_loss      = entry["stop_loss"]
    risk_per_share = round(entry_price - stop_loss, 4)
    realized_pnl   = round((exit_price - entry_price) * shares, 2)
    risk_dollars   = entry.get("risk_dollars", risk_per_share * shares)
    r_multiple     = round(realized_pnl / risk_dollars, 4) if risk_dollars else 0.0

    try:
        entry_dt     = datetime.strptime(
            f"{entry['date']} {entry['time'].replace(' CT', '')}",
            "%Y-%m-%d %H:%M:%S"
        )
        exit_dt      = datetime.now(CT).replace(tzinfo=None)
        duration_min = round((exit_dt - entry_dt).total_seconds() / 60, 1)
    except Exception:
        duration_min = None

    record = {
        "trade_id":       trade_id,
        "date":           date,
        "symbol":         symbol,
        "setup":          setup,
        "entry_time":     entry["time"],
        "entry_price":    entry_price,
        "stop_loss":      stop_loss,
        "target_1":       entry.get("target_1"),
        "target_2":       entry.get("target_2"),
        "target_3":       entry.get("target_3"),
        "position_size":  shares,
        "risk_dollars":   risk_dollars,
        "risk_per_share": risk_per_share,
        "entry_order_id": entry.get("order_id"),
        "exit_time":      datetime.now(CT).strftime("%H:%M:%S CT"),
        "exit_price":     round(exit_price, 4),
        "exit_reason":    exit_reason,
        "realized_pnl":   realized_pnl,
        "r_multiple":     r_multiple,
        "outcome":        _outcome(r_multiple),
        "duration_min":   duration_min,
        "status":         "closed",
    }

    if existing:
        journal = [record if t["trade_id"] == trade_id else t for t in journal]
    else:
        journal.append(record)

    _save(JOURNAL, journal)
    logger.info(
        f"✅ Journal: {symbol} {setup} | "
        f"P&L ${realized_pnl:+.2f} | {r_multiple:+.2f}R | {record['outcome']}"
    )
    return record


def fetch_exit_price(symbol: str) -> float | None:
    """Query Alpaca for most recent filled sell order for this symbol."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        key    = os.getenv("ALPACA_API_KEY")
        secret = os.getenv("ALPACA_SECRET_KEY")
        paper  = os.getenv("ALPACA_PAPER", "true").lower() == "true"
        if not key or not secret:
            return None

        client = TradingClient(key, secret, paper=paper)
        orders = client.get_orders(GetOrdersRequest(
            symbol=symbol,
            status=QueryOrderStatus.CLOSED,
            limit=10,
        ))
        for o in orders:
            if o.filled_avg_price and str(o.side).endswith("sell"):
                return float(o.filled_avg_price)
    except Exception as e:
        logger.warning(f"fetch_exit_price failed for {symbol}: {e}")
    return None


def get_all_trades() -> list:
    return _load(JOURNAL, [])


def get_open_entries(date: str | None = None) -> list:
    """Return execution log entries with no matching exit in the journal."""
    entries    = _load(ENTRY_LOG, [])
    journal    = _load(JOURNAL, [])
    closed_ids = {t["trade_id"] for t in journal if t.get("status") == "closed"}
    date       = date or datetime.now(CT).strftime("%Y-%m-%d")

    open_entries = []
    for e in entries:
        if not e.get("success"):
            continue
        tid = _trade_id(e["symbol"], e["setup"], e["date"])
        if tid not in closed_ids and e["date"] == date:
            open_entries.append(e)
    return open_entries


def compute_metrics(trades: list | None = None) -> dict:
    """Compute aggregate statistics across all closed trades."""
    trades  = trades or get_all_trades()
    closed  = [t for t in trades if t.get("status") == "closed"]

    if not closed:
        return {"total": 0}

    total_pnl    = sum(t["realized_pnl"] for t in closed)
    r_vals       = [t["r_multiple"] for t in closed]
    wins         = [t for t in closed if t["outcome"] in ("full_win", "partial_win")]
    losses       = [t for t in closed if t["outcome"] == "loss"]
    scratches    = [t for t in closed if t["outcome"] == "scratch"]
    gross_profit = sum(t["realized_pnl"] for t in wins)
    gross_loss   = abs(sum(t["realized_pnl"] for t in losses))
    profit_factor = round(gross_profit / gross_loss, 2) if gross_loss else float("inf")

    by_setup = {}
    for t in closed:
        s = t["setup"]
        by_setup.setdefault(s, {"trades": 0, "pnl": 0.0, "r": []})
        by_setup[s]["trades"] += 1
        by_setup[s]["pnl"]    += t["realized_pnl"]
        by_setup[s]["r"].append(t["r_multiple"])

    for s, v in by_setup.items():
        v["pnl"]   = round(v["pnl"], 2)
        v["avg_r"] = round(sum(v["r"]) / len(v["r"]), 3)
        del v["r"]

    return {
        "total":         len(closed),
        "wins":          len(wins),
        "losses":        len(losses),
        "scratches":     len(scratches),
        "win_rate":      round(len(wins) / len(closed), 3),
        "total_pnl":     round(total_pnl, 2),
        "avg_r":         round(sum(r_vals) / len(r_vals), 3),
        "best_r":        round(max(r_vals), 3),
        "worst_r":       round(min(r_vals), 3),
        "profit_factor": profit_factor,
        "gross_profit":  round(gross_profit, 2),
        "gross_loss":    round(gross_loss, 2),
        "by_setup":      by_setup,
    }
