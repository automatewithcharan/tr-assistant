#!/usr/bin/env python3
"""
Sync tracked positions with actual Kite holdings.
Run this any time you suspect the bot's records are out of sync
(e.g. after manual sells, GTT triggers, or broker auto-squareoffs).

Usage:
    python scripts/sync_holdings.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT = Path(__file__).parent.parent


def _load_env():
    env_path = _ROOT / ".env.dev"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def main():
    _load_env()
    from pkscreener.classes.KiteOrderManager import KiteOrderManager

    mgr = KiteOrderManager()
    print("Fetching holdings from Kite and reconciling...\n")
    result = mgr.reconcile_with_broker()

    if result.get("error"):
        print(f"ERROR: {result['error']}")
        print("Make sure the Kite token is valid (run daily_prewarm.sh first).")
        return

    print(f"Broker holdings:    {result['broker_positions']}")
    print(f"Tracked (was open): {result['tracked_positions']}")
    print()

    if result["auto_closed"]:
        print(f"Closed {len(result['auto_closed'])} ghost position(s) — sold outside our system:")
        for sym in result["auto_closed"]:
            print(f"  ✅ {sym}")
        print()

    if result["mismatches"]:
        print("Qty mismatches (need manual review):")
        for m in result["mismatches"]:
            print(f"  ⚠️  {m}")
        print()

    if result["untracked"]:
        print("Positions at broker NOT in our system (bought outside the bot):")
        for sym in result["untracked"]:
            print(f"  ℹ️  {sym}")
        print()

    if not result["auto_closed"] and not result["mismatches"] and not result["untracked"]:
        print("✅ Everything in sync.")

    print("\nRun `python scripts/performance_monitor.py --no-discord` to see updated P&L.")


if __name__ == "__main__":
    main()
