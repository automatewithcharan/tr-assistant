#!/usr/bin/env python3
"""Backtest PKScreener Discord alert lanes with Kite minute candles.

Parses exported Discord text for newer PKScreener Live Alert blocks and
evaluates each alert from its alert minute to the same-day close.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


BOT_BLOCK_RE = re.compile(r"^\[(\d{2}-\d{2}-\d{4} \d{2}:\d{2})\] Spidey Bot\s*$", re.M)
LANE_RE = re.compile(r"^(?P<prefix>.+?)\s+\*\*(?P<action>[^*]+)\*\*\s+`(?P<symbol>[^`]+)`", re.M)
PRICE_RE = re.compile(r"At alert:\s*₹(?P<price>[\d.]+)")
STOP_RE = re.compile(r"Stop Loss\s*`₹?(?P<stop>[\d.]+)`")
T1_RE = re.compile(r"Target 1\s*`₹?(?P<t1>[\d.]+)`")
T2_RE = re.compile(r"Target 2\s*`₹?(?P<t2>[\d.]+)`")

LANE_KEYWORDS = {
    "ob": ["OB Sustained"],
    "anomaly": ["Grade-A Runner", "Grade-B Anomaly", "Pre-Move Watch"],
    "direct": ["Strong Buy Now", "Buy Now"],
    "momentum": ["Momentum Buy", "Runner Continuation"],
}


@dataclass
class Alert:
    when: datetime
    symbol: str
    action: str
    price: float
    stop: float
    target1: float
    target2: float

    @property
    def lane(self) -> str:
        text = self.action
        if "OB" in text:
            return "ob"
        if "Anomaly" in text or "Runner" in text or "Pre-Move" in text:
            return "anomaly"
        if "Momentum" in text:
            return "momentum"
        if text in {"Strong Buy Now", "Buy Now"}:
            return "direct"
        return "other"


def _float_match(regex: re.Pattern, text: str, default: float = 0.0) -> float:
    match = regex.search(text)
    if not match:
        return default
    for value in match.groupdict().values():
        try:
            return float(value)
        except Exception:
            return default
    return default


def iter_bot_blocks(text: str) -> Iterable[tuple[datetime, str]]:
    matches = list(BOT_BLOCK_RE.finditer(text))
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        yield datetime.strptime(match.group(1), "%d-%m-%Y %H:%M"), text[start:end].strip()


def parse_alerts(path: Path, date: Optional[str], lanes: set[str], first_per_symbol: bool) -> List[Alert]:
    text = path.read_text(encoding="utf-8", errors="replace")
    target_date = datetime.strptime(date, "%Y-%m-%d").date() if date else None
    alerts: List[Alert] = []

    for when, body in iter_bot_blocks(text):
        if target_date is not None and when.date() != target_date:
            continue
        if "PKScreener Live Alert" not in body:
            continue
        chunks = re.split(r"\n(?=[^\n]*\*\*[^*]+\*\*\s+`[A-Z0-9&_.-]+`)", body)
        for chunk in chunks:
            lane_match = LANE_RE.search(chunk)
            price = _float_match(PRICE_RE, chunk)
            if not lane_match or price <= 0:
                continue
            action = lane_match.group("action").strip()
            alert = Alert(
                when=when,
                symbol=lane_match.group("symbol").strip().upper(),
                action=action,
                price=price,
                stop=_float_match(STOP_RE, chunk),
                target1=_float_match(T1_RE, chunk),
                target2=_float_match(T2_RE, chunk),
            )
            if lanes and alert.lane not in lanes:
                continue
            alerts.append(alert)

    alerts.sort(key=lambda item: item.when)
    if not first_per_symbol:
        return alerts
    seen = set()
    out = []
    for alert in alerts:
        key = (alert.when.date(), alert.symbol, alert.lane)
        if key in seen:
            continue
        seen.add(key)
        out.append(alert)
    return out


def evaluate(alerts: List[Alert], delay_sec: float) -> pd.DataFrame:
    from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider

    provider = KiteHistoricalDataProvider()
    rows = []
    for idx, alert in enumerate(alerts, start=1):
        to_dt = alert.when.replace(hour=15, minute=30, second=0)
        df = provider.get_history_range(
            alert.symbol,
            interval="minute",
            from_date=alert.when,
            to_date=to_dt,
        )
        if df is None or df.empty:
            rows.append({**alert.__dict__, "lane": alert.lane, "status": "no_data"})
            time.sleep(delay_sec)
            continue
        high = float(df["high"].astype(float).max())
        low = float(df["low"].astype(float).min())
        close = float(df["close"].astype(float).iloc[-1])
        rows.append({
            **alert.__dict__,
            "lane": alert.lane,
            "status": "ok",
            "close": round(close, 2),
            "eod_return_pct": round((close - alert.price) / alert.price * 100.0, 2),
            "max_fav_pct": round((high - alert.price) / alert.price * 100.0, 2),
            "max_adv_pct": round((low - alert.price) / alert.price * 100.0, 2),
            "stop_hit": bool(alert.stop > 0 and low <= alert.stop),
            "t1_hit": bool(alert.target1 > 0 and high >= alert.target1),
            "t2_hit": bool(alert.target2 > 0 and high >= alert.target2),
        })
        if idx % 10 == 0:
            print(f"evaluated {idx}/{len(alerts)}...")
        time.sleep(delay_sec)
    return pd.DataFrame(rows)


def print_summary(df: pd.DataFrame) -> None:
    if df.empty:
        print("No rows.")
        return
    print(f"Parsed alerts: {len(df)}")
    print(df["lane"].value_counts(dropna=False).to_string())
    ok = df[df["status"] == "ok"].copy() if "status" in df.columns else df
    if ok.empty:
        print("No evaluated rows. Kite historical data may be unavailable.")
        return
    print(f"\nEvaluated rows: {len(ok)}")
    for lane, group in ok.groupby("lane"):
        print(
            f"{lane:<8} n={len(group):>3} "
            f"win={(group['eod_return_pct'] > 0).mean()*100:>5.1f}% "
            f"avg_eod={group['eod_return_pct'].mean():>+6.2f}% "
            f"med_eod={group['eod_return_pct'].median():>+6.2f}% "
            f"avg_mfe={group['max_fav_pct'].mean():>+6.2f}% "
            f"avg_mae={group['max_adv_pct'].mean():>+6.2f}% "
            f"stop={group['stop_hit'].mean()*100:>5.1f}% "
            f"t1={group['t1_hit'].mean()*100:>5.1f}%"
        )
    print("\nWorst rows:")
    print(ok.sort_values("eod_return_pct").head(12)[[
        "when", "symbol", "lane", "action", "price", "close", "eod_return_pct", "max_adv_pct"
    ]].to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest OB/anomaly/direct alert lanes")
    parser.add_argument("--discord-file", default="Screener Alerts.txt")
    parser.add_argument("--date", help="YYYY-MM-DD. Omit to parse all dates.")
    parser.add_argument("--lanes", default="ob,anomaly", help="Comma-separated: ob,anomaly,direct,momentum,other")
    parser.add_argument("--all-alerts", action="store_true", help="Evaluate repeated alerts too")
    parser.add_argument("--delay-sec", type=float, default=0.25)
    parser.add_argument("--out", default="results/Data/alert_lane_backtest_latest.csv")
    args = parser.parse_args()

    lanes = {x.strip().lower() for x in str(args.lanes).split(",") if x.strip()}
    alerts = parse_alerts(Path(args.discord_file), args.date, lanes, first_per_symbol=not args.all_alerts)
    if not alerts:
        print("No matching alerts parsed.")
        return 1
    print(f"Loaded {len(alerts)} alert(s).")
    df = evaluate(alerts, max(0.0, float(args.delay_sec)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    print_summary(df)
    print(f"\nSaved: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
