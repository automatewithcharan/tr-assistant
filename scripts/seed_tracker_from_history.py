"""Seed per_stock_tracker.json with historical alert outcomes.

Sources:
  1. anomaly_history.csv — Tier A/B/C alerts with OHLC-derived outcomes
  2. Screener Alerts.txt — Strong Buy Now / Buy Now Discord exports

For each alert, computes:
  - 10-day no-stop swing return (entry at next day open, exit at day-10 close)
  - Matches the actual swing_alert_paper_trader strategy

Aggregates by symbol → writes per_stock_tracker compatible JSON.
"""

from __future__ import annotations
import json, re, warnings
warnings.filterwarnings("ignore")
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"
DAY_CACHE = RESULTS / "kite_history_cache" / "day"
DAY_BACKFILL = RESULTS / "kite_history_backfill" / "day"
ANOMALY_CSV = RESULTS / "anomaly_history.csv"
ALERTS_TXT = REPO / "Screener Alerts.txt"
# Write to the historical seed file (read by per_stock_tracker as immutable baseline).
# Also write to tracker.json so it's immediately usable.
TRACKER_PATH = RESULTS / "per_stock_tracker" / "tracker_seeded_historical.json"
TRACKER_LIVE_PATH = RESULTS / "per_stock_tracker" / "tracker.json"
TRACKER_CSV = RESULTS / "per_stock_tracker" / "tracker.csv"

# Same thresholds as the live tracker
WINNER_MIN_TRADES = 2
WINNER_MIN_WR = 75.0
WINNER_MIN_AVG = 5.0
BLACKLIST_MIN_TRADES_TIGHT = 3
BLACKLIST_MIN_WR_TIGHT = 20.0
BLACKLIST_MIN_AVG_TIGHT = -2.0
BLACKLIST_MIN_TRADES_LOOSE = 5
BLACKLIST_MIN_WR_LOOSE = 30.0
BLACKLIST_MIN_AVG_LOOSE = 0.0
COST_PCT = 0.30


_cache: dict = {}


def daily(sym: str) -> pd.DataFrame:
    if sym in _cache:
        return _cache[sym]
    parts = []
    fp = DAY_CACHE / f"{sym}.csv"
    if fp.exists():
        parts.append(pd.read_csv(fp))
    for folder in sorted(DAY_BACKFILL.glob("*")):
        if folder.is_dir():
            bf = folder / f"{sym}.csv"
            if bf.exists():
                parts.append(pd.read_csv(bf))
    if not parts:
        _cache[sym] = pd.DataFrame()
        return _cache[sym]
    df = pd.concat(parts, ignore_index=True)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce").dt.tz_convert(None)
    df["date"] = df["datetime"].dt.date.astype(str)
    df = df.dropna(subset=["datetime"]).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    _cache[sym] = df
    return df


def compute_swing_outcome(symbol: str, alert_date: str) -> dict | None:
    """10-day no-stop swing: entry at next-day open, exit at day-10 close."""
    d = daily(symbol)
    if d.empty:
        return None
    fut = d[d["date"] > alert_date].head(10).reset_index(drop=True)
    if fut.empty:
        return None
    entry_px = float(fut.iloc[0]["open"])
    if entry_px <= 0:
        return None
    exit_px = float(fut.iloc[-1]["close"])
    exit_date = fut.iloc[-1]["date"]
    entry_date = fut.iloc[0]["date"]
    gross = (exit_px / entry_px - 1) * 100
    net = gross - COST_PCT
    return {
        "source": "historical_seed",
        "tier": "B",  # filled later
        "entry_date": entry_date,
        "exit_date": exit_date,
        "entry_price": round(entry_px, 4),
        "exit_price": round(exit_px, 4),
        "net_return_pct": round(net, 3),
        "exit_reason": "max_hold",
    }


