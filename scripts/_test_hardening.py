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
        H._liquidate = lambda s: liquidate_ret
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

print("\nRESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
