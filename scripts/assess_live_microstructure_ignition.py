#!/usr/bin/env python3
"""Audit causal long ignition states from consecutive live snapshots."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.assess_live_runner_fade import (
    CONTRACTS,
    load_candle_groups,
    load_feature_tape,
    summarize,
)
from scripts.evaluate_live_feature_outcomes import as_bool
from scripts.evaluate_live_microstructure_tape import minute_candle_outcome


@dataclass(frozen=True)
class IgnitionRule:
    name: str
    min_acceleration_pct: float
    min_bs_ratio: float
    min_bs_surge: float = 0.0
    surge_lookback: int = 1
    require_stable_depth: bool = False


RULES = (
    IgnitionRule("accel_depth", 0.25, 1.5, min_bs_surge=1.10),
    IgnitionRule("accel_stable", 0.20, 1.5, require_stable_depth=True),
    IgnitionRule("depth_surge", 0.10, 2.0, min_bs_surge=1.50, surge_lookback=2),
    IgnitionRule("pure_accel", 0.35, 1.2),
)


def build_ignition_candidates(tape: pd.DataFrame, max_per_day: int = 3) -> pd.DataFrame:
    data = tape.copy().sort_values(["Date", "Symbol", "When"])
    if data.empty:
        return pd.DataFrame()
    data["OrderBookStable"] = data.get(
        "OrderBookStable", pd.Series(False, index=data.index)
    ).apply(as_bool)
    grouped = data.groupby(["Date", "Symbol"], sort=False)
    data["DayChangeTwoSnapshotsAgo"] = grouped["DayChangePct"].shift(2)
    data["BsOneSnapshotAgo"] = grouped["BuyerSellerRatio"].shift(1)
    data["BsTwoSnapshotsAgo"] = grouped["BuyerSellerRatio"].shift(2)
    data["SecondsAcrossSnapshots"] = (
        data["When"] - grouped["When"].shift(2)
    ).dt.total_seconds()
    data["AccelerationPct"] = data["DayChangePct"] - data["DayChangeTwoSnapshotsAgo"]

    base = (
        data["SecondsAcrossSnapshots"].between(60.0, 240.0)
        & data["SpreadOk"]
        & data["SpreadBps"].between(0.01, 20.0)
        & data["LastPrice"].ge(50.0)
        & data["TradedValue"].ge(50_000_000.0)
        & data["DayChangePct"].between(1.0, 8.0)
        & data["PullbackFromHighPct"].le(0.35)
    )
    parts = []
    for rule in RULES:
        previous_bs = (
            data["BsTwoSnapshotsAgo"]
            if rule.surge_lookback == 2
            else data["BsOneSnapshotAgo"]
        )
        selected = base & data["AccelerationPct"].ge(rule.min_acceleration_pct)
        selected &= data["BuyerSellerRatio"].ge(rule.min_bs_ratio)
        if rule.min_bs_surge > 0:
            selected &= previous_bs.gt(0.0)
            selected &= data["BuyerSellerRatio"].ge(previous_bs * rule.min_bs_surge)
        if rule.require_stable_depth:
            selected &= data["OrderBookStable"]
        rows = data[selected].copy()
        if rows.empty:
            continue
        rows = rows.drop_duplicates(["Date", "Symbol"], keep="first")
        rows["Rule"] = rule.name
        rows["IgnitionScore"] = (
            rows["AccelerationPct"].clip(upper=2.0) * 100.0
            + rows["BuyerSellerRatio"].clip(upper=10.0) * 5.0
            + rows["DayChangePct"].clip(upper=8.0) * 2.0
            - rows["PullbackFromHighPct"] * 10.0
        )
        parts.append(rows)
    if not parts:
        return pd.DataFrame()

    candidates = pd.concat(parts, ignore_index=True)
    candidates["Selected"] = False
    candidates["SignalMinute"] = candidates["When"].dt.floor("min")
    for (_, _), group in candidates.groupby(["Date", "Rule"], sort=True):
        slots = int(max_per_day)
        for _, batch in group.groupby("SignalMinute", sort=True):
            if slots <= 0:
                break
            chosen = batch.sort_values(
                ["IgnitionScore", "Symbol"], ascending=[False, True]
            ).head(slots)
            candidates.loc[chosen.index, "Selected"] = True
            slots -= len(chosen)
    return candidates.sort_values(["Date", "When", "Rule", "Symbol"]).reset_index(drop=True)


def label_candidates(
    candidates: pd.DataFrame,
    candle_groups: dict,
    *,
    horizon_minutes: int = 60,
    fee_pct: float = 0.10,
    slippage_bps: float = 5.0,
) -> pd.DataFrame:
    rows = []
    for candidate in candidates[candidates["Selected"]].to_dict(orient="records"):
        candles = candle_groups.get((str(candidate["Date"]), str(candidate["Symbol"])))
        for contract in CONTRACTS:
            outcome = minute_candle_outcome(
                candles,
                pd.Timestamp(candidate["When"]),
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default="results/Data/live_feature_recorder/live_features_20*.csv")
    parser.add_argument("--minute-cache-glob", default="results/Data/kite_history_backfill/minute/*")
    parser.add_argument("--output-dir", default="results/Data/live_microstructure_ignition")
    parser.add_argument("--max-per-day", type=int, default=3)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    args = parser.parse_args()

    tape = load_feature_tape(
        args.input,
        start_date=str(args.start_date or ""),
        end_date=str(args.end_date or ""),
    )
    candidates = build_ignition_candidates(tape, max_per_day=max(1, args.max_per_day))
    groups = load_candle_groups(
        args.minute_cache_glob,
        candidates["Symbol"].astype(str).unique().tolist() if not candidates.empty else [],
    )
    outcomes = label_candidates(candidates, groups)
    readiness = summarize(outcomes, tape["Date"].unique().tolist()) if not outcomes.empty else pd.DataFrame()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(output / "candidates.csv", index=False)
    outcomes.to_csv(output / "outcomes.csv", index=False)
    readiness.to_csv(output / "readiness.csv", index=False)
    status = {
        "evidence_type": "live_only_temporal_microstructure_ignition_research",
        "sessions": int(tape["Date"].nunique()),
        "candidate_rows": int(len(candidates)),
        "selected_candidates": int(candidates["Selected"].sum()) if not candidates.empty else 0,
        "shadow_candidates": int(readiness["ShadowCandidate"].sum()) if not readiness.empty else 0,
        "goal_evidence_passes": int(readiness["GoalEvidencePassed"].sum()) if not readiness.empty else 0,
        "paper_promotion_allowed": bool(readiness["PaperPromotionAllowed"].any()) if not readiness.empty else False,
        "real_orders_allowed": False,
        "rules": [asdict(rule) for rule in RULES],
    }
    (output / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    if not readiness.empty:
        print(readiness.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
