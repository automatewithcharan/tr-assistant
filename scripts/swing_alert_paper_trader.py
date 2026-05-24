"""Swing alert paper trader — Grade-B + Tier C anomaly alerts as swing trades.

Empirical basis (analyzed 947 alerts over Apr-May 2026):
  - Intraday hold = NET LOSER (avg -0.28%)
  - 10-day swing hold = WINNER (Grade-B +1.00%, Tier C +1.40%)
  - Grade-A and Strong Buy Now LOSE money at all holding periods

This trader implements the winning rules:
  1. Take ONLY Grade-B + Tier C alerts (skip Grade-A and Strong Buy Now)
  2. Entry at NEXT day's open (no intraday chase)
  3. Target = entry × 1.12 (+12%)
  4. Stop = entry × 0.95 (-5%)
  5. Max hold = 10 trading days
  6. Position size = ₹50,000
  7. Max concurrent = 10 (₹5L total exposure)
  8. No new entries after market closes Friday for symbols not in cache

Run daily at 9:30 AM IST (after prewarm) — fills pending entries from
yesterday's alerts, then monitors active positions for exits.
"""

from __future__ import annotations
import argparse, json, sys, warnings
warnings.filterwarnings("ignore")
from datetime import datetime, date as ddate, timedelta
from pathlib import Path
import pandas as pd
import numpy as np

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"
ANOMALY_CSV = RESULTS / "anomaly_history.csv"
DAY_CACHE = RESULTS / "kite_history_cache" / "day"
DAY_BACKFILL = RESULTS / "kite_history_backfill" / "day"
JOURNAL_PATH = RESULTS / "swing_alert_paper" / "journal.json"
TRADES_CSV = RESULTS / "swing_alert_paper" / "trades.csv"

POSITION_INR = 50000.0
MAX_CONCURRENT = 4       # ₹2L max exposure (matches user's starting capital)
TARGET_PCT = 0           # 0 = no profit target — let winners run to max_hold close
STOP_PCT = 0             # 0 = no stop loss — empirically destroys edge (cuts winners
                         # that temporarily dip then recover, e.g. DSSL +24%/10d
                         # had a -6.3% intraday drawdown that would have triggered -5% stop).
                         # Comparison on 305 Grade B+C Q≥50 trades:
                         #   WITH stop (-5%/+12%/10d): -0.24% avg, -₹36,480 cum on ₹50K
                         #   NO stop, hold 10d close:  +0.33% avg, +₹50,755 cum on ₹50K
                         # Stop costs ₹87K of edge across 305 trades.
                         # Tail risk: max single-trade drawdown observed was -15.9%.
MAX_HOLD_DAYS = 10
MIN_QUALITY_SCORE = 50.0  # Filter: only Grade-B+C alerts with quality ≥50
DELIVERY_COST_PCT = 0.30  # CNC delivery round-trip


# --- Data loaders -----------------------------------------------------------
_daily_cache: dict = {}


def load_daily(symbol: str) -> pd.DataFrame:
    if symbol in _daily_cache:
        return _daily_cache[symbol]
    parts = []
    fp = DAY_CACHE / f"{symbol}.csv"
    if fp.exists():
        parts.append(pd.read_csv(fp))
    for folder in sorted(DAY_BACKFILL.glob("*")):
        if folder.is_dir():
            bf = folder / f"{symbol}.csv"
            if bf.exists():
                parts.append(pd.read_csv(bf))
    if not parts:
        _daily_cache[symbol] = pd.DataFrame()
        return _daily_cache[symbol]
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce").dt.tz_convert(None)
    df["date"] = df["datetime"].dt.date.astype(str)
    df = df.dropna(subset=["datetime"]).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    _daily_cache[symbol] = df
    return df


def next_trading_day_open(symbol: str, after_date: str) -> tuple[str, float] | None:
    """Find the next trading day's OPEN for symbol after the given date."""
    d = load_daily(symbol)
    if d.empty:
        return None
    future = d[d["date"] > after_date].sort_values("date")
    if future.empty:
        return None
    row = future.iloc[0]
    return (row["date"], float(row["open"]))


def daily_bar(symbol: str, date: str) -> dict | None:
    d = load_daily(symbol)
    if d.empty:
        return None
    bar = d[d["date"] == date]
    if bar.empty:
        return None
    r = bar.iloc[0]
    return {"date": date, "open": float(r["open"]), "high": float(r["high"]),
            "low": float(r["low"]), "close": float(r["close"])}


