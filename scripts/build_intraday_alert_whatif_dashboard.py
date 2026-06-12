#!/usr/bin/env python3
"""Build an offline what-if dashboard for intraday alert entries/exits.

Question answered:
    If an alert fired at price P, what would have happened if we waited for
    a pullback entry like -1%/-2% from P and sold at +0.5/+1/+1.5/+2%?

The script uses structured alert journals plus post-alert candles. It is
designed for learning execution discipline, not for producing live orders.
"""

from __future__ import annotations

import argparse
from html import escape
from pathlib import Path
import sys
from typing import Optional

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from scripts.audit_intraday_goal_alert_outcomes import (  # noqa: E402
    apply_costs,
    fetch_history,
    load_alert_files,
    load_alerts,
)


def _parse_float_list(text: str) -> list[float]:
    return [float(item.strip()) for item in str(text).split(",") if item.strip()]


def _fmt_pct(value: object) -> str:
    try:
        if pd.isna(value):
            return "-"
        return f"{float(value):+.2f}%"
    except Exception:
        return "-"


def _fmt_money(value: object) -> str:
    try:
        if pd.isna(value):
            return "-"
        return f"Rs {float(value):+,.0f}"
    except Exception:
        return "-"


def _bar_color(value: float) -> str:
    if value > 0:
        return "#0f8b4c"
    if value < 0:
        return "#b42318"
    return "#475467"


def _find_entry(candles: pd.DataFrame, alert_price: float, entry_offset_pct: float) -> tuple[Optional[pd.Timestamp], Optional[float], pd.DataFrame]:
    if candles.empty or alert_price <= 0:
        return None, None, pd.DataFrame()
    trigger = alert_price * (1.0 + entry_offset_pct / 100.0)
    if entry_offset_pct >= 0:
        entry_time = candles.index[0]
        entry_price = float(candles.iloc[0]["open"])
        return entry_time, entry_price, candles
    hits = candles[candles["low"].astype(float) <= trigger]
    if hits.empty:
        return None, None, pd.DataFrame()
    entry_time = hits.index[0]
    trade_candles = candles[candles.index >= entry_time]
    return entry_time, round(trigger, 2), trade_candles


def _simulate_trade(
    candles: pd.DataFrame,
    alert_price: float,
    entry_offset_pct: float,
    target_pct: float,
    stop_pct: float,
) -> dict:
    entry_time, entry_price, trade_candles = _find_entry(candles, alert_price, entry_offset_pct)
    result = {
        "entered": False,
        "entry_time": None,
        "entry_price": None,
        "exit_time": None,
        "exit_price": None,
        "outcome": "no_entry",
        "gross_return_pct": None,
        "net_return_pct": None,
        "mfe_pct": None,
        "mae_pct": None,
    }
    if entry_time is None or entry_price is None or trade_candles.empty:
        return result

    target = entry_price * (1.0 + target_pct / 100.0)
    stop = entry_price * (1.0 - stop_pct / 100.0)
    high = float(trade_candles["high"].max())
    low = float(trade_candles["low"].min())
    exit_time = trade_candles.index[-1]
    exit_price = float(trade_candles["close"].iloc[-1])
    outcome = "eod"

    for ts, candle in trade_candles.iterrows():
        candle_low = float(candle["low"])
        candle_high = float(candle["high"])
        # Conservative for ambiguous candles: if both target and stop are inside
        # the same bar after entry, count the stop first.
        if candle_low <= stop:
            exit_time = ts
            exit_price = stop
            outcome = "stop"
            break
        if candle_high >= target:
            exit_time = ts
            exit_price = target
            outcome = "target"
            break

    costs = apply_costs(entry_price, exit_price)
    result.update(
        {
            "entered": True,
            "entry_time": entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": round(entry_price, 2),
            "exit_time": exit_time.strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": round(exit_price, 2),
            "outcome": outcome,
            "gross_return_pct": costs.gross_return_pct,
            "net_return_pct": costs.net_return_pct,
            "mfe_pct": round((high - entry_price) / entry_price * 100.0, 3),
            "mae_pct": round((low - entry_price) / entry_price * 100.0, 3),
        }
    )
    return result


