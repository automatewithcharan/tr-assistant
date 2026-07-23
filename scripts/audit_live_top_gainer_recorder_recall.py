#!/usr/bin/env python3
"""Audit live-recorder visibility before reconstructed intraday runner events.

Runner labels use the completed session and are diagnostic hindsight only. The
output measures whether the recorder contained a symbol before it crossed a
future threshold; it can never promote paper or real trading.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _number(frame: pd.DataFrame, column: str) -> pd.Series:
    values = frame.get(column, pd.Series(np.nan, index=frame.index))
    return pd.to_numeric(values, errors="coerce")


def _truthy(frame: pd.DataFrame, column: str) -> pd.Series:
    values = frame.get(column, pd.Series(False, index=frame.index))
    return values.fillna("").astype(str).str.strip().str.lower().isin(
        {"1", "1.0", "true", "yes", "y", "on"}
    )


def executable_feature_mask(
    frame: pd.DataFrame,
    *,
    min_price: float,
    min_turnover: float,
    max_spread_bps: float,
) -> pd.Series:
    """Match the exact paper observer's contemporaneous buyability gate."""
    if frame is None or frame.empty:
        return pd.Series(False, index=getattr(frame, "index", None), dtype=bool)
    price = _number(frame, "LastPrice")
    turnover = _number(frame, "TradedValue")
    bid = _number(frame, "BestBid")
    ask = _number(frame, "BestAsk")
    spread = _number(frame, "SpreadBps")
    return (
        price.ge(float(min_price))
        & turnover.ge(float(min_turnover))
        & bid.gt(0.0)
        & ask.gt(bid)
        & spread.gt(0.0)
        & spread.le(float(max_spread_bps))
        & _truthy(frame, "SpreadOk")
    )


def first_feature_blockers(
    frame: pd.DataFrame,
    *,
    min_price: float,
    min_turnover: float,
    max_spread_bps: float,
) -> str:
    """Explain why the first observed feature row was not executable."""
    if frame is None or frame.empty:
        return "not_seen"
    row = frame.iloc[[0]]
    blockers = []
    if float(_number(row, "LastPrice").iloc[0] or 0.0) < float(min_price):
        blockers.append("price")
    if float(_number(row, "TradedValue").iloc[0] or 0.0) < float(min_turnover):
        blockers.append("turnover")
    bid = float(_number(row, "BestBid").iloc[0] or 0.0)
    ask = float(_number(row, "BestAsk").iloc[0] or 0.0)
    if bid <= 0.0 or ask <= bid:
        blockers.append("quote")
    spread = float(_number(row, "SpreadBps").iloc[0] or 0.0)
    if (
        spread <= 0.0
        or spread > float(max_spread_bps)
        or not bool(_truthy(row, "SpreadOk").iloc[0])
    ):
        blockers.append("spread")
    return "|".join(blockers) if blockers else "buyable"


def load_features(pattern: str) -> dict[str, pd.DataFrame]:
    path = Path(pattern)
    grouped_frames: dict[str, list[pd.DataFrame]] = {}
    for file in sorted(path.parent.glob(path.name)):
        frame = pd.read_csv(file)
        required = {"When", "Symbol", "OpenChangePct"}
        if not required.issubset(frame.columns):
            continue
        frame["When"] = pd.to_datetime(frame["When"], errors="coerce")
        frame["Symbol"] = frame["Symbol"].astype(str).str.strip().str.upper()
        frame["OpenChangePct"] = pd.to_numeric(
            frame["OpenChangePct"], errors="coerce"
        )
        universe = pd.to_numeric(
            frame.get("UniverseSize", pd.Series(np.nan, index=frame.index)),
            errors="coerce",
        )
        rank = pd.to_numeric(
            frame.get("DayChangeRank", pd.Series(np.nan, index=frame.index)),
            errors="coerce",
        )
        generation = pd.to_numeric(
            frame.get(
                "RecorderGeneration", pd.Series(np.nan, index=frame.index)
            ),
            errors="coerce",
        )
        current_generation = pd.Series(True, index=frame.index)
        if generation.notna().any():
            dates = frame["When"].dt.date.astype(str)
            newest_by_date = generation.groupby(dates).transform("max")
            current_generation = generation.eq(newest_by_date)
        frame["RecorderGeneration"] = generation
        frame["FullUniverseRankRecorder"] = (
            universe.ge(2500) & rank.notna() & current_generation
        )
        frame = frame.dropna(subset=["When", "Symbol", "OpenChangePct"])
        for symbol, group in frame.groupby("Symbol", sort=False):
            date = str(group.iloc[0]["When"].date())
            key = f"{date}:{symbol}"
            grouped_frames.setdefault(key, []).append(group)
    return {
        key: pd.concat(frames, ignore_index=True)
        .drop_duplicates(subset=["When", "Symbol"], keep="last")
        .sort_values("When")
        .reset_index(drop=True)
        for key, frames in grouped_frames.items()
    }


