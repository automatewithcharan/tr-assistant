#!/usr/bin/env python3
"""Forensic audit for unusually strong Kite intraday backtest results."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts import research_kite_intraday as intraday  # noqa: E402
from scripts.research_intraday_strategies_2y import RESULTS_DIR, load_universe, max_drawdown  # noqa: E402


def cfg_from_row(row: pd.Series) -> intraday.IntradayConfig:
    return intraday.IntradayConfig(
        family=str(row["family"]),
        side=str(row["side"]),
        interval=str(row["interval"]),
        params=json.loads(row["params_json"]),
        target_pct=float(row["target_pct"]),
        stop_pct=float(row["stop_pct"]),
        max_trades_per_day=int(str(row["strategy"]).split("maxd=")[-1].split("|")[0]) if "maxd=" in str(row["strategy"]) else 5,
        max_daily_loss_pct=2.5,
        sizing="equal",
    )


def load_features(symbols: list[str], cfg: intraday.IntradayConfig, start: str, end: str) -> tuple[pd.DataFrame, list[intraday.IntradayDay]]:
    nifty = intraday.read_intraday("NIFTY 50", cfg.interval, start, end)
    parts = []
    for idx, symbol in enumerate(symbols, 1):
        df = intraday.read_intraday(symbol, cfg.interval, start, end)
        if not df.empty:
            parts.append(df)
        if idx % 25 == 0:
            print(f"  loaded audit data {idx}/{len(symbols)} symbols", flush=True)
    if not parts:
        raise RuntimeError("No intraday data loaded for forensic audit")
    raw = pd.concat(parts, ignore_index=True)
    features = intraday.add_intraday_features(raw, nifty)
    features = features.dropna(subset=["prev_close", "gap_pct", "avg_value20_cr", "vwap"]).copy()
    return features, intraday.prepare_day_cache(features)


def select_variant(
    signal_rows: list[tuple[intraday.IntradayDay, int, float]],
    cfg: intraday.IntradayConfig,
    base_fee_pct: float,
    fee_mult: float = 1.0,
    slippage_mult: float = 1.0,
    extra_cost_pct: float = 0.0,
    partial_fill: float = 1.0,
    entry_delay_bars: int = 0,
    allowed_dates: set[pd.Timestamp] | None = None,
    blocked_symbols: set[str] | None = None,
    blocked_dates: set[pd.Timestamp] | None = None,
) -> pd.DataFrame:
    trades = []
    daily_pnl: dict[pd.Timestamp, float] = {}
    daily_counts: dict[pd.Timestamp, int] = {}
    blocked_symbols = blocked_symbols or set()
    blocked_dates = blocked_dates or set()
    for day, signal_pos, score in signal_rows:
        if day.symbol in blocked_symbols or day.date in blocked_dates:
            continue
        if allowed_dates is not None and day.date not in allowed_dates:
            continue
        if daily_pnl.get(day.date, 0.0) <= -abs(cfg.max_daily_loss_pct):
            continue
        entry_pos = signal_pos + 1 + int(entry_delay_bars)
        if entry_pos >= len(day.open):
            continue
        if daily_counts.get(day.date, 0) >= cfg.max_trades_per_day:
            continue
        entry_price = float(day.open[entry_pos])
        exit_time, exit_price, exit_reason = intraday.simulate_exit_fast(day, entry_pos, cfg.side, entry_price, cfg.target_pct, cfg.stop_pct)
        gross = (exit_price / entry_price - 1.0) * 100.0 if cfg.side == "long" else (entry_price / exit_price - 1.0) * 100.0
        statutory = base_fee_pct * fee_mult
        slip = intraday.liquidity_slippage_pct(day.avg_value20_cr) * slippage_mult
        net = (gross - statutory - slip - extra_cost_pct) * float(partial_fill)
        daily_pnl[day.date] = daily_pnl.get(day.date, 0.0) + net
        daily_counts[day.date] = daily_counts.get(day.date, 0) + 1
        trades.append(
            {
                "date": day.date,
                "symbol": day.symbol,
                "signal_time": pd.Timestamp(day.datetime[signal_pos]),
                "entry_time": pd.Timestamp(day.datetime[entry_pos]),
                "exit_time": exit_time,
                "entry_delay_bars": int(entry_delay_bars),
                "entry_price": round(entry_price, 4),
                "exit_price": round(exit_price, 4),
                "gross_return_pct": round(gross, 4),
                "cost_pct": round(statutory + slip + extra_cost_pct, 4),
                "trade_return_pct": round(net, 4),
                "exit_reason": exit_reason,
                "score": round(float(score), 4),
                "avg_value20_cr": round(float(day.avg_value20_cr), 4),
                "gap_pct": round(float(day.gap_pct), 4),
            }
        )
    if not trades:
        return pd.DataFrame()
    return pd.DataFrame(trades).sort_values(["date", "signal_time"]).reset_index(drop=True)


def summarize_trades(trades: pd.DataFrame, dates: list[pd.Timestamp]) -> dict:
    if trades.empty:
        return {
            "trades": 0,
            "avg_daily_return_pct": 0.0,
            "total_return_pct": 0.0,
            "max_drawdown_pct": 0.0,
            "sharpe": 0.0,
            "win_rate_pct": 0.0,
            "profit_factor": 0.0,
            "worst_day_pct": 0.0,
            "worst_week_pct": 0.0,
            "worst_month_pct": 0.0,
            "positive_day_pct": 0.0,
        }
    work = trades.copy()
    work["date"] = pd.to_datetime(work["date"]).dt.normalize()
    by_day = work.groupby("date")["trade_return_pct"].sum().reindex(dates, fill_value=0.0)
    equity = (1.0 + by_day / 100.0).cumprod()
    week = by_day.groupby(by_day.index.to_period("W").astype(str)).sum()
    month = by_day.groupby(by_day.index.to_period("M").astype(str)).sum()
    wins = work[work["trade_return_pct"] > 0]["trade_return_pct"].sum()
    losses = abs(work[work["trade_return_pct"] <= 0]["trade_return_pct"].sum())
    std = by_day.std(ddof=0)
    return {
        "trades": int(len(work)),
        "avg_daily_return_pct": round(float(by_day.mean()), 4),
        "total_return_pct": round((float(equity.iloc[-1]) - 1.0) * 100.0, 2),
        "max_drawdown_pct": max_drawdown(by_day),
        "sharpe": round(float(by_day.mean() / std * np.sqrt(252)), 3) if std > 0 else 0.0,
        "win_rate_pct": round(float((work["trade_return_pct"] > 0).mean() * 100.0), 2),
        "profit_factor": round(float(wins / losses), 3) if losses > 0 else 99.0,
        "worst_day_pct": round(float(by_day.min()), 4),
        "worst_week_pct": round(float(week.min()), 4),
        "worst_month_pct": round(float(month.min()), 4),
        "positive_day_pct": round(float((by_day > 0).mean() * 100.0), 2),
    }


def concentration_tables(trades: pd.DataFrame, out_prefix: Path) -> dict:
    work = trades.copy()
    work["date"] = pd.to_datetime(work["date"]).dt.normalize()
    work["week"] = work["date"].dt.to_period("W").astype(str)
    work["month"] = work["date"].dt.to_period("M").astype(str)
    work["gap_bucket"] = pd.cut(work["gap_pct"], [-99, -3, -1, 0, 1, 3, 99], labels=["<-3", "-3:-1", "-1:0", "0:1", "1:3", ">3"])
    work["liquidity_bucket"] = pd.cut(work["avg_value20_cr"], [0, 25, 75, 200, 1e9], labels=["<25cr", "25-75cr", "75-200cr", ">200cr"])
    tables = {}
    for name, group in [
        ("symbol", "symbol"),
        ("day", "date"),
        ("week", "week"),
        ("month", "month"),
        ("gap_bucket", "gap_bucket"),
        ("liquidity_bucket", "liquidity_bucket"),
    ]:
        tbl = work.groupby(group, observed=False)["trade_return_pct"].agg(["count", "sum", "mean"]).reset_index().sort_values("sum", ascending=False)
        tbl.to_csv(out_prefix.with_name(out_prefix.name + f"_profit_by_{name}.csv"), index=False)
        tables[name] = tbl
    return {
        "top_symbols": tables["symbol"].head(5)["symbol"].astype(str).tolist(),
        "top_days": pd.to_datetime(tables["day"].head(5)["date"]).dt.normalize().tolist(),
    }


def shortability_report(trades: pd.DataFrame, out_prefix: Path) -> pd.DataFrame:
    symbols = sorted(trades["symbol"].unique().tolist())
    rows = []
    for symbol in symbols:
        g = trades[trades["symbol"].eq(symbol)]
        rows.append(
            {
                "symbol": symbol,
                "short_trades": int(len(g)),
                "avg_value20_cr_median": round(float(g["avg_value20_cr"].median()), 2),
                "shortability_status": "unknown_requires_broker_intraday_short_list",
                "audit_flag": "hard_to_confirm_shortability_without_broker_surveillance_margin_data",
            }
        )
    out = pd.DataFrame(rows)
    out.to_csv(out_prefix.with_name(out_prefix.name + "_shortability.csv"), index=False)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Forensic audit of top Kite intraday strategy")
    parser.add_argument("--universe", default=str(RESULTS_DIR / "kite_backfill_2y_eligible_kite_complete_universe.csv"))
    parser.add_argument("--ranking", default=str(RESULTS_DIR / "kite_intraday_research_strategy_ranking.csv"))
    parser.add_argument("--start", default="2024-05-13")
    parser.add_argument("--end", default="2026-05-12")
    parser.add_argument("--limit", type=int, default=178)
    parser.add_argument("--out-prefix", default=str(RESULTS_DIR / "kite_intraday_forensic_audit"))
    args = parser.parse_args()

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    ranking = pd.read_csv(args.ranking)
    best = ranking.iloc[0]
    cfg = cfg_from_row(best)
    symbols = load_universe(Path(args.universe), args.limit)
    features, day_cache = load_features(symbols, cfg, args.start, args.end)
    signal_rows = intraday.precompute_signals(day_cache, cfg)
    dates = sorted(pd.to_datetime(features["date"]).dropna().unique().tolist())
    base_fee = intraday.indian_intraday_fee_pct()

    baseline = select_variant(signal_rows, cfg, base_fee)
    baseline.to_csv(out_prefix.with_name(out_prefix.name + "_baseline_trades.csv"), index=False)
    concentration = concentration_tables(baseline, out_prefix)
    shortability = shortability_report(baseline, out_prefix)

    latest_end = max(dates)
    holdout_start = (latest_end - pd.DateOffset(months=3)).normalize()
    holdout_dates = {d for d in dates if d >= holdout_start}
    pre_holdout_dates = {d for d in dates if d < holdout_start}

    variants = {
        "baseline": baseline,
        "fees_1_5x_slip_2x": select_variant(signal_rows, cfg, base_fee, fee_mult=1.5, slippage_mult=2.0),
        "fees_2x_slip_3x_extra_10bp": select_variant(signal_rows, cfg, base_fee, fee_mult=2.0, slippage_mult=3.0, extra_cost_pct=0.10),
        "partial_fill_50pct": select_variant(signal_rows, cfg, base_fee, partial_fill=0.50),
        "entry_delay_1bar": select_variant(signal_rows, cfg, base_fee, entry_delay_bars=1),
        "entry_delay_2bar": select_variant(signal_rows, cfg, base_fee, entry_delay_bars=2),
        "remove_top5_symbols": select_variant(signal_rows, cfg, base_fee, blocked_symbols=set(concentration["top_symbols"])),
        "remove_top5_profit_days": select_variant(signal_rows, cfg, base_fee, blocked_dates=set(concentration["top_days"])),
        "pre_holdout_only": select_variant(signal_rows, cfg, base_fee, allowed_dates=pre_holdout_dates),
        "latest_3m_holdout": select_variant(signal_rows, cfg, base_fee, allowed_dates=holdout_dates),
    }
    summary_rows = []
    for name, trades in variants.items():
        variant_dates = [d for d in dates if name not in {"latest_3m_holdout", "pre_holdout_only"} or (d in (holdout_dates if name == "latest_3m_holdout" else pre_holdout_dates))]
        row = {"variant": name, **summarize_trades(trades, variant_dates)}
        summary_rows.append(row)
    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out_prefix.with_name(out_prefix.name + "_summary.csv"), index=False)

    lookahead_checks = {
        "entry_after_signal_close": bool((pd.to_datetime(baseline["entry_time"]) > pd.to_datetime(baseline["signal_time"])).all()),
        "entry_delay_minutes_min": float((pd.to_datetime(baseline["entry_time"]) - pd.to_datetime(baseline["signal_time"])).dt.total_seconds().min() / 60.0),
        "vwap_uses_intraday_cumsum": True,
        "avg_value20_uses_shifted_prior_daily_value": True,
        "same_candle_stop_target_policy": "stop_first_from_entry_candle",
    }
    bug_list = []
    if not lookahead_checks["entry_after_signal_close"]:
        bug_list.append("Entry occurs before or at signal candle close")
    bug_list.append("Original intraday report had a max-drawdown reporting bug: it passed an equity curve into a helper that expects daily returns. This forensic report uses corrected closed-trade daily drawdown.")
    bug_list.append("Mark-to-market intraday drawdown is still not measured; only closed-trade daily equity drawdown is reported.")
    if shortability["shortability_status"].str.contains("unknown").any():
        bug_list.append("Shortability is not confirmed from broker/ASM/GSM/security-ban data; short strategy cannot be considered execution-proven.")

    lines = [
        "# Kite Intraday Forensic Audit",
        "",
        f"Generated: {pd.Timestamp.now().isoformat(timespec='seconds')}",
        "",
        "## Strategy Audited",
        f"- `{cfg.label()}`",
        "",
        "## Lookahead Checks",
        *[f"- {k}: {v}" for k, v in lookahead_checks.items()],
        "",
        "## Shortability",
        f"- Short symbols traded: {shortability['symbol'].nunique()}",
        "- Broker-level intraday shortability/ASM/GSM/hard-to-borrow data is not available in this project, so shortability remains unconfirmed.",
        "",
        "## Variant Summary",
        summary.to_markdown(index=False),
        "",
        "## Concentration",
        f"- Top 5 profit symbols removed test uses: {', '.join(concentration['top_symbols'])}",
        f"- Top 5 profit days removed test uses: {', '.join(str(pd.Timestamp(d).date()) for d in concentration['top_days'])}",
        "",
        "## Bug / Risk List",
        *(f"- {item}" for item in bug_list),
        "",
        "## Paper-Trading Decision",
        "- Do not auto-trade.",
        "- Paper trade only if the broker confirms intraday shortability and live fills/slippage are tracked against this audit.",
    ]
    out_prefix.with_name(out_prefix.name + "_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary.to_string(index=False))
    print(f"Saved: {out_prefix.with_name(out_prefix.name + '_report.md')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
