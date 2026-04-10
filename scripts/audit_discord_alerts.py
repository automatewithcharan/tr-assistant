#!/usr/bin/env python3
"""
Audit actionable Discord buy alerts for a single trading day using Kite minute data.

This script parses the exported Discord channel text, keeps the first actionable
buy alert per symbol, and measures what happened afterward:
  - close-to-alert return
  - max upside / downside from alert price
  - stop / T1 / T2 hit rates
  - malformed alerts (targets below current price, stop above price)
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)


def _load_float_env(name: str, default: float) -> float:
    raw_value = os.environ.get(name, default)
    try:
        return float(raw_value)
    except Exception:
        return float(default)


DEFAULT_BUY_COST_PCT = _load_float_env("CHARAN_EST_BUY_COST_PCT", 0.00118)
DEFAULT_SELL_COST_PCT = _load_float_env("CHARAN_EST_SELL_COST_PCT", 0.00104)
DEFAULT_SLIPPAGE_BPS = _load_float_env("CHARAN_EST_SLIPPAGE_BPS", 0.0)


def _apply_trade_costs(entry_price: float, exit_price: float, buy_cost_pct: float, sell_cost_pct: float, slippage_bps: float) -> dict:
    if entry_price <= 0 or exit_price <= 0:
        return {
            "exec_entry_price": entry_price,
            "exec_exit_price": exit_price,
            "gross_return_pct": 0.0,
            "net_return_pct": 0.0,
            "entry_charges": 0.0,
            "exit_charges": 0.0,
            "slippage_cost": 0.0,
        }

    slip_frac = max(0.0, slippage_bps) / 10000.0
    exec_entry = entry_price * (1 + slip_frac)
    exec_exit = exit_price * (1 - slip_frac)
    entry_charges = exec_entry * max(0.0, buy_cost_pct)
    exit_charges = exec_exit * max(0.0, sell_cost_pct)
    gross_return_pct = ((exit_price - entry_price) / entry_price) * 100
    net_capital = exec_entry + entry_charges
    net_pnl = exec_exit - exec_entry - entry_charges - exit_charges
    net_return_pct = (net_pnl / net_capital) * 100 if net_capital > 0 else 0.0
    slippage_cost = max(0.0, exec_entry - entry_price) + max(0.0, exit_price - exec_exit)
    return {
        "exec_entry_price": round(exec_entry, 2),
        "exec_exit_price": round(exec_exit, 2),
        "gross_return_pct": round(gross_return_pct, 2),
        "net_return_pct": round(net_return_pct, 2),
        "entry_charges": round(entry_charges, 2),
        "exit_charges": round(exit_charges, 2),
        "slippage_cost": round(slippage_cost, 2),
    }


BOT_BLOCK_RE = re.compile(r"^\[(\d{2}-\d{2}-\d{4} \d{2}:\d{2})\] Spidey Bot\s*$", re.M)
MAIN_BUY_RE = re.compile(
    r"\*\*🟢 BUY — `(?P<sym>[^`]+)`\*\*.*?"
    r"Price now: `₹(?P<price>[\d.]+)`.*?"
    r"EXIT if price drops to `₹(?P<stop>[\d.]+)`.*?"
    r"SELL at `₹(?P<t1>[\d.]+)`.*?or `₹(?P<t2>[\d.]+)`",
    re.S,
)
RESCAN_BUY_RE = re.compile(
    r"\*\*🔍 MISSED EARLIER, NOW STRONG — `(?P<sym>[^`]+)`\*\*.*?"
    r"Price now: `₹(?P<price>[\d.]+)`.*?"
    r"EXIT if price drops to `₹(?P<stop>[\d.]+)`.*?"
    r"SELL at `₹(?P<t1>[\d.]+)`.*?or `₹(?P<t2>[\d.]+)`",
    re.S,
)
DEMAND_BUY_RE = re.compile(
    r"\*\*🔮 (?P<sym>[A-Z0-9&\-]+) — Buyers are pushing this stock up steadily\*\*.*?"
    r"Buy \*\*\d+ shares\*\* at ~`₹(?P<price>[\d.]+)`.*?"
    r"EXIT if price drops to `₹(?P<stop>[\d.]+)`.*?"
    r"SELL near `₹(?P<t2>[\d.]+)`",
    re.S,
)
MOMENTUM_BUY_RE = re.compile(
    r"\*\*🔵 MOMENTUM BUY \[[A-Z]\] — `(?P<sym>[^`]+)`\*\*.*?"
    r"Price `₹(?P<price>[\d.]+)`.*?"
    r"Stop Loss `₹(?P<stop>[\d.]+)` \| 🟡 Target 1 `₹(?P<t1>[\d.]+)` \| 🟢 Target 2 `₹(?P<t2>[\d.]+)`",
    re.S,
)
LEGACY_BUY_RE = re.compile(
    r"\*\*🟢 BUY `(?P<sym>[^`]+)`.*?"
    r"Price `₹(?P<price>[\d.]+)`.*?"
    r"Stop Loss `(?P<stop>[\d.]+)` \| Target 1 `(?P<t1>[\d.]+)` \| Target 2 `(?P<t2>[\d.]+)`",
    re.S,
)


@dataclass
class Alert:
    when: datetime
    symbol: str
    category: str
    price: float
    stop: float
    target1: float
    target2: float
    raw_title: str

    @property
    def malformed(self) -> bool:
        if self.stop > 0 and self.stop >= self.price:
            return True
        if self.target1 > 0 and self.target1 <= self.price:
            return True
        if self.target2 > 0 and self.target2 <= self.price:
            return True
        return False


@dataclass
class Outcome:
    symbol: str
    category: str
    alert_time: str
    alert_price: float
    stop: float
    target1: float
    target2: float
    close_price: float
    eod_return_pct: float
    net_eod_return_pct: float
    max_up_pct: float
    max_down_pct: float
    exec_entry_price: float
    exec_exit_price: float
    entry_charges: float
    exit_charges: float
    slippage_cost: float
    stop_hit: bool
    t1_hit: bool
    t2_hit: bool
    malformed: bool


def iter_bot_blocks(text: str) -> Iterable[tuple[datetime, str]]:
    matches = list(BOT_BLOCK_RE.finditer(text))
    for idx, match in enumerate(matches):
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        when = datetime.strptime(match.group(1), "%d-%m-%Y %H:%M")
        body = text[start:end].strip()
        yield when, body


def parse_block(when: datetime, body: str) -> List[Alert]:
    alerts: List[Alert] = []

    for regex, category in [
        (MAIN_BUY_RE, "buy"),
        (RESCAN_BUY_RE, "rescan"),
        (DEMAND_BUY_RE, "demand"),
        (MOMENTUM_BUY_RE, "momentum"),
        (LEGACY_BUY_RE, "legacy"),
    ]:
        match = regex.search(body)
        if not match:
            continue
        data = match.groupdict()
        target1 = float(data.get("t1") or 0.0)
        target2 = float(data.get("t2") or 0.0)
        alerts.append(
            Alert(
                when=when,
                symbol=str(data["sym"]).strip(),
                category=category,
                price=float(data["price"]),
                stop=float(data["stop"]),
                target1=target1,
                target2=target2,
                raw_title=body.splitlines()[0].strip(),
            )
        )
        break

    return alerts


def load_alerts(discord_path: Path, date_str: str, first_per_symbol: bool = True) -> List[Alert]:
    text = discord_path.read_text(errors="ignore")
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    alerts: List[Alert] = []

    for when, body in iter_bot_blocks(text):
        if when.date() != target_date:
            continue
        alerts.extend(parse_block(when, body))

    alerts.sort(key=lambda item: item.when)
    if not first_per_symbol:
        return alerts

    first_seen = set()
    deduped: List[Alert] = []
    for alert in alerts:
        key = alert.symbol.upper()
        if key in first_seen:
            continue
        first_seen.add(key)
        deduped.append(alert)
    return deduped


def evaluate_alerts(
    alerts: List[Alert],
    delay_sec: float = 0.35,
    buy_cost_pct: float = DEFAULT_BUY_COST_PCT,
    sell_cost_pct: float = DEFAULT_SELL_COST_PCT,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> List[Outcome]:
    from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider

    provider = KiteHistoricalDataProvider()
    outcomes: List[Outcome] = []

    for idx, alert in enumerate(alerts, start=1):
        from_dt = alert.when
        to_dt = alert.when.replace(hour=15, minute=30, second=0)
        try:
            df = provider.get_history_range(
                alert.symbol,
                interval="minute",
                from_date=from_dt,
                to_date=to_dt,
            )
        except Exception as exc:
            print(f"[warn] {alert.symbol}: {exc}")
            time.sleep(delay_sec)
            continue

        if df is None or df.empty:
            print(f"[warn] {alert.symbol}: no minute candles after alert")
            time.sleep(delay_sec)
            continue

        highs = df["high"].astype(float)
        lows = df["low"].astype(float)
        closes = df["close"].astype(float)
        close_price = float(closes.iloc[-1])
        max_high = float(highs.max())
        min_low = float(lows.min())
        cost_result = _apply_trade_costs(
            alert.price,
            close_price,
            buy_cost_pct=buy_cost_pct,
            sell_cost_pct=sell_cost_pct,
            slippage_bps=slippage_bps,
        )

        outcomes.append(
            Outcome(
                symbol=alert.symbol,
                category=alert.category,
                alert_time=alert.when.strftime("%H:%M"),
                alert_price=alert.price,
                stop=alert.stop,
                target1=alert.target1,
                target2=alert.target2,
                close_price=round(close_price, 2),
                eod_return_pct=cost_result["gross_return_pct"],
                net_eod_return_pct=cost_result["net_return_pct"],
                max_up_pct=round((max_high - alert.price) / alert.price * 100, 2),
                max_down_pct=round((min_low - alert.price) / alert.price * 100, 2),
                exec_entry_price=cost_result["exec_entry_price"],
                exec_exit_price=cost_result["exec_exit_price"],
                entry_charges=cost_result["entry_charges"],
                exit_charges=cost_result["exit_charges"],
                slippage_cost=cost_result["slippage_cost"],
                stop_hit=alert.stop > 0 and min_low <= alert.stop,
                t1_hit=alert.target1 > 0 and max_high >= alert.target1,
                t2_hit=alert.target2 > 0 and max_high >= alert.target2,
                malformed=alert.malformed,
            )
        )

        if idx % 10 == 0:
            print(f"evaluated {idx}/{len(alerts)} alerts...")
        time.sleep(delay_sec)

    return outcomes


def print_summary(
    alerts: List[Alert],
    outcomes: List[Outcome],
    buy_cost_pct: float,
    sell_cost_pct: float,
    slippage_bps: float,
) -> None:
    by_category = Counter(alert.category for alert in alerts)
    malformed = [alert for alert in alerts if alert.malformed]

    print(f"Actionable alerts parsed: {len(alerts)}")
    print(f"Malformed alerts: {len(malformed)}")
    print("By category:")
    for category, count in sorted(by_category.items()):
        print(f"  {category:<8} {count}")

    if malformed:
        print("\nMalformed examples:")
        for alert in malformed[:10]:
            print(
                f"  {alert.when:%H:%M} {alert.symbol:<12} "
                f"price={alert.price:.2f} stop={alert.stop:.2f} "
                f"t1={alert.target1:.2f} t2={alert.target2:.2f}"
            )

    if not outcomes:
        print("\nNo outcomes evaluated.")
        return

    df = pd.DataFrame([o.__dict__ for o in outcomes])
    win_rate = (df["net_eod_return_pct"] > 0).mean() * 100
    gross_win_rate = (df["eod_return_pct"] > 0).mean() * 100
    avg_cost_drag = (df["eod_return_pct"] - df["net_eod_return_pct"]).mean()
    print("\nOutcome summary:")
    print(f"  Evaluated alerts: {len(df)}")
    print(
        f"  Net assumptions: buy {buy_cost_pct*100:.3f}% | "
        f"sell {sell_cost_pct*100:.3f}% | slippage {slippage_bps:.1f} bps/side"
    )
    print(f"  Gross win rate to close: {gross_win_rate:.1f}%")
    print(f"  Net win rate to close: {win_rate:.1f}%")
    print(f"  Avg gross close return: {df['eod_return_pct'].mean():+.2f}%")
    print(f"  Avg net close return: {df['net_eod_return_pct'].mean():+.2f}%")
    print(f"  Median net close return: {df['net_eod_return_pct'].median():+.2f}%")
    print(f"  Avg cost drag: {-avg_cost_drag:.2f}%")
    print(f"  Avg max upside: {df['max_up_pct'].mean():+.2f}%")
    print(f"  Avg max downside: {df['max_down_pct'].mean():+.2f}%")
    print(f"  Stop hit rate: {(df['stop_hit']).mean()*100:.1f}%")
    print(f"  T1 hit rate: {(df['t1_hit']).mean()*100:.1f}%")
    print(f"  T2 hit rate: {(df['t2_hit']).mean()*100:.1f}%")

    print("\nBy category:")
    for category, group in df.groupby("category"):
        print(
            f"  {category:<8} alerts={len(group):>3} "
            f"wr={(group['net_eod_return_pct'] > 0).mean()*100:>5.1f}% "
            f"net={group['net_eod_return_pct'].mean():>+6.2f}% "
            f"gross={group['eod_return_pct'].mean():>+6.2f}% "
            f"t2={(group['t2_hit']).mean()*100:>5.1f}%"
        )

    print("\nTop winners:")
    top = df.sort_values("net_eod_return_pct", ascending=False).head(12)
    print(top[["symbol", "category", "alert_time", "alert_price", "close_price", "net_eod_return_pct", "eod_return_pct", "max_up_pct"]].to_string(index=False))

    print("\nTop losers:")
    bottom = df.sort_values("net_eod_return_pct").head(12)
    print(bottom[["symbol", "category", "alert_time", "alert_price", "close_price", "net_eod_return_pct", "eod_return_pct", "max_down_pct"]].to_string(index=False))


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit actionable Discord alerts with Kite minute candles")
    parser.add_argument("--date", required=True, help="Trading date in YYYY-MM-DD format")
    parser.add_argument(
        "--discord-file",
        default="Screener Alerts - Text Channels - general [1488777695750914060].txt",
        help="Path to the exported Discord text file",
    )
    parser.add_argument(
        "--all-alerts",
        action="store_true",
        help="Evaluate every actionable alert instead of only the first alert per symbol",
    )
    parser.add_argument(
        "--delay-sec",
        type=float,
        default=0.35,
        help="Sleep between Kite historical requests",
    )
    parser.add_argument("--buy-cost-pct", type=float, default=DEFAULT_BUY_COST_PCT, help="Estimated buy-side cost percentage")
    parser.add_argument("--sell-cost-pct", type=float, default=DEFAULT_SELL_COST_PCT, help="Estimated sell-side cost percentage")
    parser.add_argument("--slippage-bps", type=float, default=DEFAULT_SLIPPAGE_BPS, help="Estimated slippage in basis points per side")
    args = parser.parse_args()

    discord_path = Path(args.discord_file)
    if not discord_path.exists():
        print(f"Discord export not found: {discord_path}")
        return 1

    alerts = load_alerts(discord_path, args.date, first_per_symbol=not args.all_alerts)
    if not alerts:
        print(f"No actionable alerts found for {args.date}")
        return 1

    print(f"Loaded {len(alerts)} actionable alerts for {args.date}")
    outcomes = evaluate_alerts(
        alerts,
        delay_sec=max(0.0, args.delay_sec),
        buy_cost_pct=max(0.0, float(args.buy_cost_pct)),
        sell_cost_pct=max(0.0, float(args.sell_cost_pct)),
        slippage_bps=max(0.0, float(args.slippage_bps)),
    )
    print_summary(
        alerts,
        outcomes,
        buy_cost_pct=max(0.0, float(args.buy_cost_pct)),
        sell_cost_pct=max(0.0, float(args.sell_cost_pct)),
        slippage_bps=max(0.0, float(args.slippage_bps)),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
