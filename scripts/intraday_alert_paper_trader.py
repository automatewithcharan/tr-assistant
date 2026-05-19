"""Intraday Alert Paper Trader — Tier C and Strong Buy Now only.

Mechanical paper-trading of the SUBSET of alerts that has positive expectancy:
  - Tier C anomaly alerts (from anomaly_history.csv)
  - Strong Buy Now alerts (from anomaly_history.csv, Action contains 'Strong Buy Now'
    or from the SignalMonitor 🚀 alerts; both go to the same history file)

STRICT RULES (frozen, no tuning):
  1. ONLY Tier C and Strong Buy Now signals (skip A, B, regular Buy Now)
  2. Entry at the alert's PriceNow
  3. Target = TakeProfit1 from the alert (mechanical exit when intraday high >= T1)
  4. Stop = entry × 0.97  (tight −3% stop, OVERRIDE the alert's wider stop)
  5. EOD force-close at 3:20 PM IST if still open (NO overnight holds)
  6. Position size = ₹50,000 each, max 4 concurrent positions
  7. Skip alerts that fire AFTER 14:00 IST (no time for T1 to hit)
  8. Skip duplicate (same symbol, already active today)

This is the measurement experiment to determine if the Tier C / Strong Buy Now
subset really delivers 15%+/month forward, or if that intuition was small-sample.

Run via cron after market close — process the full day's alerts at once.
"""

from __future__ import annotations
import argparse, json, os, sys
from datetime import datetime, time as dtime, date as ddate
from pathlib import Path
import pandas as pd
import numpy as np

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"
ANOMALY_CSV = RESULTS / "anomaly_history.csv"
JOURNAL_PATH = RESULTS / "intraday_alert_paper" / "journal.json"
TRADES_CSV = RESULTS / "intraday_alert_paper" / "trades.csv"
INTRADAY_DIR = RESULTS / "kite_history_backfill" / "5minute" / "2024-05-13_2026-05-12"
DAY_CACHE = RESULTS / "kite_history_cache" / "day"
LIVE_INTRADAY_CACHE = RESULTS / "kite_history_cache" / "5minute"

POSITION_SIZE = 50000.0
MAX_CONCURRENT = 4
STOP_PCT = 3.0           # tighter than alert's 5%
MAX_ENTRY_TIME = dtime(14, 0)  # IST — skip alerts after 2 PM
EOD_FORCE_EXIT = dtime(15, 20)  # IST


def load_journal() -> dict:
    if JOURNAL_PATH.exists():
        return json.loads(JOURNAL_PATH.read_text())
    return {
        "config": {
            "strategy": "intraday_alerts|tier_c+strong_buy_now|t=alert_t1|s=3%|hold=eod_only",
            "position_size_inr": POSITION_SIZE,
            "max_concurrent": MAX_CONCURRENT,
            "stop_pct": STOP_PCT,
            "max_entry_time_ist": "14:00",
            "eod_force_exit_ist": "15:20",
            "started": ddate.today().isoformat(),
        },
        "trades": [],
        "daily_log": [],
    }


def save_journal(j: dict):
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL_PATH.write_text(json.dumps(j, indent=2, default=str))


def load_intraday_bars(symbol: str, target_date: ddate) -> pd.DataFrame:
    """Load 5-min bars for a symbol on target_date. Try live cache first, then backfill."""
    target_str = target_date.isoformat()
    # Try live 5-min cache (rolling, has today's data after market hours)
    live = LIVE_INTRADAY_CACHE / f"{symbol}.csv"
    if live.exists():
        df = pd.read_csv(live)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["date"] = df["datetime"].dt.date.astype(str)
        d = df[df["date"] == target_str]
        if not d.empty:
            return d
    # Fall back to backfill (only goes through 2026-05-12)
    bf = INTRADAY_DIR / f"{symbol}.csv"
    if bf.exists():
        df = pd.read_csv(bf)
        df["datetime"] = pd.to_datetime(df["datetime"])
        df["date"] = df["datetime"].dt.date.astype(str)
        d = df[df["date"] == target_str]
        if not d.empty:
            return d
    return pd.DataFrame()


def load_daily_bar(symbol: str, target_date: ddate) -> dict | None:
    """Fallback when no 5-min data — use day high/low/close from kite_history_cache."""
    fp = DAY_CACHE / f"{symbol}.csv"
    if not fp.exists():
        return None
    df = pd.read_csv(fp)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["date"] = df["datetime"].dt.date.astype(str)
    d = df[df["date"] == target_date.isoformat()]
    if d.empty:
        return None
    r = d.iloc[0]
    return {
        "open": float(r["open"]), "high": float(r["high"]),
        "low": float(r["low"]), "close": float(r["close"]),
    }