def collect_anomaly_alerts() -> list[dict]:
    """Pull all alerts from anomaly_history with prices."""
    if not ANOMALY_CSV.exists():
        return []
    df = pd.read_csv(ANOMALY_CSV)
    df["Date"] = pd.to_datetime(df["Date"]).dt.date.astype(str)
    df["alert_price"] = pd.to_numeric(df["PriceNow"], errors="coerce")
    df = df[df["alert_price"].notna() & (df["alert_price"] > 0)]
    # Deduplicate: first alert per (date, symbol)
    df = df.sort_values("When").drop_duplicates(subset=["Date", "Symbol"], keep="first")
    return [
        {"date": r["Date"], "symbol": str(r["Symbol"]).upper(), "tier": r["Tier"],
         "alert_price": r["alert_price"], "source": "anomaly_history"}
        for _, r in df.iterrows()
    ]


def collect_screener_alerts() -> list[dict]:
    """Parse Strong Buy Now / Buy Now / Grade alerts from Discord export."""
    if not ALERTS_TXT.exists():
        return []
    text = ALERTS_TXT.read_text()
    entries = re.split(r'\n(?=\[\d{2}-\d{2}-\d{4} \d{2}:\d{2}\])', text)
    rows = []
    pat = re.compile(
        r'(?:🚀|🟢|🔥|⚡|👀).*?(Strong Buy Now|Buy Now|Grade-?A|Grade-?B|Pre-Move|Anomaly|Runner)'
        r'.*?`([A-Z0-9&\-]+)`'
        r'.*?At alert:?\s*₹?([\d.,]+)',
        re.DOTALL
    )
    for e in entries:
        m = re.match(r'\[(\d{2})-(\d{2})-(\d{4})', e)
        if not m: continue
        dd, mm, yyyy = m.groups()
        try:
            date = f"{yyyy}-{mm}-{dd}"
        except: continue
        for am in pat.finditer(e):
            kind_text = am.group(1).lower()
            tier = "C" if "pre" in kind_text else "A" if "grade-a" in kind_text or "runner" in kind_text else "B"
            sym = am.group(2).upper()
            try:
                px = float(am.group(3).replace(",", ""))
            except: continue
            rows.append({"date": date, "symbol": sym, "tier": tier, "alert_price": px, "source": "screener_alerts"})
    return rows


def build_tracker() -> dict:
    print("[i] Collecting historical alerts...")
    a_alerts = collect_anomaly_alerts()
    s_alerts = collect_screener_alerts()
    all_alerts = a_alerts + s_alerts
    print(f"    anomaly_history: {len(a_alerts)} alerts")
    print(f"    screener_alerts: {len(s_alerts)} alerts")
    print(f"    combined raw:    {len(all_alerts)}")

    # Deduplicate across sources by (date, symbol)
    seen = {}
    for a in all_alerts:
        key = (a["date"], a["symbol"])
        if key not in seen:
            seen[key] = a
    unique = list(seen.values())
    print(f"    unique (date, symbol): {len(unique)}")

    print("[i] Computing 10-day no-stop outcomes for each...")
    by_sym: dict[str, list[dict]] = defaultdict(list)
    skipped = 0
    for i, a in enumerate(unique, 1):
        if i % 500 == 0:
            print(f"    processed {i}/{len(unique)}")
        outcome = compute_swing_outcome(a["symbol"], a["date"])
        if outcome is None:
            skipped += 1
            continue
        outcome["tier"] = a["tier"]
        by_sym[a["symbol"]].append(outcome)
    print(f"    {sum(len(v) for v in by_sym.values())} trades with outcomes ({skipped} skipped — no OHLC)")

    # Build tracker record
    record = {}
    for sym, trades in by_sym.items():
        trades = sorted(trades, key=lambda x: x["exit_date"])
        n = len(trades)
        rets = [t["net_return_pct"] for t in trades]
        wins = sum(1 for r in rets if r > 0)
        wr = wins / n * 100
        avg = sum(rets) / n
        status = "NEUTRAL"; reason = ""
        if n >= WINNER_MIN_TRADES and wr >= WINNER_MIN_WR and avg >= WINNER_MIN_AVG:
            status = "PROVEN_WINNER"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}%"
        elif n >= BLACKLIST_MIN_TRADES_TIGHT and wr <= BLACKLIST_MIN_WR_TIGHT and avg <= BLACKLIST_MIN_AVG_TIGHT:
            status = "BLACKLISTED"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}% (tight rule)"
        elif n >= BLACKLIST_MIN_TRADES_LOOSE and wr <= BLACKLIST_MIN_WR_LOOSE and avg <= BLACKLIST_MIN_AVG_LOOSE:
            status = "BLACKLISTED"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}% (loose rule)"
        elif n >= 3 and (wr < 40 or avg < 0):
            status = "WATCH"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}%"
        elif n >= 2 and wr >= 60 and avg >= 2:
            status = "WATCH_POSITIVE"
            reason = f"{n} trades, {wr:.0f}% WR, avg {avg:+.2f}%"

        record[sym] = {
            "n": n, "wins": wins, "win_rate_pct": round(wr, 1),
            "avg_net_pct": round(avg, 3),
            "best_pct": round(max(rets), 2), "worst_pct": round(min(rets), 2),
            "total_cum_pct": round(sum(rets), 2),
            "first_trade": trades[0]["exit_date"], "last_trade": trades[-1]["exit_date"],
            "status": status, "status_reason": reason,
            "trades": trades,
        }
    return record


