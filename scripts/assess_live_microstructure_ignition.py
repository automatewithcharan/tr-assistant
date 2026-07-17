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
SELECTION_END = "2026-07-16"
FORWARD_START = "2026-07-17"


def build_ignition_candidates(tape: pd.DataFrame, max_per_day: int = 1) -> pd.DataFrame:
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

    minute = data["When"].dt.hour * 60 + data["When"].dt.minute
    base = (
        data["SecondsAcrossSnapshots"].between(60.0, 240.0)
        & minute.between(9 * 60 + 18, 14 * 60 + 30)
        & data["SpreadOk"]
        & data["BestBid"].gt(0.0)
        & data["BestAsk"].gt(data["BestBid"])
        & data["SpreadBps"].between(0.01, 20.0)
        & data["LastPrice"].ge(50.0)
        & data["TradedValue"].ge(30_000_000.0)
        & data["DayChangePct"].between(0.5, 3.0)
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
    parser.add_argument("--max-per-day", type=int, default=1)
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
    all_dates = sorted(tape["Date"].astype(str).unique().tolist())
    selection_dates = [date for date in all_dates if date <= SELECTION_END]
    forward_dates = [date for date in all_dates if date >= FORWARD_START]
    selection_outcomes = outcomes[outcomes["Date"].astype(str) <= SELECTION_END].copy() if not outcomes.empty else pd.DataFrame()
    readiness = (
        summarize(selection_outcomes, selection_dates)
        if not selection_outcomes.empty
        else pd.DataFrame()
    )
    frozen_configs = set()
    if not readiness.empty:
        frozen_rows = readiness[readiness["ShadowCandidate"].astype(bool)]
        frozen_configs = set(zip(frozen_rows["Rule"], frozen_rows["Contract"]))
    forward_outcomes = pd.DataFrame()
    if frozen_configs and not outcomes.empty:
        config_keys = list(zip(outcomes["Rule"], outcomes["Contract"]))
        forward_mask = outcomes["Date"].astype(str).ge(FORWARD_START) & pd.Series(
            [key in frozen_configs for key in config_keys], index=outcomes.index
        )
        forward_outcomes = outcomes[forward_mask].copy()

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(output / "candidates.csv", index=False)
    outcomes.to_csv(output / "outcomes.csv", index=False)
    readiness.to_csv(output / "readiness.csv", index=False)
    forward_outcomes.to_csv(output / "forward_outcomes.csv", index=False)
    status = {
        "evidence_type": "live_only_temporal_microstructure_ignition_research",
        "selection_end": SELECTION_END,
        "forward_start": FORWARD_START,
        "selection_sessions": len(selection_dates),
        "forward_sessions": len(forward_dates),
        "candidate_rows": int(len(candidates)),
        "selected_candidates": int(candidates["Selected"].sum()) if not candidates.empty else 0,
        "frozen_configs": [f"{rule}|{contract}" for rule, contract in sorted(frozen_configs)],
        "selection_rejected": not bool(frozen_configs),
        "shadow_candidates": int(readiness["ShadowCandidate"].sum()) if not readiness.empty else 0,
        "goal_evidence_passes": int(readiness["GoalEvidencePassed"].sum()) if not readiness.empty else 0,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "rules": [asdict(rule) for rule in RULES],
    }
    (output / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    if not readiness.empty:
        print(readiness.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
