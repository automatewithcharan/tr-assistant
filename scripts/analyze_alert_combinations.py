#!/usr/bin/env python3
"""Analyze single and mixed alert-lane combinations.

Input is produced by scripts/backtest_alert_lanes.py. A combination "wins" for a
symbol/day when all required lanes appeared for that symbol on that date; entry
is measured from the latest alert among the required lanes.
"""

from __future__ import annotations

import argparse
from itertools import combinations
from pathlib import Path
from typing import Iterable

import pandas as pd


def _combo_label(items: Iterable[str]) -> str:
    return "+".join(items)


def _build_combo_rows(df: pd.DataFrame, dimensions: list[str], max_size: int) -> pd.DataFrame:
    rows = []
    grouped = df.groupby(["date", "symbol"], sort=False)

    for keys, group in grouped:
        date, symbol = keys
        for dimension in dimensions:
            values = sorted({str(v) for v in group[dimension].dropna().unique() if str(v)})
            for size in range(1, min(max_size, len(values)) + 1):
                for combo in combinations(values, size):
                    combo_group = group[group[dimension].isin(combo)].sort_values("when")
                    if set(combo_group[dimension]) != set(combo):
                        continue
                    entry = combo_group.iloc[-1]
                    rows.append({
                        "dimension": dimension,
                        "combo_size": size,
                        "combo": _combo_label(combo),
                        "date": date,
                        "symbol": symbol,
                        "entry_when": entry["when"],
                        "entry_lane": entry.get("lane", ""),
                        "entry_action": entry.get("action", ""),
                        "entry_price": entry.get("price", 0.0),
                        "close": entry.get("close", 0.0),
                        "eod_return_pct": entry.get("eod_return_pct", 0.0),
                        "max_fav_pct": entry.get("max_fav_pct", 0.0),
                        "max_adv_pct": entry.get("max_adv_pct", 0.0),
                        "stop_hit": bool(entry.get("stop_hit", False)),
                        "t1_hit": bool(entry.get("t1_hit", False)),
                    })
    return pd.DataFrame(rows)


def _summarize(combo_rows: pd.DataFrame) -> pd.DataFrame:
    if combo_rows.empty:
        return pd.DataFrame()

    def win_rate(series: pd.Series) -> float:
        return round((series > 0).mean() * 100.0, 2)

    grouped = combo_rows.groupby(["dimension", "combo_size", "combo"], dropna=False)
    summary = grouped.agg(
        n=("symbol", "count"),
        symbols=("symbol", lambda s: ",".join(sorted(set(map(str, s)))[:8])),
        win_rate_pct=("eod_return_pct", win_rate),
        avg_eod_pct=("eod_return_pct", lambda s: round(s.mean(), 3)),
        median_eod_pct=("eod_return_pct", lambda s: round(s.median(), 3)),
        avg_mfe_pct=("max_fav_pct", lambda s: round(s.mean(), 3)),
        avg_mae_pct=("max_adv_pct", lambda s: round(s.mean(), 3)),
        stop_hit_pct=("stop_hit", lambda s: round(s.mean() * 100.0, 2)),
        t1_hit_pct=("t1_hit", lambda s: round(s.mean() * 100.0, 2)),
    ).reset_index()

    summary.sort_values(
        by=["win_rate_pct", "avg_eod_pct", "n"],
        ascending=[False, False, False],
        inplace=True,
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze alert lane/action combinations")
    parser.add_argument("--input", default="results/Data/alert_lane_backtest_all_lanes.csv")
    parser.add_argument("--out-prefix", default="results/Data/alert_combo")
    parser.add_argument("--max-size", type=int, default=3)
    parser.add_argument("--min-samples", type=int, default=1)
    args = parser.parse_args()

    df = pd.read_csv(args.input, parse_dates=["when"])
    df = df[df.get("status", "ok") == "ok"].copy()
    if df.empty:
        print("No evaluated rows found.")
        return 1

    df["date"] = df["when"].dt.date.astype(str)
    df["action_family"] = df["action"].astype(str)
    df["lane_action"] = df["lane"].astype(str) + ":" + df["action"].astype(str)

    combo_rows = _build_combo_rows(
        df,
        dimensions=["lane", "action_family", "lane_action"],
        max_size=max(1, int(args.max_size)),
    )
    summary = _summarize(combo_rows)

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    detail_path = out_prefix.with_name(out_prefix.name + "_details.csv")
    summary_path = out_prefix.with_name(out_prefix.name + "_summary.csv")
    combo_rows.to_csv(detail_path, index=False)
    summary.to_csv(summary_path, index=False)

    filtered = summary[summary["n"] >= int(args.min_samples)].copy()
    print(f"Input rows: {len(df)}")
    print(f"Combo detail rows: {len(combo_rows)}")
    print(f"Summary rows: {len(summary)}")
    print(f"Saved: {detail_path}")
    print(f"Saved: {summary_path}")

    print("\nTop combinations:")
    cols = [
        "dimension", "combo_size", "combo", "n", "win_rate_pct",
        "avg_eod_pct", "median_eod_pct", "avg_mfe_pct", "avg_mae_pct", "t1_hit_pct",
    ]
    print(filtered[cols].head(30).to_string(index=False))

    robust = filtered[(filtered["n"] >= 5) & (filtered["win_rate_pct"] >= 80.0)]
    print("\nRobust-looking combos (n>=5, win>=80%):")
    if robust.empty:
        print("None.")
    else:
        print(robust[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
