"""Full alert outcomes — every Strong Buy / Buy Now from Screener Alerts.txt.

Parses EVERY alert from the file (April 1 - May 22, 2026), extracts symbol +
alert price + targets, then for each one looks up:
  - Same-day intraday: high, low, close (computes %, hit T1?)
  - Swing: 3d, 5d, 10d, 20d max gain
  - Best exit timing

Output: per-stock summary showing which stocks were profitable for intraday
vs swing, and aggregate totals.
"""

from __future__ import annotations
import re, sys, warnings
warnings.filterwarnings("ignore")
from datetime import datetime, date as ddate
from pathlib import Path
from collections import defaultdict
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"
DAY_CACHE = RESULTS / "kite_history_cache" / "day"
DAY_BACKFILL = RESULTS / "kite_history_backfill" / "day"
ALERTS_TXT = REPO / "Screener Alerts.txt"
OUT_DIR = RESULTS / "full_alert_outcomes"
OUT_DIR.mkdir(parents=True, exist_ok=True)


# ------------------------------------------------------------------
# Parser
# ------------------------------------------------------------------
def parse_all_alerts() -> pd.DataFrame:
    text = ALERTS_TXT.read_text()
    entries = re.split(r'\n(?=\[\d{2}-\d{2}-\d{4} \d{2}:\d{2}\])', text)
    rows = []

    # Multiple alert formats — extract every variant
    # Format A: "Strong Buy Now `SYMBOL`" or "🚀 Buy Now `SYMBOL`" etc with At alert: ₹X
    pat_full = re.compile(
        r'(?:🚀|🟢|🔥|⚡|👀).*?(Strong Buy Now|Buy Now|Grade-?A|Grade-?B|Pre-Move|Anomaly|Runner).*?'
        r'`?([A-Z0-9&\-]+)`?.*?'
        r'At alert:?\s*₹?([\d.,]+)',
        re.DOTALL
    )
    # Targets/stop captured separately (any of these patterns)
    pat_t1 = re.compile(r'Target 1\s*`?₹?([\d.,]+)')
    pat_t2 = re.compile(r'Target 2\s*`?₹?([\d.,]+)')
    pat_stop = re.compile(r'Stop\s*(?:Loss)?\s*`?₹?([\d.,]+)')

    for e in entries:
        m = re.match(r'\[(\d{2})-(\d{2})-(\d{4}) (\d{2}):(\d{2})\]', e)
        if not m: continue
        dd, mm, yyyy, hh, mn = m.groups()
        try:
            ts = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mn))
        except: continue
        if ts.date() > ddate(2026, 5, 22):
            continue

        # Determine alert kind from emoji and text
        el = e.lower()
        if "🚀" in e or "strong buy now" in el:
            kind = "strong_buy"
        elif "🔥" in e or "grade-a" in el or "grade a" in el or "runner" in el:
            kind = "grade_a"
        elif "⚡" in e or "grade-b" in el or "grade b" in el:
            kind = "grade_b"
        elif "👀" in e or "pre-move" in el:
            kind = "pre_move"
        elif "🟢" in e and "buy now" in el:
            kind = "buy_now"
        else:
            continue

        # Look for symbol + price together
        m2 = pat_full.search(e)
        if not m2:
            continue
        symbol = m2.group(2).upper()
        try:
            alert_px = float(m2.group(3).replace(",", ""))
        except: continue

        # Extract T1, T2, Stop (optional)
        t1 = float(pat_t1.search(e).group(1).replace(",", "")) if pat_t1.search(e) else None
        t2 = float(pat_t2.search(e).group(1).replace(",", "")) if pat_t2.search(e) else None
        stop = float(pat_stop.search(e).group(1).replace(",", "")) if pat_stop.search(e) else None

        rows.append({
            "ts": ts, "date": ts.date().isoformat(), "time": ts.strftime("%H:%M"),
            "kind": kind, "symbol": symbol,
            "alert_price": alert_px, "t1": t1, "t2": t2, "stop": stop,
        })

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df = df.sort_values("ts")
    # De-duplicate: same (date, symbol, kind) → keep first (earliest alert that day)
    df = df.drop_duplicates(subset=["date", "symbol", "kind"], keep="first")
    return df


