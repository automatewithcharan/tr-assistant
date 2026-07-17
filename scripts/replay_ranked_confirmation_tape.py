#!/usr/bin/env python3
"""Replay the exact ranked-confirmation paper lane from recorded live ticks.

This is an exact tape replay, not a candle approximation. It refuses inputs
that omit any field used by the production decision or fill simulation.
"""

from __future__ import annotations

import argparse
import glob
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Iterable, List, Tuple

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pkscreener.classes.RankedConfirmationPaperTrader import (
    RankedConfirmationPaperTrader,
    RankedPaperConfig,
)


DEFAULT_INPUT = "results/Data/ranked_confirmation_paper/observations_*.csv"
DEFAULT_OUT_PREFIX = "results/Data/ranked_confirmation_replay/exact_replay"

REQUIRED_COLUMNS = {
    "date", "symbol", "alert_time", "alert_price", "strength", "checks",
    "timestamp", "price", "volume", "buy_sell_ratio", "spread_bps", "vwap",
    "open_price", "prev_close", "day_high", "best_bid", "best_ask", "buy_qty",
    "sell_qty",
}


def load_tapes(pattern: str) -> pd.DataFrame:
    paths = sorted(glob.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"No observation tapes matched: {pattern}")
    parts = []
    for path in paths:
        frame = pd.read_csv(path)
        missing = REQUIRED_COLUMNS - set(frame.columns)
        if missing:
            raise ValueError(f"{path} missing replay columns: {', '.join(sorted(missing))}")
        frame["source_file"] = path
        parts.append(frame)
    data = pd.concat(parts, ignore_index=True)
    data["timestamp"] = pd.to_datetime(data["timestamp"], errors="coerce")
    data = data.dropna(subset=["timestamp"]).copy()
    data["date"] = data["date"].astype(str)
    data["symbol"] = data["symbol"].astype(str).str.strip().str.upper()
    data.sort_values(["date", "timestamp", "symbol"], inplace=True)
    return data.reset_index(drop=True)


def _tick(row: pd.Series) -> dict:
    return {
        "last_price": float(row["price"]),
        "volume_traded": float(row["volume"]),
        "total_buy_quantity": float(row["buy_qty"]),
        "total_sell_quantity": float(row["sell_qty"]),
        "best_bid": float(row["best_bid"]),
        "best_ask": float(row["best_ask"]),
        "average_traded_price": float(row["vwap"]),
        "open": float(row["open_price"]),
        "close": float(row["prev_close"]),
        "high": float(row["day_high"]),
    }


