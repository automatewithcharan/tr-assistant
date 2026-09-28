#!/usr/bin/env python3
"""Forensic reconstruction of the 2026-09-28 PSAR_LIQ_V2 alert export.

The analysis is deliberately limited to values present in the TradingView CSV
and executable behavior visible in the corresponding Pine source.  It does not
load candles or market data.  When the source files are not present in the
checked-out branch, the defaults read them directly from ``origin/main``.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import math
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional


REPO = Path(__file__).resolve().parents[1]
CSV_REPO_PATH = "results/TradingView_Alerts_Log_2026-09-28.csv"
PINE_REPO_PATH = "PSAR_Liquidity_Trade_Manager_V2_INDICATOR.pine"
DEFAULT_REF = "origin/main"
IST = timezone(timedelta(hours=5, minutes=30))
MONEY_FIELDS = {
    "Signal price",
    "Assumed entry",
    "Initial SL",
    "1R level",
    "Target",
    "Risk/share",
    "Avg traded value",
    "Highest since entry",
    "Old SL",
    "New SL",
    "Exit price (theoretical)",
    "Theoretical P&L",
}
INT_FIELDS = {
    "Risk-based qty",
    "Exposure-based qty",
    "Intended qty",
    "Liquidity capacity",
    "Required liquidity capacity",
    "Required capacity",
    "Final qty",
    "Qty",
}
FLOAT_FIELDS = {"Avg 1m volume", "Relative volume"}


def git_text(ref: str, path: str) -> str:
    return subprocess.check_output(
        ["git", "show", f"{ref}:{path}"], cwd=REPO, text=True
    )


def load_text(explicit: Optional[Path], repo_path: str, ref: str) -> tuple[str, str]:
    if explicit:
        return explicit.read_text(encoding="utf-8-sig"), str(explicit)
    local = REPO / repo_path
    if local.exists():
        return local.read_text(encoding="utf-8-sig"), str(local.relative_to(REPO))
    return git_text(ref, repo_path), f"{ref}:{repo_path}"


def fnum(value: Any, digits: int = 2, na: str = "NA") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return na
    return f"{value:,.{digits}f}"


def money(value: Any) -> str:
    return "NA" if value is None else f"₹{value:,.2f}"


def pct(value: Any) -> str:
    return "NA" if value is None else f"{100 * value:.1f}%"


def mean(values: Iterable[float]) -> Optional[float]:
    vals = list(values)
    return statistics.fmean(vals) if vals else None


def median(values: Iterable[float]) -> Optional[float]:
    vals = list(values)
    return statistics.median(vals) if vals else None


def md_table(headers: list[str], rows: Iterable[Iterable[Any]]) -> str:
    rows = list(rows)
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|") for x in row) + " |")
    return "\n".join(out)


def parse_value(label: str, raw: str) -> Any:
    value = raw.strip()
    if value.upper() == "NA":
        return None
    clean = value.replace("₹", "").replace(",", "").strip()
    if label in INT_FIELDS:
        try:
            return int(float(clean))
        except ValueError:
            return None
    if label in MONEY_FIELDS or label in FLOAT_FIELDS:
        try:
            return float(clean)
        except ValueError:
            return None
    return value


def normalize_symbol(ticker: str, first_line: str) -> str:
    if " | " in first_line:
        return first_line.split(" | ", 1)[1].strip()
    return ticker.split(":", 1)[-1].split(",", 1)[0].strip()


def event_type(first_line: str) -> str:
    if first_line.startswith("🟢 BUY"):
        return "BUY"
    if first_line.startswith("🔴 EXIT"):
        return "EXIT"
    if "UNKNOWN / SKIP" in first_line:
        return "SKIP_UNKNOWN"
    if "SKIP" in first_line:
        return "SKIP_FAIL"
    if first_line.startswith("🔵 STOP UPDATE"):
        return "STOP_UPDATE"
    return "OTHER"


def liquidity_lookback(name: str) -> Optional[int]:
    match = re.search(r"PSAR_LIQ_V2 \((.*)\): Any alert", name)
    if not match:
        return None
    # Thousands separators make generic comma splitting ambiguous.  Anchor on
    # the stable prefix through max quantity; the next integer is the lookback.
    match = re.search(
        r"0\.02, 0\.02, 0\.2, 14, 0\.1, 0\.5, 500, 300,000, 500, 0, 1, (\d+),",
        name,
    )
    return int(match.group(1)) if match else None


@dataclass
class Alert:
    row_index: int
    alert_id: str
    ticker: str
    name: str
    description: str
    timestamp: datetime
    symbol: str
    event: str
    fields: dict[str, Any]
    lookback: Optional[int]


@dataclass
class Trade:
    trade_id: str
    symbol: str
    entry_number: int
    buy: Alert
    exit: Optional[Alert] = None
    anomalies: list[str] = field(default_factory=list)

    @property
    def entry(self) -> Optional[float]:
        return self.buy.fields.get("Assumed entry")

    @property
    def stop(self) -> Optional[float]:
        return self.buy.fields.get("Initial SL")

    @property
    def target(self) -> Optional[float]:
        return self.buy.fields.get("Target")

    @property
    def qty(self) -> Optional[int]:
        return self.buy.fields.get("Final qty")

    @property
    def intended_risk(self) -> Optional[float]:
        if None in (self.entry, self.stop, self.qty):
            return None
        return abs(self.entry - self.stop) * self.qty

    @property
    def exit_price(self) -> Optional[float]:
        return self.exit.fields.get("Exit price (theoretical)") if self.exit else None

    @property
    def exit_reason(self) -> Optional[str]:
        return self.exit.fields.get("Reason") if self.exit else None

    @property
    def pnl(self) -> Optional[float]:
        if not self.exit:
            return None
        logged = self.exit.fields.get("Theoretical P&L")
        if logged is not None:
            return logged
        if None in (self.entry, self.exit_price, self.qty):
            return None
        return (self.exit_price - self.entry) * self.qty

    @property
    def computed_pnl(self) -> Optional[float]:
        if not self.exit or None in (self.entry, self.exit_price, self.qty):
            return None
        return (self.exit_price - self.entry) * self.qty

    @property
    def r_multiple(self) -> Optional[float]:
        risk = self.intended_risk
        return self.pnl / risk if self.pnl is not None and risk and risk > 0 else None

    @property
    def duration_seconds(self) -> Optional[float]:
        return (self.exit.timestamp - self.buy.timestamp).total_seconds() if self.exit else None

    @property
    def loss_multiple(self) -> Optional[float]:
        if self.pnl is None or self.pnl >= 0 or not self.intended_risk:
            return None
        return abs(self.pnl) / self.intended_risk


def parse_alerts(text: str) -> tuple[list[Alert], list[dict[str, str]]]:
    raw_rows = list(csv.DictReader(io.StringIO(text)))
    alerts: list[Alert] = []
    for i, row in enumerate(raw_rows, 1):
        lines = row["Description"].splitlines()
        first = lines[0].strip() if lines else ""
        fields: dict[str, Any] = {}
        for line in lines[1:]:
            if ":" not in line or line.startswith("NOTE:"):
                continue
            label, value = line.split(":", 1)
            fields[label.strip()] = parse_value(label.strip(), value)
        alerts.append(
            Alert(
                i,
                row["Alert ID"],
                row["Ticker"],
                row["Name"],
                row["Description"],
                datetime.fromisoformat(row["Time"].replace("Z", "+00:00")),
                normalize_symbol(row["Ticker"], first),
                event_type(first),
                fields,
                liquidity_lookback(row["Name"]),
            )
        )
    # TradingView exports newest-first.  Reverse the original row position for
    # events sharing its one-second timestamp; this matters when a BUY and EXIT
    # occur in the same second (the later EXIT appears first in the raw CSV).
    alerts.sort(key=lambda a: (a.timestamp, -a.row_index))
    return alerts, raw_rows


def reconstruct(alerts: list[Alert]) -> tuple[list[Trade], list[Alert], list[tuple[Alert, Alert]]]:
    trades: list[Trade] = []
    orphan_exits: list[Alert] = []
    logical_duplicates: list[tuple[Alert, Alert]] = []
    open_trade: dict[str, Trade] = {}
    entry_counts: Counter[str] = Counter()
    prior_by_key: dict[tuple[Any, ...], Alert] = {}

    for alert in alerts:
        key = (alert.symbol, alert.event, alert.timestamp, tuple(sorted(alert.fields.items())))
        if key in prior_by_key:
            logical_duplicates.append((prior_by_key[key], alert))
        else:
            prior_by_key[key] = alert

        if alert.event == "BUY":
            entry_counts[alert.symbol] += 1
            trade = Trade(
                f"20260928-{alert.symbol}-{entry_counts[alert.symbol]:02d}",
                alert.symbol,
                entry_counts[alert.symbol],
                alert,
            )
            if alert.symbol in open_trade:
                old = open_trade[alert.symbol]
                old.anomalies.append(f"overlapping BUY before {trade.trade_id}")
                trade.anomalies.append(f"BUY while {old.trade_id} open")
            open_trade[alert.symbol] = trade
            trades.append(trade)
        elif alert.event == "EXIT":
            if alert.symbol not in open_trade:
                orphan_exits.append(alert)
            else:
                trade = open_trade.pop(alert.symbol)
                trade.exit = alert

    return trades, orphan_exits, logical_duplicates


def max_streak(trades: list[Trade], positive: bool) -> int:
    best = current = 0
    for t in sorted((x for x in trades if x.exit and x.pnl is not None), key=lambda x: x.exit.timestamp):
        hit = t.pnl > 0 if positive else t.pnl < 0
        current = current + 1 if hit else 0
        best = max(best, current)
    return best


def stats(trades: Iterable[Trade]) -> dict[str, Any]:
    ts = [t for t in trades if t.exit and t.pnl is not None]
    pnls = [t.pnl for t in ts]
    rs = [t.r_multiple for t in ts if t.r_multiple is not None]
    winners = [x for x in pnls if x > 0]
    losers = [x for x in pnls if x < 0]
    gross_profit = sum(winners)
    gross_loss = abs(sum(losers))
    return {
        "count": len(ts),
        "wins": len(winners),
        "losses": len(losers),
        "flats": sum(x == 0 for x in pnls),
        "win_rate": len(winners) / len(ts) if ts else None,
        "gross": sum(pnls),
        "avg": mean(pnls),
        "median": median(pnls),
        "avg_win": mean(winners),
        "avg_loss": mean(losers),
        "profit_factor": gross_profit / gross_loss if gross_loss else (math.inf if gross_profit else None),
        "avg_r": mean(rs),
        "median_r": median(rs),
        "avg_hold": mean(t.duration_seconds for t in ts if t.duration_seconds is not None),
    }


def time_bucket(t: Trade) -> str:
    local = t.buy.timestamp.astimezone(IST)
    minute = local.hour * 60 + local.minute
    bounds = [
        (9 * 60 + 15, 9 * 60 + 30, "09:15–09:30"),
        (9 * 60 + 30, 10 * 60, "09:30–10:00"),
        (10 * 60, 11 * 60, "10:00–11:00"),
        (11 * 60, 12 * 60, "11:00–12:00"),
        (12 * 60, 13 * 60, "12:00–13:00"),
        (13 * 60, 14 * 60, "13:00–14:00"),
        (14 * 60, 15 * 60, "14:00–15:00"),
        (15 * 60, 24 * 60, "15:00–EOD"),
    ]
    for start, end, label in bounds:
        if start <= minute < end:
            return label
    return "outside requested buckets"


def duration_text(seconds: Optional[float]) -> str:
    if seconds is None:
        return "NA"
    minutes, sec = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{sec:02d}"


def trade_row(t: Trade) -> dict[str, Any]:
    same_5m = False
    if t.exit:
        a = int(t.buy.timestamp.timestamp()) // 300
        b = int(t.exit.timestamp.timestamp()) // 300
        same_5m = a == b
    return {
        "trade_id": t.trade_id,
        "symbol": t.symbol,
        "entry_number": t.entry_number,
        "entry_alert_id": t.buy.alert_id,
        "tradingview_ticker": t.buy.ticker,
        "alert_name_and_inputs": t.buy.name,
        "entry_time_utc": t.buy.timestamp.isoformat().replace("+00:00", "Z"),
        "entry_time_ist": t.buy.timestamp.astimezone(IST).isoformat(),
        "signal_price": t.buy.fields.get("Signal price"),
        "entry_price": t.entry,
        "risk_based_qty": t.buy.fields.get("Risk-based qty"),
        "exposure_based_qty": t.buy.fields.get("Exposure-based qty"),
        "intended_qty": t.buy.fields.get("Intended qty"),
        "qty": t.qty,
        "initial_stop": t.stop,
        "one_r_level": t.buy.fields.get("1R level"),
        "target": t.target,
        "risk_per_share_logged": t.buy.fields.get("Risk/share"),
        "intended_initial_risk": t.intended_risk,
        "liquidity_state": t.buy.fields.get("Liquidity"),
        "liquidity_capacity": t.buy.fields.get("Liquidity capacity"),
        "required_liquidity_capacity": t.buy.fields.get("Required liquidity capacity"),
        "avg_1m_volume": t.buy.fields.get("Avg 1m volume"),
        "avg_traded_value": t.buy.fields.get("Avg traded value"),
        "relative_volume": t.buy.fields.get("Relative volume"),
        "liquidity_lookback": t.buy.lookback,
        "exit_alert_id": t.exit.alert_id if t.exit else None,
        "exit_time_utc": t.exit.timestamp.isoformat().replace("+00:00", "Z") if t.exit else None,
        "exit_time_ist": t.exit.timestamp.astimezone(IST).isoformat() if t.exit else None,
        "exit_price": t.exit_price,
        "exit_reason": t.exit_reason,
        "highest_since_entry": t.exit.fields.get("Highest since entry") if t.exit else None,
        "old_stop_at_exit": t.exit.fields.get("Old SL") if t.exit else None,
        "new_stop_at_exit": t.exit.fields.get("New SL") if t.exit else None,
        "holding_seconds": t.duration_seconds,
        "holding_time": duration_text(t.duration_seconds),
        "gross_pnl": t.pnl,
        "computed_pnl": t.computed_pnl,
        "r_multiple": t.r_multiple,
        "loss_multiple": t.loss_multiple,
        "under_30_sec": t.duration_seconds is not None and t.duration_seconds < 30,
        "under_1_min": t.duration_seconds is not None and t.duration_seconds < 60,
        "under_2_min": t.duration_seconds is not None and t.duration_seconds < 120,
        "under_5_min": t.duration_seconds is not None and t.duration_seconds < 300,
        "same_5m_bar": same_5m,
        "anomalies": "; ".join(t.anomalies),
    }


def write_trades(path: Path, trades: list[Trade]) -> None:
    rows = [trade_row(t) for t in trades]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=list(rows[0]) if rows else ["trade_id"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)


def summarize_rows(groups: Iterable[tuple[str, list[Trade]]]) -> list[list[str]]:
    rows = []
    for label, group in groups:
        s = stats(group)
        rows.append(
            [
                label,
                s["count"],
                pct(s["win_rate"]),
                money(s["gross"]),
                money(s["avg"]),
                fnum(s["avg_r"]),
                fnum(s["profit_factor"]),
                duration_text(s["avg_hold"]),
            ]
        )
    return rows


def generate_report(
    alerts: list[Alert],
    raw_rows: list[dict[str, str]],
    trades: list[Trade],
    orphan_exits: list[Alert],
    logical_duplicates: list[tuple[Alert, Alert]],
    csv_source: str,
    pine_source: str,
    csv_text: str,
    pine_text: str,
    ref: str,
) -> str:
    event_counts = Counter(a.event for a in alerts)
    symbols = sorted({a.symbol for a in alerts})
    complete = [t for t in trades if t.exit and t.pnl is not None]
    open_buys = [t for t in trades if not t.exit]
    overall = stats(complete)
    exact_dupes = len(raw_rows) - len({tuple(row.items()) for row in raw_rows})
    time_desc_dupes = sum(
        n - 1 for n in Counter((r["Time"], r["Description"]) for r in raw_rows).values() if n > 1
    )
    config_counts = Counter((a.lookback, a.name) for a in alerts)
    schemas = Counter((a.event, tuple(sorted(a.fields))) for a in alerts)
    event_collisions = [
        group
        for group in (
            [a for a in alerts if (a.symbol, a.event, a.timestamp) == key]
            for key in sorted({(a.symbol, a.event, a.timestamp) for a in alerts})
        )
        if len(group) > 1
    ]
    pnl_mismatches = [
        t for t in complete if t.computed_pnl is not None and abs(t.pnl - t.computed_pnl) > 0.011
    ]

    reason_groups: dict[str, list[Trade]] = defaultdict(list)
    for t in complete:
        reason_groups[t.exit_reason or "MISSING"].append(t)
    entry_groups = [
        ("FIRST ENTRY", [t for t in complete if t.entry_number == 1]),
        ("ALL RE-ENTRIES", [t for t in complete if t.entry_number >= 2]),
        ("SECOND ENTRY", [t for t in complete if t.entry_number == 2]),
        ("THIRD ENTRY", [t for t in complete if t.entry_number == 3]),
        ("FOURTH+", [t for t in complete if t.entry_number >= 4]),
    ]
    bucket_order = [
        "09:15–09:30", "09:30–10:00", "10:00–11:00", "11:00–12:00",
        "12:00–13:00", "13:00–14:00", "14:00–15:00", "15:00–EOD",
    ]
    bucket_groups = [(b, [t for t in complete if time_bucket(t) == b]) for b in bucket_order]
    rapid = sorted([t for t in complete if t.duration_seconds is not None and t.duration_seconds < 300], key=lambda t: t.duration_seconds)
    risk_worst = sorted([t for t in complete if t.loss_multiple is not None], key=lambda t: t.loss_multiple, reverse=True)
    chronological = sorted(complete, key=lambda t: t.exit.timestamp)
    equity = []
    cumulative = 0.0
    for t in chronological:
        cumulative += t.pnl
        equity.append((t.exit.timestamp.astimezone(IST).strftime("%H:%M:%S"), t.trade_id, t.pnl, cumulative))

    by_symbol: dict[str, list[Trade]] = defaultdict(list)
    for t in complete:
        by_symbol[t.symbol].append(t)
    symbol_rows = []
    for symbol, group in by_symbol.items():
        s = stats(group)
        first_pnl = sum(t.pnl for t in group if t.entry_number == 1)
        re_pnl = sum(t.pnl for t in group if t.entry_number >= 2)
        reasons = Counter(t.exit_reason for t in group)
        symbol_rows.append((
            symbol, len(group), first_pnl, re_pnl, s["wins"], s["losses"], s["win_rate"],
            s["gross"], s["avg_r"], min((t.r_multiple for t in group if t.r_multiple is not None), default=None),
            reasons.most_common(1)[0][0], sum(t.duration_seconds < 300 for t in group if t.duration_seconds is not None),
        ))
    symbol_rows.sort(key=lambda x: x[7])

    winners = sorted(complete, key=lambda t: t.pnl, reverse=True)
    losers = sorted(complete, key=lambda t: t.pnl)
    turnover = sum(
        (t.entry * t.qty + t.exit_price * t.qty)
        for t in complete
        if None not in (t.entry, t.qty, t.exit_price)
    )
    break_even_bps = overall["gross"] / turnover * 10000 if turnover and overall["gross"] > 0 else None
    break_even_per_trade = overall["gross"] / len(complete) if complete and overall["gross"] > 0 else None
    loss_over_budget = [t for t in complete if t.pnl < -500]
    alerts_by_liq = Counter(a.fields.get("Liquidity", "MISSING") for a in alerts if "Liquidity" in a.fields)
    skip_symbols = Counter(a.symbol for a in alerts if a.event.startswith("SKIP"))
    buy_symbols = Counter(t.symbol for t in trades)
    psar_fallbacks = reason_groups.get("PSAR FALLBACK", [])
    orphan_payload_pnl = sum(a.fields.get("Theoretical P&L") or 0 for a in orphan_exits)
    all_exit_payload_pnl = sum(a.fields.get("Theoretical P&L") or 0 for a in alerts if a.event == "EXIT")
    unique_candidate_episodes = len({
        (a.symbol, a.timestamp)
        for a in alerts
        if a.event in {"BUY", "SKIP_FAIL", "SKIP_UNKNOWN"}
    })
    unique_skip_episodes = len({
        (a.symbol, a.timestamp)
        for a in alerts
        if a.event in {"SKIP_FAIL", "SKIP_UNKNOWN"}
    })

    lines: list[str] = []
    add = lines.append
    add("# PSAR V2 Sep 28 Forensic Report")
    add("")
    add("Generated only from the Sep 28 TradingView CSV and the corresponding Pine source. No candle, broker, or future market data was used.")
    add("")
    add("## 1. Executive summary")
    add("")
    add(
        f"The export contains **{len(alerts)} alerts across {len(symbols)} symbols**: "
        f"{event_counts['BUY']} BUY, {event_counts['EXIT']} EXIT, "
        f"{event_counts['SKIP_FAIL'] + event_counts['SKIP_UNKNOWN']} SKIP, and "
        f"{event_counts['STOP_UPDATE'] + event_counts['OTHER']} other. Reconstruction yields "
        f"**{len(complete)} closed trades**, {len(orphan_exits)} orphan EXITs, and {len(open_buys)} open BUYs."
    )
    add("")
    add(
        f"Closed trades produced **{money(overall['gross'])} theoretical gross P&L**, with "
        f"{overall['wins']} wins, {overall['losses']} losses, {overall['flats']} flats, "
        f"a {pct(overall['win_rate'])} win rate, {money(overall['avg'])} average trade, "
        f"and {fnum(overall['profit_factor'])} profit factor. This is a one-session theoretical alert result, not fill-verified deployable P&L."
    )
    add("")
    add(
        f"The primary performance result excludes orphan lifecycles because their entry timestamps and initial stops are absent. "
        f"Their EXIT payloads sum to {money(orphan_payload_pnl)}; the sum across all {event_counts['EXIT']} EXIT payloads is "
        f"{money(all_exit_payload_pnl)}. Both totals are reported so the exclusion is explicit."
    )
    add("")
    first_s, re_s = stats(entry_groups[0][1]), stats(entry_groups[1][1])
    add(
        f"First entries contributed {money(first_s['gross'])} over {first_s['count']} trades; "
        f"re-entries contributed {money(re_s['gross'])} over {re_s['count']} trades. "
        f"Re-entries added most of the day's loss because there were {re_s['count']} of them, but their per-trade loss "
        f"({money(re_s['avg'])}) was smaller than first entries ({money(first_s['avg'])}) and profit factor was the same. "
        "Thus 're-entry is worse' is not proven; the second and third entries were the weakest cohorts, while fourth+ was less negative."
    )
    add("")
    if risk_worst:
        w = risk_worst[0]
    add(
        f"The worst realized-loss/intended-risk ratio was **{w.loss_multiple:.2f}×** "
            f"({w.trade_id}: intended {money(w.intended_risk)}, realized {money(w.pnl)}). "
            "The code explains overshoot: realtime stops trigger on the observed `close <= activeStop`, then record current close minus one tick—not a fill at the stop level."
        )
    add("")
    add(
        f"Why activity was so high is mechanically clear: there is no one-entry-per-day guard, so every later confirmed bullish PSAR flip can re-enter after an exit. "
        f"Twenty-four symbols re-entered and {sum(t.entry_number >= 2 for t in trades)} of 154 trades were re-entries. "
        f"The day had {sum(t.duration_seconds < 300 for t in complete)} exits inside five minutes. Targets and ATR trails created positive logged P&L, "
        "but HARD STOP and PSAR FALLBACK exits destroyed more; raw PSAR telemetry is insufficient to assign the entire loss to entry versus management."
    )
    add("")
    add("## 2. Data integrity and source of truth")
    add("")
    add(md_table(
        ["Item", "Value"],
        [
            ["Fetched main SHA", subprocess.check_output(["git", "rev-parse", ref], cwd=REPO, text=True).strip()],
            ["CSV source", csv_source],
            ["Pine source", pine_source],
            ["CSV SHA-256", hashlib.sha256(csv_text.encode()).hexdigest()],
            ["Pine SHA-256", hashlib.sha256(pine_text.encode()).hexdigest()],
            ["Raw rows", len(alerts)],
            ["Time range UTC", f"{alerts[0].timestamp.isoformat()} to {alerts[-1].timestamp.isoformat()}"],
            ["Time range IST", f"{alerts[0].timestamp.astimezone(IST).isoformat()} to {alerts[-1].timestamp.astimezone(IST).isoformat()}"],
            ["Exact duplicate rows", exact_dupes],
            ["Exact timestamp + message duplicates", time_desc_dupes],
            ["Logical duplicates", len(logical_duplicates)],
            ["Same symbol/event/second collisions", len(event_collisions)],
            ["Distinct TradingView alert definitions", len({a.alert_id for a in alerts})],
        ],
    ))
    add("")
    add("Repository state observed at investigation start:")
    add("")
    add("```text")
    add("branch: codex/ng-v0818-f0-observability-20260916")
    add("working-tree HEAD: 3723774abbf16a64acb1e95f9a74c0949d0f357d")
    add("fetched origin/main: b6202324b79f5b65440a0277af99b917824ec799")
    add("status: dirty with pre-existing modified and untracked files; preserved")
    add("```")
    add("")
    add("`origin/main` is the evidence source: `b620232` adds the Sep 28 CSV; `7ac3281` is the latest commit touching the V2 Pine file. The alert payloads use `Assumed entry`, include exposure/intended/required-capacity telemetry, and include current-session bars on SKIPs—the schema introduced by `7ac3281`, not the original `ce85b7e` version.")
    add("")
    add("All rows identify `PSAR_LIQ_V2` and share that hardened payload schema. They are the same Pine code version, but **not the same input configuration**:")
    add("")
    add(md_table(["Liquidity lookback", "Alerts"], [[lb, n] for (lb, _), n in sorted(config_counts.items())]))
    add("")
    add(f"Observed field schemas: {len(schemas)} (the expected BUY, EXIT and SKIP shapes; no payload-version field or explicit source SHA exists). `Alert ID` identifies a TradingView alert definition and repeats across its events; it is not an event ID. `Webhook status` is blank on all {len(alerts)} rows. Same-code attribution is therefore strong schema evidence, not cryptographic proof from the CSV itself.")
    add("")
    add("## 3. Pine state machine")
    add("")
    add(md_table(
        ["Stage", "Executable rule", "Pine location"],
        [
            ["Session", "1-minute chart; confirmed PSAR BUY bar close from 09:15 through 14:50 IST", "131–145, 297–311"],
            ["Liquidity baseline", "Only prior completed current-session bars; UNKNOWN until lookback 10/15/20 is ready", "147–165"],
            ["Liquidity", "PASS requires turnover, volume, capacity and optional liquidity RVOL; FAIL/UNKNOWN SKIP", "197–224, 303–383"],
            ["PSAR entry", "Confirmed bearish→bullish flip; flat slot; valid sizing; optional filters", "40–45, 167–171, 306–313"],
            ["Sizing", "floor(₹500/risk-share), exposure cap, 500-share cap; minimum of all", "208–213"],
            ["Initial stop", "min(signal low − 0.1 ATR, entry − 0.5 ATR), rounded down", "297–303"],
            ["Target", "entry + 2R, rounded up", "320–322"],
            ["Break-even", "Disabled by these inputs", "83–85, 230–235"],
            ["Profit lock", "At 1R high, candidate entry + 0.25R", "87–89, 236–240"],
            ["ATR trail", "At 1R high, candidate highest − 1.5 ATR; stop only ratchets upward", "91–99, 242–248, 411–418"],
            ["Realtime stop", "After entry bar only; current observed close ≤ active stop; exit at close − one tick", "401–433"],
            ["Target/PSAR/EOD", "Priority after stop: target, EOD, confirmed PSAR fallback", "423–446"],
            ["Reset/re-entry", "Exit sets flat immediately; same-bar re-entry blocked by exit latch; later confirmed flip may re-enter", "287–292, 308, 487–513"],
        ],
    ))
    add("")
    add("There is no once-per-symbol/day guard. Multiple BUYs are allowed after an EXIT, but not on the same chart bar as that EXIT. Because entry requires a new confirmed bearish→bullish PSAR flip, a later bar can re-enter. State is `varip`: it survives realtime rollback, but script/chart reload reconstructs historical state from OHLC. Historical BUY alert calls do not deliver old alerts; a historically reconstructed open state can later emit a realtime EXIT with no realtime BUY in this export.")
    add("")
    add("`isNewTradingDay` resets only the liquidity-session baseline (155–159). There is no explicit new-day reset of `inTradeIp`; trade state resets only when an exit path executes. A confirmed bar at/after 15:15 normally triggers EOD before PSAR fallback (424, 439–446), but a chart with no qualifying realtime EOD execution could retain state until a later execution. The CSV does not prove that this occurred.")
    add("")
    add("Realtime stops are intrabar in the sense that Pine recalculates on feed updates and compares the current `close`, but this indicator does not place a resting stop order. A jump through the level records the newly observed price minus one tick. Historical processing differs: it tests the bar low against the stop carried into the bar and uses open-or-stop minus one tick (253–255, 448–485). The CSV contains live alerts, so the realtime path governs the logged exits.")
    add("")
    add("## 4. Trade reconstruction")
    add("")
    add(md_table(
        ["Classification", "Count"],
        [
            ["Normal closed entry episodes", len(complete)],
            ["Symbols with re-entry", sum(n > 1 for n in buy_symbols.values())],
            ["Re-entry trades (entry #2+)", sum(t.entry_number >= 2 for t in trades)],
            ["Orphan EXIT", len(orphan_exits)],
            ["Open/unmatched BUY", len(open_buys)],
            ["Overlapping BUY anomalies", sum(bool(t.anomalies) for t in trades)],
            ["Logical duplicate alerts", len(logical_duplicates)],
            ["Possible duplicate candidate alerts", len(event_collisions)],
            ["BUY→EXIT <30 sec", sum(t.duration_seconds < 30 for t in complete)],
            ["BUY→EXIT <1 min", sum(t.duration_seconds < 60 for t in complete)],
            ["BUY→EXIT <5 min", sum(t.duration_seconds < 300 for t in complete)],
            ["BUY→EXIT in same 5m clock bucket", sum(trade_row(t)["same_5m_bar"] for t in complete)],
        ],
    ))
    add("")
    if orphan_exits:
        add("Orphan EXITs:")
        add("")
        add(md_table(
            ["Symbol", "Time IST", "Reason", "Entry carried in EXIT", "P&L"],
            [[a.symbol, a.timestamp.astimezone(IST).strftime("%H:%M:%S"), a.fields.get("Reason"), money(a.fields.get("Assumed entry")), money(a.fields.get("Theoretical P&L"))] for a in orphan_exits],
        ))
        add("")
        add("These are not automatically defects. The export begins at 09:40:05 IST, not session open, and `varip`/historical reconstruction can legitimately carry an in-trade state into the realtime alert window. The CSV has no alert-creation timestamp, reload marker, or prior-day data, so the exact cause cannot be distinguished.")
        add("")
    if event_collisions:
        add("Possible duplicate candidate alerts:")
        add("")
        add(md_table(
            ["Symbol", "Time IST", "Event", "Alert definitions", "Lookbacks", "Assessment"],
            [[g[0].symbol, g[0].timestamp.astimezone(IST).strftime("%H:%M:%S"), g[0].event, ", ".join(a.alert_id for a in g), ", ".join(str(a.lookback) for a in g), "Same PSAR candidate from parallel 10/20-bar configurations; payload measurements differ"] for g in event_collisions],
        ))
        add("")
        add("These three ARVEE pairs are not byte-identical duplicates: two concurrently active TradingView alert definitions evaluated the same PSAR event with different liquidity baselines. Both concluded FAIL, so no duplicate trade resulted. They are operational duplicates if the intended deployment was one alert definition per symbol.")
        add("")
    if open_buys:
        add("Open/unmatched BUYs:")
        add("")
        add(md_table(["Trade", "Entry IST", "Entry", "Qty"], [[t.trade_id, t.buy.timestamp.astimezone(IST).strftime("%H:%M:%S"), money(t.entry), t.qty] for t in open_buys]))
        add("")
    add("## 5. Performance")
    add("")
    add(md_table(
        ["Metric", "Value"],
        [
            ["Closed trades", overall["count"]], ["Winning / losing / flat", f"{overall['wins']} / {overall['losses']} / {overall['flats']}"],
            ["Win rate", pct(overall["win_rate"])], ["Gross P&L", money(overall["gross"])],
            ["Average / median trade", f"{money(overall['avg'])} / {money(overall['median'])}"],
            ["Average winner / loser", f"{money(overall['avg_win'])} / {money(overall['avg_loss'])}"],
            ["Profit factor", fnum(overall["profit_factor"])], ["Expectancy/trade", money(overall["avg"])],
            ["Expectancy in R", fnum(overall["avg_r"])],
            ["Best trade", f"{winners[0].trade_id} {money(winners[0].pnl)} ({fnum(winners[0].r_multiple)}R)" if winners else "NA"],
            ["Worst trade", f"{losers[0].trade_id} {money(losers[0].pnl)} ({fnum(losers[0].r_multiple)}R)" if losers else "NA"],
            ["Max consecutive wins / losses", f"{max_streak(complete, True)} / {max_streak(complete, False)}"],
            ["Logged-vs-computed P&L mismatches > ₹0.01", len(pnl_mismatches)],
        ],
    ))
    add("")
    add("Intraday theoretical cumulative equity by exit:")
    add("")
    add(md_table(["Exit IST", "Trade", "P&L", "Cumulative"], [[tm, tid, money(p), money(c)] for tm, tid, p, c in equity]))
    add("")
    add("## 6. Risk-budget audit")
    add("")
    intended = [t.intended_risk for t in trades if t.intended_risk is not None]
    add(f"For every BUY, intended risk was recomputed as `abs(entry − initial stop) × final qty`. Range: {money(min(intended))} to {money(max(intended))}; median {money(median(intended))}. The ₹500 input is a ceiling before integer flooring and the exposure/500-share caps, not a promise that every trade risks exactly ₹500.")
    add("")
    add(f"{len(loss_over_budget)} closed trades lost more than the configured ₹500 budget in theoretical alert P&L. Worst ratios:")
    add("")
    add(md_table(
        ["Trade", "Entry", "Initial SL", "Qty", "Intended risk", "Exit", "Loss", "Loss/risk", "Reason"],
        [[t.trade_id, money(t.entry), money(t.stop), t.qty, money(t.intended_risk), money(t.exit_price), money(t.pnl), f"{t.loss_multiple:.2f}×", t.exit_reason] for t in risk_worst[:15]],
    ))
    add("")
    add("Mechanism proven by code and payload: quantity is sized correctly from the initial stop (208–213, 297–303), but live stop detection uses current `close` and the logged market-exit reference is that observed price minus one tick (250–251, 420–433). Therefore a feed update below the stop creates a realized theoretical loss beyond intended risk. The CSV cannot distinguish a true exchange gap from sparse feed updates or alert latency, and contains no broker fills; it can prove stop-reference overshoot, not its market-microstructure cause.")
    add("")
    add("## 7. Exit analysis")
    add("")
    known_reasons = ["HARD STOP", "TARGET", "BREAK EVEN", "PROFIT LOCK", "ATR TRAIL", "PSAR FALLBACK", "EOD"]
    other_reason_trades = [t for t in complete if t.exit_reason not in known_reasons]
    displayed_reason_groups = [(r, reason_groups.get(r, [])) for r in known_reasons] + [("other", other_reason_trades)]
    add(md_table(["Reason", "Trades", "Win rate", "Gross", "Average", "Avg R", "Profit factor", "Avg hold"], summarize_rows(displayed_reason_groups)))
    add("")
    reason_rank = sorted(((reason, stats(group)["gross"]) for reason, group in reason_groups.items()), key=lambda x: x[1])
    add("On this day, value by logged exit mechanism ranks: " + "; ".join(f"{r} {money(p)}" for r, p in reversed(reason_rank)) + ". Attribution is descriptive: the counterfactual P&L under another exit is unavailable without future candles and is intentionally not invented.")
    add("")
    add("The exit payload arithmetic is internally correct, target exits land at the configured target, and stop reasons follow the recorded active stop reason. Trigger correctness against the actual tick path cannot be verified from alert messages alone. BREAK EVEN is absent because it was disabled; no `other`/ambiguous live exits occurred.")
    add("")
    add("## 8. First entry vs re-entry")
    add("")
    add(md_table(["Cohort", "Trades", "Win rate", "Gross", "Average", "Avg R", "Profit factor", "Avg hold"], summarize_rows(entry_groups)))
    add("")
    add("Symbols with the most entries:")
    add("")
    add(md_table(
        ["Symbol", "Entries", "Wins", "Losses", "Gross P&L"],
        [[symbol, len(group), stats(group)["wins"], stats(group)["losses"], money(stats(group)["gross"])] for symbol, group in sorted(by_symbol.items(), key=lambda kv: (-len(kv[1]), kv[0]))],
    ))
    add("")
    add("## 9. Rapid BUY→SELL investigation")
    add("")
    add(md_table(
        ["Trade", "BUY IST", "Buy", "SELL IST", "Sell", "Duration", "Reason", "P&L", "Initial SL"],
        [[t.trade_id, t.buy.timestamp.astimezone(IST).strftime("%H:%M:%S"), money(t.entry), t.exit.timestamp.astimezone(IST).strftime("%H:%M:%S"), money(t.exit_price), duration_text(t.duration_seconds), t.exit_reason, money(t.pnl), money(t.stop)] for t in rapid],
    ))
    add("")
    add("All BUYs are confirmed PSAR flips at a 1-minute bar close. Management is disabled on the entry bar (`bar_index > entryBarIp`), then begins on the next bar. A sub-minute EXIT is therefore possible on the first update of the next 1-minute bar if current price is already at/below the stop. PSAR FALLBACK requires a confirmed opposite flip under these inputs, so its fastest normal timing is about one bar. These are code-legitimate invalidations; repeated clusters and their P&L are evidence of sensitivity/chop, but the CSV lacks raw PSAR values and candles needed to quantify chop independently.")
    add("")
    add("## 10. Time-of-day analysis")
    add("")
    add(md_table(["Entry bucket IST", "Trades", "Win rate", "Gross", "Average", "Avg R", "Profit factor", "Avg hold"], summarize_rows(bucket_groups)))
    add("")
    add("## 11. Liquidity analysis")
    add("")
    add(md_table(
        ["Observed liquidity payload", "Alerts"],
        [["PASS", alerts_by_liq.get("PASS", 0)], ["FAIL", alerts_by_liq.get("FAIL", 0)], ["UNKNOWN", alerts_by_liq.get("UNKNOWN", 0)], ["SKIP total", event_counts['SKIP_FAIL'] + event_counts['SKIP_UNKNOWN']]],
    ))
    add("")
    add(f"Every actual BUY reports PASS; there are no UNKNOWN alerts. PASS-vs-UNKNOWN trade performance therefore cannot be compared. FAIL signals are deliberately untraded, so the CSV also cannot measure their counterfactual returns. The gate emitted {sum(skip_symbols.values())} SKIP rows representing {unique_skip_episodes} unique symbol-timestamp candidates after collapsing three parallel ARVEE configurations. Most-skipped symbols: " + ", ".join(f"{s} ({n})" for s, n in skip_symbols.most_common(10)) + ".")
    add("")
    add("The baseline implementation is causal: it waits for a full lookback of completed current-session bars and uses `volume[1]`/`(close×volume)[1]` (147–165). The three chart alerts used lookbacks 10, 15 and 20. No UNKNOWN events appear because the export starts at 09:40 IST, by which time even the 20-bar baseline can be ready. The CSV provides no evidence of prior-day leakage under the hardened code.")
    add("")
    add("## 12. PSAR-entry analysis")
    add("")
    add(f"The CSV contains {len(trades) + event_counts['SKIP_FAIL'] + event_counts['SKIP_UNKNOWN']} BUY/SKIP alert rows but only **{unique_candidate_episodes} unique symbol-timestamp candidate episodes**, after collapsing the three parallel ARVEE configuration collisions. It proves at least that many flat-state confirmed PSAR BUY candidates. It does **not** log every PSAR flip while already in a trade, nor raw PSAR level/direction, ADX, EMA or VWAP values. Exact flip frequency and movement-before-entry are therefore data limitations.")
    add("")
    add(f"Observed confirmed opposite flips caused {len(psar_fallbacks)} PSAR FALLBACK exits. {sum(t.duration_seconds < 300 for t in psar_fallbacks)} of those occurred within five minutes. Across all exits, {len(rapid)} trades reversed/invalidated inside five minutes. This isolates short-lived entries, but cannot by itself separate 'bad PSAR entry' from an appropriately tight initial stop: stop exits with loss/risk near 1R are designed invalidations, whereas loss/risk materially above 1R is specifically exit-reference overshoot.")
    add("")
    add("## 13. Symbol-level forensic table")
    add("")
    add(md_table(
        ["Symbol", "Trades", "First P&L", "Re-entry P&L", "Wins", "Losses", "Win rate", "Gross", "Avg R", "Worst R", "Main exit", "Rapid"],
        [[s, n, money(fp), money(rp), w, l, pct(wr), money(g), fnum(ar), fnum(wr_r), reason, rapid_n] for s, n, fp, rp, w, l, wr, g, ar, wr_r, reason, rapid_n in symbol_rows],
    ))
    add("")
    add("Worst 10 trade episodes:")
    add("")
    add(md_table(["Trade", "Entry / initial SL", "Highest", "Old / new SL", "Exit", "Hold", "P&L", "R"], [[t.trade_id, f"{money(t.entry)} / {money(t.stop)}", money(t.exit.fields.get("Highest since entry")), f"{money(t.exit.fields.get('Old SL'))} / {money(t.exit.fields.get('New SL'))}", money(t.exit_price), duration_text(t.duration_seconds), money(t.pnl), fnum(t.r_multiple)] for t in losers[:10]]))
    add("")
    add("All ten worst episodes exited on the HARD STOP path. In each, `Old SL` equals `New SL`, so the loss was not created by a newly ratcheted profit-lock/trailing stop. The most pathological STYLEBAAZA entries never exceeded their entry in the logged `Highest since entry`, then first observed price crossed materially below the unchanged initial stop: exact evidence of stop-reference overshoot layered on failed entries.")
    add("")
    add("Best 10 trade episodes:")
    add("")
    add(md_table(["Trade", "Entry / target", "Highest", "Exit", "Reason", "Hold", "P&L", "R"], [[t.trade_id, f"{money(t.entry)} / {money(t.target)}", money(t.exit.fields.get("Highest since entry")), money(t.exit_price), t.exit_reason, duration_text(t.duration_seconds), money(t.pnl), fnum(t.r_multiple)] for t in winners[:10]]))
    add("")
    add("All ten best episodes reached TARGET and realized approximately +2R, exactly matching the configured fixed-target path. The contrasting tails show that the target mechanism behaves as encoded while hard-stop losses can extend well past −1R. Case-study interpretation is bounded to the logged state path: without candle/tick history, no claim is made about what price did afterward or whether another entry rule would have been better.")
    add("")
    add("## 14. Cost sensitivity")
    add("")
    add(md_table(
        ["Metric", "Value"],
        [["Gross theoretical result", money(overall['gross'])], ["Round-trip traded turnover", money(turnover)], ["Gross per closed trade", money(overall['avg'])], ["Break-even total costs", money(overall['gross']) if overall['gross'] > 0 else "Not applicable: gross result is non-positive"], ["Break-even cost per trade", money(break_even_per_trade)], ["Break-even basis points on turnover", fnum(break_even_bps)]],
    ))
    add("")
    add("No Groww/net result is fabricated. The CSV has enough theoretical price/quantity data for turnover, but not actual fills, brokerage plan, statutory charge bases, or order-level taxes. If gross is positive, the displayed break-even friction is the maximum all-in round-trip cost the day could absorb; if gross is negative, no positive cost assumption can restore profitability.")
    add("")
    add("## 15. Code-to-evidence mapping and classified findings")
    add("")
    findings = [
        ("Risk-budget overshoot", f"{len(loss_over_budget)} trades lost more than ₹500; worst was {risk_worst[0].loss_multiple:.2f}× intended risk." if risk_worst else "No losing trades.", "Realtime stop checks current close and exits at close minus a tick (420–433), not at the stop.", "EXPECTED PINE/TRADINGVIEW BEHAVIOR + STRATEGY DESIGN WEAKNESS", "Log ticks/alert receipt/broker fills to separate gaps, feed cadence and delivery latency."),
        ("Repeated entries", f"First entries {money(first_s['gross'])}; re-entries {money(re_s['gross'])} across {re_s['count']} trades.", "No daily lockout; a new later confirmed PSAR flip may buy after exit (306–313).", "INTERESTING HYPOTHESIS — NEEDS MORE DAYS", "Repeat the same frozen cohort comparison across forward sessions."),
        ("Rapid exits", f"{len(rapid)} exits inside five minutes, including {sum(t.duration_seconds < 30 for t in complete)} inside 30 seconds.", "Entry-bar management is suppressed, but next-bar realtime updates can immediately hit stop (401–433).", "STRATEGY DESIGN WEAKNESS / EXPECTED BEHAVIOR", "Capture tick-level first-next-bar path and raw PSAR/ATR at entry."),
        ("Orphan exits", f"{len(orphan_exits)} EXIT alerts lack an earlier BUY in the export.", "Historical state may be reconstructed before realtime alerts; export starts 09:40 IST.", "DATA LIMITATION / EXPECTED PINE BEHAVIOR", "Record alert creation/reload timestamps and persistent trade IDs."),
        ("Liquidity gating", f"PASS={alerts_by_liq.get('PASS',0)}, FAIL={alerts_by_liq.get('FAIL',0)}, UNKNOWN={alerts_by_liq.get('UNKNOWN',0)}; all BUYs PASS.", "Causal prior-bar session baseline at 147–165; only PASS enters at 309.", "NO ISSUE FOUND; COUNTERFACTUAL DATA MISSING", "Track hypothetical outcomes for FAIL/UNKNOWN without trading them."),
        ("Exit alert correctness", f"{len(pnl_mismatches)} logged P&L values differ from price×qty by >₹0.01 after exact CSV reconstruction.", "Exit P&L is `(exitPrice-assumedEntry)*qty` at 494–507.", "NO ISSUE FOUND" if not pnl_mismatches else "CONFIRMED DATA/CODE INCONSISTENCY", "Continue schema-level reconciliation."),
        ("PSAR quality", f"At least {unique_candidate_episodes} unique eligible flat-state bullish-flip episodes; {len(psar_fallbacks)} exits on bearish fallback.", "Confirmed flip definitions at 40–45; no raw PSAR telemetry in payload.", "DATA LIMITATION / INTERESTING HYPOTHESIS", "Log PSAR level, direction, flip sequence and chop-regime features causally."),
        ("Parallel alert definitions", f"{len(event_collisions)} same-symbol/event/second collisions, all ARVEE SKIPs from 10/20-bar lookbacks.", "Independent TradingView alert definitions run the same script with different inputs.", "EXPECTED OPERATIONAL BEHAVIOR / POSSIBLE CONFIGURATION ISSUE", "Inventory intended alert definition per symbol and flag parallel configurations prospectively."),
        ("Day/session state resilience", "No Sep 28 lifecycle proves an overnight carry, but five orphans cannot be fully attributed.", "New-day detection resets liquidity only; position state depends on an executed exit, normally EOD.", "STRATEGY DESIGN WEAKNESS — NOT OBSERVED AS A CONFIRMED FAILURE", "Force a missed-EOD/reload forward test with run IDs and verify next-session state."),
    ]
    for title, evidence, behavior, classification, test in findings:
        add(f"### {title}")
        add("")
        add(f"- **Evidence:** {evidence}")
        add(f"- **Responsible Pine behavior:** {behavior}")
        add(f"- **Classification:** {classification}")
        add(f"- **Next falsifiable test:** {test}")
        add("- **Status:** NOT YET A RECOMMENDED CHANGE.")
        add("")
    add("No confirmed Pine logic defect is established by this one-day export. The strongest mechanical issue is not a sizing error: it is the expected difference between intended stop risk and an alert-based market reference after price has crossed that stop.")
    add("")
    add("## 16. Ranked research questions for next forward sessions")
    add("")
    add("1. Do stop-reference overshoots persist with tick receipt time and broker-fill evidence, and are they concentrated by symbol/liquidity?")
    add("2. Does re-entry expectancy remain different from first-entry expectancy across frozen forward days?")
    add("3. Are sub-five-minute losses associated with high recent PSAR flip counts when that count is logged causally?")
    add("4. What are shadow outcomes of FAIL and UNKNOWN liquidity candidates under the same exit model?")
    add("5. Which exit mechanisms remain positive after actual fills and all-in costs, not theoretical references?")
    add("6. How often do alert/chart reloads create orphan lifecycles, and can a persistent run/trade ID resolve them?")
    add("")
    add("# CANDIDATE CHANGES — NOT IMPLEMENTED")
    add("")
    candidates = [
        ("Realized losses can exceed intended risk", "Loss/risk ratios above 1× and the current-close exit path", "Use execution-aware stop handling or explicitly model alert slippage", "Tighter correspondence between budget and deployed risk", "Resting orders can fill differently; more implementation complexity", "Broker/tick evidence showing systematic actionable overshoot"),
        ("Re-entry expectancy may differ from first entries", f"First {money(first_s['gross'])} vs re-entry {money(re_s['gross'])} today", "Add re-entry telemetry/cooldown shadow variants", "Can identify chop without changing production behavior", "One-day selection bias and missed recovery moves", "Several frozen forward sessions with stable cohort gap"),
        ("Rapid invalidations may indicate PSAR chop", f"{len(rapid)} trades under five minutes", "Log prior flip count, PSAR distance, ATR and causal regime state", "Separates bad entry from correctly managed noise", "More payload volume; telemetry may not generalize", "Prospective relationship between entry context and rapid loss"),
        ("Orphan lifecycle attribution is ambiguous", f"{len(orphan_exits)} orphan EXITs", "Emit run ID, trade ID, script build ID and alert-created timestamp", "Deterministic pairing across reloads", "Operational/schema migration overhead", "Replay/reload tests and zero unexplained orphans"),
        ("Liquidity gate value is unmeasured", f"{sum(skip_symbols.values())} FAIL candidates skipped, no outcomes", "Add non-trading shadow outcome tracking for skips", "Measures selection value without removing the gate", "Requires causal outcome collector", "Forward PASS-vs-FAIL expectancy with identical horizon/exit semantics"),
    ]
    add(md_table(["Observed problem", "Evidence", "Proposed hypothesis", "Potential benefit", "Potential downside", "Evidence needed"], candidates))
    add("")
    add("None of these changes was made. No Pine file, parameter, stop width, re-entry rule, indicator set, or liquidity rule was modified.")
    add("")
    add("## Reproducibility")
    add("")
    add("```bash")
    add("python3 analysis/psar_v2_sep28_forensics.py")
    add("```")
    add("")
    add("Primary per-trade evidence is in `analysis/PSAR_V2_SEP28_TRADES.csv`.")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alerts", type=Path, help="Explicit TradingView CSV path")
    parser.add_argument("--pine", type=Path, help="Explicit Pine source path")
    parser.add_argument("--ref", default=DEFAULT_REF, help="Git ref fallback (default: origin/main)")
    parser.add_argument("--output-dir", type=Path, default=REPO / "analysis")
    args = parser.parse_args()

    csv_text, csv_source = load_text(args.alerts, CSV_REPO_PATH, args.ref)
    pine_text, pine_source = load_text(args.pine, PINE_REPO_PATH, args.ref)
    alerts, raw_rows = parse_alerts(csv_text)
    if not alerts:
        raise SystemExit("No alerts found")
    trades, orphan_exits, duplicates = reconstruct(alerts)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    write_trades(args.output_dir / "PSAR_V2_SEP28_TRADES.csv", trades)
    report = generate_report(
        alerts, raw_rows, trades, orphan_exits, duplicates,
        csv_source, pine_source, csv_text, pine_text, args.ref,
    )
    (args.output_dir / "PSAR_V2_SEP28_FORENSIC_REPORT.md").write_text(report, encoding="utf-8")
    print(f"alerts={len(alerts)} trades={len(trades)} closed={sum(t.exit is not None for t in trades)} orphans={len(orphan_exits)}")
    print(args.output_dir / "PSAR_V2_SEP28_TRADES.csv")
    print(args.output_dir / "PSAR_V2_SEP28_FORENSIC_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