def evaluate_grid(args) -> pd.DataFrame:
    from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider

    data_dir = Path(args.data_dir)
    files = load_alert_files(data_dir, args.date, args.start_date, args.end_date)
    alerts = load_alerts(files, first_per_symbol_per_day=not args.all_alerts)
    if alerts.empty:
        return pd.DataFrame()
    if args.lanes:
        wanted = {lane.strip() for lane in str(args.lanes).split(",") if lane.strip()}
        alerts = alerts[alerts["lane"].isin(wanted)].copy()

    provider = KiteHistoricalDataProvider()
    entry_offsets = _parse_float_list(args.entry_offsets)
    targets = _parse_float_list(args.targets)
    cache: dict[tuple[str, str], tuple[pd.DataFrame, Optional[str]]] = {}
    rows: list[dict] = []

    for idx, alert in alerts.iterrows():
        key = (alert["Symbol"], alert["date"])
        if key not in cache:
            cache[key] = fetch_history(
                provider,
                alert["Symbol"],
                alert["SentAt"].to_pydatetime(),
                delay_sec=max(0.0, float(args.delay_sec)),
                data_dir=data_dir,
                local_interval=args.local_interval,
                prefer_local=bool(args.prefer_local),
            )
        candles, fetch_error = cache[key]
        sent_floor = alert["SentAt"].to_pydatetime().replace(second=0, microsecond=0)
        post_alert = candles[candles.index >= sent_floor] if not candles.empty else pd.DataFrame()
        for entry_offset in entry_offsets:
            for target in targets:
                sim = _simulate_trade(
                    post_alert,
                    alert_price=float(alert["PriceNow"]),
                    entry_offset_pct=float(entry_offset),
                    target_pct=float(target),
                    stop_pct=float(args.stop_pct),
                )
                rows.append(
                    {
                        "date": alert["date"],
                        "symbol": alert["Symbol"],
                        "lane": alert["lane"],
                        "action": alert.get("Action", ""),
                        "sent_at": alert["SentAt"],
                        "alert_price": float(alert["PriceNow"]),
                        "entry_offset_pct": float(entry_offset),
                        "target_pct": float(target),
                        "stop_pct": float(args.stop_pct),
                        "has_candles": not post_alert.empty,
                        "fetch_error": fetch_error,
                        **sim,
                    }
                )
        if (idx + 1) % 20 == 0:
            print(f"Prepared what-if rows for {idx + 1}/{len(alerts)} alerts...", flush=True)
    return pd.DataFrame(rows)


def summarize(results: pd.DataFrame, ticket: float) -> pd.DataFrame:
    if results.empty:
        return pd.DataFrame()
    entered = results[results["entered"] == True].copy()
    if entered.empty:
        return pd.DataFrame()
    entered["pnl_rupees"] = entered["net_return_pct"].astype(float) / 100.0 * float(ticket)
    rows = []
    keys = ["lane", "entry_offset_pct", "target_pct", "stop_pct"]
    for key, group in entered.groupby(keys, dropna=False):
        lane, entry_offset, target, stop = key
        rows.append(
            {
                "lane": lane,
                "entry_offset_pct": entry_offset,
                "target_pct": target,
                "stop_pct": stop,
                "entries": len(group),
                "target_rate_pct": (group["outcome"].eq("target").mean() * 100.0),
                "stop_rate_pct": (group["outcome"].eq("stop").mean() * 100.0),
                "win_rate_pct": (group["net_return_pct"].astype(float) > 0).mean() * 100.0,
                "avg_net_return_pct": group["net_return_pct"].astype(float).mean(),
                "median_net_return_pct": group["net_return_pct"].astype(float).median(),
                "total_pnl_rupees": group["pnl_rupees"].sum(),
                "avg_mfe_pct": group["mfe_pct"].astype(float).mean(),
                "avg_mae_pct": group["mae_pct"].astype(float).mean(),
            }
        )
    return pd.DataFrame(rows).sort_values(["total_pnl_rupees", "avg_net_return_pct"], ascending=[False, False])


def _pivot_html(summary: pd.DataFrame, lane: str, value_col: str) -> str:
    sub = summary[summary["lane"] == lane]
    if sub.empty:
        return "<p>No entries.</p>"
    pivot = sub.pivot_table(index="entry_offset_pct", columns="target_pct", values=value_col, aggfunc="mean")
    header = "".join(f"<th>sell +{escape(str(col))}%</th>" for col in pivot.columns)
    rows = []
    for idx, row in pivot.iterrows():
        cells = []
        for value in row:
            text = _fmt_money(value) if value_col == "total_pnl_rupees" else _fmt_pct(value)
            color = _bar_color(float(value)) if pd.notna(value) else "#475467"
            cells.append(f"<td style='color:{color}'>{escape(text)}</td>")
        rows.append(f"<tr><th>buy {idx:+.1f}%</th>{''.join(cells)}</tr>")
    return f"<table><thead><tr><th></th>{header}</tr></thead><tbody>{''.join(rows)}</tbody></table>"