def audit_recall(
    bar_pattern: str,
    feature_groups: dict[str, pd.DataFrame],
    thresholds: tuple[float, ...] = (5.0, 10.0),
    min_price: float = 50.0,
    min_turnover: float = 30_000_000.0,
    max_spread_bps: float = 20.0,
) -> pd.DataFrame:
    path = Path(bar_pattern)
    rows = []
    counted_events: set[tuple[str, str, float]] = set()
    full_rank_start_by_date: dict[str, pd.Timestamp] = {}
    for group in feature_groups.values():
        if "FullUniverseRankRecorder" not in group.columns:
            continue
        full_rank = group[group["FullUniverseRankRecorder"].fillna(False).astype(bool)]
        for date, date_rows in full_rank.groupby(full_rank["When"].dt.date.astype(str)):
            first = pd.Timestamp(date_rows["When"].min())
            current = full_rank_start_by_date.get(str(date))
            if current is None or first < current:
                full_rank_start_by_date[str(date)] = first
    for file in sorted(path.parent.glob(path.name)):
        bars = pd.read_csv(file)
        required = {"symbol", "datetime", "open", "high", "close", "volume"}
        if not required.issubset(bars.columns):
            continue
        bars["datetime"] = pd.to_datetime(bars["datetime"], errors="coerce")
        bars["symbol"] = bars["symbol"].astype(str).str.strip().str.upper()
        for column in ("open", "high", "close", "volume"):
            bars[column] = pd.to_numeric(bars[column], errors="coerce")
        bars = bars.dropna(subset=["datetime", "symbol", "open", "high", "close"])
        for symbol, day in bars.groupby("symbol", sort=False):
            day = day.sort_values("datetime").reset_index(drop=True)
            date = str(day.iloc[0]["datetime"].date())
            first_bar_time = pd.Timestamp(day.iloc[0]["datetime"])
            full_rank_start = full_rank_start_by_date.get(date)
            full_rank_session_eligible = bool(
                full_rank_start is not None
                and full_rank_start <= first_bar_time + pd.Timedelta(minutes=5)
            )
            day_open = float(day.iloc[0]["open"])
            turnover = float(
                (day["volume"].fillna(0.0).clip(lower=0.0) * day["close"]).sum()
            )
            if day_open < float(min_price) or turnover < float(min_turnover):
                continue
            features = feature_groups.get(f"{date}:{symbol}", pd.DataFrame())
            for threshold in thresholds:
                event_key = (date, symbol, float(threshold))
                if event_key in counted_events:
                    continue
                move = (day["high"] / day_open - 1.0) * 100.0
                crossed = day[move >= float(threshold)]
                if crossed.empty:
                    continue
                counted_events.add(event_key)
                cross_time = pd.Timestamp(crossed.iloc[0]["datetime"])
                before = (
                    features[features["When"] < cross_time].copy()
                    if not features.empty
                    else pd.DataFrame()
                )
                full_rank_before = (
                    before[
                        before.get(
                            "FullUniverseRankRecorder",
                            pd.Series(False, index=before.index),
                        ).fillna(False).astype(bool)
                    ].copy()
                    if not before.empty
                    else pd.DataFrame()
                )
                moves = (
                    before["OpenChangePct"]
                    if not before.empty
                    else pd.Series(dtype=float)
                )
                first_time = pd.Timestamp(before.iloc[0]["When"]) if not before.empty else pd.NaT
                full_rank_moves = (
                    full_rank_before["OpenChangePct"]
                    if not full_rank_before.empty
                    else pd.Series(dtype=float)
                )
                full_rank_first_time = (
                    pd.Timestamp(full_rank_before.iloc[0]["When"])
                    if not full_rank_before.empty
                    else pd.NaT
                )
                full_rank_buyable = (
                    full_rank_before[
                        executable_feature_mask(
                            full_rank_before,
                            min_price=min_price,
                            min_turnover=min_turnover,
                            max_spread_bps=max_spread_bps,
                        )
                    ].copy()
                    if not full_rank_before.empty
                    else pd.DataFrame()
                )
                full_rank_buyable_moves = (
                    full_rank_buyable["OpenChangePct"]
                    if not full_rank_buyable.empty
                    else pd.Series(dtype=float)
                )
                full_rank_first_buyable_time = (
                    pd.Timestamp(full_rank_buyable.iloc[0]["When"])
                    if not full_rank_buyable.empty
                    else pd.NaT
                )
                rows.append(
                    {
                        "Date": date,
                        "Symbol": symbol,
                        "RunnerThresholdPct": float(threshold),
                        "FirstCrossTime": cross_time,
                        "DayOpen": round(day_open, 4),
                        "Turnover": round(turnover, 2),
                        "FeatureSeenBeforeCross": bool(not before.empty),
                        "BandMinus1To3Seen": bool(moves.between(-1.0, 3.0).any()),
                        "Band0To3Seen": bool(moves.between(0.0, 3.0).any()),
                        "Band0_5To3Seen": bool(moves.between(0.5, 3.0).any()),
                        "Band0_5To5Seen": bool(moves.between(0.5, 5.0).any()),
                        "FirstFeatureTime": first_time,
                        "FirstFeatureOpenChangePct": (
                            round(float(moves.iloc[0]), 4) if not moves.empty else np.nan
                        ),
                        "LeadMinutes": (
                            round((cross_time - first_time).total_seconds() / 60.0, 3)
                            if pd.notna(first_time)
                            else np.nan
                        ),
                        "FullRankSessionEligible": full_rank_session_eligible,
                        "FullRankRecorderStart": full_rank_start,
                        "FullRankFeatureSeenBeforeCross": bool(not full_rank_before.empty),
                        "FullRankBuyableSeenBeforeCross": bool(
                            not full_rank_buyable.empty
                        ),
                        "FullRankBand0_5To3Seen": bool(
                            full_rank_moves.between(0.5, 3.0).any()
                        ),
                        "FullRankBand0_5To5Seen": bool(
                            full_rank_moves.between(0.5, 5.0).any()
                        ),
                        "FullRankBuyableBand0_5To3Seen": bool(
                            full_rank_buyable_moves.between(0.5, 3.0).any()
                        ),
                        "FullRankBuyableBand0_5To5Seen": bool(
                            full_rank_buyable_moves.between(0.5, 5.0).any()
                        ),
                        "FullRankFirstFeatureTime": full_rank_first_time,
                        "FullRankFirstBuyableFeatureTime": (
                            full_rank_first_buyable_time
                        ),
                        "FullRankLeadMinutes": (
                            round(
                                (cross_time - full_rank_first_time).total_seconds()
                                / 60.0,
                                3,
                            )
                            if pd.notna(full_rank_first_time)
                            else np.nan
                        ),
                        "FullRankBuyableLeadMinutes": (
                            round(
                                (
                                    cross_time - full_rank_first_buyable_time
                                ).total_seconds()
                                / 60.0,
                                3,
                            )
                            if pd.notna(full_rank_first_buyable_time)
                            else np.nan
                        ),
                        "FirstFullRankFeatureBuyabilityBlockers": (
                            first_feature_blockers(
                                full_rank_before,
                                min_price=min_price,
                                min_turnover=min_turnover,
                                max_spread_bps=max_spread_bps,
                            )
                        ),
                        "UsesFutureRunnerLabel": True,
                        "PaperPromotionAllowed": False,
                        "RealOrdersAllowed": False,
                    }
                )
    return pd.DataFrame(rows)