# --- Journal ----------------------------------------------------------------
def load_journal() -> dict:
    if JOURNAL_PATH.exists():
        return json.loads(JOURNAL_PATH.read_text())
    target_str = f"t={TARGET_PCT}%" if TARGET_PCT > 0 else "no-target"
    stop_str = f"s={STOP_PCT}%" if STOP_PCT > 0 else "no-stop"
    return {
        "config": {
            "strategy": f"swing_alerts|grade_b+tier_c Q>={MIN_QUALITY_SCORE:.0f}|entry=next_open|{target_str}|{stop_str}|hold={MAX_HOLD_DAYS}d",
            "position_size_inr": POSITION_INR,
            "max_concurrent": MAX_CONCURRENT,
            "target_pct": TARGET_PCT, "stop_pct": STOP_PCT,
            "max_hold_days": MAX_HOLD_DAYS,
            "min_quality_score": MIN_QUALITY_SCORE,
            "started": ddate.today().isoformat(),
            "data_source": "anomaly_history.csv (Grade-B + Tier C, quality-filtered)",
            "selection": "highest-quality first when over capacity",
        },
        "trades": [], "daily_log": [],
    }


def save_journal(j: dict):
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    JOURNAL_PATH.write_text(json.dumps(j, indent=2, default=str))


# --- Core logic -------------------------------------------------------------
def fill_pending_entries(j: dict, today: str) -> list[dict]:
    """Convert pending alerts → active positions if next-day open available."""
    filled = []
    for t in j["trades"]:
        if t.get("status") != "pending":
            continue
        # We're processing on `today` — look for the entry day (alert_date+1 trading day)
        result = next_trading_day_open(t["symbol"], t["alert_date"])
        if result is None:
            continue
        entry_date, entry_px = result
        # Don't fill if entry day is in the future (run too early)
        if entry_date > today:
            continue
        if entry_px <= 0 or np.isnan(entry_px):
            t["status"] = "skipped"
            t["skip_reason"] = "no_entry_price"
            continue
        qty = int(POSITION_INR / entry_px)
        if qty == 0:
            t["status"] = "skipped"
            t["skip_reason"] = "price_too_high_for_50k"
            continue
        t["status"] = "active"
        t["entry_date"] = entry_date
        t["entry_price"] = round(entry_px, 4)
        t["qty"] = qty
        # No stop, no target — exit only on max_hold (empirically optimal)
        t["target_price"] = round(entry_px * (1 + TARGET_PCT / 100), 4) if TARGET_PCT > 0 else None
        t["stop_price"] = round(entry_px * (1 - STOP_PCT / 100), 4) if STOP_PCT > 0 else None
        filled.append(t)
    return filled


def check_exits(j: dict, today: str) -> list[dict]:
    """Check active positions — close on stop/target/max_hold."""
    exits = []
    for t in j["trades"]:
        if t.get("status") != "active":
            continue
        sym = t["symbol"]
        entry_date = t["entry_date"]
        entry_px = t["entry_price"]
        target = t["target_price"]
        stop = t["stop_price"]

        # Get all bars after entry_date up to and including today
        d = load_daily(sym)
        if d.empty:
            continue
        bars_after = d[(d["date"] > entry_date) & (d["date"] <= today)].sort_values("date")
        if bars_after.empty:
            continue

        exit_px = None; reason = None; exit_date = None
        # Only honor stop/target if they were set (STOP_PCT/TARGET_PCT > 0)
        for _, b in bars_after.iterrows():
            lo, hi = float(b["low"]), float(b["high"])
            if stop is not None and lo <= stop:
                exit_px = stop; reason = "stop"; exit_date = b["date"]; break
            if target is not None and hi >= target:
                exit_px = target; reason = "target"; exit_date = b["date"]; break
        # Max hold check — primary exit when no stop/target
        if exit_px is None:
            held_bars = len(bars_after)
            if held_bars >= MAX_HOLD_DAYS:
                last = bars_after.iloc[MAX_HOLD_DAYS - 1] if len(bars_after) >= MAX_HOLD_DAYS else bars_after.iloc[-1]
                exit_px = float(last["close"])
                reason = "max_hold"
                exit_date = last["date"]

        if exit_px is None:
            continue  # still active

        gross = (exit_px / entry_px - 1) * 100
        net = gross - DELIVERY_COST_PCT
        t["status"] = "closed"
        t["exit_date"] = exit_date
        t["exit_price"] = round(exit_px, 4)
        t["exit_reason"] = reason
        t["gross_return_pct"] = round(gross, 3)
        t["cost_pct"] = DELIVERY_COST_PCT
        t["net_return_pct"] = round(net, 3)
        t["hold_days"] = len(bars_after)
        exits.append(t)
    return exits


