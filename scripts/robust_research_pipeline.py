#!/usr/bin/env python3
"""Robustness-first research pipeline with Kite-first data sourcing.

This script does not try to force a 3% daily target. It ranks strategies by
evidence quality: enough trades, positive split results, drawdown, Sharpe,
profit factor, regime stability, fee sensitivity, and concentration risk.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.research_advanced_intraday_2y import (  # noqa: E402
    AdvancedConfig,
    enrich_advanced_features,
    generate_configs,
    select_trades,
    summarize,
)
from scripts.research_intraday_strategies_2y import (  # noqa: E402
    DEFAULT_UNIVERSE,
    RESULTS_DIR,
    add_features,
    fetch_symbol_history,
    load_universe,
    max_drawdown,
    split_dates,
)


KITE_BACKFILL_ROOT = RESULTS_DIR / "kite_history_backfill"


@dataclass
class DataLoadResult:
    data: pd.DataFrame
    audit: pd.DataFrame
    kite_available: bool
    kite_session_reason: str


def _kite_session_status() -> tuple[bool, str]:
    try:
        from PKDevTools.classes import Archiver

        session_file = Path(Archiver.get_user_data_dir()) / "kite_connect_session.json"
        if session_file.exists():
            payload = json.loads(session_file.read_text(encoding="utf-8"))
            if str(payload.get("access_token", "")).strip():
                return True, f"saved session found: {session_file}"
        if os.environ.get("KITE_ACCESS_TOKEN") and os.environ.get("KITE_API_KEY"):
            return True, "KITE_ACCESS_TOKEN and KITE_API_KEY env vars found"
        return False, "no Kite access token/session detected"
    except Exception as exc:
        return False, f"Kite session check failed: {exc}"


def _to_naive_datetime(values) -> pd.Series:
    raw = pd.Series(values).astype(str).str.replace(r"(Z|[+-]\d{2}:?\d{2})$", "", regex=True)
    return pd.to_datetime(raw, errors="coerce")


def _normalise_daily_dates(values) -> pd.Series:
    dates = _to_naive_datetime(values)
    shifted = dates.dt.hour.eq(18) & dates.dt.minute.eq(30)
    if shifted.mean() > 0.8:
        dates = dates + pd.Timedelta(days=1)
    return dates.dt.normalize()


def _normalise_ohlcv(df: pd.DataFrame, symbol: str, source: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    if "datetime" in out.columns:
        out["date"] = pd.to_datetime(out["datetime"], errors="coerce")
    elif out.index.name in {"datetime", "date"} or not isinstance(out.index, pd.RangeIndex):
        out = out.reset_index()
        col = "datetime" if "datetime" in out.columns else "date" if "date" in out.columns else out.columns[0]
        out["date"] = pd.to_datetime(out[col], errors="coerce")
    elif "date" in out.columns:
        out["date"] = pd.to_datetime(out["date"], errors="coerce")
    else:
        return pd.DataFrame()
    out["date"] = _normalise_daily_dates(out["date"]) if "day" in str(source) else _to_naive_datetime(out["date"])
    keep = ["date", "open", "high", "low", "close", "volume"]
    if not set(keep).issubset(out.columns):
        return pd.DataFrame()
    out = out[keep].copy()
    for col in ["open", "high", "low", "close", "volume"]:
        out[col] = pd.to_numeric(out[col], errors="coerce")
    out = out.dropna(subset=["date", "open", "high", "low", "close"]).sort_values("date")
    out["symbol"] = symbol
    out["data_source"] = source
    return out.reset_index(drop=True)


def _read_kite_cache(symbol: str, start: str, end: str, interval: str = "day") -> pd.DataFrame:
    sym = str(symbol).strip().upper()
    range_path = KITE_BACKFILL_ROOT / interval / f"{pd.Timestamp(start):%Y-%m-%d}_{pd.Timestamp(end):%Y-%m-%d}" / f"{sym}.csv"
    legacy_path = RESULTS_DIR / "kite_history_cache" / interval / f"{sym}.csv"
    for source, path in [(f"kite_backfill_{interval}", range_path), (f"kite_cache_{interval}", legacy_path)]:
        if not path.exists():
            continue
        try:
            norm = _normalise_ohlcv(pd.read_csv(path), sym, source)
            if not norm.empty:
                return norm
        except Exception:
            continue
    return pd.DataFrame()


def _fetch_kite(symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
    try:
        from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider

        provider = KiteHistoricalDataProvider()
        df = provider.get_history_range(
            symbol,
            interval=interval,
            from_date=pd.Timestamp(start).to_pydatetime(),
            to_date=pd.Timestamp(end).to_pydatetime(),
        )
        norm = _normalise_ohlcv(df, symbol, f"kite_live_{interval}")
        if not norm.empty:
            cache_dir = RESULTS_DIR / "kite_history_cache" / interval
            cache_dir.mkdir(parents=True, exist_ok=True)
            norm.rename(columns={"date": "datetime"}).drop(columns=["data_source"], errors="ignore").to_csv(cache_dir / f"{symbol}.csv", index=False)
        return norm
    except Exception:
        return pd.DataFrame()


def validate_symbol_data(df: pd.DataFrame, symbol: str, start: str, end: str, source: str) -> dict:
    if df is None or df.empty:
        return {
            "symbol": symbol,
            "source": source,
            "status": "missing",
            "rows": 0,
            "start": "",
            "end": "",
            "issues": "no rows",
        }
    work = df.sort_values("date").copy()
    requested_start = pd.Timestamp(start)
    requested_end = pd.Timestamp(end)
    issues = []
    if work["date"].duplicated().any():
        issues.append("duplicate candles")
    if work["date"].dt.tz is not None:
        issues.append("timezone not normalized")
    if work["date"].min() > requested_start + pd.Timedelta(days=10):
        issues.append("starts after requested period")
    if work["date"].max() < requested_end - pd.Timedelta(days=10):
        issues.append("ends before requested period")
    bad_ohlc = ((work["high"] < work[["open", "close", "low"]].max(axis=1)) | (work["low"] > work[["open", "close", "high"]].min(axis=1))).sum()
    if bad_ohlc:
        issues.append(f"bad OHLC rows={bad_ohlc}")
    zero_vol = int((work["volume"].fillna(0) <= 0).sum())
    if zero_vol > max(5, len(work) * 0.05):
        issues.append(f"liquidity gaps/zero volume={zero_vol}")
    jumps = work["close"].pct_change().abs()
    if int((jumps > 0.35).sum()) > 0:
        issues.append("possible corporate-action jump")
    status = "ok" if not issues else "warning"
    if "starts after requested period" in issues or "ends before requested period" in issues:
        status = "incomplete"
    return {
        "symbol": symbol,
        "source": source,
        "status": status,
        "rows": int(len(work)),
        "start": str(work["date"].min().date()),
        "end": str(work["date"].max().date()),
        "issues": "; ".join(issues),
    }


def load_priority_market_data(
    symbols: Iterable[str],
    start: str,
    end: str,
    interval: str,
    allow_live_kite: bool,
    allow_yfinance_fallback: bool,
) -> DataLoadResult:
    kite_available, reason = _kite_session_status()
    parts = []
    audit_rows = []
    for idx, symbol in enumerate(symbols, 1):
        sym = str(symbol).strip().upper()
        chosen = pd.DataFrame()

        if allow_live_kite and kite_available:
            live = _fetch_kite(sym, start, end, interval)
            live_audit = validate_symbol_data(live, sym, start, end, f"kite_live_{interval}")
            if live_audit["status"] in {"ok", "warning"}:
                chosen = live
                audit_rows.append(live_audit | {"chosen": True})

        if chosen.empty:
            kite_cached = _read_kite_cache(sym, start, end, interval)
            kite_source = str(kite_cached["data_source"].iloc[0]) if not kite_cached.empty else f"kite_cache_{interval}"
            kite_audit = validate_symbol_data(kite_cached, sym, start, end, kite_source)
            if kite_audit["status"] in {"ok", "warning"}:
                chosen = kite_cached
                audit_rows.append(kite_audit | {"chosen": True})
            else:
                audit_rows.append(kite_audit | {"chosen": False})

        if chosen.empty and interval == "day" and allow_yfinance_fallback:
            yf = _normalise_ohlcv(fetch_symbol_history(sym, start, end, refresh=False), sym, "yfinance")
            yf_audit = validate_symbol_data(yf, sym, start, end, "yfinance")
            audit_rows.append(yf_audit | {"chosen": yf_audit["status"] in {"ok", "warning"}})
            if yf_audit["status"] in {"ok", "warning"}:
                chosen = yf

        if not chosen.empty:
            parts.append(chosen)
        if idx % 25 == 0:
            print(f"  data loaded {idx} symbols; usable={len(parts)}", flush=True)

    if not parts:
        raise RuntimeError("No usable data loaded from Kite cache/live Kite/yfinance.")
    data = pd.concat(parts, ignore_index=True).sort_values(["symbol", "date"]).reset_index(drop=True)
    return DataLoadResult(data=data, audit=pd.DataFrame(audit_rows), kite_available=kite_available, kite_session_reason=reason)


def enforce_kite_quality_gate(gate_file: Path, min_complete_pct: float) -> dict:
    if not gate_file.exists():
        raise RuntimeError(f"Kite quality gate file missing: {gate_file}")
    payload = json.loads(gate_file.read_text(encoding="utf-8"))
    gate_pass = bool(payload.get("gate_pass"))
    complete_pct = float(payload.get("complete_pct", 0.0))
    nifty_complete = bool(payload.get("nifty_complete"))
    if not gate_pass or complete_pct < min_complete_pct or not nifty_complete:
        raise RuntimeError(
            "Kite quality gate failed: "
            f"gate_pass={gate_pass}, complete_pct={complete_pct:.2f}, nifty_complete={nifty_complete}. "
            f"See {gate_file}"
        )
    return payload


def rejection_reasons(row: pd.Series, min_trades: int) -> list[str]:
    reasons = []
    if int(row.get("trades", 0)) < min_trades:
        reasons.append(f"trades<{min_trades}")
    if float(row.get("train_avg_daily_return_pct", 0.0)) <= 0:
        reasons.append("train<=0")
    if float(row.get("validation_avg_daily_return_pct", 0.0)) <= 0:
        reasons.append("validation<=0")
    if float(row.get("final_avg_daily_return_pct", 0.0)) <= 0:
        reasons.append("final<=0")
    if float(row.get("walk_avg_daily_return_pct", 0.0)) <= 0:
        reasons.append("walk-forward<=0")
    if float(row.get("max_drawdown_pct", 999.0)) >= 10:
        reasons.append("maxDD>=10")
    if float(row.get("profit_factor", 0.0)) <= 1.3:
        reasons.append("PF<=1.3")
    if float(row.get("sharpe", 0.0)) <= 1.0:
        reasons.append("Sharpe<=1")
    if float(row.get("top_trade_profit_share_pct", 100.0)) > 35:
        reasons.append("top trade concentration")
    if float(row.get("top_month_profit_share_pct", 100.0)) > 45:
        reasons.append("top month concentration")
    if float(row.get("fee_sensitivity_min_avg_daily_pct", -999.0)) <= 0:
        reasons.append("fee sensitivity failed")
    return reasons


def _trade_concentration(trades: pd.DataFrame) -> tuple[float, float]:
    if trades.empty or "trade_return_pct" not in trades.columns:
        return 100.0, 100.0
    positive = trades[trades["trade_return_pct"] > 0].copy()
    total_pos = float(positive["trade_return_pct"].sum())
    if total_pos <= 0:
        return 100.0, 100.0
    top_trade = float(positive["trade_return_pct"].max() / total_pos * 100.0)
    tmp = positive.copy()
    tmp["month"] = pd.to_datetime(tmp["date"]).dt.to_period("M").astype(str)
    monthly = tmp.groupby("month")["trade_return_pct"].sum()
    top_month = float(monthly.max() / total_pos * 100.0) if not monthly.empty else 100.0
    return round(top_trade, 2), round(top_month, 2)


def monte_carlo_summary(trades: pd.DataFrame, n: int = 300, seed: int = 7) -> dict:
    if trades.empty or len(trades) < 20:
        return {"mc_p05_total_pct": 0.0, "mc_p50_total_pct": 0.0, "mc_p05_maxdd_pct": 100.0}
    rng = np.random.default_rng(seed)
    returns = trades["trade_return_pct"].astype(float).to_numpy()
    totals = []
    dds = []
    for _ in range(n):
        sample = rng.choice(returns, size=len(returns), replace=True)
        equity = np.cumprod(1.0 + sample / 100.0)
        totals.append((equity[-1] - 1.0) * 100.0)
        peak = np.maximum.accumulate(equity)
        dds.append(abs(np.min(equity / peak - 1.0) * 100.0))
    return {
        "mc_p05_total_pct": round(float(np.percentile(totals, 5)), 2),
        "mc_p50_total_pct": round(float(np.percentile(totals, 50)), 2),
        "mc_p05_maxdd_pct": round(float(np.percentile(dds, 95)), 2),
    }


def robustness_score(row: pd.Series) -> float:
    score = 0.0
    score += min(float(row.get("final_avg_daily_return_pct", 0.0)) * 100.0, 20.0)
    score += min(float(row.get("walk_avg_daily_return_pct", 0.0)) * 100.0, 20.0)
    score += min(max(float(row.get("profit_factor", 0.0)) - 1.0, 0.0) * 20.0, 20.0)
    score += min(max(float(row.get("sharpe", 0.0)), 0.0) * 8.0, 16.0)
    score += max(0.0, 12.0 - float(row.get("max_drawdown_pct", 99.0)))
    score -= max(0.0, float(row.get("top_month_profit_share_pct", 100.0)) - 35.0) * 0.3
    score -= max(0.0, float(row.get("top_trade_profit_share_pct", 100.0)) - 25.0) * 0.3
    score += min(int(row.get("trades", 0)) / 100.0, 1.0) * 10.0
    return round(score, 3)


def evaluate_candidate(
    cfg: AdvancedConfig,
    features: pd.DataFrame,
    train_dates: list[pd.Timestamp],
    val_dates: list[pd.Timestamp],
    final_dates: list[pd.Timestamp],
    all_walk_dates: list[pd.Timestamp],
    base_cost: float,
) -> tuple[dict, pd.DataFrame]:
    frames = {}
    summaries = {}
    for name, dates in [("train", train_dates), ("validation", val_dates), ("final", final_dates)]:
        df = features[features["date"].isin(dates)].copy()
        trades = select_trades(df, cfg, base_cost)
        frames[name] = trades
        summaries[name] = summarize(trades, dates, cfg)

    walk_dates = all_walk_dates[252:] if len(all_walk_dates) > 252 else final_dates
    walk_trades = select_trades(features[features["date"].isin(walk_dates)].copy(), cfg, base_cost)
    walk_summary = summarize(walk_trades, walk_dates, cfg)

    all_trades = pd.concat([frames["train"], frames["validation"], frames["final"]], ignore_index=True)
    final_trades = frames["final"]
    top_trade_share, top_month_share = _trade_concentration(all_trades)
    mc = monte_carlo_summary(all_trades)

    fee_avgs = []
    for cost in [0.25, base_cost, 0.50, 0.75]:
        trades = select_trades(features[features["date"].isin(final_dates)].copy(), cfg, cost)
        fee_avgs.append(summarize(trades, final_dates, cfg)["avg_daily_return_pct"])

    row = {
        "strategy": cfg.label(),
        "family": cfg.family,
        "side": cfg.side,
        "regime": cfg.regime,
        "exit_model": cfg.exit_model,
        "sizing": cfg.sizing,
        "params_json": json.dumps(cfg.params, sort_keys=True),
        "trades": int(len(all_trades)),
        "train_avg_daily_return_pct": summaries["train"]["avg_daily_return_pct"],
        "validation_avg_daily_return_pct": summaries["validation"]["avg_daily_return_pct"],
        "final_avg_daily_return_pct": summaries["final"]["avg_daily_return_pct"],
        "walk_avg_daily_return_pct": walk_summary["avg_daily_return_pct"],
        "walk_total_return_pct": walk_summary["total_return_pct"],
        "walk_max_drawdown_pct": walk_summary["max_drawdown_pct"],
        "walk_trades": walk_summary["trades"],
        "total_return_pct": summaries["final"]["total_return_pct"],
        "max_drawdown_pct": max(summaries["train"]["max_drawdown_pct"], summaries["validation"]["max_drawdown_pct"], summaries["final"]["max_drawdown_pct"]),
        "sharpe": summaries["final"]["sharpe"],
        "win_rate_pct": summaries["final"]["win_rate_pct"],
        "profit_factor": summaries["final"]["profit_factor"],
        "top_trade_profit_share_pct": top_trade_share,
        "top_month_profit_share_pct": top_month_share,
        "fee_sensitivity_min_avg_daily_pct": round(float(min(fee_avgs)), 4),
        **mc,
    }
    return row, all_trades


def build_report(
    report_path: Path,
    data_result: DataLoadResult,
    start: str,
    end: str,
    ranking: pd.DataFrame,
    accepted: pd.DataFrame,
    rejected: pd.DataFrame,
    goal_pct: float,
) -> None:
    source_counts = data_result.audit[data_result.audit.get("chosen", False) == True]["source"].value_counts().to_dict() if not data_result.audit.empty else {}
    missing = data_result.audit[data_result.audit["status"].isin(["missing", "incomplete"])] if not data_result.audit.empty else pd.DataFrame()
    best = ranking.iloc[0] if not ranking.empty else pd.Series(dtype=object)
    worth_paper = not accepted.empty
    lines = [
        "# Robustness-First Strategy Research Report",
        "",
        f"Generated: {pd.Timestamp.now().isoformat(timespec='seconds')}",
        "",
        "## Objective",
        f"- 3% average daily return remains an aspirational benchmark, not a forced optimization target.",
        "- Ranking prioritizes robustness, validation quality, risk, and data integrity.",
        "",
        "## Data Layer",
        "- Data priority: Kite live historical data when enabled and available, then local Kite cache, then yfinance as final fallback.",
        f"- Kite availability: {data_result.kite_available} ({data_result.kite_session_reason}).",
        f"- Requested period: {start} to {end}.",
        f"- Chosen source counts: `{source_counts}`.",
        f"- Missing/incomplete audit rows: {len(missing)}.",
        "- Validation checks: missing candles, duplicate candles, OHLC consistency, timezone normalization, corporate-action jumps, zero-volume/liquidity gaps, and symbol availability.",
        "",
        "## Acceptance Criteria",
        "- At least 100 trades across the two-year test.",
        "- Positive train, validation, final test, and walk-forward results.",
        "- Max drawdown below 10%.",
        "- Profit factor above 1.3.",
        "- Sharpe above 1.0.",
        "- No single trade or month explains most profits.",
        "- Fee/slippage sensitivity must remain positive.",
        "",
        "## Best Ranked Strategy",
    ]
    if best.empty:
        lines.append("No strategy rows were evaluated.")
    else:
        lines.extend(
            [
                f"- Strategy: `{best['strategy']}`",
                f"- Robustness score: {best['robustness_score']}",
                f"- Trades: {int(best['trades'])}",
                f"- Train avg daily: {best['train_avg_daily_return_pct']:+.4f}%",
                f"- Validation avg daily: {best['validation_avg_daily_return_pct']:+.4f}%",
                f"- Final avg daily: {best['final_avg_daily_return_pct']:+.4f}%",
                f"- Walk-forward avg daily: {best['walk_avg_daily_return_pct']:+.4f}%",
                f"- Max drawdown: {best['max_drawdown_pct']:.2f}%",
                f"- Profit factor: {best['profit_factor']:.3f}",
                f"- Sharpe: {best['sharpe']:.3f}",
                f"- Rejection reason: {best['rejection_reason'] or 'ACCEPTED'}",
            ]
        )
    lines.extend(
        [
            "",
            "## Paper-Trading Recommendation",
            "Worth paper trading: " + ("YES" if worth_paper else "NO"),
        ]
    )
    if not worth_paper:
        lines.append("No candidate met the minimum robustness acceptance criteria. Continue collecting live paper data and improve data coverage before live trading.")
    kite_sources = source_counts and all(str(source).startswith("kite") for source in source_counts)
    data_limitations = [
        "- The raw broader universe can still contain newer, renamed, or delisted symbols without a full two-year Kite history.",
        "- Current liquid universe is not a point-in-time historical constituent universe, so survivorship bias is reduced only by liquidity filters, not eliminated.",
        "- Daily candles cannot prove true intraday order of target/stop; stop-first simulation is conservative.",
    ]
    if kite_sources and len(missing) == 0:
        data_limitations.insert(0, "- The selected research universe passed the local Kite daily-data audit for the requested period.")
    else:
        data_limitations.insert(0, "- Data coverage remains incomplete for the selected run; do not use these results for live decisions.")
    lines.extend(
        [
            "",
            "## 3% Goal Reality Check",
            f"- Target: {goal_pct:.2f}% average daily return.",
            f"- Best final-test daily average found in this robustness run: {float(best.get('final_avg_daily_return_pct', 0.0)):+.4f}%" if not best.empty else "- No best strategy.",
            "- Current data does not support a realistic 3% daily strategy after robustness filters.",
            "",
            "## Data Limitations Remaining",
            *data_limitations,
        ]
    )
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Robustness-first research pipeline with Kite-first data")
    parser.add_argument("--universe", default=str(DEFAULT_UNIVERSE))
    parser.add_argument("--start", default="2024-05-13")
    parser.add_argument("--end", default="2026-05-12")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--cost-pct", type=float, default=0.35)
    parser.add_argument("--goal-pct", type=float, default=3.0)
    parser.add_argument("--allow-live-kite", action="store_true", help="Fetch missing ranges from live Kite; otherwise use local cache first.")
    parser.add_argument("--kite-only", action="store_true", help="Disable yfinance fallback and use only Kite live/cache data.")
    parser.add_argument("--require-kite-quality", action="store_true", help="Abort unless the Kite backfill quality gate has passed.")
    parser.add_argument("--kite-gate-file", default=str(RESULTS_DIR / "kite_backfill_2y_quality_gate.json"))
    parser.add_argument("--min-kite-complete-pct", type=float, default=95.0)
    parser.add_argument("--candidate-count", type=int, default=120)
    parser.add_argument("--min-trades", type=int, default=100)
    parser.add_argument("--out-prefix", default=str(RESULTS_DIR / "robust_research"))
    args = parser.parse_args()

    out_prefix = Path(args.out_prefix)
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    if args.require_kite_quality:
        gate = enforce_kite_quality_gate(Path(args.kite_gate_file), args.min_kite_complete_pct)
        print(
            "Kite quality gate passed: "
            f"complete_pct={gate.get('complete_pct')} nifty_complete={gate.get('nifty_complete')}",
            flush=True,
        )
    symbols = load_universe(Path(args.universe), args.limit)
    data_result = load_priority_market_data(
        symbols,
        args.start,
        args.end,
        "day",
        allow_live_kite=args.allow_live_kite,
        allow_yfinance_fallback=not args.kite_only,
    )
    data_result.audit.to_csv(out_prefix.with_name(out_prefix.name + "_data_audit.csv"), index=False)
    if args.kite_only:
        chosen = data_result.audit[data_result.audit.get("chosen", False) == True].copy()
        non_kite = chosen[~chosen["source"].astype(str).str.startswith("kite")]
        missing = data_result.audit[data_result.audit["status"].isin(["missing", "incomplete"])]
        if not non_kite.empty or not missing.empty:
            raise RuntimeError(
                f"Kite-only data load failed: non_kite_chosen={len(non_kite)}, missing_or_incomplete={len(missing)}. "
                f"See {out_prefix.with_name(out_prefix.name + '_data_audit.csv')}"
            )

    features = enrich_advanced_features(add_features(data_result.data), args.start, args.end, refresh=False)
    features = features[(features["date"] >= pd.Timestamp(args.start)) & (features["date"] <= pd.Timestamp(args.end))].copy()
    features = features.dropna(subset=["prev_close", "gap_pct", "sma_50", "atr_pct", "vol_ratio_prev", "adx_14"]).copy()
    dates = sorted(features["date"].dropna().unique().tolist())
    train_dates, val_dates, final_dates = split_dates(dates)

    # Use the advanced grid as discovery surface, plus the known first-pass seed.
    configs = generate_configs("quick")
    rows = []
    for idx, cfg in enumerate(configs, 1):
        # Cheap prefilter: evaluate all-period trade count only before full robustness.
        trades = select_trades(features, cfg, args.cost_pct)
        if len(trades) >= max(20, int(args.min_trades * 0.25)):
            summary = summarize(trades, dates, cfg)
            rows.append((summary["avg_daily_return_pct"], summary["profit_factor"], summary["sharpe"], cfg))
        if idx % 1000 == 0:
            print(f"  screened {idx}/{len(configs)} configs", flush=True)
    rows = sorted(rows, key=lambda item: (item[0], item[1], item[2]), reverse=True)[: max(1, args.candidate_count)]
    candidate_configs = [item[3] for item in rows]

    ranking_rows = []
    trade_frames = []
    for cfg in candidate_configs:
        row, trades = evaluate_candidate(cfg, features, train_dates, val_dates, final_dates, dates, args.cost_pct)
        ranking_rows.append(row)
        if not trades.empty:
            trade_frames.append(trades.assign(strategy_label=cfg.label()))

    ranking = pd.DataFrame(ranking_rows)
    if not ranking.empty:
        # Quarter/month walk-forward proxy: use existing split signs as hard gate.
        ranking["rejection_reason"] = ranking.apply(lambda r: "; ".join(rejection_reasons(r, args.min_trades)), axis=1)
        ranking["accepted"] = ranking["rejection_reason"].eq("")
        ranking["robustness_score"] = ranking.apply(robustness_score, axis=1)
        ranking.sort_values(["accepted", "robustness_score", "final_avg_daily_return_pct"], ascending=[False, False, False], inplace=True)
    ranking.to_csv(out_prefix.with_name(out_prefix.name + "_robustness_ranking.csv"), index=False)
    if trade_frames:
        pd.concat(trade_frames, ignore_index=True).to_csv(out_prefix.with_name(out_prefix.name + "_candidate_trades.csv"), index=False)

    accepted = ranking[ranking["accepted"]] if not ranking.empty else pd.DataFrame()
    rejected = ranking[~ranking["accepted"]] if not ranking.empty else pd.DataFrame()
    rejected.to_csv(out_prefix.with_name(out_prefix.name + "_rejected_strategies.csv"), index=False)
    accepted.to_csv(out_prefix.with_name(out_prefix.name + "_accepted_strategies.csv"), index=False)
    build_report(
        out_prefix.with_name(out_prefix.name + "_final_report.md"),
        data_result,
        args.start,
        args.end,
        ranking,
        accepted,
        rejected,
        args.goal_pct,
    )
    print(f"Saved: {out_prefix.with_name(out_prefix.name + '_final_report.md')}")
    print(f"Accepted strategies: {len(accepted)}")
    if not ranking.empty:
        print(ranking.head(10)[["strategy", "robustness_score", "trades", "final_avg_daily_return_pct", "profit_factor", "sharpe", "rejection_reason"]].to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