def summarize(events: pd.DataFrame) -> pd.DataFrame:
    if events is None or events.empty:
        return pd.DataFrame()
    rows = []
    for threshold, group in events.groupby("RunnerThresholdPct", sort=True):
        seen = group[group["FeatureSeenBeforeCross"]]
        full_rank = group[
            group.get(
                "FullRankSessionEligible", pd.Series(False, index=group.index)
            ).fillna(False).astype(bool)
        ]
        full_rank_seen = full_rank[full_rank["FullRankFeatureSeenBeforeCross"]]
        full_rank_buyable_seen = full_rank[
            full_rank["FullRankBuyableSeenBeforeCross"]
        ]
        count = len(group)
        rows.append(
            {
                "RunnerThresholdPct": float(threshold),
                "Sessions": int(group["Date"].nunique()),
                "RunnerEvents": int(count),
                "FeatureSeenBeforeCrossPct": round(
                    float(group["FeatureSeenBeforeCross"].mean() * 100.0), 2
                ),
                "BandMinus1To3RecallPct": round(
                    float(group["BandMinus1To3Seen"].mean() * 100.0), 2
                ),
                "Band0To3RecallPct": round(
                    float(group["Band0To3Seen"].mean() * 100.0), 2
                ),
                "Band0_5To3RecallPct": round(
                    float(group["Band0_5To3Seen"].mean() * 100.0), 2
                ),
                "Band0_5To5RecallPct": round(
                    float(group["Band0_5To5Seen"].mean() * 100.0), 2
                ),
                "MedianFirstFeatureMovePct": round(
                    float(seen["FirstFeatureOpenChangePct"].median()), 3
                ) if not seen.empty else 0.0,
                "MedianLeadMinutes": round(float(seen["LeadMinutes"].median()), 2)
                if not seen.empty
                else 0.0,
                "FullRankEligibleSessions": int(full_rank["Date"].nunique()),
                "FullRankRunnerEvents": int(len(full_rank)),
                "FullRankSeenBeforeCrossPct": round(
                    float(full_rank["FullRankFeatureSeenBeforeCross"].mean() * 100.0), 2
                ) if not full_rank.empty else 0.0,
                "FullRankBuyableSeenBeforeCrossPct": round(
                    float(full_rank["FullRankBuyableSeenBeforeCross"].mean() * 100.0),
                    2,
                ) if not full_rank.empty else 0.0,
                "FullRankBand0_5To3RecallPct": round(
                    float(full_rank["FullRankBand0_5To3Seen"].mean() * 100.0), 2
                ) if not full_rank.empty else 0.0,
                "FullRankBand0_5To5RecallPct": round(
                    float(full_rank["FullRankBand0_5To5Seen"].mean() * 100.0), 2
                ) if not full_rank.empty else 0.0,
                "FullRankBuyableBand0_5To3RecallPct": round(
                    float(
                        full_rank["FullRankBuyableBand0_5To3Seen"].mean()
                        * 100.0
                    ),
                    2,
                ) if not full_rank.empty else 0.0,
                "FullRankBuyableBand0_5To5RecallPct": round(
                    float(
                        full_rank["FullRankBuyableBand0_5To5Seen"].mean()
                        * 100.0
                    ),
                    2,
                ) if not full_rank.empty else 0.0,
                "FullRankMedianLeadMinutes": round(
                    float(full_rank_seen["FullRankLeadMinutes"].median()), 2
                ) if not full_rank_seen.empty else 0.0,
                "FullRankBuyableMedianLeadMinutes": round(
                    float(
                        full_rank_buyable_seen[
                            "FullRankBuyableLeadMinutes"
                        ].median()
                    ),
                    2,
                ) if not full_rank_buyable_seen.empty else 0.0,
                "UsesFutureRunnerLabel": True,
                "EvidenceAccepted": False,
                "PaperPromotionAllowed": False,
                "RealOrdersAllowed": False,
            }
        )
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--features",
        default="results/Data/live_feature_recorder/live_features_20*.csv",
    )
    parser.add_argument(
        "--bars",
        default=(
            "results/Data/intraday_event_shadow_rebuilt/"
            "intraday_event_shadow/bars_20*.csv"
        ),
    )
    parser.add_argument(
        "--output-dir", default="results/Data/live_top_gainer_recorder_recall"
    )
    parser.add_argument("--thresholds", default="5,10")
    parser.add_argument("--min-price", type=float, default=50.0)
    parser.add_argument("--min-turnover", type=float, default=30_000_000.0)
    parser.add_argument("--max-spread-bps", type=float, default=20.0)
    args = parser.parse_args()

    thresholds = tuple(
        sorted({float(item) for item in args.thresholds.split(",") if item.strip()})
    )
    feature_groups = load_features(args.features)
    events = audit_recall(
        args.bars,
        feature_groups,
        thresholds=thresholds,
        min_price=args.min_price,
        min_turnover=args.min_turnover,
        max_spread_bps=args.max_spread_bps,
    )
    summary = summarize(events)
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    events.to_csv(output / "events.csv", index=False)
    summary.to_csv(output / "summary.csv", index=False)
    status = {
        "evidence_type": "diagnostic_live_recorder_eventual_runner_recall",
        "sessions": int(events["Date"].nunique()) if not events.empty else 0,
        "runner_events": int(len(events)),
        "uses_future_runner_labels": True,
        "can_promote_strategy": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "summary": summary.to_dict(orient="records"),
    }
    (output / "status.json").write_text(
        json.dumps(status, indent=2, default=str), encoding="utf-8"
    )
    if not summary.empty:
        print(summary.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
