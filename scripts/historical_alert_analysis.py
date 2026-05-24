"""Historical alert performance — measure what would have actually worked.

Pulls every alert from:
  - Screener Alerts.txt (the Discord export, April-May 2026)
  - anomaly_history.csv (the structured anomaly detector log)

For each unique (date, symbol, alert_type, alert_price):
  - Look up that day's intraday bars (5-min if available, daily otherwise)
  - Compute intraday outcomes: max gain, max drawdown, EOD return
  - Look up next N days: 3-day, 5-day, 10-day, 20-day max gain
  - Compare exit strategies:
      A. Hold to EOD same day (intraday)
      B. Exit at T1 if hit, else EOD (mechanical T1)
      C. Exit at T1 if hit, else stop at -3%, else EOD
      D. Hold 5 days, exit at end
      E. Hold 10 days
      F. Hold 20 days
      G. Best theoretical: sell at +20-day high (omniscient)
      H. Stop -5% / Target +10% over 20 days

Group results by alert_type. Compute portfolio-level P&L assuming ₹50K per trade.

Output: detailed CSV + summary report.
"""

from __future__ import annotations
import re, sys, json, warnings
warnings.filterwarnings("ignore")
from datetime import datetime, date as ddate, timedelta
from pathlib import Path
from collections import defaultdict
import pandas as pd
import numpy as np

REPO = Path(__file__).resolve().parents[1]
RESULTS = REPO / "results" / "Data"
DAY_CACHE = RESULTS / "kite_history_cache" / "day"
DAY_BACKFILL = RESULTS / "kite_history_backfill" / "day"
INTRADAY_5M = RESULTS / "kite_history_backfill" / "5minute" / "2024-05-13_2026-05-12"
ALERTS_TXT = REPO / "Screener Alerts.txt"
ANOMALY_CSV = RESULTS / "anomaly_history.csv"
OUT_DIR = RESULTS / "historical_alert_analysis"
OUT_DIR.mkdir(parents=True, exist_ok=True)

POSITION_INR = 50000.0
INTRADAY_COST_PCT = 0.10  # Zerodha intraday fees + slippage round-trip
DELIVERY_COST_PCT = 0.30  # Delivery for swing holds


# ------------------------------------------------------------------
# 1) Parse Screener Alerts.txt
# ------------------------------------------------------------------
def parse_screener_alerts() -> pd.DataFrame:
    """Extract every alert with symbol + price + targets + stop."""
    if not ALERTS_TXT.exists():
        print(f"[!] {ALERTS_TXT} missing")
        return pd.DataFrame()
    text = ALERTS_TXT.read_text()
    entries = re.split(r'\n(?=\[\d{2}-\d{2}-\d{4} \d{2}:\d{2}\])', text)

    rows = []
    # Pattern variants for different alert types
    # Strong Buy Now / Buy Now with full details
    pat_buy = re.compile(
        r'(?:🚀|🟢).*?(?:Strong Buy Now|Buy Now).*?`([A-Z0-9&\-]+)`[\s\S]{0,400}?'
        r'At alert:?\s*₹?([\d.]+)[\s\S]{0,400}?'
        r'Stop Loss\s*`?₹?([\d.]+)`?[\s\S]{0,200}?'
        r'Target 1\s*`?₹?([\d.]+)`?[\s\S]{0,200}?'
        r'Target 2\s*`?₹?([\d.]+)`?'
    )
    # Anomaly Grade-A/B/C
    pat_grade = re.compile(
        r'(?:🔥|⚡|👀).*?(?:Grade-?[ABC]|Pre-Move|Anomaly|Runner).*?`([A-Z0-9&\-]+)`[\s\S]{0,300}?'
        r'At alert:?\s*₹?([\d.]+)[\s\S]{0,300}?'
        r'(?:Stop Loss|SL).*?`?₹?([\d.]+)`?[\s\S]{0,200}?'
        r'Target 1\s*`?₹?([\d.]+)`?'
    )
    # Generic Sell Now / EXIT (we'll handle separately)

    for e in entries:
        m = re.match(r'\[(\d{2})-(\d{2})-(\d{4}) (\d{2}):(\d{2})\]', e)
        if not m: continue
        dd, mm, yyyy, hh, mn = m.groups()
        try:
            ts = datetime(int(yyyy), int(mm), int(dd), int(hh), int(mn))
        except: continue

        el = e.lower()
        # Determine alert type
        if "strong buy now" in el or "🚀" in e:
            atype = "strong_buy"
        elif "🔥" in e or "grade-a" in el or "grade a" in el:
            atype = "grade_a"
        elif "⚡" in e or "grade-b" in el or "grade b" in el:
            atype = "grade_b"
        elif "👀" in e or "pre-move" in el:
            atype = "pre_move"
        elif "🟢" in e and "buy now" in el:
            atype = "buy_now"
        elif "🔴" in e and ("exit" in el or "fading" in el):
            atype = "exit"
        else:
            continue

        # Try matching detailed pattern
        for bm in pat_buy.finditer(e):
            try:
                rows.append({
                    "ts": ts, "date": ts.date().isoformat(), "time": ts.strftime("%H:%M"),
                    "alert_type": atype, "symbol": bm.group(1).upper(),
                    "alert_price": float(bm.group(2)),
                    "stop": float(bm.group(3)),
                    "target1": float(bm.group(4)),
                    "target2": float(bm.group(5)),
                    "source": "screener_alerts",
                })
            except (ValueError, IndexError):
                pass
        for gm in pat_grade.finditer(e):
            try:
                rows.append({
                    "ts": ts, "date": ts.date().isoformat(), "time": ts.strftime("%H:%M"),
                    "alert_type": atype, "symbol": gm.group(1).upper(),
                    "alert_price": float(gm.group(2)),
                    "stop": float(gm.group(3)),
                    "target1": float(gm.group(4)),
                    "target2": None,
                    "source": "screener_alerts",
                })
            except (ValueError, IndexError):
                pass

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # Drop duplicates: same (date, symbol, alert_type) — keep earliest
    df = df.sort_values("ts").drop_duplicates(subset=["date", "symbol", "alert_type"], keep="first")
    return df


