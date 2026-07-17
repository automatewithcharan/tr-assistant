#!/usr/bin/env python3
"""Evaluate forward outcomes for live feature recorder rows.

This is the post-market judge for the live-only evidence stream. It does not
create alerts or orders. It labels each recorded live feature row with what
happened after that moment using local 5-minute candles.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
from typing import Iterable

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_live_microstructure_tape import (
    load_minute_candles,
    minute_candle_outcome,
)


DEFAULT_INPUT = "results/Data/live_feature_recorder/live_features_*.csv"
DEFAULT_DECISION_INPUT = "results/Data/live_feature_recorder/lane_decisions_*.csv"
DEFAULT_DATA_DIR = "results/Data/kite_history_cache/5minute"
DEFAULT_OUT_PREFIX = "results/Data/live_feature_recorder/live_feature_outcomes"
DEFAULT_DECISION_OUT_PREFIX = "results/Data/live_feature_recorder/lane_decision_outcomes"
TARGETS = (0.5, 0.7, 1.0, 1.5, 3.0)
STOPS = (0.5, 1.0, 2.5)
MARKET_OPEN_MINUTE = 9 * 60 + 15
MARKET_LAST_CANDLE_MINUTE = 15 * 60 + 29


def as_bool(value: object) -> bool:
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, float, np.integer, np.floating)):
        return float(value) != 0.0
    return str(value).strip().lower() in {"1", "1.0", "true", "yes", "y", "on"}


def boolean_series(values: pd.Series) -> pd.Series:
    return values.apply(as_bool).astype(bool)


def _metric_suffix(value: float) -> str:
    return str(float(value)).replace(".", "_").replace("-", "m")


def _input_files(pattern: str) -> list[Path]:
    path = Path(pattern)
    if any(char in str(pattern) for char in "*?[]"):
        return sorted(path.parent.glob(path.name))
    return [path] if path.exists() else []


def load_live_features(pattern: str) -> pd.DataFrame:
    frames = []
    for path in _input_files(pattern):
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if df.empty:
            continue
        df["SourceFile"] = str(path)
        frames.append(df)
    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    required = {"When", "Symbol", "LastPrice"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"live feature input missing columns: {', '.join(sorted(missing))}")
    out["When"] = pd.to_datetime(out["When"], errors="coerce")
    out["Date"] = out["When"].dt.date.astype(str)
    out["Symbol"] = out["Symbol"].astype(str).str.strip().str.upper()
    out["LastPrice"] = pd.to_numeric(out["LastPrice"], errors="coerce")
    return out.dropna(subset=["When", "LastPrice"]).reset_index(drop=True)


def load_lane_decisions(pattern: str) -> pd.DataFrame:
    """Load pass/reject lane decisions and normalize entry price columns."""
    frames = []
    for path in _input_files(pattern):
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if df.empty:
            continue
        df["SourceFile"] = str(path)
        frames.append(df)
    if not frames:
        return pd.DataFrame()

    out = pd.concat(frames, ignore_index=True)
    required = {"When", "Symbol"}
    missing = required - set(out.columns)
    if missing:
        raise ValueError(f"lane decision input missing columns: {', '.join(sorted(missing))}")
    if "PriceNow" not in out.columns and "LastPrice" not in out.columns:
        raise ValueError("lane decision input missing price column: PriceNow or LastPrice")
    out["When"] = pd.to_datetime(out["When"], errors="coerce")
    out["Date"] = out["When"].dt.date.astype(str)
    out["Symbol"] = out["Symbol"].astype(str).str.strip().str.upper()
    price_col = "PriceNow" if "PriceNow" in out.columns else "LastPrice"
    out["PriceNow"] = pd.to_numeric(out[price_col], errors="coerce")
    out["LastPrice"] = out["PriceNow"]
    for column in ("Lane", "Stage", "Decision", "Reason"):
        if column not in out.columns:
            out[column] = ""
    return out.dropna(subset=["When", "LastPrice"]).reset_index(drop=True)


def load_symbol_days(data_dir: str, symbols: Iterable[str]) -> dict[tuple[str, str], pd.DataFrame]:
    out: dict[tuple[str, str], pd.DataFrame] = {}
    data_root = Path(data_dir)
    for symbol in sorted({str(sym).strip().upper() for sym in symbols if str(sym).strip()}):
        path = data_root / f"{symbol}.csv"
        if not path.exists():
            continue
        try:
            df = pd.read_csv(path)
        except Exception:
            continue
        if "datetime" not in df.columns:
            for candidate in ("date", "timestamp", "time"):
                if candidate in df.columns:
                    df["datetime"] = df[candidate]
                    break
        if not {"datetime", "high", "low", "close"}.issubset(df.columns):
            continue
        df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
        df = df.dropna(subset=["datetime"]).sort_values("datetime").copy()
        for col in ("high", "low", "close"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["high", "low", "close"])
        if df.empty:
            continue
        df["Date"] = df["datetime"].dt.date.astype(str)
        for date, day in df.groupby("Date", sort=False):
            out[(symbol, str(date))] = day.reset_index(drop=True)
    return out


def simulate_target_stop(
    future: pd.DataFrame,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
) -> tuple[str, float, str]:
    if future.empty or entry_price <= 0:
        return "no_future", 0.0, ""
    target = entry_price * (1.0 + target_pct / 100.0)
    stop = entry_price * (1.0 - stop_pct / 100.0)
    highs = future["high"].to_numpy(dtype=float)
    lows = future["low"].to_numpy(dtype=float)
    times = future["datetime"].astype(str).to_numpy()
    target_hits = np.flatnonzero(highs >= target)
    stop_hits = np.flatnonzero(lows <= stop)
    first_target = int(target_hits[0]) if len(target_hits) else None
    first_stop = int(stop_hits[0]) if len(stop_hits) else None
    if first_target is not None and first_stop is not None:
        if first_target == first_stop:
            return "stop_first_same_bar", -abs(stop_pct), str(times[first_stop])
        if first_stop < first_target:
            return "stop", -abs(stop_pct), str(times[first_stop])
        return "target", float(target_pct), str(times[first_target])
    if first_stop is not None:
        return "stop", -abs(stop_pct), str(times[first_stop])
    if first_target is not None:
        return "target", float(target_pct), str(times[first_target])
    eod_close = float(future.iloc[-1]["close"])
    eod_return = (eod_close - entry_price) / entry_price * 100.0
    return "eod", eod_return, str(future.iloc[-1]["datetime"])


def feature_bucket(row: pd.Series) -> str:
    missed = as_bool(row.get("MissedRunnerCandidate", False))
    stable = as_bool(row.get("OrderBookStable", False))
    spread_ok = as_bool(row.get("SpreadOk", False))
    best_bid = float(row.get("BestBid", 0) or 0)
    best_ask = float(row.get("BestAsk", 0) or 0)
    spread_bps = float(row.get("SpreadBps", 0) or 0)
    executable_spread = (
        spread_ok
        and best_bid > 0.0
        and best_ask > best_bid
        and 0.0 < spread_bps <= 20.0
    )
    alerted = as_bool(row.get("AlreadyAlertedToday", False))
    sector = str(row.get("Sector", "") or "").strip().upper()
    sector_top2 = float(row.get("SectorTop2Count", 0) or 0) >= 2
    last_price = float(row.get("LastPrice", 0) or 0)
    traded_value = float(row.get("TradedValue", 0) or 0)
    tradeable = last_price >= 50.0 and traded_value >= 30_000_000.0
    if missed and stable and executable_spread and sector not in {"", "UNKNOWN"} and sector_top2 and tradeable:
        return "missed_stable_executable_known_sector"
    if missed and stable:
        return "missed_stable_depth"
    if missed:
        return "missed_runner"
    if alerted:
        return "alerted_today"
    if stable:
        return "stable_order_book"
    return "other"


def _label(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    try:
        if pd.isna(value):
            return fallback
    except Exception:
        pass
    text = str(value).strip()
    if not text or text.lower() == "nan":
        return fallback
    return text


def decision_bucket(row: pd.Series) -> str:
    lane = _label(row.get("Lane", ""), "unknown_lane")
    stage = _label(row.get("Stage", ""), "no_stage")
    decision = _label(row.get("Decision", ""), "no_decision")
    reason = _label(row.get("Reason", ""), "no_reason")
    return f"{lane}|{stage}|{decision}|{reason}"


def dedupe_first_evidence_state(features: pd.DataFrame) -> pd.DataFrame:
    """Freeze the first causal occurrence of each daily evidence state.

    Raw recorder rows remain append-only. This only prevents a state repeated
    every monitor cycle from receiving dozens of times more evaluation weight.
    """
    if features is None or features.empty:
        return pd.DataFrame() if features is None else features.copy()
    frame = features.copy()
    frame["When"] = pd.to_datetime(frame["When"], errors="coerce")
    minute_of_day = frame["When"].dt.hour * 60 + frame["When"].dt.minute
    frame = frame[
        minute_of_day.between(
            MARKET_OPEN_MINUTE,
            MARKET_LAST_CANDLE_MINUTE,
            inclusive="both",
        )
    ].sort_values("When")
    if "Lane" in frame.columns and frame["Lane"].astype(str).str.strip().ne("").any():
        for column in ("Lane", "Stage", "Decision", "Reason"):
            if column not in frame.columns:
                frame[column] = ""
            frame[column] = frame[column].fillna("").astype(str)
        keys = ["Date", "Symbol", "Lane", "Stage", "Decision", "Reason"]
    else:
        frame["_FeatureBucket"] = frame.apply(feature_bucket, axis=1)
        keys = ["Date", "Symbol", "_FeatureBucket"]
    return frame.drop_duplicates(keys, keep="first").drop(
        columns=["_FeatureBucket"], errors="ignore"
    ).reset_index(drop=True)


def evaluate_rows(features: pd.DataFrame, symbol_days: dict[tuple[str, str], pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for _, row in features.iterrows():
        symbol = str(row["Symbol"]).strip().upper()
        date = str(row["Date"])
        entry_time = pd.Timestamp(row["When"])
        entry_price = float(row["LastPrice"])
        day = symbol_days.get((symbol, date))
        future = pd.DataFrame()
        if day is not None and not day.empty:
            future = day[day["datetime"] >= entry_time].reset_index(drop=True)

        out = row.to_dict()
        lane = str(row.get("Lane", "") or "").strip()
        if lane:
            out["EvaluationSource"] = "lane_decision"
            out["FeatureBucket"] = decision_bucket(row)
        else:
            out["EvaluationSource"] = "live_feature"
            out["FeatureBucket"] = feature_bucket(row)
        if future.empty or entry_price <= 0:
            out["OutcomeAvailable"] = False
            out["OutcomeSource"] = "missing_local_candles"
            out["BarsAfterEntry"] = 0
            out["MaxFavorablePct"] = np.nan
            out["MaxAdversePct"] = np.nan
            out["EodReturnPct"] = np.nan
        else:
            out["OutcomeAvailable"] = True
            out["OutcomeSource"] = "legacy_local_candle_from_recorded_price"
            max_high = float(future["high"].max())
            min_low = float(future["low"].min())
            eod_close = float(future.iloc[-1]["close"])
            out["BarsAfterEntry"] = int(len(future))
            out["MaxFavorablePct"] = round((max_high - entry_price) / entry_price * 100.0, 4)
            out["MaxAdversePct"] = round((min_low - entry_price) / entry_price * 100.0, 4)
            out["EodReturnPct"] = round((eod_close - entry_price) / entry_price * 100.0, 4)

        for target in TARGETS:
            for stop in STOPS:
                suffix = f"t{_metric_suffix(target)}_s{_metric_suffix(stop)}"
                outcome, ret, exit_time = simulate_target_stop(future, entry_price, target, stop)
                out[f"{suffix}_outcome"] = outcome
                out[f"{suffix}_return_pct"] = round(ret, 4)
                out[f"{suffix}_exit_time"] = exit_time
        rows.append(out)
    return pd.DataFrame(rows)


def evaluate_rows_from_minutes(
    features: pd.DataFrame,
    candle_groups: dict[tuple[str, str], pd.DataFrame],
    horizon_minutes: int = 60,
    fee_pct: float = 0.10,
    slippage_bps: float = 5.0,
) -> pd.DataFrame:
    """Label evidence using the next untouched Kite minute open.

    The signal minute is excluded. Target/stop ambiguity is resolved against
    the trade by ``minute_candle_outcome`` and every return includes adverse
    entry/exit slippage plus fees.
    """
    rows = []
    for _, row in features.iterrows():
        symbol = str(row["Symbol"]).strip().upper()
        date = str(row["Date"])
        signal_when = pd.Timestamp(row["When"])
        candles = candle_groups.get((date, symbol))
        out = row.to_dict()
        lane = str(row.get("Lane", "") or "").strip()
        out["EvaluationSource"] = "lane_decision" if lane else "live_feature"
        out["FeatureBucket"] = decision_bucket(row) if lane else feature_bucket(row)

        signal_minute = signal_when.hour * 60 + signal_when.minute
        regular_market = MARKET_OPEN_MINUTE <= signal_minute <= MARKET_LAST_CANDLE_MINUTE
        eligible = bool(
            regular_market
            and signal_minute + int(horizon_minutes) <= MARKET_LAST_CANDLE_MINUTE
        )
        out["EvaluationEligible"] = eligible
        complete_window = False
        if eligible and candles is not None and not candles.empty:
            timestamps = pd.to_datetime(candles["datetime"], errors="coerce").dropna()
            expected_entry = signal_when.floor("min") + pd.Timedelta(minutes=1)
            expected_last = (signal_when + pd.Timedelta(minutes=int(horizon_minutes))).floor("min")
            complete_window = bool(
                not timestamps.empty
                and expected_entry in set(timestamps.tolist())
                and pd.Timestamp(timestamps.max()) >= expected_last
            )
        outcome_candles = candles if complete_window else None

        horizon = minute_candle_outcome(
            outcome_candles,
            signal_when,
            target_pct=999.0,
            stop_pct=999.0,
            horizon_minutes=horizon_minutes,
            fee_pct=fee_pct,
            slippage_bps=slippage_bps,
        )
        available = bool(horizon.get("OutcomeAvailable", False))
        out["OutcomeAvailable"] = available
        if not regular_market:
            unavailable_source = "outside_regular_market"
        elif not eligible:
            unavailable_source = "insufficient_market_horizon"
        elif not complete_window:
            unavailable_source = "incomplete_kite_minute_window"
        else:
            unavailable_source = "missing_kite_minute_candles"
        out["OutcomeSource"] = horizon.get("OutcomeSource", unavailable_source)
        out["BarsAfterEntry"] = int(horizon.get("FutureSnapshots", 0) or 0)
        out["EntryPrice"] = horizon.get("EntryAsk", np.nan)
        out["MaxFavorablePct"] = horizon.get("MfePct", np.nan)
        out["MaxAdversePct"] = horizon.get("MaePct", np.nan)
        out["ForwardReturnPct"] = horizon.get("NetReturnPct", np.nan)
        out["EodReturnPct"] = np.nan
        out["HorizonMinutes"] = int(horizon_minutes)
        out["FeePct"] = float(fee_pct)
        out["SlippageBps"] = float(slippage_bps)

        for target in TARGETS:
            for stop in STOPS:
                suffix = f"t{_metric_suffix(target)}_s{_metric_suffix(stop)}"
                outcome = minute_candle_outcome(
                    outcome_candles,
                    signal_when,
                    target_pct=target,
                    stop_pct=stop,
                    horizon_minutes=horizon_minutes,
                    fee_pct=fee_pct,
                    slippage_bps=slippage_bps,
                )
                reason = str(outcome.get("ExitReason", "no_future"))
                if reason.startswith("target"):
                    normalized_reason = "target"
                elif reason.startswith("stop"):
                    normalized_reason = "stop"
                else:
                    normalized_reason = reason
                out[f"{suffix}_outcome"] = normalized_reason
                out[f"{suffix}_return_pct"] = outcome.get("NetReturnPct", np.nan)
                out[f"{suffix}_exit_time"] = outcome.get("ExitWhen", "")
        rows.append(out)
    return pd.DataFrame(rows)


def summarize(outcomes: pd.DataFrame) -> pd.DataFrame:
    if outcomes.empty:
        return pd.DataFrame()
    target_cols = [
        col
        for col in outcomes.columns
        if col.startswith("t") and col.endswith("_outcome")
    ]
    grouped = []
    for bucket, group in outcomes.groupby("FeatureBucket", dropna=False):
        available = (
            group[boolean_series(group["OutcomeAvailable"])]
            if "OutcomeAvailable" in group.columns
            else group
        )
        row = {
            "FeatureBucket": bucket,
            "Rows": int(len(group)),
            "EligibleRows": (
                int(boolean_series(group["EvaluationEligible"]).sum())
                if "EvaluationEligible" in group.columns
                else int(len(group))
            ),
            "EvaluatedRows": int(len(available)),
            "CoveragePct": 0.0,
            "Symbols": int(group["Symbol"].nunique()) if "Symbol" in group.columns else 0,
            "AvgMfePct": round(float(available["MaxFavorablePct"].mean()), 4) if len(available) else np.nan,
            "MedianMfePct": round(float(available["MaxFavorablePct"].median()), 4) if len(available) else np.nan,
            "AvgMaePct": round(float(available["MaxAdversePct"].mean()), 4) if len(available) else np.nan,
            "AvgEodReturnPct": (
                round(float(available["EodReturnPct"].mean()), 4)
                if len(available) and available["EodReturnPct"].notna().any()
                else np.nan
            ),
            "AvgForwardReturnPct": (
                round(float(available["ForwardReturnPct"].mean()), 4)
                if len(available) and "ForwardReturnPct" in available.columns
                else np.nan
            ),
        }
        daily_return_column = (
            "ForwardReturnPct"
            if "ForwardReturnPct" in available.columns
            and available["ForwardReturnPct"].notna().any()
            else "EodReturnPct"
        )
        if len(available) and "Date" in available.columns and daily_return_column in available.columns:
            daily_forward = pd.to_numeric(
                available[daily_return_column], errors="coerce"
            ).groupby(available["Date"].astype(str)).mean().dropna()
        else:
            daily_forward = pd.Series(dtype=float)
        row["EvaluatedSessions"] = int(len(daily_forward))
        row["AvgDailyEqualReturnPct"] = (
            round(float(daily_forward.mean()), 4) if len(daily_forward) else np.nan
        )
        row["PositiveSessionPct"] = (
            round(float((daily_forward > 0).mean() * 100.0), 2)
            if len(daily_forward)
            else np.nan
        )
        row["WorstSessionPct"] = (
            round(float(daily_forward.min()), 4) if len(daily_forward) else np.nan
        )
        row["BestSessionPct"] = (
            round(float(daily_forward.max()), 4) if len(daily_forward) else np.nan
        )
        eligible_rows = int(row["EligibleRows"])
        row["CoveragePct"] = (
            round(float(len(available) / eligible_rows * 100.0), 2)
            if eligible_rows
            else 0.0
        )
        for column in ("EvaluationSource", "Lane", "Stage", "Decision", "Reason"):
            if column in group.columns:
                values = sorted(
                    {
                        str(value)
                        for value in group[column].dropna().tolist()
                        if str(value).strip() and str(value).lower() != "nan"
                    }
                )
                row[column] = values[0] if len(values) == 1 else ("mixed" if values else "")
        for col in target_cols:
            hit_rate = (available[col].astype(str) == "target").mean() * 100.0 if len(available) else 0.0
            row[f"{col.replace('_outcome', '')}_hit_rate_pct"] = round(float(hit_rate), 2)
            return_col = col.replace("_outcome", "_return_pct")
            if return_col in available.columns:
                numeric_returns = pd.to_numeric(available[return_col], errors="coerce")
                row[f"{col.replace('_outcome', '')}_avg_return_pct"] = (
                    round(float(numeric_returns.mean()), 4)
                    if len(available)
                    else np.nan
                )
                if "Date" in available.columns:
                    daily_contract = numeric_returns.groupby(
                        available["Date"].astype(str)
                    ).mean().dropna()
                    row[f"{col.replace('_outcome', '')}_avg_daily_equal_pct"] = (
                        round(float(daily_contract.mean()), 4)
                        if len(daily_contract)
                        else np.nan
                    )
        grouped.append(row)
    return pd.DataFrame(grouped).sort_values(["Rows", "FeatureBucket"], ascending=[False, True])


def write_outcomes(
    features: pd.DataFrame,
    data_dir: str,
    out_prefix_value: str,
    empty_message: str,
    minute_data_dir: str = "",
    horizon_minutes: int = 60,
    fee_pct: float = 0.10,
    slippage_bps: float = 5.0,
    dedupe_first: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, Path, Path]:
    out_prefix = Path(out_prefix_value)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    outcomes_path = Path(f"{out_prefix}_rows.csv")
    summary_path = Path(f"{out_prefix}_summary.csv")

    if features.empty:
        pd.DataFrame().to_csv(outcomes_path, index=False)
        pd.DataFrame().to_csv(summary_path, index=False)
        print(empty_message)
        print(f"Saved: {outcomes_path}")
        print(f"Saved: {summary_path}")
        return pd.DataFrame(), pd.DataFrame(), outcomes_path, summary_path

    evaluation_rows = dedupe_first_evidence_state(features) if dedupe_first else features
    if minute_data_dir:
        symbol_days = load_minute_candles(
            minute_data_dir,
            evaluation_rows["Symbol"].astype(str).unique().tolist(),
        )
        outcomes = evaluate_rows_from_minutes(
            evaluation_rows,
            symbol_days,
            horizon_minutes=horizon_minutes,
            fee_pct=fee_pct,
            slippage_bps=slippage_bps,
        )
    else:
        symbol_days = load_symbol_days(data_dir, evaluation_rows["Symbol"].unique())
        outcomes = evaluate_rows(evaluation_rows, symbol_days)
    summary = summarize(outcomes)
    outcomes.to_csv(outcomes_path, index=False)
    summary.to_csv(summary_path, index=False)
    print(f"Saved: {outcomes_path}")
    print(f"Saved: {summary_path}")
    print(f"Rows: {len(outcomes)} symbols={outcomes['Symbol'].nunique()} days={outcomes['Date'].nunique()}")
    if not summary.empty:
        print(summary.head(20).to_string(index=False))
    return outcomes, summary, outcomes_path, summary_path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--decision-input", default=DEFAULT_DECISION_INPUT)
    parser.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    parser.add_argument("--out-prefix", default=DEFAULT_OUT_PREFIX)
    parser.add_argument("--decision-out-prefix", default=DEFAULT_DECISION_OUT_PREFIX)
    parser.add_argument("--minute-data-dir", default="")
    parser.add_argument("--horizon-minutes", type=int, default=60)
    parser.add_argument("--fee-pct", type=float, default=0.10)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument(
        "--dedupe-first-state",
        action="store_true",
        help="Evaluate only the first daily occurrence of each symbol/state.",
    )
    parser.add_argument(
        "--mode",
        choices=["features", "decisions", "both"],
        default="features",
        help="Which evidence stream to label.",
    )
    args = parser.parse_args()

    if args.mode in {"features", "both"}:
        features = load_live_features(args.input)
        write_outcomes(
            features,
            args.data_dir,
            args.out_prefix,
            "No live feature rows found.",
            minute_data_dir=args.minute_data_dir,
            horizon_minutes=args.horizon_minutes,
            fee_pct=args.fee_pct,
            slippage_bps=args.slippage_bps,
            dedupe_first=args.dedupe_first_state,
        )

    if args.mode in {"decisions", "both"}:
        decisions = load_lane_decisions(args.decision_input)
        write_outcomes(
            decisions,
            args.data_dir,
            args.decision_out_prefix,
            "No lane decision rows found.",
            minute_data_dir=args.minute_data_dir,
            horizon_minutes=args.horizon_minutes,
            fee_pct=args.fee_pct,
            slippage_bps=args.slippage_bps,
            dedupe_first=args.dedupe_first_state,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
