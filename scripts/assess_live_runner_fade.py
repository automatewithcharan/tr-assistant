#!/usr/bin/env python3
"""Audit paper-only short fades after live top-gainer failure confirmation."""

from __future__ import annotations

import argparse
import glob
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_live_feature_outcomes import as_bool
from scripts.evaluate_live_microstructure_tape import (
    load_minute_candles,
    minute_candle_short_outcome,
)


@dataclass(frozen=True)
class FadeRule:
    name: str
    min_pullback_pct: float
    max_bs_ratio: float
    max_median_bs_ratio: float = 999.0
    max_sector_breadth_pct: float = 100.0


@dataclass(frozen=True)
class ExitContract:
    target_pct: float
    stop_pct: float

    @property
    def name(self) -> str:
        return f"t{self.target_pct:.1f}_s{self.stop_pct:.1f}"


RULES = (
    FadeRule("failure_pb1_bs1", 1.0, 1.0),
    FadeRule("failure_pb1_5_bs1", 1.5, 1.0),
    FadeRule("weak_depth_failure", 1.0, 0.8, max_median_bs_ratio=1.0),
    FadeRule("isolated_failure", 1.0, 1.0, max_sector_breadth_pct=50.0),
)
CONTRACTS = (
    ExitContract(0.5, 0.5),
    ExitContract(0.7, 0.5),
    ExitContract(1.0, 0.5),
    ExitContract(1.0, 1.0),
)
MARKET_OPEN_MINUTE = 9 * 60 + 15
LATEST_SIGNAL_MINUTE = 14 * 60 + 15
EXTRA_STRESS_COST_PCT = 0.10


def load_feature_tape(
    pattern: str,
    *,
    start_date: str = "",
    end_date: str = "",
) -> pd.DataFrame:
    parts = []
    for path in sorted(glob.glob(pattern)):
        try:
            frame = pd.read_csv(path)
        except (OSError, pd.errors.EmptyDataError):
            continue
        if not frame.empty:
            parts.append(frame)
    if not parts:
        raise FileNotFoundError(f"No live feature files matched: {pattern}")
    data = pd.concat(parts, ignore_index=True)
    data["When"] = pd.to_datetime(data["When"], errors="coerce")
    data["Date"] = data["When"].dt.date.astype(str)
    data["Symbol"] = data["Symbol"].astype(str).str.strip().str.upper()
    for column in (
        "LastPrice", "DayChangePct", "PullbackFromHighPct", "BuyerSellerRatio",
        "BuyerSellerMedianWindow", "BuyerSellerMinWindow", "BestBid", "BestAsk",
        "SpreadBps", "SectorBreadthPct", "TradedValue",
    ):
        values = data[column] if column in data.columns else pd.Series(0.0, index=data.index)
        data[column] = pd.to_numeric(values, errors="coerce").fillna(0.0)
    for column in ("MissedRunnerCandidate", "OrderBookStable", "SpreadOk"):
        values = data[column] if column in data.columns else pd.Series(False, index=data.index)
        data[column] = values.apply(as_bool)
    minute = data["When"].dt.hour * 60 + data["When"].dt.minute
    filtered = data[
        data["When"].notna()
        & minute.between(MARKET_OPEN_MINUTE, LATEST_SIGNAL_MINUTE)
    ]
    if start_date:
        filtered = filtered[filtered["Date"] >= start_date]
    if end_date:
        filtered = filtered[filtered["Date"] <= end_date]
    return filtered.sort_values(["Date", "When", "Symbol"]).reset_index(drop=True)


