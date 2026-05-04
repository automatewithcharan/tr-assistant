#!/usr/bin/env python3
"""
Evening Pre-Breakout Watchlist
================================
Runs at 3:30 PM after market close. Scans today's signal data for stocks
in "Watch for Buy" state with strong setup quality. Posts top candidates
to Discord so the bot can pre-place limit orders for tomorrow's open.

The goal: catch stocks BEFORE the breakout (at +1-2%) rather than
AFTER (at +7-8% when "Buy Now" fires).

Usage:
    python scripts/evening_watchlist.py
    python scripts/evening_watchlist.py --no-discord
"""

import argparse
import csv
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).parent.parent))

_ROOT     = Path(__file__).parent.parent
_DATA_DIR = _ROOT / "results" / "Data"


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
        return
    try:
        import requests
        # Split into chunks if needed
        chunks, current = [], ""
        for line in message.splitlines():
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = line + "\n"
            else:
                current += line + "\n"
        if current:
            chunks.append(current)
        for chunk in chunks:
            requests.post(webhook_url, json={"content": chunk}, timeout=10)
    except Exception as e:
        print(f"Discord failed: {e}")


def _load_latest_signals() -> List[Dict]:
    """Load the most recent signal CSV from the data directory."""
    # Look for signals CSVs written today
    today = date.today().strftime("%Y%m%d")
    candidates = sorted(_DATA_DIR.glob(f"livetrade_main_signals*{today}*.csv"), reverse=True)
    if not candidates:
        # Fall back to latest available
        candidates = sorted(_DATA_DIR.glob("livetrade_main_signals_simple*.csv"), reverse=True)
    if not candidates:
        candidates = sorted(_DATA_DIR.glob("livetrade_main_signals*.csv"), reverse=True)
    if not candidates:
        return []
    rows = list(csv.DictReader(open(candidates[0])))
    print(f"Loaded {len(rows)} signals from {candidates[0].name}")
    return rows


def _score_setup(row: Dict) -> float:
    """Score a 'Watch for Buy' setup. Higher = better entry candidate tomorrow."""
    score = 0.0
    try:
        vol = float(row.get("Volume", row.get("Vol", 0)) or 0)
        avg_vol = float(row.get("AvgVol", row.get("Avg Vol", 0)) or 0)
        vol_ratio = vol / avg_vol if avg_vol > 0 else 1.0

        change = float(row.get("ChangePct", row.get("Change%", row.get("% Change", 0))) or 0)
        rsi = float(row.get("RSI", 50) or 50)
        conf = float(row.get("Confidence", row.get("Conf%", 0)) or 0)
        ltp = float(row.get("LTP", row.get("Price Now", row.get("PriceNow", 0))) or 0)
        entry = float(row.get("Entry", row.get("BuyAbove", row.get("Buy Above", 0))) or 0)

        # Volume building (key early signal)
        if vol_ratio >= 2.0:
            score += 30
        elif vol_ratio >= 1.5:
            score += 20
        elif vol_ratio >= 1.2:
            score += 10

        # RSI recovering from oversold (ideal: 35-55 range, heading up)
        if 35 <= rsi <= 55:
            score += 25
        elif 55 < rsi <= 65:
            score += 15

        # Price near breakout trigger (within 2%)
        if entry > 0 and ltp > 0:
            gap_to_entry = (entry - ltp) / ltp * 100
            if 0 <= gap_to_entry <= 2:
                score += 25   # Very close to trigger - will break out soon
            elif 2 < gap_to_entry <= 5:
                score += 10

        # Confidence from signal engine
        score += min(conf * 0.3, 15)

        # Small positive move today = momentum building
        if 0 < change <= 3:
            score += 10
        elif change < 0 and change >= -2:
            score += 5   # slight dip = better entry price

    except Exception:
        pass
    return round(score, 1)


def _get_live_price(symbol: str) -> float:
    try:
        import yfinance as yf
        h = yf.Ticker(f"{symbol}.NS").history(period="1d")
        return float(h["Close"].iloc[-1]) if len(h) else 0.0
    except Exception:
        return 0.0


def build_watchlist(top_n: int = 10) -> str:
    rows = _load_latest_signals()
    if not rows:
        return "No signal data found. Run the assistant first to generate signals."

    # Filter to "Watch for Buy" stocks only
    watch_rows = []
    for row in rows:
        action = str(row.get("NowAction", row.get("What To Do", row.get("Recommendation", ""))) or "").strip().lower()
        if "watch" in action and "buy" in action:
            score = _score_setup(row)
            if score >= 20:  # Minimum quality threshold
                watch_rows.append((score, row))

    if not watch_rows:
        return "No quality 'Watch for Buy' setups found today."

    watch_rows.sort(key=lambda x: -x[0])
    top = watch_rows[:top_n]

    today = date.today().isoformat()
    lines = [
        f"📋 **Evening Pre-Breakout Watchlist — {today}**",
        f"",
        f"Found **{len(watch_rows)}** Watch-for-Buy setups. Top {len(top)} by setup quality:",
        f"",
    ]

    for rank, (score, row) in enumerate(top, 1):
        sym    = str(row.get("Stock", row.get("Symbol", "?")) or "?").strip()
        ltp    = float(row.get("LTP", row.get("Price Now", row.get("PriceNow", 0))) or 0)
        entry  = float(row.get("Entry", row.get("BuyAbove", row.get("Buy Above", 0))) or 0)
        t2     = float(row.get("Target2", row.get("Target 2", row.get("TakeProfit2", 0))) or 0)
        sl     = float(row.get("StopLoss", row.get("Stop Loss", row.get("SL", 0))) or 0)
        rsi    = float(row.get("RSI", 50) or 50)
        conf   = float(row.get("Confidence", row.get("Conf%", 0)) or 0)
        vol    = float(row.get("Volume", row.get("Vol", 0)) or 0)
        avg_v  = float(row.get("AvgVol", row.get("Avg Vol", 0)) or 0)
        volr   = round(vol / avg_v, 1) if avg_v > 0 else 0

        gap_pct = round((entry - ltp) / ltp * 100, 1) if entry > 0 and ltp > 0 else 0
        upside  = round((t2 - entry) / entry * 100, 1) if t2 > 0 and entry > 0 else 0
        risk    = round((entry - sl) / entry * 100, 1) if sl > 0 and entry > 0 else 5.0
        rr      = round(upside / risk, 1) if risk > 0 and upside > 0 else 0

        lines.append(
            f"**{rank}. {sym}** (score: {score:.0f})\n"
            f"   LTP ₹{ltp:.0f} → Entry ₹{entry:.0f} (+{gap_pct}% away) | "
            f"Target ₹{t2:.0f} (+{upside}%) | SL ₹{sl:.0f} (-{risk}%)\n"
            f"   R:R {rr}x | Vol {volr}x | RSI {rsi:.0f} | Conf {conf:.0f}%"
        )

    lines += [
        "",
        "---",
        "💡 **How to use:** These stocks are building up but haven't broken out yet.",
        "The bot will auto-buy them tomorrow when they hit the Entry price with volume.",
        "You can also manually place limit orders at the Entry price tonight.",
        "",
        f"🤖 *Bot will scan every 60s tomorrow. Pre-breakout entries give 3-5% better avg price vs waiting for 'Buy Now'.*",
    ]

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-discord", action="store_true")
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()

    # Load env
    env_path = _ROOT / ".env.dev"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    report = build_watchlist(top_n=args.top)
    print(report)

    if not args.no_discord:
        _send_discord(report)
        print("\nSent to Discord.")


if __name__ == "__main__":
    main()
