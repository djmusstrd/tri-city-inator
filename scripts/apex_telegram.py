#!/usr/bin/env python3
"""
APEX — two-way Telegram (carry deny/keep from your phone).

APEX alerts are one-way (sendMessage). This adds inbound handling so you can answer a carry
proposal from your phone — reply to the bot with:
    deny ARMG      keep ARMG      deny all      keep all
It records the exact same approve/deny decision the dashboard buttons write
(apex-carry-decisions.json), which the poller applies on its next EOD-window pass.

The poller calls poll_replies() each cycle (and runs a fast cadence in the EOD window so a reply
is actioned within ~a minute). Only ONE process per bot may consume getUpdates — that's the
poller here.
"""

from __future__ import annotations

import json
import logging
import urllib.request

import apex_config as cfg
from apex_health import record_carry_decision
from apex_rationale import send_telegram

logger = logging.getLogger("apex.telegram")
OFFSET_FILE = cfg.SHARED / "apex-telegram-offset.json"


def _get_offset() -> int:
    try:
        return int(json.loads(OFFSET_FILE.read_text()).get("offset", 0))
    except Exception:
        return 0


def _set_offset(o: int) -> None:
    try:
        OFFSET_FILE.write_text(json.dumps({"offset": o}))
    except Exception:
        pass


def poll_replies(state: dict | None = None) -> int:
    """Fetch new Telegram messages, apply any deny/keep commands. Returns # decisions recorded."""
    if not cfg.TELEGRAM_BOT_TOKEN:
        return 0
    offset = _get_offset()
    url = (f"https://api.telegram.org/bot{cfg.TELEGRAM_BOT_TOKEN}/getUpdates"
           f"?timeout=0&offset={offset + 1}&allowed_updates=%5B%22message%22%5D")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            data = json.loads(r.read())
    except Exception as e:
        logger.debug(f"getUpdates failed: {e}")
        return 0
    if not data.get("ok"):
        return 0

    recorded = 0
    for upd in data.get("result", []):
        _set_offset(upd["update_id"])
        msg = upd.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if cfg.TELEGRAM_CHAT_ID and chat_id != str(cfg.TELEGRAM_CHAT_ID):
            continue
        parts = (msg.get("text") or "").strip().split()
        if len(parts) < 2 or parts[0].lower() not in ("deny", "keep", "approve"):
            continue
        decision = "deny" if parts[0].lower() == "deny" else "approve"
        target = parts[1].upper()
        if target == "ALL":
            syms = [s for s, p in (state or {}).get("positions", {}).items()
                    if p.get("pending_deny")]
        else:
            syms = [target]
        for s in syms:
            record_carry_decision(s, decision)
            recorded += 1
        verb = "will flatten" if decision == "deny" else "kept overnight"
        send_telegram(f"✓ <b>{', '.join(syms) or '—'}</b> {verb} (carry {decision}).")
        if syms:
            logger.info(f"telegram reply: {decision} {syms}")
    return recorded


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    print("processed:", poll_replies())