# ------------------------------------------------------------------
# 2) Parse anomaly_history.csv
# ------------------------------------------------------------------
def parse_anomaly_csv() -> pd.DataFrame:
    if not ANOMALY_CSV.exists():
        return pd.DataFrame()
    df = pd.read_csv(ANOMALY_CSV)
    df["ts"] = pd.to_datetime(df["When"])
    df["date"] = df["ts"].dt.date.astype(str)
    df["time"] = df["ts"].dt.strftime("%H:%M")
    df["alert_type"] = df["Tier"].map({"A": "grade_a", "B": "grade_b", "C": "pre_move"}).fillna("other")
    out = pd.DataFrame({
        "ts": df["ts"], "date": df["date"], "time": df["time"],
        "alert_type": df["alert_type"], "symbol": df["Symbol"].astype(str).str.upper(),
        "alert_price": pd.to_numeric(df["PriceNow"], errors="coerce"),
        "stop": pd.to_numeric(df["SellIfBelow"], errors="coerce"),
        "target1": pd.to_numeric(df["TakeProfit1"], errors="coerce"),
        "target2": pd.to_numeric(df["TakeProfit2"], errors="coerce"),
        "source": "anomaly_history",
    })
    out = out[out["alert_price"].notna() & (out["alert_price"] > 0)].copy()
    out = out.sort_values("ts").drop_duplicates(subset=["date", "symbol", "alert_type"], keep="first")
    return out


# ------------------------------------------------------------------
# 3) Data loaders — find OHLC for symbol on/after date
# ------------------------------------------------------------------
_intraday_cache: dict = {}
_daily_cache: dict = {}


def load_daily(symbol: str) -> pd.DataFrame:
    if symbol in _daily_cache:
        return _daily_cache[symbol]
    # Try live cache first (recent)
    fp = DAY_CACHE / f"{symbol}.csv"
    df = pd.DataFrame()
    if fp.exists():
        df = pd.read_csv(fp)
    # Append from backfill folders if needed (older history)
    for folder in sorted(DAY_BACKFILL.glob("*")):
        if not folder.is_dir(): continue
        bf = folder / f"{symbol}.csv"
        if bf.exists():
            d = pd.read_csv(bf)
            df = pd.concat([df, d], ignore_index=True)
    if df.empty:
        _daily_cache[symbol] = df
        return df
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce").dt.tz_convert(None)
    df["date"] = df["datetime"].dt.date.astype(str)
    df = df.dropna(subset=["datetime"]).drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    _daily_cache[symbol] = df
    return df


def load_intraday_day(symbol: str, target_date: str) -> pd.DataFrame:
    key = (symbol, target_date)
    if key in _intraday_cache:
        return _intraday_cache[key]
    fp = INTRADAY_5M / f"{symbol}.csv"
    if not fp.exists():
        _intraday_cache[key] = pd.DataFrame()
        return _intraday_cache[key]
    df = pd.read_csv(fp)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce").dt.tz_convert(None)
    df["date"] = df["datetime"].dt.date.astype(str)
    d = df[df["date"] == target_date].sort_values("datetime").reset_index(drop=True)
    _intraday_cache[key] = d
    return d


