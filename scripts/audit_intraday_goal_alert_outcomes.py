#!/usr/bin/env python3
"""Audit structured intraday goal alert outcomes with Kite minute candles.

The live monitor journals every structured alert under:
results/Data/intraday_goal_alerts/intraday_goal_alerts_YYYY-MM-DD.csv

This script answers the question that matters before scaling real money:
after the alert was actually sent, did the stock hit stop, T1, T2, or only
show hindsight upside that was not tradeable?
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return float(default)


DEFAULT_BUY_COST_PCT = _float_env("CHARAN_EST_BUY_COST_PCT", 0.00118)
DEFAULT_SELL_COST_PCT = _float_env("CHARAN_EST_SELL_COST_PCT", 0.00104)
DEFAULT_SLIPPAGE_BPS = _float_env("CHARAN_EST_SLIPPAGE_BPS", 3.0)


@dataclass(frozen=True)
class TradeCost:
    gross_return_pct: float
    net_return_pct: float
    entry_exec: float
    exit_exec: float


def apply_costs(
    entry_price: float,
    exit_price: float,
    buy_cost_pct: float = DEFAULT_BUY_COST_PCT,
    sell_cost_pct: float = DEFAULT_SELL_COST_PCT,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> TradeCost:
    if entry_price <= 0 or exit_price <= 0:
        return TradeCost(0.0, 0.0, entry_price, exit_price)
    slip = max(0.0, slippage_bps) / 10000.0
    entry_exec = entry_price * (1 + slip)
    exit_exec = exit_price * (1 - slip)
    gross = (exit_price - entry_price) / entry_price * 100.0
    entry_charges = entry_exec * max(0.0, buy_cost_pct)
    exit_charges = exit_exec * max(0.0, sell_cost_pct)
    capital = entry_exec + entry_charges
    pnl = exit_exec - entry_exec - entry_charges - exit_charges
    net = pnl / capital * 100.0 if capital > 0 else 0.0
    return TradeCost(round(gross, 3), round(net, 3), round(entry_exec, 2), round(exit_exec, 2))


def classify_lane(action: str, checks: str) -> str:
    text = f"{action or ''} {checks or ''}".lower()
    if "enginewatch" in text or "engine confirmed watch" in text:
        return "engine_watch"
    if "top-gainer" in text or "radar" in text:
        return "top_gainer_radar"
    if "grade-a" in text or "runner" in text or "[a]" in text:
        return "grade_a_anomaly"
    if "grade-b" in text or "[b]" in text:
        return "grade_b_anomaly"
    if "grade-c" in text or "[c]" in text:
        return "grade_c_anomaly"
    if "goalmode" in text:
        return "goal_other"
    return "other"


def first_event(
    candles: pd.DataFrame,
    entry_price: float,
    stop: float,
    target1: float,
    target2: float,
) -> tuple[str, Optional[pd.Timestamp], float]:
    """Return first stop/T1/T2 event using conservative same-candle ordering."""
    for ts, row in candles.iterrows():
        low = float(row["low"])
        high = float(row["high"])
        if stop > 0 and low <= stop:
            return "stop_first", ts, stop
        if target2 > 0 and high >= target2:
            return "t2_first", ts, target2
        if target1 > 0 and high >= target1:
            return "t1_first", ts, target1
    close_price = float(candles["close"].iloc[-1]) if not candles.empty else entry_price
    return "eod", candles.index[-1] if not candles.empty else None, close_price


def prepare_candles(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "datetime" in out.columns:
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.set_index("datetime")
    elif "date" in out.columns:
        out["datetime"] = pd.to_datetime(out["date"], errors="coerce")
        out = out.set_index("datetime")
    out = out.sort_index()
    if getattr(out.index, "tz", None) is not None:
        out.index = out.index.tz_localize(None)
    return out.dropna(subset=["open", "high", "low", "close"], how="any")


def load_alert_files(data_dir: Path, date: Optional[str], start_date: Optional[str], end_date: Optional[str]) -> list[Path]:
    alert_dir = data_dir / "intraday_goal_alerts"
    if date:
        return [alert_dir / f"intraday_goal_alerts_{date}.csv"]
    files = sorted(alert_dir.glob("intraday_goal_alerts_20*.csv"))
    if start_date:
        files = [p for p in files if p.stem.rsplit("_", 1)[-1] >= start_date]
    if end_date:
        files = [p for p in files if p.stem.rsplit("_", 1)[-1] <= end_date]
    return files


def load_alerts(files: Iterable[Path], first_per_symbol_per_day: bool) -> pd.DataFrame:
    parts = [pd.read_csv(path) for path in files if path.exists()]
    if not parts:
        return pd.DataFrame()
    df = pd.concat(parts, ignore_index=True)
    for col in ["GeneratedAt", "SentAt"]:
        df[col] = pd.to_datetime(df[col], errors="coerce")
    for col in ["PriceNow", "StopLoss", "Target1", "Target2", "SignalStrengthPct", "ChangePct"]:
        df[col] = pd.to_numeric(df.get(col), errors="coerce")
    df = df.dropna(subset=["SentAt", "Symbol", "PriceNow"]).copy()
    df["Symbol"] = df["Symbol"].astype(str).str.upper().str.strip()
    df["date"] = df["SentAt"].dt.date.astype(str)
    df["lane"] = [classify_lane(a, c) for a, c in zip(df.get("Action", ""), df.get("Checks", ""))]
    df["send_delay_sec"] = (df["SentAt"] - df["GeneratedAt"]).dt.total_seconds()
    df = df.sort_values(["SentAt", "Symbol"])
    if first_per_symbol_per_day:
        df = df.drop_duplicates(subset=["date", "Symbol", "lane"], keep="first")
    return df.reset_index(drop=True)


def _load_local_history(data_dir: Path, symbol: str, sent_at: datetime, interval: str) -> pd.DataFrame:
    candidates: list[Path] = []
    candidates.append(data_dir / "kite_history_cache" / interval / f"{symbol}.csv")
    if interval == "minute":
        candle_day = sent_at.date().isoformat()
        candidates.extend(sorted((data_dir / "kiteticker_candles" / "1minute").glob(f"kiteticker_*_1minute_{candle_day}.csv")))
    backfill_root = data_dir / "kite_history_backfill" / interval
    if backfill_root.exists():
        candidates.extend(sorted(backfill_root.glob(f"*/{symbol}.csv")))
    parts = []
    target_date = sent_at.date()
    for path in candidates:
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        candles = prepare_candles(df)
        if candles.empty:
            continue
        if "symbol" in candles.columns:
            candles = candles[candles["symbol"].astype(str).str.upper().eq(str(symbol).upper())]
            if candles.empty:
                continue
        same_day = candles[candles.index.date == target_date]
        if not same_day.empty:
            parts.append(same_day)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts).sort_index().drop_duplicates()


def fetch_history(
    provider,
    symbol: str,
    sent_at: datetime,
    delay_sec: float,
    data_dir: Path,
    local_interval: str,
    prefer_local: bool,
) -> tuple[pd.DataFrame, Optional[str]]:
    from_dt = sent_at.replace(second=0, microsecond=0)
    to_dt = sent_at.replace(hour=15, minute=30, second=0, microsecond=0)
    if to_dt <= from_dt:
        return pd.DataFrame(), "alert after market close"
    if prefer_local:
        local = _load_local_history(data_dir, symbol, sent_at, local_interval)
        if not local.empty:
            return local[local.index >= from_dt], None
    try:
        raw = provider.get_history_range(symbol, interval="minute", from_date=from_dt, to_date=to_dt)
    except Exception as exc:
        local = _load_local_history(data_dir, symbol, sent_at, local_interval)
        if not local.empty:
            return local[local.index >= from_dt], f"kite fetch failed; used local {local_interval}: {exc}"
        return pd.DataFrame(), str(exc)
    finally:
        if delay_sec > 0:
            time.sleep(delay_sec)
    return prepare_candles(raw), None


def evaluate_alert(row: pd.Series, candles: pd.DataFrame, entry_model: str, fetch_error: Optional[str] = None) -> dict:
    sent_at = row["SentAt"].to_pydatetime()
    after = candles[candles.index >= sent_at.replace(second=0, microsecond=0)].copy()
    result = row.to_dict()
    result.update(
        {
            "has_minute_data": False,
            "fetch_error": fetch_error,
            "entry_model": entry_model,
            "entry_price": None,
            "entry_time": None,
            "alert_price_inside_sent_bar": None,
            "mfe_pct": None,
            "mae_pct": None,
            "first_event": "no_data",
            "first_event_time": None,
            "exit_price": None,
            "gross_return_pct": None,
            "net_return_pct": None,
            "hit_1pct": False,
            "hit_t1": False,
            "hit_t2": False,
            "hit_stop": False,
        }
    )
    if after.empty:
        return result

    sent_bar = after.iloc[0]
    alert_price = float(row["PriceNow"])
    inside = float(sent_bar["low"]) <= alert_price <= float(sent_bar["high"])
    if entry_model == "next_open" and len(after) > 1:
        entry_time = after.index[1]
        entry_price = float(after.iloc[1]["open"])
        trade_candles = after.iloc[1:]
    else:
        entry_time = after.index[0]
        entry_price = alert_price
        trade_candles = after

    if trade_candles.empty or entry_price <= 0:
        return result

    stop = float(row.get("StopLoss") or 0.0)
    t1 = float(row.get("Target1") or 0.0)
    t2 = float(row.get("Target2") or 0.0)
    high = float(trade_candles["high"].max())
    low = float(trade_candles["low"].min())
    event, event_time, exit_price = first_event(trade_candles, entry_price, stop, t1, t2)
    costs = apply_costs(entry_price, exit_price)
    result.update(
        {
            "has_minute_data": True,
            "entry_price": round(entry_price, 2),
            "entry_time": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "alert_price_inside_sent_bar": bool(inside),
            "mfe_pct": round((high - entry_price) / entry_price * 100.0, 3),
            "mae_pct": round((low - entry_price) / entry_price * 100.0, 3),
            "first_event": event,
            "first_event_time": event_time.strftime("%Y-%m-%d %H:%M:%S") if event_time is not None else None,
            "exit_price": round(exit_price, 2),
            "gross_return_pct": costs.gross_return_pct,
            "net_return_pct": costs.net_return_pct,
            "hit_1pct": bool(high >= entry_price * 1.01),
            "hit_t1": bool(t1 > 0 and high >= t1),
            "hit_t2": bool(t2 > 0 and high >= t2),
            "hit_stop": bool(stop > 0 and low <= stop),
        }
    )
    return result


def print_summary(df: pd.DataFrame) -> None:
    if df.empty:
        print("No evaluated alerts.")
        return
    evaluated = df[df["has_minute_data"] == True].copy()
    print(f"Alerts evaluated with minute data: {len(evaluated)} / {len(df)}")
    if evaluated.empty:
        return
    print(f"Median send delay: {evaluated['send_delay_sec'].median():.1f}s")
    print(f"Alert price inside sent minute bar: {evaluated['alert_price_inside_sent_bar'].mean()*100:.1f}%")
    print("\nBy lane:")
    for lane, group in evaluated.groupby("lane"):
        print(
            f"  {lane:<18} n={len(group):>3} "
            f"net={group['net_return_pct'].mean():>+6.2f}% "
            f"wr={(group['net_return_pct'] > 0).mean()*100:>5.1f}% "
            f"1pct={group['hit_1pct'].mean()*100:>5.1f}% "
            f"t1={group['hit_t1'].mean()*100:>5.1f}% "
            f"stop={group['hit_stop'].mean()*100:>5.1f}% "
            f"mfe={group['mfe_pct'].mean():>+6.2f}% "
            f"mae={group['mae_pct'].mean():>+6.2f}%"
        )
    print("\nWorst net outcomes:")
    cols = ["date", "SentAt", "Symbol", "lane", "Action", "entry_price", "first_event", "net_return_pct", "mfe_pct", "mae_pct"]
    print(evaluated.sort_values("net_return_pct").head(12)[cols].to_string(index=False))
    print("\nBest MFE after sent time:")
    print(evaluated.sort_values("mfe_pct", ascending=False).head(12)[cols].to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit structured intraday goal alert outcomes")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "results" / "Data"), help="results/Data directory")
    parser.add_argument("--date", help="Single date YYYY-MM-DD")
    parser.add_argument("--start-date", help="Start date YYYY-MM-DD")
    parser.add_argument("--end-date", help="End date YYYY-MM-DD")
    parser.add_argument("--entry-model", choices=["alert_price", "next_open"], default="next_open")
    parser.add_argument("--prefer-local", action="store_true", help="Use cached/backfilled local candles before Kite")
    parser.add_argument("--local-interval", choices=["minute", "5minute", "15minute"], default="5minute")
    parser.add_argument("--all-alerts", action="store_true", help="Keep repeated alerts per symbol/lane/day")
    parser.add_argument("--delay-sec", type=float, default=0.25)
    parser.add_argument("--out", help="Output CSV path")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    files = load_alert_files(data_dir, args.date, args.start_date, args.end_date)
    alerts = load_alerts(files, first_per_symbol_per_day=not args.all_alerts)
    if alerts.empty:
        print("No structured intraday goal alerts found.")
        return 1

    from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider

    provider = KiteHistoricalDataProvider()
    rows: list[dict] = []
    cache: dict[tuple[str, str], tuple[pd.DataFrame, Optional[str]]] = {}
    for idx, row in alerts.iterrows():
        key = (row["Symbol"], row["date"])
        if key not in cache:
            cache[key] = fetch_history(
                provider,
                row["Symbol"],
                row["SentAt"].to_pydatetime(),
                max(0.0, args.delay_sec),
                data_dir=data_dir,
                local_interval=args.local_interval,
                prefer_local=bool(args.prefer_local),
            )
        candles, fetch_error = cache[key]
        rows.append(evaluate_alert(row, candles, args.entry_model, fetch_error=fetch_error))
        if (idx + 1) % 20 == 0:
            print(f"Evaluated {idx + 1}/{len(alerts)} alerts...")

    out_df = pd.DataFrame(rows)
    out_path = Path(args.out) if args.out else data_dir / "intraday_goal_alert_outcomes" / "intraday_goal_alert_outcomes_latest.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False)
    print_summary(out_df)
    print(f"\nWrote: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