def replay_tapes(
    tapes: pd.DataFrame,
    *,
    output_dir: str,
    config: RankedPaperConfig,
    capital: float = 400_000.0,
    daily_goal_amount: float = 10_000.0,
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    output_root = Path(output_dir)
    output_root.mkdir(parents=True, exist_ok=True)
    trade_rows: List[dict] = []
    daily_rows: List[dict] = []

    for date, day in tapes.groupby("date", sort=True):
        day = day.sort_values(["timestamp", "symbol"]).copy()
        first_now = pd.Timestamp(day.iloc[0]["timestamp"]).to_pydatetime()
        trader = RankedConfirmationPaperTrader(
            output_dir=str(output_root / "engine_runs" / str(date)),
            config=config,
            now_fn=lambda value=first_now: value,
            persist=False,
        )
        registered = set()
        latest_ticks = {}
        for timestamp, batch in day.groupby("timestamp", sort=True):
            now = pd.Timestamp(timestamp).to_pydatetime()
            alert_rows = []
            for _, row in batch.iterrows():
                symbol = str(row["symbol"]).upper()
                latest_ticks[symbol] = _tick(row)
                if symbol not in registered:
                    alert_rows.append(
                        {
                            "Symbol": symbol,
                            "NowAction": "Grade-B Anomaly",
                            "PriceNow": float(row["alert_price"]),
                            "SignalStrengthPct": float(row["strength"]),
                            "Checks": str(row.get("checks", "") or ""),
                        }
                    )
                    registered.add(symbol)
            trader.process(pd.DataFrame(alert_rows), latest_ticks, now=now)

        last_timestamp = pd.Timestamp(day["timestamp"].max())
        open_positions = sum(1 for trade in trader.trades.values() if not trade.closed)
        unresolved_candidates = sum(
            1 for candidate in trader.candidates.values()
            if candidate.status == "observing"
        )
        reached_eod = (
            last_timestamp.hour * 60 + last_timestamp.minute
        ) >= config.eod_exit_minutes
        complete_tape = bool(
            open_positions == 0
            and (unresolved_candidates == 0 or reached_eod)
        )
        for trade in trader.trades.values():
            row = asdict(trade)
            row["tape_complete"] = bool(complete_tape)
            trade_rows.append(row)
        closed = [trade for trade in trader.trades.values() if trade.closed]
        net_pnl = sum(trade.net_pnl for trade in closed)
        daily_rows.append(
            {
                "date": str(date),
                "tape_rows": int(len(day)),
                "candidates": int(day["symbol"].nunique()),
                "first_tick": str(day["timestamp"].min()),
                "last_tick": str(last_timestamp),
                "tape_complete": bool(complete_tape),
                "open_positions_at_tape_end": int(open_positions),
                "unresolved_candidates_at_tape_end": int(unresolved_candidates),
                "trades": int(len(trader.trades)),
                "closed_trades": int(len(closed)),
                "wins": int(sum(1 for trade in closed if trade.net_pnl > 0)),
                "net_pnl": round(net_pnl, 2),
                "daily_return_pct": round(net_pnl / capital * 100.0, 4) if capital > 0 else 0.0,
                "goal_amount": float(daily_goal_amount),
                "goal_hit": bool(net_pnl >= daily_goal_amount),
            }
        )

    trades_df = pd.DataFrame(trade_rows)
    daily_df = pd.DataFrame(daily_rows)
    closed_df = trades_df[trades_df.get("closed", False).astype(bool)].copy() if not trades_df.empty else pd.DataFrame()
    wins = closed_df[closed_df.get("net_pnl", 0) > 0] if not closed_df.empty else pd.DataFrame()
    losses = closed_df[closed_df.get("net_pnl", 0) < 0] if not closed_df.empty else pd.DataFrame()
    gross_profit = float(wins["net_pnl"].sum()) if not wins.empty else 0.0
    gross_loss = abs(float(losses["net_pnl"].sum())) if not losses.empty else 0.0
    summary_df = pd.DataFrame(
        [{
            "evidence_type": "exact_recorded_tick_tape",
            "days": int(len(daily_df)),
            "complete_days": int(daily_df["tape_complete"].sum()) if not daily_df.empty else 0,
            "candidates": int(daily_df["candidates"].sum()) if not daily_df.empty else 0,
            "closed_trades": int(len(closed_df)),
            "wins": int(len(wins)),
            "losses": int(len(losses)),
            "win_rate_pct": round(len(wins) / len(closed_df) * 100.0, 2) if len(closed_df) else 0.0,
            "net_pnl": round(float(closed_df["net_pnl"].sum()), 2) if not closed_df.empty else 0.0,
            "avg_net_pnl": round(float(closed_df["net_pnl"].mean()), 2) if not closed_df.empty else 0.0,
            "profit_factor": round(gross_profit / gross_loss, 4) if gross_loss > 0 else 0.0,
            "positive_day_pct": round((daily_df["net_pnl"] > 0).mean() * 100.0, 2) if not daily_df.empty else 0.0,
            "goal_amount": float(daily_goal_amount),
            "goal_hit_days": int(daily_df["goal_hit"].sum()) if not daily_df.empty else 0,
            "goal_hit_pct": round(daily_df["goal_hit"].mean() * 100.0, 2) if not daily_df.empty else 0.0,
            "capital": float(capital),
            "config_json": str(asdict(config)),
        }]
    )
    trades_df.to_csv(output_root / "replay_trades.csv", index=False)
    daily_df.to_csv(output_root / "replay_daily.csv", index=False)
    summary_df.to_csv(output_root / "replay_summary.csv", index=False)
    return trades_df, daily_df, summary_df


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output-dir", default=DEFAULT_OUT_PREFIX)
    parser.add_argument("--capital", type=float, default=400_000.0)
    parser.add_argument("--daily-goal", type=float, default=10_000.0)
    parser.add_argument("--target-pct", type=float, default=1.0)
    parser.add_argument("--stop-pct", type=float, default=0.75)
    parser.add_argument("--fee-pct-per-side", type=float, default=0.05)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()

    config_values = asdict(RankedPaperConfig())
    config_values.update(
        target_pct=max(0.01, float(args.target_pct)),
        stop_pct=max(0.01, float(args.stop_pct)),
        fee_pct_per_side=max(0.0, float(args.fee_pct_per_side)),
        slippage_bps_per_side=max(0.0, float(args.slippage_bps)),
    )
    config = RankedPaperConfig(**config_values)
    tapes = load_tapes(args.input)
    _, daily, summary = replay_tapes(
        tapes,
        output_dir=args.output_dir,
        config=config,
        capital=float(args.capital),
        daily_goal_amount=float(args.daily_goal),
    )
    print(summary.to_string(index=False))
    if not daily["tape_complete"].all():
        print("WARNING: incomplete tape day(s); do not use this run for promotion.")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