def ingest_new_alerts(j: dict, today: str, lookback_days: int = 7) -> list[dict]:
    """Pick up new Grade-B + Tier C alerts not yet in journal."""
    if not ANOMALY_CSV.exists():
        return []
    df = pd.read_csv(ANOMALY_CSV)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date.astype(str)
    df["quality"] = pd.to_numeric(df["QualityScore"], errors="coerce").fillna(0)
    # Filter: Grade B or C AND quality >= MIN_QUALITY_SCORE
    df = df[df["Tier"].isin(["B", "C"]) & (df["quality"] >= MIN_QUALITY_SCORE)].copy()

    # Only alerts from last N days (catches Friday → Monday gap)
    cutoff = (ddate.fromisoformat(today) - timedelta(days=lookback_days)).isoformat()
    df = df[df["Date"] >= cutoff]
    if df.empty:
        return []

    # Sort by quality descending so highest-quality alerts get capacity first
    df = df.sort_values(["Date", "quality"], ascending=[True, False])
    df = df.drop_duplicates(subset=["Date", "Symbol"], keep="first")

    # Check what's already in the journal
    seen_keys = {f"{t['alert_date']}::{t['symbol']}" for t in j["trades"]}

    # Lazy-import the tracker. When trader runs as a script, sys.path[0] is
    # scripts/ — so 'from scripts.per_stock_tracker import X' fails silently
    # and the lambda fallback meant blacklist was effectively disabled.
    import sys as _sys, os as _os
    _repo_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
    if _repo_root not in _sys.path:
        _sys.path.insert(0, _repo_root)
    try:
        from scripts.per_stock_tracker import is_blacklisted
    except Exception as _exc:
        print(f"[!] tracker import failed: {_exc} — blacklist disabled", file=_sys.stderr)
        is_blacklisted = lambda s: (False, "")

    new_alerts = []
    for _, r in df.iterrows():
        key = f"{r['Date']}::{r['Symbol'].upper()}"
        if key in seen_keys:
            continue
        try:
            alert_px = float(r["PriceNow"])
        except (ValueError, TypeError):
            continue
        if alert_px <= 0:
            continue

        # Per-stock blacklist check: skip symbols with proven loss patterns in
        # forward paper-trading data (NOT the historical backtest)
        sym = r["Symbol"].upper()
        is_bad, bl_reason = is_blacklisted(sym)
        if is_bad:
            j["trades"].append({
                "trade_id": f"SA{len(j['trades'])+1:05d}",
                "alert_date": r["Date"], "alert_time": r["When"],
                "symbol": sym, "tier": r["Tier"],
                "alert_price": alert_px,
                "status": "skipped",
                "skip_reason": f"blacklisted: {bl_reason}",
            })
            seen_keys.add(key)
            continue

        # Check concurrent cap — count active positions
        active_count = sum(1 for t in j["trades"] if t.get("status") == "active")
        pending_count = sum(1 for t in j["trades"] if t.get("status") == "pending")
        if active_count + pending_count >= MAX_CONCURRENT:
            # Skip this one — log and continue
            j["trades"].append({
                "trade_id": f"SA{len(j['trades'])+1:05d}",
                "alert_date": r["Date"], "alert_time": r["When"],
                "symbol": r["Symbol"].upper(), "tier": r["Tier"],
                "alert_price": alert_px,
                "status": "skipped", "skip_reason": "max_concurrent_cap",
            })
            seen_keys.add(key)
            continue

        j["trades"].append({
            "trade_id": f"SA{len(j['trades'])+1:05d}",
            "alert_date": r["Date"], "alert_time": r["When"],
            "symbol": r["Symbol"].upper(), "tier": r["Tier"],
            "alert_price": alert_px,
            "status": "pending",  # will become active on next-day fill
        })
        seen_keys.add(key)
        new_alerts.append(j["trades"][-1])
    return new_alerts


def run_eod(target_date: str = None) -> dict:
    today = target_date or ddate.today().isoformat()
    j = load_journal()

    # Step 1: ingest new alerts (mark as pending)
    new_alerts = ingest_new_alerts(j, today)

    # Step 2: fill pending entries (alert from earlier days → active position now)
    filled = fill_pending_entries(j, today)

    # Step 3: check active positions for exits
    exits = check_exits(j, today)

    # Snapshot for daily_log
    active_after = sum(1 for t in j["trades"] if t.get("status") == "active")
    pending_after = sum(1 for t in j["trades"] if t.get("status") == "pending")
    closed_today = sum(1 for t in j["trades"]
                       if t.get("status") == "closed" and t.get("exit_date") == today)
    j["daily_log"].append({
        "date": today,
        "new_alerts": len(new_alerts),
        "filled_today": len(filled),
        "closed_today": closed_today,
        "active_after": active_after,
        "pending_after": pending_after,
    })
    save_journal(j)

    # Export trades CSV
    closed = [t for t in j["trades"] if t.get("status") == "closed"]
    if closed:
        pd.DataFrame(closed).to_csv(TRADES_CSV, index=False)

    return {
        "date": today,
        "new_alerts": len(new_alerts),
        "filled_today": len(filled),
        "closed_today": closed_today,
        "active_after": active_after,
        "pending_after": pending_after,
        "exits": exits,
    }