def build_fade_candidates(tape: pd.DataFrame, max_per_day: int = 3) -> pd.DataFrame:
    missed = tape[tape["MissedRunnerCandidate"]].copy()
    if missed.empty:
        return pd.DataFrame()
    watch_time = missed.groupby(["Date", "Symbol"])["When"].transform("min")
    missed["WatchTime"] = watch_time
    missed["AgeSeconds"] = (missed["When"] - missed["WatchTime"]).dt.total_seconds()
    parts = []
    base = (
        (missed["AgeSeconds"] >= 60.0)
        & missed["SpreadOk"]
        & missed["SpreadBps"].between(0.01, 20.0)
        & missed["LastPrice"].ge(50.0)
        & missed["TradedValue"].ge(50_000_000.0)
        & missed["DayChangePct"].between(5.0, 20.0)
    )
    for rule in RULES:
        selected = missed[
            base
            & missed["PullbackFromHighPct"].ge(rule.min_pullback_pct)
            & missed["BuyerSellerRatio"].le(rule.max_bs_ratio)
            & missed["BuyerSellerMedianWindow"].le(rule.max_median_bs_ratio)
            & missed["SectorBreadthPct"].le(rule.max_sector_breadth_pct)
        ].copy()
        if selected.empty:
            continue
        selected = selected.drop_duplicates(["Date", "Symbol"], keep="first")
        selected["Rule"] = rule.name
        selected["FailureScore"] = (
            selected["PullbackFromHighPct"].clip(upper=5.0) * 20.0
            + (1.0 - selected["BuyerSellerRatio"].clip(upper=1.0)) * 20.0
            + selected["DayChangePct"].clip(upper=15.0) * 2.0
            + (50.0 - selected["SectorBreadthPct"].clip(upper=50.0)) * 0.2
        )
        parts.append(selected)
    if not parts:
        return pd.DataFrame()
    candidates = pd.concat(parts, ignore_index=True)
    candidates["Selected"] = False
    candidates["SignalMinute"] = candidates["When"].dt.floor("min")
    for (date, rule), group in candidates.groupby(["Date", "Rule"], sort=True):
        slots = int(max_per_day)
        for _, batch in group.groupby("SignalMinute", sort=True):
            if slots <= 0:
                break
            chosen = batch.sort_values(
                ["FailureScore", "Symbol"], ascending=[False, True]
            ).head(slots)
            candidates.loc[chosen.index, "Selected"] = True
            slots -= len(chosen)
    return candidates.sort_values(["Date", "When", "Rule", "Symbol"]).reset_index(drop=True)


def load_candle_groups(cache_glob: str, symbols: list[str]) -> dict:
    groups = {}
    for directory in sorted(glob.glob(cache_glob)):
        if Path(directory).is_dir():
            groups.update(load_minute_candles(directory, symbols))
    return groups


def label_candidates(
    candidates: pd.DataFrame,
    candle_groups: dict,
    *,
    horizon_minutes: int = 60,
    fee_pct: float = 0.10,
    slippage_bps: float = 5.0,
) -> pd.DataFrame:
    rows = []
    selected = candidates[candidates["Selected"]].copy()
    for candidate in selected.to_dict(orient="records"):
        candles = candle_groups.get((str(candidate["Date"]), str(candidate["Symbol"])))
        signal_when = pd.Timestamp(candidate["When"])
        for contract in CONTRACTS:
            outcome = minute_candle_short_outcome(
                candles,
                signal_when,
                target_pct=contract.target_pct,
                stop_pct=contract.stop_pct,
                horizon_minutes=horizon_minutes,
                fee_pct=fee_pct,
                slippage_bps=slippage_bps,
            )
            rows.append(
                {
                    **candidate,
                    "Contract": contract.name,
                    "TargetPct": contract.target_pct,
                    "StopPct": contract.stop_pct,
                    "HorizonMinutes": horizon_minutes,
                    "FeePct": fee_pct,
                    "SlippageBps": slippage_bps,
                    **outcome,
                }
            )
    return pd.DataFrame(rows)


def _lower95(values: pd.Series) -> float:
    values = pd.to_numeric(values, errors="coerce").dropna()
    if len(values) <= 1:
        return float(values.mean()) if len(values) else 0.0
    return float(values.mean() - 1.645 * values.std(ddof=1) / math.sqrt(len(values)))


def _segment(daily: pd.DataFrame, dates: list[str]) -> dict:
    rows = daily[daily["Date"].isin(dates)]
    return {
        "days": len(rows),
        "avg": float(rows["StressDailyPnl"].mean()) if len(rows) else 0.0,
        "lower": _lower95(rows["StressDailyPnl"]),
        "positive_pct": float((rows["StressDailyPnl"] > 0).mean() * 100.0) if len(rows) else 0.0,
        "worst": float(rows["StressDailyPnl"].min()) if len(rows) else 0.0,
    }