def simulate_trade(alert: dict, target_date: ddate) -> dict:
    """Given an alert, simulate the paper trade outcome using intraday bars.

    Returns: trade dict with entry/exit/return/reason.
    """
    sym = alert["Symbol"]
    entry_px = float(alert["PriceNow"])
    alert_time_str = alert["When"]
    alert_t1 = float(alert["TakeProfit1"]) if pd.notna(alert.get("TakeProfit1")) else entry_px * 1.05
    stop_px = entry_px * (1 - STOP_PCT / 100)

    qty = int(POSITION_SIZE / entry_px) if entry_px > 0 else 0
    if qty == 0:
        return {"skipped": True, "reason": "position_size_too_small_for_price"}

    # Load intraday bars
    bars = load_intraday_bars(sym, target_date)
    if bars.empty:
        # Fallback: use daily bar (less accurate but functional)
        day = load_daily_bar(sym, target_date)
        if day is None:
            return {"skipped": True, "reason": "no_data_for_outcome"}
        # Worst-case attribution: stop checked before target
        if day["low"] <= stop_px:
            exit_px, reason = stop_px, "stop"
        elif day["high"] >= alert_t1:
            exit_px, reason = alert_t1, "target_t1"
        else:
            exit_px, reason = day["close"], "eod_close"
        gross = (exit_px / entry_px - 1) * 100
        return {
            "skipped": False, "symbol": sym, "tier": alert.get("Tier"),
            "alert_time": alert_time_str, "entry_price": round(entry_px, 4),
            "exit_price": round(exit_px, 4), "target_t1": round(alert_t1, 4),
            "stop": round(stop_px, 4), "qty": qty,
            "gross_return_pct": round(gross, 4),
            "cost_pct": 0.10,  # Zerodha intraday: ₹20 + STT/GST/slippage ~0.10%
            "net_return_pct": round(gross - 0.10, 4),
            "exit_reason": reason, "data_source": "daily_bar_fallback",
        }

    # Use 5-min bars: scan after alert_time, find first stop/target hit, else EOD close
    bars = bars.sort_values("datetime").reset_index(drop=True)
    alert_dt = pd.to_datetime(alert_time_str)
    # Find first bar at or after alert time
    after = bars[bars["datetime"] >= alert_dt]
    if after.empty:
        return {"skipped": True, "reason": "no_bars_after_alert_time"}

    exit_px = None; reason = None
    for _, b in after.iterrows():
        hi, lo = float(b["high"]), float(b["low"])
        if lo <= stop_px:
            exit_px, reason = stop_px, "stop"
            break
        if hi >= alert_t1:
            exit_px, reason = alert_t1, "target_t1"
            break
    if exit_px is None:
        exit_px = float(after.iloc[-1]["close"])
        reason = "eod_close"

    gross = (exit_px / entry_px - 1) * 100
    return {
        "skipped": False, "symbol": sym, "tier": alert.get("Tier"),
        "alert_time": alert_time_str, "entry_price": round(entry_px, 4),
        "exit_price": round(exit_px, 4), "target_t1": round(alert_t1, 4),
        "stop": round(stop_px, 4), "qty": qty,
        "gross_return_pct": round(gross, 4),
        "cost_pct": 0.10,
        "net_return_pct": round(gross - 0.10, 4),
        "exit_reason": reason, "data_source": "5min_bars",
    }