# ------------------------------------------------------------------
# 4) Compute outcomes for one alert
# ------------------------------------------------------------------
def compute_outcomes(alert: dict) -> dict:
    sym = alert["symbol"]
    target_date = alert["date"]
    entry = float(alert["alert_price"])
    t1 = float(alert.get("target1") or 0)
    stop = float(alert.get("stop") or 0)
    alert_ts = alert["ts"] if isinstance(alert["ts"], pd.Timestamp) else pd.Timestamp(alert["ts"])

    out = {
        "symbol": sym, "date": target_date, "alert_type": alert["alert_type"],
        "alert_price": entry, "t1": t1, "stop": stop,
    }

    # --- INTRADAY outcomes (same day) ---
    intra = load_intraday_day(sym, target_date)
    if not intra.empty:
        after = intra[intra["datetime"] >= alert_ts]
        if not after.empty:
            day_high = float(after["high"].max())
            day_low = float(after["low"].min())
            day_close = float(after.iloc[-1]["close"])
            out["intra_max_pct"] = round((day_high - entry) / entry * 100, 3)
            out["intra_min_pct"] = round((day_low - entry) / entry * 100, 3)
            out["intra_eod_pct"] = round((day_close - entry) / entry * 100, 3)
            # Strategy A: hold to EOD
            out["strategy_eod_pct"] = out["intra_eod_pct"] - INTRADAY_COST_PCT
            # Strategy B: mechanical T1 (sell when first hit, else EOD)
            if t1 > 0:
                hit_t1 = False
                for _, b in after.iterrows():
                    if b["high"] >= t1:
                        hit_t1 = True; break
                if hit_t1:
                    out["strategy_t1_pct"] = round((t1 - entry) / entry * 100 - INTRADAY_COST_PCT, 3)
                else:
                    out["strategy_t1_pct"] = out["strategy_eod_pct"]
            # Strategy C: T1 with -3% stop (whichever fires first)
            stop3 = entry * 0.97
            exit_px = None; reason_3 = "eod"
            for _, b in after.iterrows():
                if b["low"] <= stop3:
                    exit_px = stop3; reason_3 = "stop3"; break
                if t1 > 0 and b["high"] >= t1:
                    exit_px = t1; reason_3 = "t1"; break
            if exit_px is None:
                exit_px = day_close
            out["strategy_t1_stop3_pct"] = round((exit_px / entry - 1) * 100 - INTRADAY_COST_PCT, 3)
            out["strategy_t1_stop3_reason"] = reason_3

    # --- DAILY outcomes (next N days) ---
    daily = load_daily(sym)
    if not daily.empty:
        # Find the row for target_date or first after
        future = daily[daily["date"] > target_date].sort_values("date").reset_index(drop=True)
        for n_days, label in [(3, "3d"), (5, "5d"), (10, "10d"), (20, "20d")]:
            window = future.head(n_days)
            if window.empty: continue
            win_high = float(window["high"].max())
            win_low = float(window["low"].min())
            win_close = float(window.iloc[-1]["close"])
            out[f"{label}_max_pct"] = round((win_high - entry) / entry * 100, 3)
            out[f"{label}_min_pct"] = round((win_low - entry) / entry * 100, 3)
            out[f"{label}_end_pct"] = round((win_close - entry) / entry * 100, 3)
            out[f"strategy_hold_{label}_pct"] = round(out[f"{label}_end_pct"] - DELIVERY_COST_PCT, 3)
        # Stop -5% / Target +10% over 20 days
        stop5 = entry * 0.95; targ10 = entry * 1.10
        exit_px = None; reason = "20d_close"
        for _, b in future.head(20).iterrows():
            if b["low"] <= stop5:
                exit_px = stop5; reason = "stop5"; break
            if b["high"] >= targ10:
                exit_px = targ10; reason = "target10"; break
        if exit_px is None and not future.empty:
            exit_px = float(future.iloc[min(19, len(future)-1)]["close"])
            reason = "20d_close"
        if exit_px:
            out["strategy_swing_t10_s5_pct"] = round((exit_px / entry - 1) * 100 - DELIVERY_COST_PCT, 3)
            out["strategy_swing_t10_s5_reason"] = reason
    return out