def write_dashboard(summary: pd.DataFrame, results: pd.DataFrame, out_html: Path, ticket: float) -> None:
    out_html.parent.mkdir(parents=True, exist_ok=True)
    if summary.empty:
        error_count = int(results["fetch_error"].notna().sum()) if "fetch_error" in results else 0
        body = f"""
        <h1>Intraday Alert What-If Dashboard</h1>
        <section class="panel">
          <h2>No Evaluated Entries Yet</h2>
          <p>Rows generated: {len(results):,}. Fetch errors: {error_count:,}.</p>
          <p>This usually means Kite historical data is unavailable, or local candle cache does not contain these dates.</p>
        </section>
        """
    else:
        lanes = list(summary["lane"].dropna().unique())
        cards = []
        for _, row in summary.head(12).iterrows():
            cards.append(
                f"""
                <div class="card">
                  <div class="muted">{escape(str(row['lane']))}</div>
                  <h3>Buy {row['entry_offset_pct']:+.1f}% / Sell +{row['target_pct']:.1f}%</h3>
                  <div class="big" style="color:{_bar_color(float(row['total_pnl_rupees']))}">{escape(_fmt_money(row['total_pnl_rupees']))}</div>
                  <p>{int(row['entries'])} entries | WR {_fmt_pct(row['win_rate_pct'])} | Target {_fmt_pct(row['target_rate_pct'])} | Stop {_fmt_pct(row['stop_rate_pct'])}</p>
                </div>
                """
            )
        lane_sections = []
        for lane in lanes:
            lane_sections.append(
                f"""
                <section class="panel">
                  <h2>{escape(str(lane))}</h2>
                  <h3>Total P&L per Rs {ticket:,.0f} trade</h3>
                  {_pivot_html(summary, lane, 'total_pnl_rupees')}
                  <h3>Average Net Return</h3>
                  {_pivot_html(summary, lane, 'avg_net_return_pct')}
                </section>
                """
            )
        sample_cols = [
            "date",
            "symbol",
            "lane",
            "entry_offset_pct",
            "target_pct",
            "outcome",
            "net_return_pct",
            "mfe_pct",
            "mae_pct",
        ]
        sample = results[results["entered"] == True].copy()
        if not sample.empty:
            sample = sample.sort_values("net_return_pct", ascending=False).head(40)
        table_rows = []
        for _, row in sample.iterrows():
            table_rows.append(
                "<tr>"
                + "".join(f"<td>{escape(str(row.get(col, '')))}</td>" for col in sample_cols)
                + "</tr>"
            )
        body = f"""
        <h1>Intraday Alert What-If Dashboard</h1>
        <p class="muted">Tests pullback entries and fixed target exits after the alert was sent. Conservative same-candle rule: stop beats target when both are possible.</p>
        <section class="grid">{''.join(cards)}</section>
        {''.join(lane_sections)}
        <section class="panel">
          <h2>Best Example Trades</h2>
          <table><thead><tr>{''.join(f'<th>{escape(col)}</th>' for col in sample_cols)}</tr></thead><tbody>{''.join(table_rows)}</tbody></table>
        </section>
        """
    html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Intraday Alert What-If Dashboard</title>
  <style>
    body {{ font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f6f7f9; color: #111827; }}
    h1 {{ margin: 28px auto 8px; max-width: 1180px; font-size: 28px; }}
    h2 {{ margin: 0 0 14px; font-size: 20px; }}
    h3 {{ margin: 18px 0 8px; font-size: 15px; }}
    p {{ line-height: 1.45; }}
    .muted {{ color: #667085; }}
    .grid {{ max-width: 1180px; margin: 18px auto; display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }}
    .card, .panel {{ background: white; border: 1px solid #e4e7ec; border-radius: 8px; box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04); }}
    .card {{ padding: 16px; }}
    .card h3 {{ margin-top: 6px; }}
    .big {{ font-size: 26px; font-weight: 750; }}
    .panel {{ max-width: 1180px; margin: 16px auto; padding: 18px; overflow-x: auto; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ border-bottom: 1px solid #eaecf0; padding: 9px 10px; text-align: right; white-space: nowrap; }}
    th:first-child, td:first-child {{ text-align: left; }}
    thead th {{ background: #f2f4f7; color: #344054; }}
  </style>
</head>
<body>{body}</body>
</html>
"""
    out_html.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build intraday alert what-if dashboard")
    parser.add_argument("--data-dir", default=str(REPO_ROOT / "results" / "Data"))
    parser.add_argument("--date")
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--entry-offsets", default="0,-0.5,-1,-1.5,-2")
    parser.add_argument("--targets", default="0.5,1,1.5,2,3")
    parser.add_argument("--stop-pct", type=float, default=1.0)
    parser.add_argument("--ticket", type=float, default=50000.0)
    parser.add_argument("--lanes", default="")
    parser.add_argument("--all-alerts", action="store_true")
    parser.add_argument("--prefer-local", action="store_true")
    parser.add_argument("--local-interval", choices=["minute", "5minute", "15minute"], default="5minute")
    parser.add_argument("--delay-sec", type=float, default=0.15)
    parser.add_argument("--out-dir", default="")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir) if args.out_dir else data_dir / "intraday_alert_whatif"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = evaluate_grid(args)
    summary = summarize(results, ticket=float(args.ticket))
    results_path = out_dir / "whatif_trades.csv"
    summary_path = out_dir / "whatif_summary.csv"
    html_path = out_dir / "whatif_dashboard.html"
    results.to_csv(results_path, index=False)
    summary.to_csv(summary_path, index=False)
    write_dashboard(summary, results, html_path, ticket=float(args.ticket))
    print(f"Wrote: {results_path}")
    print(f"Wrote: {summary_path}")
    print(f"Wrote: {html_path}")
    if summary.empty:
        print("No evaluated entries yet. Check Kite auth or local candle cache coverage.")
    else:
        print(summary.head(12).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