def process_day(target_date: ddate) -> dict:
    """Process all eligible alerts for one trading day."""
    if not ANOMALY_CSV.exists():
        return {"date": target_date.isoformat(), "error": "no_anomaly_history"}

    j = load_journal()
    seen_trade_keys = {
        f"{t['date']}::{t.get('symbol', '?')}::{t.get('alert_time', '?')}"
        for t in j["trades"] if t.get("symbol")
    }

    df = pd.read_csv(ANOMALY_CSV)
    df_today = df[df["Date"] == target_date.isoformat()].copy()
    if df_today.empty:
        j["daily_log"].append({"date": target_date.isoformat(), "alerts_total": 0, "eligible": 0, "executed": 0})
        save_journal(j)
        return {"date": target_date.isoformat(), "alerts_total": 0}

    # Filter: Tier C OR Action contains "Strong Buy Now"
    eligible = df_today[
        (df_today["Tier"] == "C") |
        (df_today["Action"].astype(str).str.contains("Strong Buy Now", case=False, na=False))
    ].copy()

    # Parse alert time, skip alerts after 14:00 IST
    eligible["alert_dt"] = pd.to_datetime(eligible["When"])
    eligible = eligible[eligible["alert_dt"].dt.time < MAX_ENTRY_TIME]

    # De-dup: first alert per symbol
    eligible = eligible.sort_values("alert_dt").drop_duplicates(subset=["Symbol"], keep="first")

    executed = []
    skipped_concurrent = 0
    today_str = target_date.isoformat()
    today_active = sum(1 for t in j["trades"] if t.get("date") == today_str and t.get("skipped") is False)

    for _, alert in eligible.iterrows():
        # Concurrent cap (across the day; we don't model intraday rotation since EOD-force-exit)
        if today_active >= MAX_CONCURRENT:
            skipped_concurrent += 1
            continue
        key = f"{today_str}::{alert['Symbol']}::{alert['When']}"
        if key in seen_trade_keys:
            continue

        outcome = simulate_trade(alert.to_dict(), target_date)
        trade_record = {
            "date": today_str, "trade_id": f"IA{len(j['trades'])+1:05d}",
            **outcome,
        }
        j["trades"].append(trade_record)
        seen_trade_keys.add(key)
        if not outcome.get("skipped"):
            today_active += 1
            executed.append(trade_record)

    # Daily log
    j["daily_log"].append({
        "date": today_str,
        "alerts_total": int(len(df_today)),
        "tier_c_alerts": int((df_today["Tier"] == "C").sum()),
        "strong_buy_alerts": int(df_today["Action"].astype(str).str.contains("Strong Buy Now", case=False, na=False).sum()),
        "eligible_after_time_filter": int(len(eligible)),
        "executed": int(len(executed)),
        "skipped_concurrent_cap": int(skipped_concurrent),
    })
    save_journal(j)

    # Export CSV
    trades_df = pd.DataFrame([t for t in j["trades"] if not t.get("skipped")])
    TRADES_CSV.parent.mkdir(parents=True, exist_ok=True)
    trades_df.to_csv(TRADES_CSV, index=False)

    return {
        "date": today_str, "alerts_total": int(len(df_today)),
        "eligible": int(len(eligible)), "executed": int(len(executed)),
        "trades": executed,
    }


def print_stats(j: dict):
    if not j["trades"]:
        print("[i] No trades yet.")
        return
    trades_df = pd.DataFrame([t for t in j["trades"] if not t.get("skipped")])
    if trades_df.empty:
        print("[i] No executed trades yet.")
        return
    n = len(trades_df)
    wins = trades_df[trades_df["net_return_pct"] > 0]
    avg = trades_df["net_return_pct"].mean()
    total = trades_df["net_return_pct"].sum()
    win_rate = len(wins) / n * 100

    print(f"\n=== INTRADAY ALERT PAPER TRADER — STATS ===")
    print(f"  Config: {j['config']['strategy']}")
    print(f"  Started: {j['config']['started']}")
    print(f"  Total trades:    {n}")
    print(f"  Win rate:        {win_rate:.1f}%")
    print(f"  Avg net/trade:   {avg:+.3f}%")
    print(f"  Cumulative net:  {total:+.2f}%  (sequential, 1-position rotated)")
    print(f"\n  By exit reason:")
    print(trades_df.groupby("exit_reason")["net_return_pct"].agg(["count", "mean"]).round(3).to_string())
    print(f"\n  By tier:")
    if "tier" in trades_df.columns:
        print(trades_df.groupby("tier")["net_return_pct"].agg(["count", "mean"]).round(3).to_string())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--backfill-days", type=int, default=0, help="Replay this many days back (for seeding)")
    ap.add_argument("--stats", action="store_true", help="Print stats then exit")
    args = ap.parse_args()

    if args.stats:
        print_stats(load_journal())
        return 0

    if args.backfill_days > 0:
        # Replay past N days
        df = pd.read_csv(ANOMALY_CSV)
        df["Date"] = pd.to_datetime(df["Date"]).dt.date
        recent_dates = sorted(set(df["Date"].tolist()))[-args.backfill_days:]
        for d in recent_dates:
            res = process_day(d)
            print(f"[{d}] alerts={res.get('alerts_total', 0)} executed={res.get('executed', 0)}")
        print_stats(load_journal())
        return 0

    target = ddate.fromisoformat(args.date) if args.date else ddate.today()
    res = process_day(target)
    print(f"[{target}] alerts={res.get('alerts_total', 0)} eligible={res.get('eligible', 0)} executed={res.get('executed', 0)}")
    for t in res.get("trades", []):
        print(f"  {t['symbol']:12} entry=₹{t['entry_price']:>8} exit=₹{t.get('exit_price','?'):>8} net={t.get('net_return_pct','?'):+.2f}%  reason={t.get('exit_reason')}")
    print_stats(load_journal())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
