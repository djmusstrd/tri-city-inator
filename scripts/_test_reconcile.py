#!/usr/bin/env python3
"""Unit test for reconcile_with_broker — drives every divergence branch with a fake broker.
Throwaway; run directly. Monkeypatches the broker client, journal, and telegram so it never
touches live state or sends a real alert."""
import sys
sys.path.insert(0, "scripts")
import apex_health as H

# ── fakes ───────────────────────────────────────────────────────────────────────
class FakePos:
    def __init__(self, symbol, qty, avg): self.symbol = symbol; self.qty = str(qty); self.avg_entry_price = str(avg)
class FakeOrder:
    def __init__(self, symbol, side="sell", fap=None): self.symbol = symbol; self.side = side; self.filled_avg_price = (str(fap) if fap else None); self.id = "o1"
class FakeClient:
    def __init__(self, pos, open_orders, closed_orders):
        self._pos, self._open, self._closed = pos, open_orders, closed_orders
        self.submitted = []
    def get_all_positions(self): return self._pos
    def get_orders(self, req):
        status = str(getattr(req, "status", "")).lower()
        if "open" in status: return self._open
        syms = getattr(req, "symbols", None)
        return [o for o in self._closed if (not syms or o.symbol in syms)]
    def cancel_order_by_id(self, oid): pass
    def submit_order(self, req):
        self.submitted.append(req)
        class _R: id = "newstop"
        return _R()

ALERTS = []
JOURNAL = []
def install(client):
    H._trading_client = lambda: client
    H.send_telegram = lambda m: ALERTS.append(m)
    H._journal_exit = lambda r: JOURNAL.append(r)

def reset(): ALERTS.clear(); JOURNAL.clear()

fails = 0
def check(name, cond):
    global fails
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond: fails += 1

# A. in-sync no-op
reset(); install(FakeClient([FakePos("AAA", 100, 10.0)], [FakeOrder("AAA")], []))
st = {"daily_pnl": 0.0, "positions": {"AAA": {"entry": 10.0, "stop": 9.5, "qty": 100}}}
n = H.reconcile_with_broker(st, dry_run=False)
print("A. in-sync"); check("0 fixes", n == 0); check("position kept", "AAA" in st["positions"]); check("no alerts", not ALERTS)

# B. phantom / stop-out: in state, gone at broker, closed sell fill @ 9.50
reset(); install(FakeClient([], [], [FakeOrder("LOSS", "sell", 9.50)]))
st = {"daily_pnl": 0.0, "positions": {"LOSS": {"entry": 10.0, "stop": 9.5, "qty": 100, "trigger": "ORB15"}}}
n = H.reconcile_with_broker(st, dry_run=False)
print("B. phantom/stop-out"); check("1 fix", n == 1); check("removed from state", "LOSS" not in st["positions"])
check("daily_pnl debited -50", st["daily_pnl"] == -50.0); check("journaled", len(JOURNAL) == 1 and JOURNAL[0]["pnl"] == -50.0)
check("alert fired", any("reconciled" in a.lower() for a in ALERTS))

# C. orphan: at broker, missing from state -> adopt + place a GTC protective stop
reset(); fc = FakeClient([FakePos("ORPH", 50, 4.0)], [], []); install(fc)
st = {"daily_pnl": 0.0, "positions": {}}
n = H.reconcile_with_broker(st, dry_run=False)
print("C. orphan"); check("1 fix", n == 1); check("adopted", "ORPH" in st["positions"])
check("qty/entry adopted", st["positions"].get("ORPH", {}).get("qty") == 50 and st["positions"]["ORPH"]["entry"] == 4.0)
check("stop below entry", 0 < st["positions"]["ORPH"]["stop"] < 4.0); check("orphan alert", any("ORPHAN" in a for a in ALERTS))
check("GTC stop placed on adopt", any("gtc" in str(getattr(r, "time_in_force", "")).lower() for r in fc.submitted))

# D. qty drift: state 100, broker 60
reset(); install(FakeClient([FakePos("DRIFT", 60, 5.0)], [FakeOrder("DRIFT")], []))
st = {"daily_pnl": 0.0, "positions": {"DRIFT": {"entry": 5.0, "stop": 4.7, "qty": 100}}}
n = H.reconcile_with_broker(st, dry_run=False)
print("D. qty drift"); check("1 fix", n == 1); check("synced to 60", st["positions"]["DRIFT"]["qty"] == 60)
check("drift alert", any("drift" in a.lower() for a in ALERTS))

# E. unprotected: held, no open order (alert-only)
reset(); install(FakeClient([FakePos("NAKED", 100, 8.0)], [], []))
st = {"daily_pnl": 0.0, "positions": {"NAKED": {"entry": 8.0, "stop": 7.6, "qty": 100}}}
n = H.reconcile_with_broker(st, dry_run=False)
print("E. unprotected"); check("0 fixes (alert-only)", n == 0); check("still held", "NAKED" in st["positions"])
check("UNPROTECTED alert", any("UNPROTECTED" in a for a in ALERTS))

# F. dry_run short-circuit
reset(); install(FakeClient([FakePos("X", 1, 1.0)], [], []))
st = {"daily_pnl": 0.0, "positions": {}}
n = H.reconcile_with_broker(st, dry_run=True)
print("F. dry_run"); check("no-op", n == 0 and not ALERTS and not st["positions"])

print("\nRESULT:", "ALL PASS" if fails == 0 else f"{fails} FAILED")
sys.exit(1 if fails else 0)
