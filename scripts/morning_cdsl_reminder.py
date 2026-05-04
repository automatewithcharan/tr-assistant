#!/usr/bin/env python3
"""
Morning CDSL authorization reminder.
Runs at 9:15 AM IST. Checks if any open CNC positions exist that need
CDSL TPIN authorization before the bot can sell them today.
Posts a single actionable Discord message.

CDSL TPIN is a SEBI-mandated security step for selling delivery (CNC) shares.
The permanent fix is to sign DDPI with Zerodha — see message below.
"""
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

_ROOT     = Path(__file__).parent.parent
_LOG_PATH = _ROOT / "results" / "Data" / "kite_orders_log.csv"
_POS_PATH = _ROOT / "results" / "Data" / "kite_tracked_positions.json"


def _open_positions():
    """Return list of open CNC symbols from order log (FIFO)."""
    if not _LOG_PATH.exists():
        return []
    rows = [r for r in csv.DictReader(open(_LOG_PATH)) if r.get("success") == "True"]
    qty_map = defaultdict(float)
    for r in sorted(rows, key=lambda x: x.get("timestamp", "")):
        sym = r["symbol"]
        qty = float(r.get("qty") or 0)
        if r.get("side") == "BUY":
            qty_map[sym] += qty
        elif r.get("side") == "SELL":
            qty_map[sym] = max(0, qty_map[sym] - qty)
    return [sym for sym, qty in qty_map.items() if qty > 0]


def _send_discord(message: str):
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        env_path = _ROOT / ".env.dev"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("DISCORD_WEBHOOK_URL="):
                    webhook_url = line.split("=", 1)[1].strip()
                    break
    if not webhook_url:
        print("DISCORD_WEBHOOK_URL not set")
        return
    try:
        import requests
        requests.post(webhook_url, json={"content": message[:1900]}, timeout=10)
        print("Discord reminder sent.")
    except Exception as e:
        print(f"Discord failed: {e}")


def main():
    open_syms = _open_positions()
    today = date.today().isoformat()

    if not open_syms:
        print("No open positions — no CDSL reminder needed.")
        return

    syms_str = ", ".join(f"`{s}`" for s in open_syms)
    message = (
        f"☀️ **Morning checklist — {today}**\n\n"
        f"You have **{len(open_syms)} open CNC positions** that need CDSL TPIN authorization "
        f"before the bot can auto-sell them today:\n"
        f"{syms_str}\n\n"
        f"**Steps (2 minutes):**\n"
        f"1. Open Kite app or kite.zerodha.com\n"
        f"2. Go to **Holdings** → click **Authorize** (top right)\n"
        f"3. Select all holdings → enter your **CDSL TPIN** → submit\n\n"
        f"Once done, the bot can sell any of these positions automatically.\n\n"
        f"---\n"
        f"💡 **Permanent fix:** Sign **DDPI** (Demat Debit & Pledge Instruction) with Zerodha once — "
        f"after that, CNC sells never need TPIN again.\n"
        f"Apply at: zerodha.com → Account → Settings → DDPI"
    )

    print(message)
    _send_discord(message)


if __name__ == "__main__":
    main()
