#!/usr/bin/env python3
"""Unit tests for hardening batches A/D/E/F. Throwaway; monkeypatches broker + files so it
never touches live state or sends a real alert."""
import sys, tempfile, json
from pathlib import Path
sys.path.insert(0, "scripts")
import apex_health as H
import apex_execute as E
import apex_config as cfg
import apex_poller as P

fails = 0
def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond: fails += 1

ALERTS, JOURNAL = [], []
def patch_health(liquidate_ret=None, reprotect_ret=True):
    ALERTS.clear(); JOURNAL.clear()
    H.send_telegram = lambda m: ALERTS.append(m)
    H._journal_exit = lambda r: JOURNAL.append(r)
    if liquidate_ret is not None:
        # _liquidate now returns (status, fill_price); accept a bare status for convenience.
        ret = liquidate_ret if isinstance(liquidate_ret, tuple) else (liquidate_ret, None)
        H._liquidate = lambda s: ret
    H._reprotect = lambda s, p: reprotect_ret

# ── A. _close honors liquidation result ───────────────────────────────────────────
print("A. _close + liquidation verification")
# fail -> keep position, re-protect, alert, do NOT journal/pop
patch_health(liquidate_ret="fail")
st = {"daily_pnl": 0.0, "positions": {"FAILX": {"entry": 10.0, "stop": 9.5, "qty": 100, "health": 30}}}
H._close("FAILX", st["positions"]["FAILX"], {"price": 9.6, "health": 30}, "test", st, dry_run=False)
check("position KEPT on close fail", "FAILX" in st["positions"])
check("not journaled on fail", len(JOURNAL) == 0)
check("daily_pnl untouched on fail", st["daily_pnl"] == 0.0)
check("loud alert on fail", any("FAILED to close" in a for a in ALERTS))
# ok -> journal + pop
patch_health(liquidate_ret="ok")
st = {"daily_pnl": 0.0, "positions": {"OKX": {"entry": 10.0, "stop": 9.5, "qty": 100, "health": 30}}}
H._close("OKX", st["positions"]["OKX"], {"price": 11.0, "health": 30, "gain_pct": 10.0}, "test", st, dry_run=False)
check("position closed on ok", "OKX" not in st["positions"])
check("journaled on ok", len(JOURNAL) == 1 and JOURNAL[0]["pnl"] == 100.0)
# ok WITH a realized fill price -> P&L/exit booked on the fill, not the health snapshot
patch_health(liquidate_ret=("ok", 10.80))
st = {"daily_pnl": 0.0, "positions": {"FILLX": {"entry": 10.0, "stop": 9.5, "qty": 100, "health": 30}}}
H._close("FILLX", st["positions"]["FILLX"], {"price": 11.0, "health": 30, "gain_pct": 10.0}, "test", st, dry_run=False)
check("exit booked on realized fill (not snapshot)",
      JOURNAL[0]["exit"] == 10.8 and JOURNAL[0]["pnl"] == 80.0 and st["daily_pnl"] == 80.0)
# gone -> treat as closed
patch_health(liquidate_ret="gone")
st = {"daily_pnl": 0.0, "positions": {"GONEX": {"entry": 10.0, "stop": 9.5, "qty": 100, "health": 30}}}
H._close("GONEX", st["positions"]["GONEX"], {"price": 9.5, "health": 30, "gain_pct": -5.0}, "test", st, dry_run=False)
check("position closed on 'gone'", "GONEX" not in st["positions"])

# ── E. _confirm_order status handling ─────────────────────────────────────────────
print("E. _confirm_order")
class O:
    def __init__(s, status, fap=None, fqty=None): s.status=status; s.filled_avg_price=fap; s.filled_qty=fqty
class CE:
    def __init__(s, o): s.o=o
    def get_order_by_id(s, oid): return s.o
