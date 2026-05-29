#!/usr/bin/env python3
"""Watch-only scoreboard for the live actionable BUY alerts.

These are the "BUY (engine confirmed) / Buy Now" alerts the bot pushes to
Discord — the chase-the-move feed, NOT a traded strategy. This scores how they
would have done if you'd bought at the alert's entry and managed with its own
stop/target, so their edge (or lack of it) can be watched objectively instead
of judged from a single example.

HONEST LIMITATIONS (surfaced on the dashboard too):
  - Source is the daily actionable-board snapshot (signalmonitor_all_actionable_*).
    It is point-in-time, so alerts that fired and FADED before the snapshot
    (e.g. an intraday burst that reversed) are NOT captured. That biases the
    scoreboard OPTIMISTIC — read it as a best case, not the full truth.
  - Outcome is a simple N-day forward simulation off DAILY bars (no intraday
    path), net of a flat cost estimate.
"""
import glob
import re
from datetime import date as ddate
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "results" / "Data"
DAY_CACHE = DATA / "kite_history_cache" / "day"

HOLD_DAYS = 5          # forward trading days to evaluate each alert
COST_PCT = 0.25        # flat round-trip cost estimate (%), applied to every outcome
_DATE_RE = re.compile(r"actionable_(\d{8})\.csv$")


def _load_day_bars(symbol: str) -> pd.DataFrame:
    fp = DAY_CACHE / f"{symbol}.csv"
    if not fp.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(fp)
        df["date"] = pd.to_datetime(df["datetime"]).dt.date.astype(str)
        return df.sort_values("date")
    except Exception:
        return pd.DataFrame()


def _outcome(symbol: str, alert_date: str, entry: float, stop: float, t1: float) -> dict:
    """N-day forward outcome from daily bars, entered at `entry` the day AFTER
    the alert. Stop checked before target on each bar (worst-case)."""
    if not (entry and entry > 0):
        return {"status": "skip", "reason": "no_entry_price"}
    # Sanitize a nonsensical stop/target (must straddle entry for a long).
    if not stop or stop <= 0 or stop >= entry:
        stop = entry * 0.97
    if not t1 or t1 <= entry:
        t1 = entry * 1.03

    bars = _load_day_bars(symbol)
    if bars.empty:
        return {"status": "skip", "reason": "no_price_data"}
    fwd = bars[bars["date"] > alert_date].head(HOLD_DAYS)
    if fwd.empty:
        return {"status": "pending", "reason": "too_recent"}

    exit_px, reason = None, None
    for _, b in fwd.iterrows():
        lo, hi = float(b["low"]), float(b["high"])
        if lo <= stop:
            exit_px, reason = stop, "stop"
            break
        if hi >= t1:
            exit_px, reason = t1, "target"
            break
    if exit_px is None:
        exit_px, reason = float(fwd.iloc[-1]["close"]), "hold_end"

    gross = (exit_px / entry - 1) * 100
    return {
        "status": "closed",
        "exit_reason": reason,
        "net_pct": round(gross - COST_PCT, 2),
        "days_avail": len(fwd),
    }


def _iter_snapshots():
    """Yield (alert_date, dataframe) for each archived board + the latest one."""
    for fp in sorted(glob.glob(str(DATA / "signalmonitor_all_actionable_2*.csv"))):
        m = _DATE_RE.search(fp)
        if not m:
            continue
        d = f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
        try:
            yield d, pd.read_csv(fp)
        except Exception:
            continue
    latest = DATA / "signalmonitor_all_actionable_latest.csv"
    if latest.exists():
        try:
            df = pd.read_csv(latest)
            d = str(pd.to_datetime(df["RunAt"].iloc[0]).date()) if "RunAt" in df and len(df) else ddate.today().isoformat()
            yield d, df
        except Exception:
            pass


def compute_watch(lookback_days: int = 30) -> dict:
    """Score every actionable BUY-Now alert across recent snapshots."""
    cutoff = (pd.Timestamp.today() - pd.Timedelta(days=lookback_days)).date().isoformat()
    seen = set()
    rows = []
    for alert_date, df in _iter_snapshots():
        if alert_date < cutoff or "NowAction" not in df.columns:
            continue
        buys = df[df["NowAction"].astype(str).str.strip().str.lower() == "buy now"]
        for _, a in buys.iterrows():
            sym = str(a.get("Symbol"))
            key = (alert_date, sym)
            if key in seen:
                continue
            seen.add(key)
            o = _outcome(
                sym, alert_date,
                float(a.get("Entry") or 0),
                float(a.get("StopLoss") or 0),
                float(a.get("Target1") or 0),
            )
            rows.append({"date": alert_date, "symbol": sym, **o})

    closed = [r for r in rows if r["status"] == "closed"]
    pending = [r for r in rows if r["status"] == "pending"]
    rets = [r["net_pct"] for r in closed]
    agg = {
        "total_alerts": len(rows),
        "closed": len(closed),
        "pending": len(pending),
        "skipped": sum(1 for r in rows if r["status"] == "skip"),
        "win_rate": round(sum(1 for r in rets if r > 0) / len(rets) * 100, 0) if rets else None,
        "avg_net": round(sum(rets) / len(rets), 2) if rets else None,
        "targets": sum(1 for r in closed if r["exit_reason"] == "target"),
        "stops": sum(1 for r in closed if r["exit_reason"] == "stop"),
        "hold_end": sum(1 for r in closed if r["exit_reason"] == "hold_end"),
        "hold_days": HOLD_DAYS,
    }
    rows.sort(key=lambda r: r["date"], reverse=True)
    return {"agg": agg, "rows": rows}


if __name__ == "__main__":
    import json
    w = compute_watch()
    print(json.dumps(w["agg"], indent=2))
    print("\nrecent:")
    for r in w["rows"][:15]:
        net = f"{r['net_pct']:+.2f}% ({r['exit_reason']})" if r["status"] == "closed" else r["status"]
        print(f"  {r['date']}  {r['symbol']:12s}  {net}")