def print_stats():
    j = load_journal()
    trades = j["trades"]
    closed = [t for t in trades if t.get("status") == "closed"]
    active = [t for t in trades if t.get("status") == "active"]
    pending = [t for t in trades if t.get("status") == "pending"]
    skipped = [t for t in trades if t.get("status") == "skipped"]
    print(f"\n=== SWING ALERT PAPER TRADER — STATS ===")
    print(f"  Config: {j['config']['strategy']}")
    print(f"  Started: {j['config']['started']}")
    print(f"  Total tracked: {len(trades)}")
    print(f"    Closed:  {len(closed)}")
    print(f"    Active:  {len(active)}")
    print(f"    Pending: {len(pending)}")
    print(f"    Skipped: {len(skipped)} ({sum(1 for t in skipped if t.get('skip_reason')=='max_concurrent_cap')} max-cap, {sum(1 for t in skipped if t.get('skip_reason')=='no_entry_price')} no-data)")
    if closed:
        df = pd.DataFrame(closed)
        n = len(df)
        wins = df[df["net_return_pct"] > 0]
        print(f"\n  CLOSED TRADES:")
        print(f"    Win rate:        {len(wins)/n*100:.1f}%")
        print(f"    Avg net/trade:   {df['net_return_pct'].mean():+.3f}%")
        print(f"    Avg hold days:   {df['hold_days'].mean():.1f}")
        print(f"    Cumulative net:  {df['net_return_pct'].sum():+.2f}%")
        cum_inr = df["net_return_pct"].sum() / 100 * POSITION_INR
        print(f"    Cumulative ₹:    ₹{cum_inr:+,.0f}  (single ₹50K rotated)")
        print(f"\n  By exit reason:")
        print(df.groupby("exit_reason")["net_return_pct"].agg(["count", "mean"]).round(3).to_string())
        print(f"\n  By tier:")
        print(df.groupby("tier")["net_return_pct"].agg(["count", "mean"]).round(3).to_string())
    if active:
        print(f"\n  ACTIVE POSITIONS ({len(active)}):")
        for t in active[:10]:
            print(f"    {t['symbol']:12} entry=₹{t['entry_price']:>8.2f}  target=₹{t['target_price']:>8.2f}  stop=₹{t['stop_price']:>8.2f}  on {t['entry_date']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="YYYY-MM-DD (default: today)")
    ap.add_argument("--backfill-from", help="Replay daily from this date through today (YYYY-MM-DD)")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    if args.stats:
        print_stats()
        return 0

    if args.backfill_from:
        df = pd.read_csv(ANOMALY_CSV)
        df["Date"] = pd.to_datetime(df["Date"]).dt.date.astype(str)
        all_dates = sorted(set(df["Date"].tolist()))
        replay = [d for d in all_dates if d >= args.backfill_from]
        # Also add a few days beyond the latest alert to close out positions
        last_alert = max(replay) if replay else args.backfill_from
        extra_dates = []
        cur = ddate.fromisoformat(last_alert)
        for i in range(15):
            cur = cur + timedelta(days=1)
            extra_dates.append(cur.isoformat())
        for d in replay + extra_dates:
            res = run_eod(d)
            print(f"[{d}] new={res['new_alerts']:>3}  filled={res['filled_today']:>3}  closed={res['closed_today']:>3}  active={res['active_after']:>3}  pending={res['pending_after']:>3}")
        print_stats()
        return 0

    res = run_eod(args.date)
    print(f"[{res['date']}] new_alerts={res['new_alerts']}  filled={res['filled_today']}  closed_today={res['closed_today']}  active={res['active_after']}  pending={res['pending_after']}")
    for ex in res.get("exits", []):
        print(f"  EXIT {ex['symbol']:12} {ex['exit_reason']:10} entry=₹{ex['entry_price']:>8.2f} → exit=₹{ex['exit_price']:>8.2f}  net={ex['net_return_pct']:+.2f}%  hold={ex['hold_days']}d")
    print_stats()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
