#!/usr/bin/env python3
"""Build a causal, paper-only continuation tape from live leader snapshots."""

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

from scripts.assess_live_runner_fade import load_feature_tape
from scripts.evaluate_live_microstructure_tape import executable_outcome


@dataclass(frozen=True)
class Contract:
    target_pct: float
    stop_pct: float

    @property
    def name(self) -> str:
        return f"t{self.target_pct:.1f}_s{self.stop_pct:.1f}"


CONTRACTS = (
    Contract(0.5, 0.5),
    Contract(0.7, 0.5),
    Contract(1.0, 1.0),
    Contract(1.5, 1.0),
)
MOVE_BINS = (0.5, 1.0, 2.0, 3.0, 5.000001)
MOVE_LABELS = ("0.5-1", "1-2", "2-3", "3-5")
EARLIEST_SIGNAL_MINUTE = 9 * 60 + 18
LATEST_SIGNAL_MINUTE = 14 * 60 + 15
EXPLORATORY_RULE_DEFINED_ON = "2026-07-18"
FROZEN_FORWARD_START = "2026-07-20"


def load_reconstructed_bars(
    pattern: str,
    symbols: set[str] | None = None,
) -> tuple[dict[tuple[str, str], pd.DataFrame], list[str]]:
    """Load combined reconstructed one-minute files by date and symbol."""
    groups: dict[tuple[str, str], pd.DataFrame] = {}
    dates: set[str] = set()
    wanted = {str(symbol).strip().upper() for symbol in symbols or set()}
    for file in sorted(glob.glob(pattern)):
        try:
            frame = pd.read_csv(file)
        except (OSError, pd.errors.EmptyDataError):
            continue
        required = {"symbol", "datetime", "open", "high", "low", "close"}
        if not required.issubset(frame.columns):
            continue
        frame["symbol"] = frame["symbol"].astype(str).str.strip().str.upper()
        if wanted:
            frame = frame[frame["symbol"].isin(wanted)].copy()
        frame["datetime"] = pd.to_datetime(frame["datetime"], errors="coerce")
        for column in ("open", "high", "low", "close"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(
            subset=["symbol", "datetime", "open", "high", "low", "close"]
        )
        frame["Date"] = frame["datetime"].dt.date.astype(str)
        for (date, symbol), day in frame.groupby(["Date", "symbol"], sort=False):
            key = (str(date), str(symbol))
            current = groups.get(key)
            merged = day if current is None else pd.concat([current, day], ignore_index=True)
            groups[key] = (
                merged.drop_duplicates("datetime", keep="last")
                .sort_values("datetime")
                .reset_index(drop=True)
            )
            dates.add(str(date))
    return groups, sorted(dates)


def build_candidates(tape: pd.DataFrame) -> pd.DataFrame:
    """Freeze the first executable state in each daily movement band."""
    if tape is None or tape.empty:
        return pd.DataFrame()
    data = tape.copy().sort_values(["Date", "Symbol", "When"])
    for column in (
        "OpenChangePct",
        "BuyerSellerRatio",
        "PullbackFromHighPct",
        "LastPrice",
        "TradedValue",
        "BestBid",
        "BestAsk",
        "SpreadBps",
        "SectorAvgChangePct",
        "SectorBreadthPct",
        "MarketBreadthPct",
    ):
        values = data[column] if column in data.columns else pd.Series(np.nan, index=data.index)
        data[column] = pd.to_numeric(values, errors="coerce")

    grouped = data.groupby(["Date", "Symbol"], sort=False)
    prior_when = grouped["When"].shift(2)
    prior_move = grouped["OpenChangePct"].shift(2)
    prior_bs = grouped["BuyerSellerRatio"].shift(2)
    elapsed_minutes = (data["When"] - prior_when).dt.total_seconds() / 60.0
    data["SnapshotSpanMinutes"] = elapsed_minutes
    data["MoveAccelerationPct"] = data["OpenChangePct"] - prior_move
    data["MoveVelocityPctPerMinute"] = data["MoveAccelerationPct"] / elapsed_minutes
    data["BuyerSellerSurge"] = np.where(
        prior_bs > 0.0,
        data["BuyerSellerRatio"] / prior_bs,
        np.nan,
    )
    data["SectorRelativePct"] = data["OpenChangePct"] - data["SectorAvgChangePct"]
    data["MoveBand"] = pd.cut(
        data["OpenChangePct"],
        bins=MOVE_BINS,
        labels=MOVE_LABELS,
        right=False,
        include_lowest=True,
    )
    minute = data["When"].dt.hour * 60 + data["When"].dt.minute
    eligible = (
        minute.between(EARLIEST_SIGNAL_MINUTE, LATEST_SIGNAL_MINUTE)
        & data["MoveBand"].notna()
        & data["SnapshotSpanMinutes"].between(1.0, 5.0)
        & data["LastPrice"].ge(50.0)
        & data["TradedValue"].ge(30_000_000.0)
        & data["BestBid"].gt(0.0)
        & data["BestAsk"].gt(data["BestBid"])
        & data["SpreadBps"].between(0.01, 20.0)
    )
    candidates = data[eligible].copy()
    if candidates.empty:
        return candidates
    candidates.sort_values(["Date", "Symbol", "When"], inplace=True)
    candidates = candidates.drop_duplicates(
        ["Date", "Symbol", "MoveBand"], keep="first"
    ).reset_index(drop=True)
    candidates["ExtensionAccelerationCohort"] = (
        candidates["MoveBand"].astype(str).eq("3-5")
        & candidates["MoveAccelerationPct"].ge(0.2)
        & candidates["BuyerSellerSurge"].ge(1.0)
    )
    candidates["UsesFutureRunnerLabel"] = False
    candidates["PaperPromotionAllowed"] = False
    candidates["RealOrdersAllowed"] = False
    return candidates


def five_minute_bar_outcome(
    candles: pd.DataFrame | None,
    signal_when: pd.Timestamp,
    target_pct: float,
    stop_pct: float,
    horizon_minutes: int,
    fee_pct: float,
    slippage_bps: float,
) -> dict:
    """Enter at the next untouched five-minute open with conservative exits."""
    if candles is None or candles.empty:
        return {"OutcomeAvailable": False}
    signal_when = pd.Timestamp(signal_when)
    signal_floor = signal_when.floor("min")
    cutoff = signal_when + pd.Timedelta(minutes=int(horizon_minutes))
    future = candles[
        (candles["datetime"] > signal_floor) & (candles["datetime"] <= cutoff)
    ].copy()
    if future.empty:
        return {"OutcomeAvailable": False}
    entry_when = pd.Timestamp(future.iloc[0]["datetime"])
    if entry_when - signal_floor > pd.Timedelta(minutes=5):
        return {"OutcomeAvailable": False}
    required_last = cutoff.floor("5min")
    if pd.Timestamp(future["datetime"].max()) < required_last:
        return {"OutcomeAvailable": False}

    slip = float(slippage_bps) / 10_000.0
    entry = float(future.iloc[0]["open"]) * (1.0 + slip)
    if entry <= 0:
        return {"OutcomeAvailable": False}
    target = entry * (1.0 + float(target_pct) / 100.0)
    stop = entry * (1.0 - float(stop_pct) / 100.0)
    reason = "horizon"
    exit_price = float(future.iloc[-1]["close"]) * (1.0 - slip)
    exit_when = pd.Timestamp(future.iloc[-1]["datetime"])
    for bar in future.itertuples(index=False):
        open_price = float(bar.open)
        high = float(bar.high)
        low = float(bar.low)
        when = pd.Timestamp(bar.datetime)
        if open_price <= stop:
            reason = "stop_gap"
            exit_price = open_price * (1.0 - slip)
            exit_when = when
            break
        if open_price >= target:
            reason = "target_gap"
            exit_price = target
            exit_when = when
            break
        stop_hit = low <= stop
        target_hit = high >= target
        if stop_hit:
            reason = "stop_first_same_bar" if target_hit else "stop"
            exit_price = stop * (1.0 - slip)
            exit_when = when
            break
        if target_hit:
            reason = "target"
            exit_price = target
            exit_when = when
            break
    gross = (exit_price - entry) / entry * 100.0
    return {
        "OutcomeAvailable": True,
        "OutcomeSource": "reconstructed_5minute_next_untouched_open",
        "EntryAsk": round(entry, 4),
        "EntryWhen": entry_when,
        "ExitPrice": round(exit_price, 4),
        "ExitWhen": exit_when,
        "ExitReason": reason,
        "GrossReturnPct": round(gross, 6),
        "NetReturnPct": round(gross - float(fee_pct), 6),
        "MfePct": round(
            (float(future["high"].max()) - entry) / entry * 100.0, 6
        ),
        "MaePct": round(
            (float(future["low"].min()) - entry) / entry * 100.0, 6
        ),
        "FutureSnapshots": int(len(future)),
    }


def label_candidates(
    candidates: pd.DataFrame,
    candle_groups: dict[tuple[str, str], pd.DataFrame],
    *,
    horizon_minutes: int = 60,
    fee_pct: float = 0.10,
    slippage_bps: float = 5.0,
) -> pd.DataFrame:
    rows = []
    for candidate in candidates.to_dict(orient="records"):
        signal_when = pd.Timestamp(candidate["When"])
        candles = candle_groups.get(
            (str(candidate["Date"]), str(candidate["Symbol"]))
        )
        for contract in CONTRACTS:
            outcome = five_minute_bar_outcome(
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


def label_candidates_from_snapshots(
    candidates: pd.DataFrame,
    tape: pd.DataFrame,
    *,
    horizon_minutes: int = 60,
    fee_pct: float = 0.10,
) -> pd.DataFrame:
    """Evaluate observed ask entries against later observed executable bids."""
    if candidates is None or candidates.empty or "RowId" not in candidates.columns:
        return pd.DataFrame()
    groups = {
        (str(date), str(symbol)): group.copy()
        for (date, symbol), group in tape.groupby(["Date", "Symbol"], sort=False)
    }
    rows = []
    for candidate in candidates.to_dict(orient="records"):
        group = groups.get((str(candidate["Date"]), str(candidate["Symbol"])))
        for contract in CONTRACTS:
            outcome = executable_outcome(
                group,
                int(candidate["RowId"]),
                target_pct=contract.target_pct,
                stop_pct=contract.stop_pct,
                horizon_minutes=horizon_minutes,
                fee_pct=fee_pct,
            ) if group is not None else {"OutcomeAvailable": False}
            rows.append(
                {
                    **candidate,
                    "Contract": contract.name,
                    "TargetPct": contract.target_pct,
                    "StopPct": contract.stop_pct,
                    "HorizonMinutes": horizon_minutes,
                    "FeePct": fee_pct,
                    "SlippageBps": 0.0,
                    "OutcomeSource": "live_snapshot_observed_ask_to_bid",
                    **outcome,
                }
            )
    return pd.DataFrame(rows)


def _lower95(values: pd.Series) -> float:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    if len(numeric) <= 1:
        return float(numeric.mean()) if len(numeric) else 0.0
    return float(numeric.mean() - 1.645 * numeric.std(ddof=1) / math.sqrt(len(numeric)))


def summarize(
    outcomes: pd.DataFrame,
    session_dates: list[str],
    forward_start: str = FROZEN_FORWARD_START,
) -> pd.DataFrame:
    if outcomes is None or outcomes.empty:
        return pd.DataFrame()
    all_dates = sorted({str(date) for date in session_dates})
    rows = []
    phases = (
        ("retrospective", [date for date in all_dates if date < forward_start]),
        ("frozen_forward", [date for date in all_dates if date >= forward_start]),
    )
    for phase, dates in phases:
        if not dates:
            continue
        phase_outcomes = outcomes[outcomes["Date"].astype(str).isin(dates)].copy()
        cohort_groups = [
            ("move_band", str(band), str(contract), group)
            for (band, contract), group in phase_outcomes.groupby(
                ["MoveBand", "Contract"], observed=True
            )
        ]
        extension = phase_outcomes[
            phase_outcomes.get(
                "ExtensionAccelerationCohort",
                pd.Series(False, index=phase_outcomes.index),
            ).fillna(False).astype(bool)
        ]
        cohort_groups.extend(
            ("extension_acceleration", "3-5", str(contract), group)
            for contract, group in extension.groupby("Contract", sort=True)
        )
        for cohort, band, contract, group in cohort_groups:
            available = group[
                group["OutcomeAvailable"].fillna(False).astype(bool)
            ].copy()
            daily_values = (
                pd.to_numeric(available["NetReturnPct"], errors="coerce")
                .groupby(available["Date"].astype(str))
                .mean()
            )
            daily = pd.Series(0.0, index=dates, dtype=float)
            overlap = daily.index.intersection(daily_values.index)
            daily.loc[overlap] = daily_values.loc[overlap]
            target_hits = available["ExitReason"].astype(str).str.startswith("target")
            rows.append(
                {
                    "EvidencePhase": phase,
                    "Cohort": cohort,
                    "MoveBand": str(band),
                    "Contract": str(contract),
                    "Sessions": len(dates),
                    "Candidates": int(len(group)),
                    "Evaluated": int(len(available)),
                    "CoveragePct": round(len(available) / len(group) * 100.0, 2)
                    if len(group)
                    else 0.0,
                    "TargetHitPct": round(float(target_hits.mean() * 100.0), 2)
                    if len(available)
                    else 0.0,
                    "AvgNetReturnPct": round(
                        float(
                            pd.to_numeric(
                                available["NetReturnPct"], errors="coerce"
                            ).mean()
                        ),
                        4,
                    )
                    if len(available)
                    else 0.0,
                    "AvgDailyEqualReturnPct": round(float(daily.mean()), 4),
                    "Lower95DailyEqualReturnPct": round(_lower95(daily), 4),
                    "PositiveSessionPct": round(
                        float((daily > 0).mean() * 100.0), 2
                    ),
                    "EvidenceAccepted": False,
                    "PaperPromotionAllowed": False,
                    "RealOrdersAllowed": False,
                }
            )
    result = pd.DataFrame(rows)
    if not result.empty:
        result.sort_values(
            [
                "EvidencePhase",
                "Lower95DailyEqualReturnPct",
                "AvgDailyEqualReturnPct",
                "Cohort",
            ],
            ascending=[True, False, False, True],
            inplace=True,
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features", default="results/Data/live_feature_recorder/live_features_20*.csv"
    )
    parser.add_argument(
        "--bars",
        default=(
            "results/Data/intraday_event_shadow_rebuilt/"
            "intraday_event_shadow/bars_20*.csv"
        ),
    )
    parser.add_argument(
        "--output-dir", default="results/Data/live_leader_continuation"
    )
    parser.add_argument("--horizon-minutes", type=int, default=60)
    parser.add_argument("--fee-pct", type=float, default=0.10)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()

    tape = load_feature_tape(args.features)
    tape = tape.reset_index(drop=True)
    tape["RowId"] = np.arange(len(tape), dtype=int)
    candidates = build_candidates(tape)
    groups, session_dates = load_reconstructed_bars(
        args.bars,
        set(candidates["Symbol"].astype(str)) if not candidates.empty else set(),
    )
    evaluable_candidates = (
        candidates[candidates["Date"].astype(str).isin(session_dates)].copy()
        if not candidates.empty
        else candidates
    )
    outcomes = label_candidates(
        evaluable_candidates,
        groups,
        horizon_minutes=max(1, int(args.horizon_minutes)),
        fee_pct=max(0.0, float(args.fee_pct)),
        slippage_bps=max(0.0, float(args.slippage_bps)),
    )
    summary = summarize(outcomes, session_dates)
    snapshot_outcomes = label_candidates_from_snapshots(
        candidates,
        tape,
        horizon_minutes=max(1, int(args.horizon_minutes)),
        fee_pct=max(0.0, float(args.fee_pct)),
    )
    snapshot_dates = sorted(tape["Date"].astype(str).unique().tolist())
    snapshot_summary = summarize(snapshot_outcomes, snapshot_dates)
    snapshot_extension = snapshot_summary[
        snapshot_summary["EvidencePhase"].eq("retrospective")
        & snapshot_summary["Cohort"].eq("extension_acceleration")
        & snapshot_summary["Contract"].eq("t1.0_s1.0")
    ]
    snapshot_extension_row = (
        snapshot_extension.iloc[0].to_dict() if not snapshot_extension.empty else {}
    )
    extension_selection_rejected = bool(
        not snapshot_extension_row
        or int(snapshot_extension_row.get("Evaluated", 0) or 0) < 30
        or float(snapshot_extension_row.get("AvgNetReturnPct", 0.0) or 0.0) <= 0.0
        or float(
            snapshot_extension_row.get("Lower95DailyEqualReturnPct", 0.0) or 0.0
        )
        <= 0.0
        or float(snapshot_extension_row.get("PositiveSessionPct", 0.0) or 0.0)
        < 55.0
    )
    available_outcomes = (
        outcomes[outcomes["OutcomeAvailable"].fillna(False).astype(bool)].copy()
        if not outcomes.empty
        else pd.DataFrame()
    )
    evaluated_states = (
        available_outcomes[["Date", "Symbol", "MoveBand"]].drop_duplicates()
        if not available_outcomes.empty
        else pd.DataFrame()
    )

    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(output / "candidates.csv", index=False)
    outcomes.to_csv(output / "outcomes.csv", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    snapshot_outcomes.to_csv(output / "snapshot_outcomes.csv", index=False)
    snapshot_summary.to_csv(output / "snapshot_summary.csv", index=False)
    available_snapshot_outcomes = (
        snapshot_outcomes[
            snapshot_outcomes["OutcomeAvailable"].fillna(False).astype(bool)
        ].copy()
        if not snapshot_outcomes.empty
        else pd.DataFrame()
    )
    evaluated_snapshot_states = (
        available_snapshot_outcomes[["Date", "Symbol", "MoveBand"]]
        .drop_duplicates()
        if not available_snapshot_outcomes.empty
        else pd.DataFrame()
    )
    status = {
        "evidence_type": "live_only_temporal_leader_continuation_research",
        "sessions": len(session_dates),
        "candidate_rows_all_recorder_sessions": int(len(candidates)),
        "candidate_rows_rebuilt_sessions": int(len(evaluable_candidates)),
        "evaluated_candidate_states": int(len(evaluated_states)),
        "evaluated_contract_outcomes": int(len(available_outcomes)),
        "snapshot_sessions": len(snapshot_dates),
        "evaluated_snapshot_candidate_states": int(len(evaluated_snapshot_states)),
        "evaluated_snapshot_contract_outcomes": int(len(available_snapshot_outcomes)),
        "move_bands": list(MOVE_LABELS),
        "exploratory_rule_defined_on": EXPLORATORY_RULE_DEFINED_ON,
        "frozen_forward_start": FROZEN_FORWARD_START,
        "extension_acceleration_rule": {
            "move_band": "3-5",
            "min_move_acceleration_pct": 0.2,
            "min_buyer_seller_surge": 1.0,
        },
        "extension_acceleration_selection_rejected": extension_selection_rejected,
        "extension_acceleration_selection_source": (
            "live_snapshot_observed_ask_to_bid"
        ),
        "extension_acceleration_rejection_reason": (
            "Ten-session executable snapshot replay failed sample/performance gates"
            if extension_selection_rejected
            else "selection gates passed; forward evidence still required"
        ),
        "forward_candidate_states": int(
            evaluable_candidates[
                evaluable_candidates["Date"].astype(str).ge(FROZEN_FORWARD_START)
                & evaluable_candidates["ExtensionAccelerationCohort"].fillna(False).astype(bool)
            ][["Date", "Symbol", "MoveBand"]].drop_duplicates().shape[0]
        )
        if not evaluable_candidates.empty
        else 0,
        "uses_future_runner_labels": False,
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "contracts": [asdict(contract) for contract in CONTRACTS],
    }
    (output / "status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    if not summary.empty:
        print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