# ------------------------------------------------------------------
# Data loader
# ------------------------------------------------------------------
_daily_cache: dict = {}


def load_daily(symbol: str) -> pd.DataFrame:
    if symbol in _daily_cache:
        return _daily_cache[symbol]
    parts = []
    fp = DAY_CACHE / f"{symbol}.csv"
    if fp.exists():
        parts.append(pd.read_csv(fp))
    for folder in sorted(DAY_BACKFILL.glob("*")):
        if not folder.is_dir(): continue
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


# ------------------------------------------------------------------
# Outcomes
# ------------------------------------------------------------------
def compute(alert: dict) -> dict:
    sym = alert["symbol"]
    date = alert["date"]
    entry = float(alert["alert_price"])
    t1 = float(alert["t1"]) if alert.get("t1") else 0
    stop = float(alert["stop"]) if alert.get("stop") else 0

    out = dict(alert)
    daily = load_daily(sym)
    if daily.empty:
        out["has_data"] = False
        return out

    # Same-day bar
    today_bar = daily[daily["date"] == date]
    if today_bar.empty:
        out["has_data"] = False
        return out
    out["has_data"] = True
    r = today_bar.iloc[0]
    out["intra_max_pct"] = round((r["high"] - entry) / entry * 100, 2)
    out["intra_min_pct"] = round((r["low"] - entry) / entry * 100, 2)
    out["intra_eod_pct"] = round((r["close"] - entry) / entry * 100, 2)
    if t1 > 0:
        out["intra_hit_t1"] = bool(r["high"] >= t1)
    if stop > 0:
        out["intra_hit_stop"] = bool(r["low"] <= stop)

    # Multi-day swing windows
    future = daily[daily["date"] > date].head(20).reset_index(drop=True)
    for n in [3, 5, 10, 20]:
        win = future.head(n)
        if win.empty: continue
        out[f"swing_{n}d_max_pct"] = round((win["high"].max() - entry) / entry * 100, 2)
        out[f"swing_{n}d_close_pct"] = round((win["close"].iloc[-1] - entry) / entry * 100, 2)
    return out