def main():
    record = build_tracker()
    TRACKER_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Write to PERSISTENT seed file (never overwritten by per_stock_tracker.py)
    TRACKER_PATH.write_text(json.dumps(record, indent=2, default=str))
    # Also write to live tracker.json so it's immediately active
    TRACKER_LIVE_PATH.write_text(json.dumps(record, indent=2, default=str))
    rows = [{
        "symbol": s, "n": r["n"], "wins": r["wins"], "wr": r["win_rate_pct"],
        "avg_net": r["avg_net_pct"], "total_cum": r["total_cum_pct"],
        "best": r["best_pct"], "worst": r["worst_pct"],
        "first": r["first_trade"], "last": r["last_trade"],
        "status": r["status"], "reason": r["status_reason"],
    } for s, r in record.items()]
    pd.DataFrame(rows).sort_values("total_cum", ascending=False).to_csv(TRACKER_CSV, index=False)
    print(f"\n[+] Tracker seeded: {len(record)} symbols")
    print(f"    → {TRACKER_PATH}")
    print(f"    → {TRACKER_CSV}")

    by_status = defaultdict(list)
    for s, r in record.items():
        by_status[r["status"]].append((s, r))
    print(f"\n=== STATUS COUNTS ===")
    for status in ["PROVEN_WINNER", "WATCH_POSITIVE", "NEUTRAL", "WATCH", "BLACKLISTED"]:
        print(f"  {status:18}  {len(by_status.get(status, []))} symbols")

    if by_status.get("PROVEN_WINNER"):
        print(f"\n🟢 PROVEN WINNERS (top 20):")
        for s, r in sorted(by_status["PROVEN_WINNER"], key=lambda x: -x[1]["total_cum_pct"])[:20]:
            print(f"  {s:12} n={r['n']:>2} WR={r['win_rate_pct']:>5.1f}% avg={r['avg_net_pct']:+6.2f}% total={r['total_cum_pct']:+7.2f}%")

    if by_status.get("BLACKLISTED"):
        print(f"\n🔴 BLACKLISTED (top 20 worst):")
        for s, r in sorted(by_status["BLACKLISTED"], key=lambda x: x[1]["total_cum_pct"])[:20]:
            print(f"  {s:12} n={r['n']:>2} WR={r['win_rate_pct']:>5.1f}% avg={r['avg_net_pct']:+6.2f}% total={r['total_cum_pct']:+7.2f}%")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