def summarize(
    outcomes: pd.DataFrame,
    session_dates: list[str],
    *,
    capital: float = 400_000.0,
    ticket: float = 50_000.0,
    daily_goal: float = 10_000.0,
) -> pd.DataFrame:
    rows = []
    dates = sorted({str(value) for value in session_dates})
    if len(dates) >= 7:
        final_dates = dates[-2:]
        validation_dates = dates[-4:-2]
        train_dates = dates[:-4]
    else:
        final_dates = dates[-1:]
        validation_dates = dates[-2:-1]
        train_dates = dates[:-2]
    for (rule, contract), group in outcomes.groupby(["Rule", "Contract"], sort=True):
        available = group[group["OutcomeAvailable"].apply(as_bool)].copy()
        available["StressReturnPct"] = (
            pd.to_numeric(available["NetReturnPct"], errors="coerce")
            - EXTRA_STRESS_COST_PCT
        )
        pnl = available.groupby("Date")["StressReturnPct"].sum() * ticket / 100.0
        daily = pd.DataFrame({"Date": dates})
        daily["StressDailyPnl"] = daily["Date"].map(pnl).fillna(0.0)
        train = _segment(daily, train_dates)
        validation = _segment(daily, validation_dates)
        final = _segment(daily, final_dates)
        overall = _segment(daily, dates)
        sample_gate = len(dates) >= 20 and len(available) >= 30
        performance_gate = (
            train["avg"] > 0
            and validation["avg"] > 0
            and final["avg"] > 0
            and overall["lower"] > 0
            and overall["positive_pct"] >= 55.0
        )
        shadow = bool(sample_gate and performance_gate)
        goal_pass = bool(
            shadow
            and overall["avg"] >= daily_goal
            and float((daily["StressDailyPnl"] >= daily_goal).mean() * 100.0) >= 50.0
        )
        rows.append(
            {
                "Rule": rule,
                "Contract": contract,
                "Sessions": len(dates),
                "Candidates": int(len(group)),
                "Evaluated": int(len(available)),
                "CoveragePct": round(len(available) / len(group) * 100.0, 2) if len(group) else 0.0,
                "Wins": int((available["StressReturnPct"] > 0).sum()),
                "WinRatePct": round(float((available["StressReturnPct"] > 0).mean() * 100.0), 2) if len(available) else 0.0,
                "TrainAvgDailyPnl": round(train["avg"], 2),
                "ValidationAvgDailyPnl": round(validation["avg"], 2),
                "FinalAvgDailyPnl": round(final["avg"], 2),
                "StressAvgDailyPnl": round(overall["avg"], 2),
                "StressLower95DailyPnl": round(overall["lower"], 2),
                "PositiveSessionPct": round(overall["positive_pct"], 2),
                "WorstSessionPnl": round(overall["worst"], 2),
                "Capital": capital,
                "Ticket": ticket,
                "DailyGoal": daily_goal,
                "SampleGatePassed": sample_gate,
                "PerformanceGatePassed": performance_gate,
                "ShadowCandidate": shadow,
                "GoalEvidencePassed": goal_pass,
                # Research summaries never promote themselves. Promotion must
                # come from a separately frozen untouched-forward registry.
                "PaperPromotionAllowed": False,
                "RealOrdersAllowed": False,
            }
        )
    result = pd.DataFrame(rows)
    if not result.empty:
        result.sort_values(
            ["ShadowCandidate", "StressLower95DailyPnl", "StressAvgDailyPnl"],
            ascending=[False, False, False],
            inplace=True,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="results/Data/live_feature_recorder/live_features_20*.csv")
    parser.add_argument("--minute-cache-glob", default="results/Data/kite_history_backfill/minute/*")
    parser.add_argument("--output-dir", default="results/Data/live_runner_fade")
    parser.add_argument("--max-per-day", type=int, default=3)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    args = parser.parse_args()
    tape = load_feature_tape(
        args.input,
        start_date=str(args.start_date or ""),
        end_date=str(args.end_date or ""),
    )
    candidates = build_fade_candidates(tape, max_per_day=max(1, int(args.max_per_day)))
    groups = load_candle_groups(
        args.minute_cache_glob,
        candidates["Symbol"].astype(str).unique().tolist() if not candidates.empty else [],
    )
    outcomes = label_candidates(candidates, groups)
    summary = summarize(outcomes, tape["Date"].unique().tolist()) if not outcomes.empty else pd.DataFrame()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(output / "candidates.csv", index=False)
    outcomes.to_csv(output / "outcomes.csv", index=False)
    summary.to_csv(output / "readiness.csv", index=False)
    status = {
        "evidence_type": "live_only_runner_failure_short_research",
        "sessions": int(tape["Date"].nunique()),
        "candidate_rows": int(len(candidates)),
        "selected_candidates": int(candidates["Selected"].sum()) if not candidates.empty else 0,
        "shadow_candidates": int(summary["ShadowCandidate"].sum()) if not summary.empty else 0,
        "goal_evidence_passes": int(summary["GoalEvidencePassed"].sum()) if not summary.empty else 0,
        "paper_promotion_allowed": bool(summary["PaperPromotionAllowed"].any()) if not summary.empty else False,
        "real_orders_allowed": False,
        "rules": [asdict(rule) for rule in RULES],
        "contracts": [asdict(contract) for contract in CONTRACTS],
    }
    (output / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    if not summary.empty:
        print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
