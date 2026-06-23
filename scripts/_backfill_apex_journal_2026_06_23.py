#!/usr/bin/env python3
"""
One-off journal backfill — 2026-06-23.

Three APEX exits were journaled BEFORE the exit-fill-accuracy fix (commit 1701722) by the
poller restart at 12:31 CT, so their P&L was booked on the pre-close health snapshot price
instead of the realized broker fill. This corrects those three entries to broker truth.

Source of truth: Alpaca FILL activities for 2026-06-23 (avg fill price per side):
  QH    buy 62 @ 8.60  / sell 62 @ 7.90              -> exit 7.90,  pnl -43.40 (-8.14%)
  AMDL  buy  7 @ 66.68 / sell 6@66.37 + 1@66.42      -> exit 66.38, pnl -2.12  (-0.45%)
  ARMG  buy 10 @ 44.04 / sell 10 @ 43.51             -> exit 43.51, pnl -5.30  (-1.20%)

AMKR (the 4th pre-fix exit) is intentionally untouched: it closed via the reconcile path,
which already used _last_exit_fill (broker truth). All post-fix exits (MXL/NUAI/ERAS/QTTB/
CLYM) were verified to already match broker fills.

Idempotent: an entry that already carries `exit_pre_backfill` is skipped. Originals are kept
in `exit_pre_backfill` / `pnl_pre_backfill` for audit. state.daily_pnl is realigned so the
journal sum and state agree (state resets on the next trading date regardless).

Run from the repo root. logs/ and shared/ are gitignored (runtime data) — this script is the
committed, reproducible record of the correction.
"""
import json
from pathlib import Path

DATE = "2026-06-23"
JOURNAL = Path("logs/apex-journal.json")
STATE = Path("shared/apex-state.json")

# Broker-truth corrections (entries were already correct; only the exit fill was wrong).
FIXES = {
    "QH":   {"exit": 7.90,  "pnl": -43.40, "gain_pct": -8.14},
    "AMDL": {"exit": 66.38, "pnl": -2.12,  "gain_pct": -0.45},
    "ARMG": {"exit": 43.51, "pnl": -5.30,  "gain_pct": -1.20},
}


def main() -> None:
    rows = json.loads(JOURNAL.read_text())
    changed, delta = [], 0.0
    for r in rows:
        if str(r.get("timestamp", ""))[:10] != DATE:
            continue
        s = r.get("symbol")
        if s in FIXES and "exit_pre_backfill" not in r:
            f = FIXES[s]
            delta += f["pnl"] - r.get("pnl", 0.0)
            r["exit_pre_backfill"] = r.get("exit")
            r["pnl_pre_backfill"] = r.get("pnl")
            r["exit"] = f["exit"]
            r["pnl"] = f["pnl"]
            r["gain_pct"] = f["gain_pct"]
            r["backfill_note"] = (
                "exit corrected to broker fill avg (pre-fix snapshot value); "
                f"backfilled {DATE}")
            changed.append(s)

    if not changed:
        print("Nothing to backfill (already corrected or entries absent).")
        return

    JOURNAL.write_text(json.dumps(rows, indent=2))
    print("corrected:", changed, "| daily_pnl delta:", round(delta, 2))

    st = json.loads(STATE.read_text())
    old = st.get("daily_pnl", 0.0)
    st["daily_pnl"] = round(old + delta, 2)
    STATE.write_text(json.dumps(st, indent=2))
    print(f"state.daily_pnl: {old} -> {st['daily_pnl']}")

    today_sum = round(sum(r.get("pnl", 0) for r in rows
                          if str(r.get("timestamp", ""))[:10] == DATE), 2)
    print("journal today sum:", today_sum, "| matches state:", today_sum == st["daily_pnl"])


if __name__ == "__main__":
    main()
