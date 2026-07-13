#!/usr/bin/env python3
"""Evaluate recorded live microstructure snapshots using executable prices."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


DEFAULT_INPUT = "results/Data/live_feature_recorder/live_features_*.csv"
DEFAULT_OUTPUT = "results/Data/live_microstructure_tape"


def _truthy(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().str.lower().isin({"true", "1", "yes", "y"})


def load_tape(pattern: str) -> pd.DataFrame:
    path = Path(pattern)
    files = sorted(path.parent.glob(path.name))
    parts = [pd.read_csv(file) for file in files if file.is_file()]
    if not parts:
        return pd.DataFrame()
    tape = pd.concat(parts, ignore_index=True)
    required = {
        "When", "Symbol", "LastPrice", "BestBid", "BestAsk", "SpreadBps",
        "DayChangePct", "PullbackFromHighPct", "TradedValue", "BuyerSellerRatio",
        "BuyerSellerMedianWindow", "BuyerSellerMinWindow", "OrderBookStable",
        "SpreadOk", "SectorBreadthPct", "SectorTop2Count", "IsTopGainerNow",
        "MissedRunnerCandidate",
    }
    missing = required - set(tape.columns)
    if missing:
        raise ValueError(f"live tape missing columns: {', '.join(sorted(missing))}")
    tape["When"] = pd.to_datetime(tape["When"], errors="coerce")
    tape = tape.dropna(subset=["When"]).copy()
    tape["Date"] = tape["When"].dt.date.astype(str)
    tape["Symbol"] = tape["Symbol"].astype(str).str.strip().str.upper()
    numeric = [
        "LastPrice", "BestBid", "BestAsk", "SpreadBps", "DayChangePct",
        "PullbackFromHighPct", "TradedValue", "BuyerSellerRatio",
        "BuyerSellerMedianWindow", "BuyerSellerMinWindow", "SectorBreadthPct",
        "SectorTop2Count",
    ]
    for column in numeric:
        tape[column] = pd.to_numeric(tape[column], errors="coerce").fillna(0.0)
    for column in ["OrderBookStable", "SpreadOk", "IsTopGainerNow", "MissedRunnerCandidate"]:
        tape[column] = _truthy(tape[column])
    tape = tape.sort_values(["Date", "Symbol", "When"]).reset_index(drop=True)
    tape["RowId"] = np.arange(len(tape), dtype=int)
    return tape


def candidate_rules(tape: pd.DataFrame) -> dict[str, pd.Series]:
    minute = tape["When"].dt.hour * 60 + tape["When"].dt.minute
    common = (
        minute.between(9 * 60 + 20, 14 * 60 + 30)
        & (tape["LastPrice"] >= 50.0)
        & (tape["BestBid"] > 0.0)
        & (tape["BestAsk"] > 0.0)
        & (tape["BestAsk"] >= tape["BestBid"])
        & tape["SpreadOk"]
        & (tape["SpreadBps"] <= 20.0)
        & (tape["TradedValue"] >= 30_000_000.0)
        & tape["DayChangePct"].between(0.5, 5.0)
        & (tape["PullbackFromHighPct"] <= 0.75)
    )
    stable_depth = (
        common
        & tape["OrderBookStable"]
        & (tape["BuyerSellerRatio"] >= 1.5)
        & (tape["BuyerSellerMedianWindow"] >= 1.3)
        & (tape["BuyerSellerMinWindow"] >= 0.9)
    )
    return {
        "stable_depth": stable_depth,
        "stable_sector_cluster": (
            stable_depth
            & (tape["SectorBreadthPct"] >= 55.0)
            & (tape["SectorTop2Count"] >= 2)
        ),
        "stable_top_gainer": stable_depth & tape["IsTopGainerNow"],
        "stable_missed_runner": stable_depth & tape["MissedRunnerCandidate"],
    }


def build_candidates(tape: pd.DataFrame) -> pd.DataFrame:
    parts = []
    for rule, mask in candidate_rules(tape).items():
        selected = tape[mask].copy()
        selected.sort_values(["Date", "Symbol", "When"], inplace=True)
        selected = selected.drop_duplicates(["Date", "Symbol"], keep="first")
        selected["Rule"] = rule
        parts.append(selected)
    if not parts:
        return pd.DataFrame()
    candidates = pd.concat(parts, ignore_index=True)
    candidates["MicroScore"] = (
        candidates["BuyerSellerRatio"].clip(upper=10.0) * 5.0
        + candidates["BuyerSellerMedianWindow"].clip(upper=10.0) * 3.0
        + candidates["SectorBreadthPct"] * 0.2
        + candidates["DayChangePct"] * 4.0
        - candidates["PullbackFromHighPct"] * 10.0
        - candidates["SpreadBps"] * 0.2
    )
    candidates["FirstBatchSelected"] = False
    for (_, rule), group in candidates.groupby(["Date", "Rule"], sort=False):
        earliest = group["When"].min()
        batch = group[group["When"].eq(earliest)]
        winner = batch.sort_values(["MicroScore", "Symbol"], ascending=[False, True]).index[0]
        candidates.loc[winner, "FirstBatchSelected"] = True
    return candidates


def executable_outcome(
    group: pd.DataFrame,
    candidate_row_id: int,
    target_pct: float,
    stop_pct: float,
    horizon_minutes: int,
    fee_pct: float,
) -> dict:
    ordered = group.sort_values("When").reset_index(drop=True)
    positions = np.flatnonzero(ordered["RowId"].to_numpy(dtype=int) == int(candidate_row_id))
    if not len(positions):
        return {"OutcomeAvailable": False}
    index = int(positions[0])
    entry = float(ordered.iloc[index]["BestAsk"])
    if entry <= 0:
        return {"OutcomeAvailable": False}
    cutoff = pd.Timestamp(ordered.iloc[index]["When"]) + pd.Timedelta(minutes=int(horizon_minutes))
    future = ordered.iloc[index + 1 :]
    future = future[future["When"] <= cutoff].copy()
    if future.empty:
        return {"OutcomeAvailable": False}
    # A long position can only be sold into an observed bid. Last traded price
    # is deliberately not used as a fallback because it may not be executable.
    future = future[future["BestBid"] > 0].copy()
    if future.empty:
        return {"OutcomeAvailable": False}
    exits = future["BestBid"].to_numpy(dtype=float)
    target_price = entry * (1.0 + float(target_pct) / 100.0)
    stop_price = entry * (1.0 - float(stop_pct) / 100.0)
    reason = "horizon"
    gross = (float(exits[-1]) - entry) / entry * 100.0
    for price in exits:
        if float(price) <= stop_price:
            reason = "stop"
            gross = (float(price) - entry) / entry * 100.0
            break
        if float(price) >= target_price:
            reason = "target"
            gross = float(target_pct)
            break
    return {
        "OutcomeAvailable": True,
        "EntryAsk": round(entry, 4),
        "ExitReason": reason,
        "GrossReturnPct": round(gross, 6),
        "NetReturnPct": round(gross - float(fee_pct), 6),
        "MfePct": round((float(np.max(exits)) - entry) / entry * 100.0, 6),
        "MaePct": round((float(np.min(exits)) - entry) / entry * 100.0, 6),
        "FutureSnapshots": int(len(exits)),
    }


def load_minute_candles(
    data_dir: str,
    symbols: list[str],
) -> dict[tuple[str, str], pd.DataFrame]:
    """Load complete Kite minute candles keyed by (date, symbol)."""
    root = Path(data_dir)
    groups: dict[tuple[str, str], pd.DataFrame] = {}
    for symbol in sorted(set(symbols)):
        path = root / f"{symbol}.csv"
        if not path.exists():
            continue
        frame = pd.read_csv(path)
        if "datetime" not in frame.columns and "date" in frame.columns:
            frame = frame.rename(columns={"date": "datetime"})
        if not {"datetime", "open", "high", "low", "close"}.issubset(frame.columns):
            continue
        raw = frame["datetime"].astype(str).str.replace(
            r"(Z|[+-]\d{2}:?\d{2})$", "", regex=True
        )
        frame["datetime"] = pd.to_datetime(raw, errors="coerce")
        for column in ["open", "high", "low", "close"]:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frame = frame.dropna(
            subset=["datetime", "open", "high", "low", "close"]
        ).sort_values("datetime")
        frame["Date"] = frame["datetime"].dt.date.astype(str)
        for date, day in frame.groupby("Date", sort=False):
            groups[(str(date), symbol)] = day.reset_index(drop=True)
    return groups


def minute_candle_outcome(
    candles: pd.DataFrame,
    signal_when: pd.Timestamp,
    target_pct: float,
    stop_pct: float,
    horizon_minutes: int,
    fee_pct: float,
    slippage_bps: float,
) -> dict:
    """Simulate a causal long entry from complete one-minute candles.

    The signal minute is excluded because its OHLC includes prices from before
    the second-level signal. Entry is the next full minute's open plus adverse
    slippage. If target and stop occur in the same candle, stop wins.
    """
    if candles is None or candles.empty:
        return {"OutcomeAvailable": False}
    signal_when = pd.Timestamp(signal_when)
    entry_minute = signal_when.floor("min") + pd.Timedelta(minutes=1)
    cutoff = signal_when + pd.Timedelta(minutes=int(horizon_minutes))
    future = candles[
        (candles["datetime"] >= entry_minute) & (candles["datetime"] <= cutoff)
    ].copy()
    if future.empty or pd.Timestamp(future.iloc[0]["datetime"]) != entry_minute:
        return {"OutcomeAvailable": False}

    slip = float(slippage_bps) / 10_000.0
    entry = float(future.iloc[0]["open"]) * (1.0 + slip)
    if entry <= 0:
        return {"OutcomeAvailable": False}
    target_price = entry * (1.0 + float(target_pct) / 100.0)
    stop_price = entry * (1.0 - float(stop_pct) / 100.0)
    reason = "horizon"
    exit_price = float(future.iloc[-1]["close"]) * (1.0 - slip)
    exit_when = pd.Timestamp(future.iloc[-1]["datetime"])

    for bar in future.itertuples(index=False):
        open_price = float(bar.open)
        high = float(bar.high)
        low = float(bar.low)
        when = pd.Timestamp(bar.datetime)
        if open_price <= stop_price:
            reason = "stop_gap"
            exit_price = open_price * (1.0 - slip)
            exit_when = when
            break
        if open_price >= target_price:
            reason = "target_gap"
            exit_price = target_price
            exit_when = when
            break
        stop_hit = low <= stop_price
        target_hit = high >= target_price
        if stop_hit:
            reason = "stop_first_same_bar" if target_hit else "stop"
            exit_price = stop_price * (1.0 - slip)
            exit_when = when
            break
        if target_hit:
            reason = "target"
            exit_price = target_price
            exit_when = when
            break

    gross = (exit_price - entry) / entry * 100.0
    highs = future["high"].to_numpy(dtype=float)
    lows = future["low"].to_numpy(dtype=float)
    return {
        "OutcomeAvailable": True,
        "OutcomeSource": "kite_minute_next_open",
        "EntryAsk": round(entry, 4),
        "ExitPrice": round(exit_price, 4),
        "ExitWhen": exit_when,
        "ExitReason": reason,
        "GrossReturnPct": round(gross, 6),
        "NetReturnPct": round(gross - float(fee_pct), 6),
        "MfePct": round((float(np.max(highs)) - entry) / entry * 100.0, 6),
        "MaePct": round((float(np.min(lows)) - entry) / entry * 100.0, 6),
        "FutureSnapshots": int(len(future)),
    }


def label_candidates(
    tape: pd.DataFrame,
    candidates: pd.DataFrame,
    targets: list[float],
    stops: list[float],
    horizon_minutes: int,
    fee_pct: float,
) -> pd.DataFrame:
    group_map = {
        (date, symbol): group
        for (date, symbol), group in tape.groupby(["Date", "Symbol"], sort=False)
    }
    rows = []
    for candidate in candidates.to_dict(orient="records"):
        group = group_map.get((candidate["Date"], candidate["Symbol"]))
        if group is None:
            continue
        for target in targets:
            for stop in stops:
                outcome = executable_outcome(
                    group,
                    int(candidate["RowId"]),
                    target,
                    stop,
                    horizon_minutes,
                    fee_pct,
                )
                rows.append(
                    {
                        **candidate,
                        "TargetPct": float(target),
                        "StopPct": float(stop),
                        "HorizonMinutes": int(horizon_minutes),
                        "FeePct": float(fee_pct),
                        **outcome,
                    }
                )
    return pd.DataFrame(rows)


def label_candidates_from_minutes(
    candidates: pd.DataFrame,
    candle_groups: dict[tuple[str, str], pd.DataFrame],
    targets: list[float],
    stops: list[float],
    horizon_minutes: int,
    fee_pct: float,
    slippage_bps: float,
) -> pd.DataFrame:
    rows = []
    for candidate in candidates.to_dict(orient="records"):
        candles = candle_groups.get((candidate["Date"], candidate["Symbol"]))
        for target in targets:
            for stop in stops:
                outcome = minute_candle_outcome(
                    candles,
                    pd.Timestamp(candidate["When"]),
                    target,
                    stop,
                    horizon_minutes,
                    fee_pct,
                    slippage_bps,
                )
                rows.append(
                    {
                        **candidate,
                        "TargetPct": float(target),
                        "StopPct": float(stop),
                        "HorizonMinutes": int(horizon_minutes),
                        "FeePct": float(fee_pct),
                        "SlippageBps": float(slippage_bps),
                        **outcome,
                    }
                )
    return pd.DataFrame(rows)


def _daily_metrics(group: pd.DataFrame, dates: list[str], prefix: str) -> dict:
    first = group[group["FirstBatchSelected"]].drop_duplicates(["Date"], keep="first")
    daily = first.set_index("Date")["NetReturnPct"].reindex(dates, fill_value=0.0)
    if daily.empty:
        return {
            f"{prefix}Sessions": 0,
            f"{prefix}Trades": 0,
            f"{prefix}AvgDailyPct": 0.0,
            f"{prefix}PositiveDayPct": 0.0,
            f"{prefix}WorstDayPct": 0.0,
            f"{prefix}BestDayPct": 0.0,
        }
    return {
        f"{prefix}Sessions": len(dates),
        f"{prefix}Trades": int(first["Date"].isin(dates).sum()),
        f"{prefix}AvgDailyPct": round(float(daily.mean()), 6),
        f"{prefix}PositiveDayPct": round(float((daily > 0).mean() * 100.0), 4),
        f"{prefix}WorstDayPct": round(float(daily.min()), 6),
        f"{prefix}BestDayPct": round(float(daily.max()), 6),
    }


def summarize(
    outcomes: pd.DataFrame,
    all_dates: list[str],
    validation_days: int = 2,
) -> pd.DataFrame:
    if outcomes is None or outcomes.empty or "OutcomeAvailable" not in outcomes.columns:
        return pd.DataFrame()
    available = outcomes[outcomes["OutcomeAvailable"]].copy()
    if available.empty:
        return pd.DataFrame()
    validation_days = max(1, min(int(validation_days), max(1, len(all_dates) - 1)))
    split = len(all_dates) - validation_days
    train_dates = all_dates[:split]
    validation_dates = all_dates[split:]
    rows = []
    for keys, group in available.groupby(["Rule", "TargetPct", "StopPct"], sort=True):
        rule, target, stop = keys
        rows.append(
            {
                "Rule": rule,
                "TargetPct": target,
                "StopPct": stop,
                "Sessions": len(all_dates),
                "Candidates": int(group[["Date", "Symbol"]].drop_duplicates().shape[0]),
                "CandidateAvgNetPct": round(float(group["NetReturnPct"].mean()), 6),
                **_daily_metrics(group, train_dates, "Train"),
                **_daily_metrics(group, validation_dates, "Validation"),
                **_daily_metrics(group, all_dates, "All"),
                "TrainSelected": False,
                "EvidenceAccepted": False,
                "RealOrdersAllowed": False,
            }
        )
    summary = pd.DataFrame(rows)
    if not summary.empty:
        # Select each rule's contract using training dates only. Validation is
        # reported after selection and never participates in the ranking.
        for _, group in summary.groupby("Rule", sort=False):
            winner = group.sort_values(
                ["TrainAvgDailyPct", "TrainWorstDayPct", "TargetPct", "StopPct"],
                ascending=[False, False, True, True],
            ).index[0]
            summary.loc[winner, "TrainSelected"] = True
        summary.sort_values(
            ["TrainSelected", "ValidationAvgDailyPct", "TrainAvgDailyPct", "Candidates"],
            ascending=[False, False, False, False],
            inplace=True,
        )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output-prefix", default=DEFAULT_OUTPUT)
    parser.add_argument("--targets", default="0.5,0.7,1.0,1.5")
    parser.add_argument("--stops", default="0.5,0.7,1.0")
    parser.add_argument("--horizon-minutes", type=int, default=30)
    parser.add_argument("--fee-pct", type=float, default=0.10)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument("--start-date", default="")
    parser.add_argument("--end-date", default="")
    parser.add_argument("--validation-days", type=int, default=2)
    parser.add_argument("--rules", default="")
    parser.add_argument("--minute-data-dir", default="")
    args = parser.parse_args()
    tape = load_tape(args.input)
    if tape.empty:
        raise RuntimeError("no live feature tape found")
    if args.start_date:
        tape = tape[tape["Date"] >= str(args.start_date)].copy()
    if args.end_date:
        tape = tape[tape["Date"] <= str(args.end_date)].copy()
    if tape.empty:
        raise RuntimeError("no live feature tape remains after date filters")
    candidates = build_candidates(tape)
    if args.rules:
        selected_rules = {
            item.strip() for item in str(args.rules).split(",") if item.strip()
        }
        candidates = candidates[candidates["Rule"].isin(selected_rules)].copy()
    if candidates.empty:
        raise RuntimeError("no candidates passed the requested causal rules")
    targets = [float(item) for item in args.targets.split(",") if item.strip()]
    stops = [float(item) for item in args.stops.split(",") if item.strip()]
    if args.minute_data_dir:
        candles = load_minute_candles(
            args.minute_data_dir,
            candidates["Symbol"].astype(str).unique().tolist(),
        )
        outcomes = label_candidates_from_minutes(
            candidates,
            candles,
            targets,
            stops,
            args.horizon_minutes,
            args.fee_pct,
            args.slippage_bps,
        )
    else:
        outcomes = label_candidates(
            tape,
            candidates,
            targets,
            stops,
            args.horizon_minutes,
            args.fee_pct,
        )
    summary = summarize(
        outcomes,
        sorted(tape["Date"].unique().tolist()),
        validation_days=args.validation_days,
    )
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    candidates.to_csv(prefix.with_name(prefix.name + "_candidates.csv"), index=False)
    pd.DataFrame(
        {"symbol": sorted(candidates["Symbol"].astype(str).unique().tolist())}
    ).to_csv(prefix.with_name(prefix.name + "_universe.csv"), index=False)
    outcomes.to_csv(prefix.with_name(prefix.name + "_outcomes.csv"), index=False)
    summary.to_csv(prefix.with_name(prefix.name + "_summary.csv"), index=False)
    print(f"tape_rows={len(tape)} sessions={tape['Date'].nunique()} candidates={len(candidates)} outcomes={len(outcomes)}")
    print(summary.head(20).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
