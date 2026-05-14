#!/usr/bin/env python3
"""
TRI-CITY EXECUTE — Auto-execute Tri-City scanner signals via Alpaca.

Called by the Claude Tri-City cron when a qualifying setup is detected from
the TradingView Tri-City Inator scanner table.

Setup types:
    BREAKOUT     — above ORH, high vol, EMA stack confirmed, RSI > 50
    CONTINUATION — above ORH, EMA dev 0–1%, RSI 50–65 (pullback into ORH area)
    PULLBACK     — above EMA mid, dev -0.5–0.8%, RSI 38–55 (intraday dip entry)

Guards (in order):
  1. already_executed_today   — no duplicate setups per symbol per day
  2. already_in_position      — no duplicate symbols
  3. max_positions             — no more than MAX_POSITIONS open at once (default: 3)
  4. daily_loss_limit          — circuit breaker if down MAX_DAILY_LOSS (default: -$300)
  5. time_window               — no new entries after NO_ENTRY_AFTER (default: 1:00 PM CT)
  6. market_regime             — block LONGs if SPY down > SPY_BEAR_THRESHOLD (default: -1.5%)
  7. rvol                      — require RVol >= MIN_RVOL vs 20-day avg (default: 1.5x)

Position structure (50-25-25 scale-out):
    T1 (+T1_PCT%): sell 50% → move stop to breakeven
    T2 (+T2_PCT%): sell 25% → lock at T2
    T3 (+T3_PCT%): trail 25% → position manager trails on EMA/VWAP breach or EOD close

Usage:
    python -W ignore scripts/tri_city_execute.py \\
        --symbol NVDA --price 142.50 --orh 140.00 --orl 136.00 \\
        --rsi 62.0 --ema_dev 0.35 --signal "BREAKOUT" --setup BREAKOUT

    python -W ignore scripts/tri_city_execute.py \\
        --symbol NVDA --price 142.50 --orh 140.00 --orl 136.00 \\
        --rsi 62.0 --ema_dev 0.35 --signal "BREAKOUT" --setup BREAKOUT --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta
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

from managers.trade_executor import execute_tri_city_trade, get_open_positions

CT       = ZoneInfo("America/Chicago")
LOG_FILE = WORKSPACE / "logs" / "tri-city-executions.json"

STOP_OFFSET = 0.13   # 13 cents below ORH (all setup types)

# ── Guard parameters (all overridable via .env) ───────────────────────────────
MAX_POSITIONS      = int(os.getenv("MAX_POSITIONS",        "3"))
MAX_DAILY_LOSS     = float(os.getenv("MAX_DAILY_LOSS",     "-300"))
MIN_RVOL           = float(os.getenv("MIN_RVOL",           "1.5"))
SPY_BEAR_THRESHOLD = float(os.getenv("SPY_BEAR_THRESHOLD", "-1.5"))
_no_entry_hour     = int(os.getenv("NO_ENTRY_HOUR",        "13"))
_no_entry_minute   = int(os.getenv("NO_ENTRY_MINUTE",      "0"))
NO_ENTRY_AFTER     = (_no_entry_hour, _no_entry_minute)
RVOL_LOOKBACK      = int(os.getenv("RVOL_LOOKBACK",        "20"))

# ── Position sizing ───────────────────────────────────────────────────────────
RISK_PCT      = float(os.getenv("RISK_PCT",    "2.0"))    # % of equity to risk
STOP_PCT      = float(os.getenv("STOP_PCT",    "5.0"))    # fallback stop % if ORH not usable
T1_PCT        = float(os.getenv("T1_PCT",      "10.0"))   # T1 take-profit %
T2_PCT        = float(os.getenv("T2_PCT",      "20.0"))   # T2 take-profit %
T3_PCT        = float(os.getenv("T3_PCT",      "30.0"))   # T3 take-profit % (trailed)
FIXED_RISK    = float(os.getenv("FIXED_RISK",  "150"))    # fallback if equity unavailable

ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


# ── Execution log ──────────────────────────────────────────────────────────────

def load_log() -> list:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            pass
    return []


def save_log(entries: list):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(json.dumps(entries, indent=2, default=str))


# ── Guard 1 & 2: duplicate checks ─────────────────────────────────────────────

def already_executed_today(symbol: str, setup: str) -> bool:
    today = datetime.now(CT).strftime("%Y-%m-%d")
    for e in load_log():
        if (e.get("symbol") == symbol and e.get("setup") == setup
                and e.get("date") == today):
            logger.info(f"Already executed {setup} on {symbol} today.")
            return True
    return False


def already_in_position(symbol: str) -> bool:
    positions = get_open_positions()
    if symbol in [p["ticker"] for p in positions]:
        logger.info(f"Already holding {symbol}.")
        return True
    return False


# ── Guard 3: max positions ─────────────────────────────────────────────────────

def check_max_positions() -> bool:
    positions = get_open_positions()
    if len(positions) >= MAX_POSITIONS:
        logger.info(f"Max positions: {len(positions)}/{MAX_POSITIONS} open.")
        return True
    return False


# ── Guard 4: daily loss limit ──────────────────────────────────────────────────

def check_daily_loss_limit(today: str) -> bool:
    try:
        from managers.trade_journal import get_all_trades
        trades = get_all_trades()
        today_pnl = sum(
            t["realized_pnl"] for t in trades
            if t.get("date") == today and t.get("status") == "closed"
        )
        if today_pnl <= MAX_DAILY_LOSS:
            logger.warning(
                f"Daily loss limit hit: ${today_pnl:.2f} "
                f"(limit ${MAX_DAILY_LOSS:.0f}). No new entries."
            )
            return True
    except Exception as e:
        logger.warning(f"check_daily_loss_limit: {e}")
    return False


# ── Guard 5: time window ───────────────────────────────────────────────────────

def check_time_window(now: datetime) -> bool:
    cutoff = now.replace(
        hour=NO_ENTRY_AFTER[0], minute=NO_ENTRY_AFTER[1],
        second=0, microsecond=0
    )
    if now >= cutoff:
        logger.info(f"Past entry cutoff {NO_ENTRY_AFTER[0]:02d}:{NO_ENTRY_AFTER[1]:02d} CT.")
        return True
    return False


# ── Guard 6: SPY market regime ─────────────────────────────────────────────────

def get_spy_regime() -> tuple[str, float | None]:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return "UNKNOWN", None
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import StockSnapshotRequest
        client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
        snap = client.get_stock_snapshot(StockSnapshotRequest(symbol_or_symbols="SPY"))
        s = snap.get("SPY")
        if not s or not s.daily_bar or not s.prev_daily_bar:
            return "UNKNOWN", None
        prev   = float(s.prev_daily_bar.close)
        curr   = float(s.daily_bar.close)
        change = round((curr - prev) / prev * 100, 2)
        if change <= SPY_BEAR_THRESHOLD:
            return "BEAR", change
        elif change >= 0.5:
            return "BULL", change
        return "NEUTRAL", change
    except Exception as e:
        logger.warning(f"get_spy_regime: {e}")
        return "UNKNOWN", None


# ── Guard 7: relative volume ───────────────────────────────────────────────────

def get_rvol(symbol: str) -> float | None:
    """
    RVol = today's cumulative volume / (20-day avg daily vol × fraction of day elapsed).
    Uses yfinance for historical avg (free tier); Alpaca snapshot for today's volume.
    """
    try:
        import yfinance as yf
        now = datetime.now(CT)
        df = yf.download(symbol, period=f"{RVOL_LOOKBACK * 2}d", interval="1d",
                         progress=False, auto_adjust=True)
        if df.empty:
            return None
        if hasattr(df.columns, "levels"):
            df.columns = df.columns.get_level_values(0)
        df.columns = [c.lower() for c in df.columns]

        today_str = now.strftime("%Y-%m-%d")
        today_row = df[df.index.strftime("%Y-%m-%d") == today_str]["volume"]
        today_vol = float(today_row.iloc[-1]) if not today_row.empty else None

        hist = df[df.index.strftime("%Y-%m-%d") != today_str]["volume"].dropna()
        if today_vol is None or hist.empty:
            return None

        hist_vols    = list(hist)[-RVOL_LOOKBACK:]
        avg_daily    = sum(hist_vols) / len(hist_vols)
        open_ct      = now.replace(hour=8, minute=30, second=0, microsecond=0)
        close_ct     = now.replace(hour=15, minute=0,  second=0, microsecond=0)
        total_sec    = (close_ct - open_ct).total_seconds()
        elapsed_sec  = max(60, (now - open_ct).total_seconds())
        fraction     = min(1.0, elapsed_sec / total_sec)
        expected     = avg_daily * fraction
        if expected <= 0:
            return None
        return round(today_vol / expected, 2)
    except Exception as e:
        logger.warning(f"get_rvol {symbol}: {e}")
        return None


# ── Account equity ─────────────────────────────────────────────────────────────

def get_account_equity() -> float | None:
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return None
    try:
        from alpaca.trading.client import TradingClient
        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        return float(client.get_account().equity)
    except Exception as e:
        logger.warning(f"get_account_equity: {e}")
        return None


# ── Signal calculation ─────────────────────────────────────────────────────────

def calculate_signal(symbol: str, price: float, orh: float, setup: str) -> dict:
    """
    Build trade signal with stop, targets, and position size.

    Stop placement:
        BREAKOUT / CONTINUATION — 13 cents below ORH (key breakout level)
        PULLBACK                — 13 cents below ORH if price is within 2% above ORH;
                                  otherwise percentage-based stop (STOP_PCT%)

    Position sizing: risk RISK_PCT% of account equity. Falls back to FIXED_RISK.
    """
    # Stop
    pct_above_orh = (price - orh) / orh * 100 if orh > 0 else 999
    if setup in ("BREAKOUT", "CONTINUATION") or (setup == "PULLBACK" and 0 < pct_above_orh <= 2.0):
        stop = round(orh - STOP_OFFSET, 2)
    else:
        stop = round(price * (1 - STOP_PCT / 100), 2)

    # Safety guard: stop must always be below entry
    if stop >= price:
        logger.warning(f"Stop ${stop} >= entry ${price} — falling back to {STOP_PCT}% below entry")
        stop = round(price * (1 - STOP_PCT / 100), 2)

    risk_per_share = round(price - stop, 4)
    if risk_per_share < 0.05:
        logger.warning(f"Risk per share too small ({risk_per_share:.2f}) — skipping trade")
        raise ValueError(f"Risk per share {risk_per_share:.2f} too small to size position safely")

    equity           = get_account_equity()
    max_risk_dollars = (equity * RISK_PCT / 100) if (equity and equity > 0) else FIXED_RISK
    position_size    = max(1, int(max_risk_dollars / risk_per_share))

    target_1 = round(price * (1 + T1_PCT / 100), 2)
    target_2 = round(price * (1 + T2_PCT / 100), 2)
    target_3 = round(price * (1 + T3_PCT / 100), 2)

    return {
        "ticker":        symbol,
        "entry_price":   price,
        "position_size": position_size,
        "stop_loss":     stop,
        "target_1":      target_1,
        "target_2":      target_2,
        "target_3":      target_3,
        "direction":     "BULLISH",
        "confidence":    1.0,
    }


# ── Execution log ──────────────────────────────────────────────────────────────

def log_execution(symbol: str, setup: str, signal: dict, result,
                  rvol: float | None = None,
                  spy_regime: str | None = None,
                  spy_change: float | None = None,
                  cup: bool = False):
    now = datetime.now(CT)
    risk_dollars = round(
        signal["position_size"] * (signal["entry_price"] - signal["stop_loss"]), 2
    )
    entry = {
        "date":          now.strftime("%Y-%m-%d"),
        "time":          now.strftime("%H:%M:%S CT"),
        "symbol":        symbol,
        "setup":         setup,
        "cup":           cup,
        "entry_price":   signal["entry_price"],
        "stop_loss":     signal["stop_loss"],
        "target_1":      signal["target_1"],
        "target_2":      signal["target_2"],
        "target_3":      signal["target_3"],
        "position_size": signal["position_size"],
        "risk_dollars":  risk_dollars,
        "rvol":          rvol,
        "spy_regime":    spy_regime,
        "spy_change":    spy_change,
        "success":       result.success,
        "order_id":      result.order_id,
        "error":         result.error,
    }
    log = load_log()
    log.append(entry)
    save_log(log)
    return entry


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Tri-City auto-executor")
    parser.add_argument("--symbol",   required=True)
    parser.add_argument("--price",    required=True, type=float)
    parser.add_argument("--orh",      required=True, type=float,
                        help="Opening range high from Tri-City scanner")
    parser.add_argument("--orl",      required=True, type=float,
                        help="Opening range low from Tri-City scanner")
    parser.add_argument("--rsi",      required=True, type=float)
    parser.add_argument("--ema_dev",  required=True, type=float,
                        help="EMA deviation %% from scanner table")
    parser.add_argument("--signal",   required=True,
                        help='Signal text from scanner, e.g. "BREAKOUT"')
    parser.add_argument("--setup",    required=True,
                        choices=["BREAKOUT", "CONTINUATION", "PULLBACK"])
    parser.add_argument("--cup",      action="store_true",
                        help="Cup pattern detected (high-conviction flag from scanner)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print signal without placing orders")
    parser.add_argument("--override-cutoff", action="store_true",
                        help="Allow entry past the time cutoff (user-confirmed late trade)")
    args = parser.parse_args()

    symbol = args.symbol.upper()
    now    = datetime.now(CT)
    today  = now.strftime("%Y-%m-%d")

    cup_tag = " + CUP 🏆" if args.cup else ""
    print("=" * 64)
    print(f"TRI-CITY EXECUTE — {now.strftime('%Y-%m-%d %H:%M CT')}")
    print(f"Symbol: {symbol} | Setup: {args.setup}{cup_tag} | Price: ${args.price:.2f}")
    print(f"ORH: ${args.orh:.2f} | ORL: ${args.orl:.2f} | "
          f"RSI: {args.rsi:.1f} | EMA Dev%: {args.ema_dev:+.2f}%")
    print("=" * 64)

    # ── Guard 1: already executed ──────────────────────────────────────────────
    if already_executed_today(symbol, args.setup):
        print(f"SKIP: {args.setup} already executed for {symbol} today.")
        sys.exit(0)

    # ── Guard 2: already in position ───────────────────────────────────────────
    if already_in_position(symbol):
        print(f"SKIP: Already holding {symbol}.")
        sys.exit(0)

    # ── Guard 3: max positions ─────────────────────────────────────────────────
    if check_max_positions():
        print(f"SKIP: Max positions ({MAX_POSITIONS}) already open.")
        sys.exit(0)

    # ── Guard 4: daily loss limit ──────────────────────────────────────────────
    if check_daily_loss_limit(today):
        print(f"SKIP: Daily loss limit (${MAX_DAILY_LOSS:.0f}) reached.")
        sys.exit(0)

    # ── Guard 5: time window ───────────────────────────────────────────────────
    if not args.dry_run and not getattr(args, "override_cutoff", False) and check_time_window(now):
        # Emit POST_CUTOFF_SIGNAL so Claude cron can alert the user
        signal_preview = calculate_signal(symbol, args.price, args.orh, args.setup)
        risk_per_share = round(args.price - signal_preview["stop_loss"], 2)
        print(
            f"POST_CUTOFF_SIGNAL | {symbol} | {args.setup} | "
            f"price={args.price} | orh={args.orh} | orl={args.orl} | "
            f"rsi={args.rsi} | ema_dev={args.ema_dev} | cup={args.cup} | "
            f"signal={args.signal} | stop={signal_preview['stop_loss']} | "
            f"risk_per_share={risk_per_share} | shares={signal_preview['position_size']} | "
            f"cutoff={NO_ENTRY_AFTER[0]:02d}:{NO_ENTRY_AFTER[1]:02d}CT"
        )
        sys.exit(0)

    # ── Guard 6: market regime ─────────────────────────────────────────────────
    spy_regime, spy_change = get_spy_regime()
    spy_str = f"{spy_change:+.2f}%" if spy_change is not None else "N/A"
    print(f"SPY regime: {spy_regime} ({spy_str})")
    if spy_regime == "BEAR" and not args.dry_run:
        print(f"SKIP: SPY bearish ({spy_str}) — blocking LONG entries.")
        sys.exit(0)

    # ── Guard 7: relative volume ───────────────────────────────────────────────
    rvol = get_rvol(symbol)
    rvol_str = f"{rvol:.2f}x" if rvol is not None else "N/A"
    print(f"RVol: {rvol_str} (min {MIN_RVOL:.1f}x)")
    if rvol is not None and rvol < MIN_RVOL and not args.dry_run:
        print(f"SKIP: RVol {rvol_str} below minimum {MIN_RVOL:.1f}x.")
        sys.exit(0)

    # ── Build signal ───────────────────────────────────────────────────────────
    signal       = calculate_signal(symbol, args.price, args.orh, args.setup)
    risk_dollars = round(
        signal["position_size"] * (signal["entry_price"] - signal["stop_loss"]), 2
    )

    print(f"\nSignal:")
    print(f"  Entry:  ${signal['entry_price']:.2f}")
    print(f"  Stop:   ${signal['stop_loss']:.2f}  "
          f"(${signal['entry_price'] - signal['stop_loss']:.2f}/share)")
    print(f"  T1:     ${signal['target_1']:.2f}  (+{T1_PCT:.0f}%) — sell 50%")
    print(f"  T2:     ${signal['target_2']:.2f}  (+{T2_PCT:.0f}%) — sell 25%")
    print(f"  T3:     ${signal['target_3']:.2f}  (+{T3_PCT:.0f}%) — trail 25%")
    print(f"  Size:   {signal['position_size']} shares")
    print(f"  Risk:   ${risk_dollars:.2f}")
    if args.cup:
        print(f"  Cup:    YES — high-conviction setup")

    if args.dry_run:
        print("\n[DRY RUN] — no order placed.")
        return

    # ── Execute ────────────────────────────────────────────────────────────────
    print("\nPlacing 50-25-25 bracket orders via Alpaca...")
    result = execute_tri_city_trade("tri_city", signal)

    if result.success:
        log_execution(symbol, args.setup, signal, result,
                      rvol=rvol, spy_regime=spy_regime, spy_change=spy_change,
                      cup=args.cup)
        print(f"\n✅ ORDERS PLACED")
        print(f"   Order ID: {result.order_id}")
        print(f"   Shares:   {result.shares_filled}")
        print(f"   Fill ~:   ${result.avg_fill_price:.2f}")
        print(f"   Logged:   {LOG_FILE}")
    else:
        print(f"\n❌ EXECUTION FAILED: {result.error}")
        log_execution(symbol, args.setup, signal, result,
                      rvol=rvol, spy_regime=spy_regime, spy_change=spy_change,
                      cup=args.cup)
        sys.exit(1)


if __name__ == "__main__":
    main()
