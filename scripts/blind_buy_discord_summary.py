#!/usr/bin/env python3
"""Build the daily blind-buy Discord summary (prints to stdout; the EOD wrapper posts it).

Shows the daily counts/P&L plus the actual PICKS and HOLDINGS so the feed is
useful to follow, not just numbers.
"""
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
JPATH = ROOT / "results" / "Data" / "blind_buy_alert_paper" / "journal.json"
ALERTS_CSV = ROOT / "results" / "Data" / "blind_buy_alerts.csv"


def latest_picks_line():
    """The actual blind-buy picks from the most recent alert day (shown even when
    the paper account is full and skips them — so the daily feed is never empty)."""
    if not ALERTS_CSV.exists():
        return None
    try:
        a = pd.read_csv(ALERTS_CSV)
    except Exception:
        return None
    if a.empty or "date" not in a.columns:
        return None
    last = a["date"].max()
    rows = a[a["date"] == last]
    picks = ", ".join(
        f"{r['symbol']} @₹{r.get('alert_price','?')} (SL ₹{r.get('stop','?')}, T1 ₹{r.get('target1','?')})"
        for _, r in rows.head(12).iterrows()
    )
    return f"\U0001f195 Latest blind-buy picks ({last}): {picks}"


def main():
    if not JPATH.exists():
        print("Blind-Buy Paper: no journal yet")
        return
    j = json.loads(JPATH.read_text())
    trades = j["trades"]
    closed = [t for t in trades if t.get("status") == "closed"]
    active = [t for t in trades if t.get("status") == "active"]
    pending = [t for t in trades if t.get("status") == "pending"]
    today = j["daily_log"][-1] if j.get("daily_log") else {}
    date = today.get("date", "?")
    ret_col = "blended_net_pct" if closed and "blended_net_pct" in closed[0] else "net_return_pct"

    def holding_line():
        if active:
            return "\U0001f4cc Holding: " + ", ".join(t["symbol"] for t in active[:15])
        return None

    print(f"\U0001f4c8 **Blind-Buy Paper — {date}**")
    if not closed:
        print(f"New: {today.get('new_alerts',0)} | Filled: {today.get('filled_today',0)} | Active: {len(active)}")
        for ln in (latest_picks_line(), holding_line()):
            if ln:
                print(ln)
        print("No closed trades yet.")
        return

    df = pd.DataFrame(closed)
    df[ret_col] = pd.to_numeric(df[ret_col], errors="coerce")
    n = len(df)
    avg = df[ret_col].mean()
    wr = (df[ret_col] > 0).mean() * 100
    df["_pos"] = pd.to_numeric(df.get("position_inr"), errors="coerce").fillna(50000)
    cum_inr = (df["_pos"] * df[ret_col] / 100.0).sum()
    capital = j.get("config", {}).get("total_capital_inr") or 200000
    ret_on_cap = cum_inr / capital * 100
    closed_today_n = today.get("closed_today", 0)
    today_pl_inr = sum(
        float(t.get("position_inr", 50000)) * float(t.get(ret_col, 0) or 0) / 100.0
        for t in trades
        if t.get("status") == "closed" and t.get("exit_date") == date
    )
    print(f"Today: filled={today.get('filled_today',0)}, closed={closed_today_n}, P&L ₹{today_pl_inr:+,.0f}")
    print(f"Pipeline: {len(pending)} pending | {len(active)} active")
    for ln in (latest_picks_line(), holding_line()):
        if ln:
            print(ln)
    print(f"All-time: {n} closed, WR {wr:.0f}%, avg {avg:+.2f}%/trade, "
          f"cum ₹{cum_inr:+,.0f} ({ret_on_cap:+.2f}% on ₹{capital/100000:.0f}L)")


if __name__ == "__main__":
    main()