fp, fq, stt = E._confirm_order(CE(O("filled", "12.50", "30")), "x")
check("filled -> price+qty+status", fp == 12.5 and fq == 30 and stt == "filled")
fp, fq, stt = E._confirm_order(CE(O("rejected")), "x")
check("rejected -> status rejected, no price", stt == "rejected" and fp is None)
fp, fq, stt = E._confirm_order(CE(O("partially_filled", "12.40", "10")), "x")
check("partial -> usable price+qty", fp == 12.4 and fq == 10)

# ── D. atomic save_state round-trip ───────────────────────────────────────────────
print("D. atomic state write")
with tempfile.TemporaryDirectory() as td:
    cfg.STATE_FILE = Path(td) / "apex-state.json"
    data = {"date": "2026-06-22", "daily_pnl": -1.23, "positions": {"AAA": {"qty": 5}}, "executed_today": ["AAA"]}
    P.save_state(data)
    back = P.load_state()
    check("round-trips correctly", back == data)
    check("no leftover .tmp", not (Path(td) / "apex-state.json.tmp").exists())

# ── F. force_eod flattens a position with no live bars ────────────────────────────
print("F. force_eod final flatten")
patch_health()  # default _liquidate untouched; dry_run path won't call it
H.load_carry_decisions = lambda: {"approve": [], "deny": []}
st = {"daily_pnl": 0.0, "positions": {"EODX": {"entry": 10.0, "stop": 9.5, "qty": 100,
                                               "health": 70, "last_price": 10.5, "status": "intraday"}}}
n = H.manage_positions({}, st, "risk_on", dry_run=True, live_quotes=None, force_eod=True)
check("force_eod flattened (no bars)", "EODX" not in st["positions"] and n == 1)
check("flatten journaled", any("final EOD" in (j.get("reason") or "") for j in JOURNAL))
# without force_eod, the same no-bars position is left alone
st = {"daily_pnl": 0.0, "positions": {"KEEPX": {"entry": 10.0, "stop": 9.5, "qty": 100,
                                                "health": 70, "last_price": 10.5, "status": "intraday"}}}
n = H.manage_positions({}, st, "risk_on", dry_run=True, live_quotes=None, force_eod=False)
check("no-bars kept when not EOD", "KEEPX" in st["positions"])

# ── G. _ensure_gtc_stop: places GTC when missing, idempotent when one exists ──────
print("G. _ensure_gtc_stop (GTC-on-carry)")
class GOrder:
    def __init__(s, tif="day", otype="stop"): s.time_in_force=tif; s.order_type=otype; s.id="x"
class GClient:
    def __init__(s, opens): s._opens=opens; s.submitted=[]; s.cancelled=[]
    def get_orders(s, req): return s._opens
    def cancel_order_by_id(s, oid): s.cancelled.append(oid)
    def submit_order(s, req): s.submitted.append(req)
# no stop -> cancels nothing, places a GTC stop
gc = GClient([]); H._trading_client = lambda: gc
ok = H._ensure_gtc_stop("NEW", {"qty": 10, "stop": 9.0}, dry_run=False)
check("placed GTC when none existed", ok and len(gc.submitted) == 1 and "gtc" in str(getattr(gc.submitted[0], "time_in_force", "")).lower())
# existing DAY stop -> cancel it then place GTC
gc = GClient([GOrder(tif="day", otype="stop")]); H._trading_client = lambda: gc
H._ensure_gtc_stop("DAYX", {"qty": 10, "stop": 9.0}, dry_run=False)
check("DAY stop replaced (cancel+submit GTC)", len(gc.cancelled) == 1 and len(gc.submitted) == 1)
# already GTC -> idempotent, no cancel/submit
gc = GClient([GOrder(tif="gtc", otype="stop")]); H._trading_client = lambda: gc
H._ensure_gtc_stop("GTCX", {"qty": 10, "stop": 9.0}, dry_run=False)
check("idempotent when GTC already present", len(gc.cancelled) == 0 and len(gc.submitted) == 0)
# dry_run -> no-op
gc = GClient([]); H._trading_client = lambda: gc
check("dry_run no-op", H._ensure_gtc_stop("DRYX", {"qty": 10, "stop": 9.0}, dry_run=True) and not gc.submitted)

print("\nRESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
