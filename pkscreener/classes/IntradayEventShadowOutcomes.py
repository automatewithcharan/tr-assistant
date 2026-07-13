"""Conservative outcome labels for paper-only live event shadow records."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


def _simulate(
    future: pd.DataFrame,
    entry_price: float,
    target_pct: float,
    stop_pct: float,
    side: str = "long",
) -> tuple[float, str, bool]:
    if side not in {"long", "short"}:
        raise ValueError(f"unsupported side: {side}")
    direction = 1.0 if side == "long" else -1.0
    target = entry_price * (1.0 + direction * float(target_pct) / 100.0)
    stop = entry_price * (1.0 - direction * float(stop_pct) / 100.0)
    for row in future.itertuples(index=False):
        if side == "long":
            hit_target = float(row.high) >= target
            hit_stop = float(row.low) <= stop
        else:
            hit_target = float(row.low) <= target
            hit_stop = float(row.high) >= stop
        if hit_target and hit_stop:
            return -float(stop_pct), "ambiguous_stop_target", False
        if hit_stop:
            return -float(stop_pct), "stop", False
        if hit_target:
            return float(target_pct), "target", True
    eod_return = direction * (float(future.iloc[-1]["close"]) - entry_price) / entry_price * 100.0
    return eod_return, "eod", False


def _evaluate(
    output_dir: str | Path,
    date: str,
    target_pct: float,
    stop_pct: float,
    cost_pct: float,
    required_last_bar: str,
    side: str,
    output_stem: str,
    summary_stem: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    root = Path(output_dir) / "intraday_event_shadow"
    bars_path = root / f"bars_{date}.csv"
    events_path = root / f"events_{date}.csv"
    if not bars_path.exists() or not events_path.exists():
        return pd.DataFrame(), pd.DataFrame()

    bars = pd.read_csv(bars_path, parse_dates=["datetime"])
    events = pd.read_csv(events_path, parse_dates=["signal_time"])
    if bars.empty or events.empty:
        return pd.DataFrame(), pd.DataFrame()
    required_bar = pd.Timestamp(f"{date} {required_last_bar}")
    latest_by_symbol = bars.groupby(bars["symbol"].astype(str).str.upper())["datetime"].max()

    rows = []
    for event in events.to_dict(orient="records"):
        symbol = str(event.get("symbol", "")).strip().upper()
        signal_time = pd.Timestamp(event["signal_time"])
        symbol_bars = bars[bars["symbol"].astype(str).str.upper().eq(symbol)].sort_values("datetime")
        future = symbol_bars[symbol_bars["datetime"] > signal_time].copy()
        complete = bool(latest_by_symbol.get(symbol, pd.Timestamp.min) >= required_bar)
        if future.empty:
            continue
        entry_time = pd.Timestamp(future.iloc[0]["datetime"])
        entry_price = float(future.iloc[0]["open"])
        if entry_price <= 0:
            continue
        gross, reason, target_before_stop = _simulate(
            future,
            entry_price,
            target_pct,
            stop_pct,
            side=side,
        )
        if side == "long":
            mfe = (float(future["high"].max()) - entry_price) / entry_price * 100.0
            mae = (float(future["low"].min()) - entry_price) / entry_price * 100.0
            eod = (float(future.iloc[-1]["close"]) - entry_price) / entry_price * 100.0
        else:
            mfe = (entry_price - float(future["low"].min())) / entry_price * 100.0
            mae = (entry_price - float(future["high"].max())) / entry_price * 100.0
            eod = (entry_price - float(future.iloc[-1]["close"])) / entry_price * 100.0
        evidence = f"live_5minute_shadow_{side}_conservative"
        rows.append(
            {
                **event,
                "side": side,
                "entry_time": entry_time.isoformat(),
                "entry_price": round(entry_price, 4),
                "target_pct": float(target_pct),
                "stop_pct": float(stop_pct),
                "cost_pct": float(cost_pct),
                "gross_return_pct": round(float(gross), 6),
                "net_return_pct": round(float(gross) - float(cost_pct), 6),
                "exit_reason": reason,
                "target_before_stop": bool(target_before_stop),
                "max_favorable_pct": round(float(mfe), 6),
                "max_adverse_pct": round(float(mae), 6),
                "eod_return_pct": round(float(eod), 6),
                "bars_after_entry": int(len(future)),
                "complete_session": complete,
                "evidence": evidence,
            }
        )
    outcomes = pd.DataFrame(rows)
    if outcomes.empty:
        return outcomes, pd.DataFrame()
    complete = outcomes[outcomes["complete_session"]].copy()
    summary_rows = []
    for event_type, group in complete.groupby("event_type", sort=True):
        summary_rows.append(
            {
                "date": str(date),
                "side": side,
                "event_type": event_type,
                "events": int(len(group)),
                "target_before_stop_pct": round(float(group["target_before_stop"].mean() * 100.0), 4),
                "positive_net_pct": round(float((group["net_return_pct"] > 0).mean() * 100.0), 4),
                "avg_net_return_pct": round(float(group["net_return_pct"].mean()), 6),
                "median_net_return_pct": round(float(group["net_return_pct"].median()), 6),
                "avg_mfe_pct": round(float(group["max_favorable_pct"].mean()), 6),
                "avg_mae_pct": round(float(group["max_adverse_pct"].mean()), 6),
                "ambiguous_events": int(group["exit_reason"].eq("ambiguous_stop_target").sum()),
                "evidence": evidence,
            }
        )
    summary = pd.DataFrame(summary_rows)
    outcomes.to_csv(root / f"{output_stem}_{date}.csv", index=False)
    summary.to_csv(root / f"{summary_stem}_{date}.csv", index=False)
    return outcomes, summary


def evaluate_event_shadow_day(
    output_dir: str | Path,
    date: str,
    target_pct: float = 1.5,
    stop_pct: float = 1.0,
    cost_pct: float = 0.15,
    required_last_bar: str = "15:25:00",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    return _evaluate(
        output_dir,
        date,
        target_pct,
        stop_pct,
        cost_pct,
        required_last_bar,
        side="long",
        output_stem="outcomes",
        summary_stem="outcome_summary",
    )


def evaluate_event_shadow_fade_day(
    output_dir: str | Path,
    date: str,
    target_pct: float = 2.0,
    stop_pct: float = 0.5,
    cost_pct: float = 0.15,
    required_last_bar: str = "15:25:00",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Evaluate the bullish event stream as a paper-only short fade."""
    return _evaluate(
        output_dir,
        date,
        target_pct,
        stop_pct,
        cost_pct,
        required_last_bar,
        side="short",
        output_stem="fade_outcomes",
        summary_stem="fade_outcome_summary",
    )