# ------------------------------------------------------------------
# 5) Main analysis pipeline
# ------------------------------------------------------------------
def main():
    print("[i] Parsing screener_alerts.txt ...")
    sa = parse_screener_alerts()
    print(f"    extracted {len(sa)} alerts with full price data")

    print("[i] Parsing anomaly_history.csv ...")
    ah = parse_anomaly_csv()
    print(f"    extracted {len(ah)} anomaly alerts")

    all_alerts = pd.concat([sa, ah], ignore_index=True) if not sa.empty else ah
    all_alerts = all_alerts.sort_values("ts").drop_duplicates(subset=["date", "symbol", "alert_type"], keep="first")
    print(f"[i] Combined unique alerts: {len(all_alerts)}")

    # Save raw alerts
    all_alerts.to_csv(OUT_DIR / "all_alerts_raw.csv", index=False)
    print(f"    → wrote {OUT_DIR/'all_alerts_raw.csv'}")

    if all_alerts.empty:
        return 0

    # Compute outcomes
    print(f"[i] Computing outcomes for {len(all_alerts)} alerts ...")
    outcomes = []
    for i, row in enumerate(all_alerts.to_dict("records"), 1):
        if i % 200 == 0:
            print(f"    {i}/{len(all_alerts)} processed")
        try:
            outcomes.append(compute_outcomes(row))
        except Exception as exc:
            print(f"    [!] Error on {row.get('symbol')} {row.get('date')}: {exc}")

    out_df = pd.DataFrame(outcomes)
    out_df.to_csv(OUT_DIR / "alert_outcomes.csv", index=False)
    print(f"    → wrote {OUT_DIR/'alert_outcomes.csv'} ({len(out_df)} rows)")

    # ------------------- SUMMARY -------------------
    print("\n" + "="*70)
    print("ALERT PERFORMANCE BY TYPE")
    print("="*70)

    strat_cols = [c for c in out_df.columns if c.startswith("strategy_")]
    by_type = out_df.groupby("alert_type")

    # Per-strategy summary by alert type
    summary_rows = []
    for alert_type, sub in by_type:
        n = len(sub)
        row = {"alert_type": alert_type, "n": n}
        for col in ["intra_max_pct", "intra_eod_pct", "5d_max_pct", "10d_max_pct", "20d_max_pct"] + strat_cols:
            if col in sub.columns:
                vals = pd.to_numeric(sub[col], errors="coerce").dropna()
                if not vals.empty:
                    row[f"{col}_avg"] = round(vals.mean(), 3)
                    row[f"{col}_win_rate"] = round((vals > 0).mean() * 100, 1)
        summary_rows.append(row)
    summary = pd.DataFrame(summary_rows).set_index("alert_type")
    summary.to_csv(OUT_DIR / "summary_by_type.csv")
    print(f"\n[+] Summary by alert type → {OUT_DIR/'summary_by_type.csv'}")

    print(f"\n=== AVERAGE NET % BY STRATEGY (per alert type) ===")
    print(f"{'type':12} {'n':>6} {'EOD':>8} {'T1+stop3':>10} {'hold_5d':>9} {'hold_10d':>9} {'hold_20d':>9} {'swing_t10_s5':>14}")
    for alert_type, sub in by_type:
        n = len(sub)
        def avg(col):
            v = pd.to_numeric(sub.get(col, pd.Series()), errors="coerce").dropna()
            return v.mean() if not v.empty else float("nan")
        print(f"{alert_type:12} {n:>6} "
              f"{avg('strategy_eod_pct'):>+8.3f} "
              f"{avg('strategy_t1_stop3_pct'):>+10.3f} "
              f"{avg('strategy_hold_5d_pct'):>+9.3f} "
              f"{avg('strategy_hold_10d_pct'):>+9.3f} "
              f"{avg('strategy_hold_20d_pct'):>+9.3f} "
              f"{avg('strategy_swing_t10_s5_pct'):>+14.3f}")

    # ------------------- PORTFOLIO SIMULATION -------------------
    # Assume ₹50K per trade, sequential (one trade at a time per day)
    # Pick best strategy per alert_type
    print(f"\n=== PORTFOLIO P&L (₹50K/trade, sequential) ===")
    print(f"Picking best strategy per alert_type based on average per-trade net %.")
    for alert_type, sub in by_type:
        # Find best strategy column for this type
        best_col = None; best_avg = -99
        for col in strat_cols:
            v = pd.to_numeric(sub.get(col, pd.Series()), errors="coerce").dropna()
            if len(v) > 0 and v.mean() > best_avg:
                best_avg = v.mean(); best_col = col
        if not best_col: continue
        vals = pd.to_numeric(sub[best_col], errors="coerce").dropna()
        n_trades = len(vals)
        cum_pct = vals.sum()
        net_inr = (cum_pct / 100) * POSITION_INR
        print(f"  {alert_type:12} best={best_col:30} n={n_trades:>5}  cum={cum_pct:>+8.2f}%  net=₹{net_inr:>+12,.0f}")

    print(f"\n[+] All outputs in {OUT_DIR}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
