#!/usr/bin/env python3
"""
Unified strategy-vs-benchmark report for swing trading.

This script compares:
  1. Historical swing backtest summary
  2. Real forward-backtest trades
  3. Broad-market / mutual-fund-style benchmarks
  4. Walk-forward filtered forward results (true out-of-sample selection)

It is intentionally conservative:
  - Forward results are evaluated only on existing unseen trade rows
  - Walk-forward chooses filters using prior signal days only
  - Benchmarks are measured over the exact same forward date range
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[1]
RESULTS_DIR = REPO_ROOT / "results" / "Data"

import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class ForwardFilterConfig:
    min_confidence: float
    min_volume_ratio: float
    min_rank_score: float
    allowed_regimes: str
    top_n_per_day: int


def fetch_benchmark_history(start_date: str = None, period: str = "2y") -> Dict[str, List[Tuple[str, float]]]:
    result = {"nifty50": [], "nifty500": []}
    for key, ticker in [("nifty50", "^NSEI"), ("nifty500", "^CRSLDX")]:
        try:
            kwargs = {"progress": False, "auto_adjust": True}
            if start_date:
                kwargs["start"] = start_date
            else:
                kwargs["period"] = period
            df = yf.download(ticker, **kwargs)
            if df is None or df.empty or "Close" not in df.columns:
                continue
            closes = df["Close"].squeeze().dropna()
            for dt_idx, price in closes.items():
                date_str = dt_idx.strftime("%Y-%m-%d") if hasattr(dt_idx, "strftime") else str(dt_idx)[:10]
                result[key].append((date_str, float(price)))
        except Exception:
            continue
    return result


def _find_latest_file(pattern: str) -> Optional[Path]:
    matches = sorted(RESULTS_DIR.glob(pattern), key=lambda p: p.stat().st_mtime, reverse=True)
    return matches[0] if matches else None


def _load_forward_trades(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    df = pd.read_csv(csv_path)
    if df.empty:
        raise ValueError(f"No forward trades found in {csv_path}")
    required = {"SignalDate", "Symbol", "TradeReturnPct", "ConfidencePct", "VolumeRatio", "RankScore", "Regime"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{csv_path} is missing columns: {sorted(missing)}")
    df["SignalDate"] = pd.to_datetime(df["SignalDate"]).dt.strftime("%Y-%m-%d")
    for col in ["TradeReturnPct", "ConfidencePct", "VolumeRatio", "RankScore", "HoldDays"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["Regime"] = df["Regime"].astype(str).str.upper().fillna("UNKNOWN")
    df = df.sort_values(["SignalDate", "RankScore", "Symbol"], ascending=[True, False, True]).reset_index(drop=True)
    return df


def _load_swing_backtest_summary(csv_path: Path) -> pd.DataFrame:
    if not csv_path.exists():
        raise FileNotFoundError(csv_path)
    df = pd.read_csv(csv_path)
    if df.empty:
        raise ValueError(f"No swing backtest summary found in {csv_path}")
    return df


def _generate_filter_configs() -> List[ForwardFilterConfig]:
    configs: List[ForwardFilterConfig] = []
    for min_conf in [40.0, 45.0, 48.0]:
        for min_vol in [2.0, 2.5, 3.0]:
            for min_rank in [60.0, 65.0, 70.0]:
                for regimes in ["BULL,NEUTRAL", "BULL"]:
                    for top_n in [1, 2, 3]:
                        configs.append(
                            ForwardFilterConfig(
                                min_confidence=min_conf,
                                min_volume_ratio=min_vol,
                                min_rank_score=min_rank,
                                allowed_regimes=regimes,
                                top_n_per_day=top_n,
                            )
                        )
    return configs


def _apply_forward_config(df: pd.DataFrame, cfg: ForwardFilterConfig) -> pd.DataFrame:
    if df is None or df.empty:
        return df.iloc[0:0].copy()
    allowed = {part.strip().upper() for part in str(cfg.allowed_regimes).split(",") if part.strip()}
    filtered = df[
        (df["ConfidencePct"] >= cfg.min_confidence)
        & (df["VolumeRatio"] >= cfg.min_volume_ratio)
        & (df["RankScore"] >= cfg.min_rank_score)
        & (df["Regime"].isin(allowed))
    ].copy()
    if filtered.empty:
        return filtered
    return filtered.groupby("SignalDate", group_keys=False).head(cfg.top_n_per_day).reset_index(drop=True)


def _describe_forward_config(cfg: ForwardFilterConfig) -> str:
    return (
        f"conf>={cfg.min_confidence:.1f}, vol>={cfg.min_volume_ratio:.1f}, "
        f"rank>={cfg.min_rank_score:.1f}, regimes={cfg.allowed_regimes}, top{cfg.top_n_per_day}/day"
    )


def _max_drawdown_pct(daily_returns_pct: Iterable[float]) -> float:
    equity = 1.0
    peak = 1.0
    max_dd = 0.0
    for ret_pct in daily_returns_pct:
        equity *= 1.0 + (float(ret_pct or 0.0) / 100.0)
        peak = max(peak, equity)
        if peak > 0:
            dd = (equity / peak - 1.0) * 100.0
            max_dd = min(max_dd, dd)
    return round(abs(max_dd), 2)


def _build_strategy_curve(df: pd.DataFrame) -> Tuple[pd.DataFrame, Dict[str, float]]:
    if df.empty:
        empty = pd.DataFrame(columns=["SignalDate", "DailyReturnPct", "Equity"])
        return empty, {
            "signal_days": 0,
            "trade_count": 0,
            "win_rate_pct": 0.0,
            "avg_trade_return_pct": 0.0,
            "median_trade_return_pct": 0.0,
            "avg_daily_return_pct": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
        }

    daily = (
        df.groupby("SignalDate", as_index=False)
        .agg(
            DailyReturnPct=("TradeReturnPct", "mean"),
            Trades=("Symbol", "count"),
        )
        .sort_values("SignalDate")
        .reset_index(drop=True)
    )
    equity_values: List[float] = []
    equity = 100.0
    for ret in daily["DailyReturnPct"].tolist():
        equity *= 1.0 + (float(ret or 0.0) / 100.0)
        equity_values.append(round(equity, 4))
    daily["Equity"] = equity_values

    metrics = {
        "signal_days": int(daily["SignalDate"].nunique()),
        "trade_count": int(len(df)),
        "win_rate_pct": round(float((df["TradeReturnPct"] > 0).mean() * 100.0), 2),
        "avg_trade_return_pct": round(float(df["TradeReturnPct"].mean()), 2),
        "median_trade_return_pct": round(float(df["TradeReturnPct"].median()), 2),
        "avg_daily_return_pct": round(float(daily["DailyReturnPct"].mean()), 2),
        "total_return_pct": round(float(equity_values[-1] - 100.0), 2),
        "max_drawdown_pct": _max_drawdown_pct(daily["DailyReturnPct"].tolist()),
    }
    return daily, metrics


def _history_to_series(history: List[Tuple[str, float]], start: str, end: str) -> pd.Series:
    if not history:
        return pd.Series(dtype=float)
    rows = [(pd.to_datetime(d), float(v)) for d, v in history if start <= d <= end]
    if not rows:
        return pd.Series(dtype=float)
    s = pd.Series({d: v for d, v in rows}).sort_index()
    return s


def _benchmark_total_return_pct(series: pd.Series) -> Optional[float]:
    if series is None or series.empty or len(series) < 2:
        return None
    start = float(series.iloc[0])
    end = float(series.iloc[-1])
    if start <= 0:
        return None
    return round((end / start - 1.0) * 100.0, 2)


def _flat_cagr_return_pct(start: str, end: str, annual_rate: float = 0.12) -> float:
    start_ts = pd.to_datetime(start)
    end_ts = pd.to_datetime(end)
    years = max((end_ts - start_ts).days / 365.25, 0.0)
    return round(((1.0 + annual_rate) ** years - 1.0) * 100.0, 2)


def _summarize_benchmarks(start: str, end: str) -> pd.DataFrame:
    history = fetch_benchmark_history(start_date=start)
    rows: List[Dict[str, object]] = []
    for label, key in [("Nifty 50", "nifty50"), ("Nifty 500", "nifty500")]:
        series = _history_to_series(history.get(key, []), start, end)
        rows.append(
            {
                "Benchmark": label,
                "ReturnPct": _benchmark_total_return_pct(series),
                "DataPoints": int(len(series)),
            }
        )
    rows.append(
        {
            "Benchmark": "Flat 12% CAGR",
            "ReturnPct": _flat_cagr_return_pct(start, end, annual_rate=0.12),
            "DataPoints": None,
        }
    )
    return pd.DataFrame(rows)


def _walk_forward_forward_trades(
    df: pd.DataFrame,
    configs: Iterable[ForwardFilterConfig],
    train_days: int,
    min_train_trades: int,
) -> Tuple[pd.DataFrame, List[Dict[str, object]]]:
    signal_days = sorted(df["SignalDate"].unique().tolist())
    chosen_frames: List[pd.DataFrame] = []
    decisions: List[Dict[str, object]] = []

    for idx in range(train_days, len(signal_days)):
        train_dates = signal_days[idx - train_days:idx]
        test_date = signal_days[idx]
        train_df = df[df["SignalDate"].isin(train_dates)].copy()
        test_df = df[df["SignalDate"] == test_date].copy()
        if train_df.empty or test_df.empty:
            continue

        best_cfg: Optional[ForwardFilterConfig] = None
        best_score = float("-inf")
        best_metrics: Optional[Dict[str, float]] = None

        for cfg in configs:
            selected_train = _apply_forward_config(train_df, cfg)
            if len(selected_train) < min_train_trades or selected_train["SignalDate"].nunique() < max(2, train_days // 2):
                continue
            _, metrics = _build_strategy_curve(selected_train)
            score = metrics["avg_daily_return_pct"] * math.sqrt(max(metrics["signal_days"], 1))
            if score > best_score:
                best_score = score
                best_cfg = cfg
                best_metrics = metrics

        if best_cfg is None or best_metrics is None:
            continue

        selected_test = _apply_forward_config(test_df, best_cfg)
        _, test_metrics = _build_strategy_curve(selected_test)
        decisions.append(
            {
                "TestDate": test_date,
                "TrainWindow": f"{train_dates[0]}..{train_dates[-1]}",
                "TrainTrades": int(best_metrics["trade_count"]),
                "TrainSignalDays": int(best_metrics["signal_days"]),
                "TrainAvgDailyPct": best_metrics["avg_daily_return_pct"],
                "ChosenConfig": _describe_forward_config(best_cfg),
                "TestTrades": int(test_metrics["trade_count"]),
                "TestDailyPct": test_metrics["avg_daily_return_pct"],
            }
        )
        if not selected_test.empty:
            selected_test = selected_test.copy()
            selected_test["WFTrainWindow"] = f"{train_dates[0]}..{train_dates[-1]}"
            selected_test["WFConfig"] = _describe_forward_config(best_cfg)
            chosen_frames.append(selected_test)

    if not chosen_frames:
        return df.iloc[0:0].copy(), decisions
    return pd.concat(chosen_frames, ignore_index=True), decisions


def _pick_backtest_reference(df: pd.DataFrame) -> Dict[str, object]:
    best = df.sort_values(["avg_ret", "win_rate", "total"], ascending=[False, False, False]).iloc[0]
    return {
        "scenario": str(best["scenario"]),
        "target_pct": float(best["target_pct"]),
        "total_trades": int(best["total"]),
        "win_rate_pct": round(float(best["win_rate"]), 2),
        "avg_trade_return_pct": round(float(best["avg_ret"]), 2),
        "avg_hold_days": round(float(best["avg_hold"]), 2),
    }


def _write_report(
    output_path: Path,
    forward_source: Path,
    swing_backtest_source: Path,
    baseline_metrics: Dict[str, float],
    walk_metrics: Dict[str, float],
    benchmark_df: pd.DataFrame,
    backtest_ref: Dict[str, object],
    decisions_df: pd.DataFrame,
) -> None:
    best_benchmark = benchmark_df["ReturnPct"].dropna().max() if not benchmark_df.empty else None
    baseline_gap = None if best_benchmark is None else round(baseline_metrics["total_return_pct"] - float(best_benchmark), 2)
    walk_gap = None if best_benchmark is None else round(walk_metrics["total_return_pct"] - float(best_benchmark), 2)

    lines = [
        "# Strategy Benchmark Report",
        "",
        f"- Forward trades source: `{forward_source}`",
        f"- Swing backtest source: `{swing_backtest_source}`",
        "",
        "## Backtest Reference",
        "",
        f"- Scenario: `{backtest_ref['scenario']}` @ target `{backtest_ref['target_pct']:.1f}%`",
        f"- Backtest trades: `{backtest_ref['total_trades']}`",
        f"- Backtest win rate: `{backtest_ref['win_rate_pct']:.2f}%`",
        f"- Backtest average trade return: `{backtest_ref['avg_trade_return_pct']:+.2f}%`",
        f"- Backtest average hold: `{backtest_ref['avg_hold_days']:.2f}` days",
        "",
        "## Forward Results",
        "",
        f"- Baseline forward trades: `{baseline_metrics['trade_count']}` across `{baseline_metrics['signal_days']}` signal days",
        f"- Baseline win rate: `{baseline_metrics['win_rate_pct']:.2f}%`",
        f"- Baseline average trade return: `{baseline_metrics['avg_trade_return_pct']:+.2f}%`",
        f"- Baseline total compounded return: `{baseline_metrics['total_return_pct']:+.2f}%`",
        f"- Baseline max drawdown: `{baseline_metrics['max_drawdown_pct']:.2f}%`",
        "",
        f"- Walk-forward selected trades: `{walk_metrics['trade_count']}` across `{walk_metrics['signal_days']}` signal days",
        f"- Walk-forward win rate: `{walk_metrics['win_rate_pct']:.2f}%`",
        f"- Walk-forward average trade return: `{walk_metrics['avg_trade_return_pct']:+.2f}%`",
        f"- Walk-forward total compounded return: `{walk_metrics['total_return_pct']:+.2f}%`",
        f"- Walk-forward max drawdown: `{walk_metrics['max_drawdown_pct']:.2f}%`",
        "",
        "## Benchmarks",
        "",
    ]
    for _, row in benchmark_df.iterrows():
        val = row["ReturnPct"]
        val_text = "n/a" if pd.isna(val) else f"{float(val):+.2f}%"
        lines.append(f"- {row['Benchmark']}: `{val_text}`")
    lines.extend(
        [
            "",
            "## Reality Check",
            "",
            f"- Backtest avg trade vs baseline forward avg trade: `{baseline_metrics['avg_trade_return_pct'] - float(backtest_ref['avg_trade_return_pct']):+.2f} pct points`",
            f"- Baseline forward vs best benchmark: `{'n/a' if baseline_gap is None else f'{baseline_gap:+.2f}%'} `",
            f"- Walk-forward forward vs best benchmark: `{'n/a' if walk_gap is None else f'{walk_gap:+.2f}%'} `",
            "",
            "## Walk-Forward Decisions",
            "",
            f"- Decision count: `{len(decisions_df)}`",
        ]
    )
    if not decisions_df.empty:
        lines.append("")
        lines.append("| TestDate | TrainWindow | TrainTrades | TrainAvgDailyPct | TestTrades | TestDailyPct | ChosenConfig |")
        lines.append("| --- | --- | ---: | ---: | ---: | ---: | --- |")
        for _, row in decisions_df.head(20).iterrows():
            lines.append(
                f"| {row['TestDate']} | {row['TrainWindow']} | {row['TrainTrades']} | "
                f"{float(row['TrainAvgDailyPct']):+.2f}% | {row['TestTrades']} | {float(row['TestDailyPct']):+.2f}% | {row['ChosenConfig']} |"
            )
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare swing strategy vs benchmarks and walk-forward forward results")
    parser.add_argument("--forward-trades", default="", help="Forward trades CSV. Default: latest forward_bt_v2*_trades_*.csv")
    parser.add_argument("--swing-backtest-summary", default=str(RESULTS_DIR / "swing_backtest_summary_v2.csv"))
    parser.add_argument("--train-days", type=int, default=8, help="Prior signal days used for each walk-forward choice")
    parser.add_argument("--min-train-trades", type=int, default=4, help="Minimum trades in the training sample for a config")
    parser.add_argument("--output-prefix", default="strategy_benchmark_report", help="Prefix for markdown/csv outputs")
    args = parser.parse_args()

    forward_path = Path(args.forward_trades) if args.forward_trades else _find_latest_file("forward_bt_v2*_trades_*.csv")
    if forward_path is None:
        raise FileNotFoundError("No forward trade CSV found in results/Data")
    swing_path = Path(args.swing_backtest_summary)

    forward_df = _load_forward_trades(forward_path)
    swing_df = _load_swing_backtest_summary(swing_path)

    baseline_curve, baseline_metrics = _build_strategy_curve(forward_df)
    configs = _generate_filter_configs()
    wf_df, decisions = _walk_forward_forward_trades(
        forward_df,
        configs=configs,
        train_days=max(3, int(args.train_days)),
        min_train_trades=max(2, int(args.min_train_trades)),
    )
    wf_curve, wf_metrics = _build_strategy_curve(wf_df)

    start_date = str(forward_df["SignalDate"].min())
    end_date = str(forward_df["SignalDate"].max())
    benchmark_df = _summarize_benchmarks(start_date, end_date)
    backtest_ref = _pick_backtest_reference(swing_df)

    output_prefix = str(args.output_prefix).strip() or "strategy_benchmark_report"
    markdown_path = RESULTS_DIR / f"{output_prefix}.md"
    baseline_curve_path = RESULTS_DIR / f"{output_prefix}_baseline_curve.csv"
    wf_curve_path = RESULTS_DIR / f"{output_prefix}_walk_forward_curve.csv"
    decisions_path = RESULTS_DIR / f"{output_prefix}_walk_forward_decisions.csv"
    benchmarks_path = RESULTS_DIR / f"{output_prefix}_benchmarks.csv"

    _write_report(
        output_path=markdown_path,
        forward_source=forward_path,
        swing_backtest_source=swing_path,
        baseline_metrics=baseline_metrics,
        walk_metrics=wf_metrics,
        benchmark_df=benchmark_df,
        backtest_ref=backtest_ref,
        decisions_df=pd.DataFrame(decisions),
    )
    baseline_curve.to_csv(baseline_curve_path, index=False)
    wf_curve.to_csv(wf_curve_path, index=False)
    pd.DataFrame(decisions).to_csv(decisions_path, index=False)
    benchmark_df.to_csv(benchmarks_path, index=False)

    print("Strategy benchmark report ready")
    print(f"  Forward trades: {forward_path}")
    print(f"  Backtest summary: {swing_path}")
    print(f"  Report: {markdown_path}")
    print(f"  Baseline curve: {baseline_curve_path}")
    print(f"  Walk-forward curve: {wf_curve_path}")
    print(f"  Walk-forward decisions: {decisions_path}")
    print(f"  Benchmarks: {benchmarks_path}")
    print("")
    print("Baseline forward:")
    print(f"  trades={baseline_metrics['trade_count']} days={baseline_metrics['signal_days']} "
          f"win_rate={baseline_metrics['win_rate_pct']:.1f}% total={baseline_metrics['total_return_pct']:+.2f}% "
          f"max_dd={baseline_metrics['max_drawdown_pct']:.2f}%")
    print("Walk-forward forward:")
    print(f"  trades={wf_metrics['trade_count']} days={wf_metrics['signal_days']} "
          f"win_rate={wf_metrics['win_rate_pct']:.1f}% total={wf_metrics['total_return_pct']:+.2f}% "
          f"max_dd={wf_metrics['max_drawdown_pct']:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