def main():
    print("[i] Parsing all alerts from Screener Alerts.txt...")
    alerts = parse_all_alerts()
    print(f"    Parsed: {len(alerts)} unique (date, symbol, kind) alerts through 2026-05-22")
    print(f"    Date range: {alerts['date'].min()} to {alerts['date'].max()}")
    print(f"\n    By kind:")
    print(alerts.groupby("kind").size().to_string())

    if alerts.empty: return 0
    alerts.to_csv(OUT_DIR / "all_parsed_alerts.csv", index=False)

    # Compute outcomes
    print(f"\n[i] Computing outcomes...")
    results = []
    for i, row in enumerate(alerts.to_dict("records"), 1):
        if i % 500 == 0: print(f"    {i}/{len(alerts)} processed")
        results.append(compute(row))

    df = pd.DataFrame(results)
    df.to_csv(OUT_DIR / "all_alert_outcomes.csv", index=False)
    with_data = df[df["has_data"] == True].copy()
    print(f"\n    With OHLC data: {len(with_data)} / {len(df)} ({len(with_data)/len(df)*100:.0f}%)")

    # --------------- AGGREGATE BY ALERT KIND ---------------
    print(f"\n{'='*80}\nPER-ALERT-KIND OUTCOMES\n{'='*80}")
    print(f"{'Kind':12} {'n':>6} {'IntraEOD':>10} {'IntraEOD WR':>13} {'IntraMax%':>10} {'5dEnd':>9} {'10dEnd':>9} {'20dMax':>9}")
    for kind, sub in with_data.groupby("kind"):
        n = len(sub)
        eod = pd.to_numeric(sub["intra_eod_pct"], errors="coerce").dropna()
        i_max = pd.to_numeric(sub["intra_max_pct"], errors="coerce").dropna()
        s5 = pd.to_numeric(sub.get("swing_5d_close_pct", pd.Series()), errors="coerce").dropna()
        s10 = pd.to_numeric(sub.get("swing_10d_close_pct", pd.Series()), errors="coerce").dropna()
        s20max = pd.to_numeric(sub.get("swing_20d_max_pct", pd.Series()), errors="coerce").dropna()
        print(f"{kind:12} {n:>6} {eod.mean():>+10.2f} {(eod>0).mean()*100:>12.1f}% {i_max.mean():>+10.2f} {s5.mean():>+9.2f} {s10.mean():>+9.2f} {s20max.mean():>+9.2f}")

    # --------------- TOP PROFITABLE STOCKS ---------------
    print(f"\n{'='*80}\nTOP 20 INTRADAY WINNERS (sort by EOD same-day return)\n{'='*80}")
    intra_winners = with_data[with_data["intra_eod_pct"] > 0].sort_values("intra_eod_pct", ascending=False).head(20)
    for _, r in intra_winners.iterrows():
        print(f"  {r['date']} {r['symbol']:12} {r['kind']:11} alert=₹{r['alert_price']:>9.2f}  intraEOD={r['intra_eod_pct']:>+5.2f}%  intraMax={r.get('intra_max_pct',0):>+5.2f}%")

    print(f"\n{'='*80}\nTOP 20 SWING WINNERS (sort by 10-day close return)\n{'='*80}")
    swing_winners = with_data[pd.to_numeric(with_data.get("swing_10d_close_pct", 0), errors="coerce").fillna(-99) > 0].sort_values("swing_10d_close_pct", ascending=False).head(20)
    for _, r in swing_winners.iterrows():
        print(f"  {r['date']} {r['symbol']:12} {r['kind']:11} alert=₹{r['alert_price']:>9.2f}  10dClose={r.get('swing_10d_close_pct',0):>+5.2f}%  20dMax={r.get('swing_20d_max_pct',0):>+5.2f}%")

    print(f"\n{'='*80}\nWORST 10 ALERTS (biggest same-day losses)\n{'='*80}")
    losers = with_data[with_data["intra_eod_pct"] < 0].sort_values("intra_eod_pct").head(10)
    for _, r in losers.iterrows():
        print(f"  {r['date']} {r['symbol']:12} {r['kind']:11} alert=₹{r['alert_price']:>9.2f}  intraEOD={r['intra_eod_pct']:>+5.2f}%")

    # --------------- PORTFOLIO P&L SIMULATION ---------------
    print(f"\n{'='*80}\nPORTFOLIO P&L — IF YOU TOOK EVERY ALERT (₹50K each)\n{'='*80}")
    pos = 50000
    for strategy_label, col in [
        ("Hold to EOD (intraday)", "intra_eod_pct"),
        ("Hold 5 days (swing)", "swing_5d_close_pct"),
        ("Hold 10 days (swing)", "swing_10d_close_pct"),
        ("Hold 20 days (swing)", "swing_20d_close_pct"),
        ("Best theoretical: 20-day MAX", "swing_20d_max_pct"),
    ]:
        print(f"\n  STRATEGY: {strategy_label}")
        print(f"  {'Kind':12} {'n':>5} {'avg %':>8} {'cum %':>9} {'cum ₹':>14} {'win rate':>10}")
        for kind, sub in with_data.groupby("kind"):
            v = pd.to_numeric(sub.get(col, pd.Series()), errors="coerce").dropna()
            if v.empty: continue
            avg = v.mean(); cum = v.sum()
            wr = (v > 0).mean() * 100
            cum_inr = (cum/100) * pos
            print(f"  {kind:12} {len(v):>5} {avg:>+8.2f} {cum:>+9.2f} ₹{cum_inr:>+12,.0f} {wr:>9.1f}%")

    print(f"\n[+] All outputs in {OUT_DIR}/")
    print(f"    all_parsed_alerts.csv  — every alert detected")
    print(f"    all_alert_outcomes.csv — with intraday + swing returns per alert")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
