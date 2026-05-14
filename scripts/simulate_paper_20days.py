#!/usr/bin/env python3
"""
Historical paper-trading simulation — last 20 trading days from Kite cache.

Strategy: 5minute | short | late_day_reversal
  late_minutes=270, move_pct=1.2, target=1.1%, stop=0.4%, max_trades=5

Rules:
  - No strategy logic changed
  - Tier 0 checks only marked if measured evidence exists
  - Signals filtered through ShortabilityValidator (ASM/GSM/liquidity)
  - Fills simulated at next-bar open + half-spread (pessimistic)
  - Stop/target checked bar-by-bar (stop_first policy)
  - Circuit breakers enforced per day
  - Per-day reports written; EOD runs baseline_stage_report.py

Usage:
    python scripts/simulate_paper_20days.py               # last 20 days
    python scripts/simulate_paper_20days.py --days 10     # last N days
    python scripts/simulate_paper_20days.py --date-from 2026-04-13  # from a date
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import warnings
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

RESULTS_DIR  = BASE_DIR / "results" / "Data"
LIQUIDITY_CSV = str(RESULTS_DIR / "kite_intraday_forensic_audit_shortability.csv")
CACHE_FOLDER = RESULTS_DIR / "kite_history_backfill" / "5minute" / "2024-05-13_2026-05-12"

from scripts.research_kite_intraday import (
    IntradayConfig, IntradayDay, add_intraday_features,
    best_signal_fast, prepare_day_cache, read_intraday,
)
from pkscreener.classes.ShortabilityValidator import ShortabilityValidator
from pkscreener.classes.IntradayPaperTrader import (
    BAD_FILL_THRESHOLD_PCT, BACKTEST_FEE_PCT, BACKTEST_SLIPPAGE_PCT,
    SLIPPAGE_ALERT_MULT, MAX_DAILY_LOSS_PCT, MAX_DAILY_TRADES,
)
from PKDevTools.classes import Archiver

# ---------------------------------------------------------------------------
# Strategy config — DO NOT CHANGE
# ---------------------------------------------------------------------------

CFG = IntradayConfig(
    family="late_day_reversal",
    side="short",
    interval="5minute",
    params={
        "late_minutes":    270,
        "move_pct":        1.2,
        "opening_minutes": 30,
        "min_price":       40.0,
        "min_value_cr":    25.0,
    },
    target_pct=1.1,
    stop_pct=0.4,
    max_trades_per_day=5,
    max_daily_loss_pct=2.5,
    sizing="equal",
)

PAPER_DIR = Path(Archiver.get_user_data_dir()) / "intraday_paper"
PAPER_DIR.mkdir(parents=True, exist_ok=True)

REPORT_DIR = RESULTS_DIR / "paper_reports"
REPORT_DIR.mkdir(parents=True, exist_ok=True)

TRADE_FIELDS = [
    "date", "symbol", "strategy", "signal_time", "intended_entry",
    "stop_loss", "target", "volume_ratio", "liquidity_at_signal_cr",
    "spread_at_signal", "broker_would_allow", "rejection_reason",
    "simulated_fill", "fill_slippage_pct", "fill_quality",
    "exit_price", "exit_time", "exit_reason",
    "pnl_pct", "pnl_after_costs",
    "stop_outcome", "target_outcome", "max_adverse_move", "closed",
]

# ---------------------------------------------------------------------------
# Data loading helpers
# ---------------------------------------------------------------------------

def load_universe() -> List[Dict]:
    """Load symbols that pass the ₹25Cr liquidity gate (matches CFG)."""
    rows = []
    if not os.path.isfile(LIQUIDITY_CSV):
        return rows
    with open(LIQUIDITY_CSV) as f:
        for r in csv.DictReader(f):
            liq = float(r.get("avg_value20_cr_median") or 0)
            if liq >= float(CFG.params.get("min_value_cr", 25)):
                rows.append({"symbol": r["symbol"].strip().upper(), "avg_value_cr": liq})
    return rows


def get_trading_dates(n_days: int = 20, date_from: Optional[str] = None) -> List[str]:
    """Return sorted list of last N trading dates available in the cache."""
    ref = pd.read_csv(
        CACHE_FOLDER / "RELIANCE.csv",
        usecols=["datetime"],
    )
    ref["date"] = pd.to_datetime(ref["datetime"]).dt.date
    dates = sorted(ref["date"].unique().tolist())
    if date_from:
        cutoff = datetime.strptime(date_from, "%Y-%m-%d").date()
        dates = [d for d in dates if d >= cutoff]
    return [str(d) for d in dates[-n_days:]]


def _detect_cache_dates() -> tuple[str, str]:
    """Parse start/end from the available 5-minute cache folder name."""
    folder = CACHE_FOLDER
    name = folder.name  # e.g. "2024-05-13_2026-05-12"
    parts = name.split("_")
    if len(parts) == 2:
        return parts[0], parts[1]
    raise RuntimeError(f"Cannot parse cache dates from folder name: {name}")


def load_5min_features(
    symbols: List[str],
    start: str,
    end: str,
) -> tuple[pd.DataFrame, list[IntradayDay]]:
    """Load and featurise 5-minute data for the given symbols and date range.

    Loads the full available cache (necessary because cache_path encodes exact
    start/end in the folder name), then filters rows to [start, end] after
    feature computation so the rolling window warmup is available.
    """
    cache_start, cache_end = _detect_cache_dates()
    nifty = read_intraday("NIFTY 50", "5minute", cache_start, cache_end)
    parts = []
    for sym in symbols:
        df = read_intraday(sym, "5minute", cache_start, cache_end)
        if not df.empty:
            parts.append(df)
    if not parts:
        return pd.DataFrame(), []
    raw = pd.concat(parts, ignore_index=True)
    features = add_intraday_features(raw, nifty)
    features = features.dropna(subset=["prev_close", "gap_pct", "avg_value20_cr", "vwap"]).copy()
    # Filter to the requested window after feature computation (warmup was included above)
    start_ts = pd.Timestamp(start)
    end_ts   = pd.Timestamp(end) + pd.Timedelta(days=1)
    features = features[(features["datetime"] >= start_ts) & (features["datetime"] < end_ts)].copy()
    day_cache = prepare_day_cache(features)
    return features, day_cache


# ---------------------------------------------------------------------------
# Per-day simulation
# ---------------------------------------------------------------------------

def _est_spread(close_price: float, avg_value_cr: float) -> float:
    """Estimate bid-ask spread at signal time (afternoon heuristic)."""
    if avg_value_cr >= 100:
        return close_price * 0.0008   # 0.08% for large-caps
    elif avg_value_cr >= 25:
        return close_price * 0.0015   # 0.15% for mid-caps
    else:
        return close_price * 0.0025   # 0.25% for small-caps


def simulate_day(
    date_str: str,
    day_cache: list[IntradayDay],
    universe_map: Dict[str, float],
    validator: ShortabilityValidator,
    verbose: bool = True,
) -> List[Dict]:
    """
    Simulate one trading day.  Returns list of trade dicts (paper trade format).
    """
    date_ts = pd.Timestamp(date_str)
    day_trades: List[Dict] = []

    # Daily circuit breakers
    running_pnl   = 0.0
    trade_count   = 0
    day_halted    = False
    halt_reason   = ""
    cb_events: List[str] = []

    # Per-symbol strike tracker (reset each day)
    symbol_strikes: Dict[str, int] = {}
    disabled_symbols: Dict[str, str] = {}

    days_for_date = [d for d in day_cache if d.date == date_ts]

    signals_generated  = 0
    signals_rejected   = 0

    for day in days_for_date:
        sym = day.symbol
        if day_halted:
            break

        liq = universe_map.get(sym, 0.0)
        if liq < float(CFG.params.get("min_value_cr", 25)):
            continue

        # Shortability check
        short_result = validator.validate(sym, avg_value_cr=liq)
        if not short_result.allowed:
            validator.log_rejection(
                sym, "13:45:00", short_result.reason, short_result.status,
                price=float(day.close[-1]) if len(day.close) else 0.0,
                strategy=CFG.strategy_key(),
            )
            signals_rejected += 1
            continue

        # Symbol circuit breaker
        if sym in disabled_symbols:
            signals_rejected += 1
            continue

        # Signal detection
        sig = best_signal_fast(day, CFG)
        if sig is None:
            continue

        signal_pos, score = sig
        signals_generated += 1

        # Signal bar info
        signal_close = float(day.close[signal_pos])
        signal_time  = str(day.datetime[signal_pos])[-8:]  # HH:MM:SS

        if signal_close < float(CFG.params.get("min_price", 40)):
            signals_rejected += 1
            continue

        # Entry: next bar open (entry_after_signal_close confirmed in forensic audit)
        entry_pos = signal_pos + 1
        if entry_pos >= len(day.open):
            continue

        intended_entry = float(day.open[entry_pos])
        spread         = _est_spread(intended_entry, liq)

        # Simulated fill: next bar open + half spread (pessimistic short entry)
        simulated_fill = intended_entry + spread / 2.0
        slippage_pct   = (simulated_fill - intended_entry) / intended_entry * 100

        # Fill quality
        if slippage_pct > SLIPPAGE_ALERT_MULT * BACKTEST_SLIPPAGE_PCT:
            fill_quality = "slippage_alert"
        elif slippage_pct > BAD_FILL_THRESHOLD_PCT:
            fill_quality = "bad_fill"
        else:
            fill_quality = "good"

        # Update symbol strike counter
        if slippage_pct > BAD_FILL_THRESHOLD_PCT:
            symbol_strikes[sym] = symbol_strikes.get(sym, 0) + 1
            if symbol_strikes[sym] >= 3:
                disabled_symbols[sym] = f"3 consecutive bad fills (last {slippage_pct:.3f}%)"
                cb_events.append(f"[SYMBOL CB] {sym} disabled after 3 bad fills")
        else:
            symbol_strikes[sym] = 0

        # Stop / target levels
        stop_price   = round(simulated_fill * (1 + CFG.stop_pct / 100), 2)
        target_price = round(simulated_fill * (1 - CFG.target_pct / 100), 2)

        # Walk bars for exit
        exit_price    = 0.0
        exit_time     = ""
        exit_reason   = "eod"
        stop_outcome  = "pending"
        target_outcome = "pending"
        day_high      = simulated_fill
        day_low       = simulated_fill

        for bar_i in range(entry_pos + 1, len(day.open)):
            bar_high = float(day.high[bar_i])
            bar_low  = float(day.low[bar_i])
            day_high = max(day_high, bar_high)
            day_low  = min(day_low, bar_low)

            # Stop first policy (conservative)
            if bar_high >= stop_price:
                exit_price    = stop_price
                exit_time     = str(day.datetime[bar_i])[-8:]
                exit_reason   = "stop_hit"
                stop_outcome  = "hit"
                target_outcome = "escaped"
                break
            if bar_low <= target_price:
                exit_price    = target_price
                exit_time     = str(day.datetime[bar_i])[-8:]
                exit_reason   = "target_hit"
                target_outcome = "hit"
                stop_outcome  = "escaped"
                break
        else:
            # EOD exit at last close
            exit_price     = float(day.close[-1])
            exit_time      = str(day.datetime[-1])[-8:]
            exit_reason    = "eod"
            stop_outcome   = "escaped" if day_high < stop_price else "hit"
            target_outcome = "hit" if day_low <= target_price else "escaped"

        # P&L (short: profit when price falls)
        pnl_pct         = (simulated_fill - exit_price) / simulated_fill * 100
        cost_pct        = 2 * (BACKTEST_FEE_PCT + BACKTEST_SLIPPAGE_PCT)
        pnl_after_costs = pnl_pct - cost_pct
        max_adverse     = (day_high - simulated_fill) / simulated_fill * 100

        trade = {
            "date":                  date_str,
            "symbol":                sym,
            "strategy":              "late_day_reversal",
            "signal_time":           signal_time,
            "intended_entry":        round(intended_entry, 2),
            "stop_loss":             stop_price,
            "target":                target_price,
            "volume_ratio":          1.0,
            "liquidity_at_signal_cr": round(liq, 2),
            "spread_at_signal":      round(spread, 4),
            "broker_would_allow":    True,
            "rejection_reason":      "",
            "simulated_fill":        round(simulated_fill, 4),
            "fill_slippage_pct":     round(slippage_pct, 4),
            "fill_quality":          fill_quality,
            "exit_price":            round(exit_price, 2),
            "exit_time":             exit_time,
            "exit_reason":           exit_reason,
            "pnl_pct":               round(pnl_pct, 4),
            "pnl_after_costs":       round(pnl_after_costs, 4),
            "stop_outcome":          stop_outcome,
            "target_outcome":        target_outcome,
            "max_adverse_move":      round(max_adverse, 4),
            "closed":                True,
        }
        day_trades.append(trade)
        trade_count += 1
        running_pnl += pnl_after_costs

        # Daily circuit breaker
        if not day_halted:
            if running_pnl <= -MAX_DAILY_LOSS_PCT:
                day_halted  = True
                halt_reason = f"Daily loss {running_pnl:.2f}% breached {MAX_DAILY_LOSS_PCT}% cap"
                cb_events.append(f"[DAILY CB] {halt_reason}")
            elif trade_count >= MAX_DAILY_TRADES:
                day_halted  = True
                halt_reason = f"Max daily trades ({MAX_DAILY_TRADES}) reached"
                cb_events.append(f"[DAILY CB] {halt_reason}")

    if verbose:
        wins     = sum(1 for t in day_trades if t["pnl_after_costs"] > 0)
        net_pnl  = sum(t["pnl_after_costs"] for t in day_trades)
        avg_slip = (
            sum(t["fill_slippage_pct"] for t in day_trades) / len(day_trades)
            if day_trades else 0.0
        )
        rej_sum  = validator.get_rejection_summary(date_str)
        n_rej    = sum(rej_sum.values())
        print(
            f"  {date_str}  signals={signals_generated:2d}  "
            f"rejected={signals_rejected + n_rej:2d}  "
            f"trades={len(day_trades):2d}  "
            f"wins={wins:2d}  "
            f"net={net_pnl:+.3f}%  "
            f"slip={avg_slip:+.4f}%  "
            + (f"  CB: {'; '.join(cb_events)}" if cb_events else "")
        )

    return day_trades


def persist_day(date_str: str, trades: List[Dict]) -> None:
    """Write trades to the IntradayPaperTrader CSV format."""
    path = PAPER_DIR / f"intraday_paper_{date_str}.csv"
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
        writer.writeheader()
        writer.writerows(trades)


def write_daily_report(date_str: str, trades: List[Dict], validator: ShortabilityValidator) -> str:
    """Generate and save a per-day report; return the report text."""
    from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader
    from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator
    trader   = IntradayPaperTrader()
    reporter = IntradayPaperReportGenerator(trader, validator)
    text     = reporter.daily_report(date_str)
    report_path = REPORT_DIR / f"daily_report_{date_str}.txt"
    report_path.write_text(text)
    return text


# ---------------------------------------------------------------------------
# Main simulation loop
# ---------------------------------------------------------------------------

def main(n_days: int = 20, date_from: Optional[str] = None) -> None:
    print("=" * 66)
    print("  PAPER TRADING SIMULATION — last 20 days")
    print("  Strategy: late_day_reversal | short | 5minute")
    print("  Params: late_minutes=270, move_pct=1.2, t=1.1%, s=0.4%, maxd=5")
    print("  This simulation does NOT change strategy rules.")
    print("=" * 66)

    universe = load_universe()
    if not universe:
        print("ERROR: Liquidity CSV not found. Run forensic_audit_intraday.py first.")
        sys.exit(1)
    universe_map = {e["symbol"]: e["avg_value_cr"] for e in universe}
    print(f"  Universe: {len(universe)} symbols ≥₹25Cr avg daily value")

    validator = ShortabilityValidator()
    validator.load_liquidity_map(LIQUIDITY_CSV)
    validator._cache_date = date.today().isoformat()   # skip NSE HTTP call during simulation
    validator._refresh_surveillance_lists()

    trading_dates = get_trading_dates(n_days, date_from)
    print(f"  Simulating: {trading_dates[0]}  →  {trading_dates[-1]}  ({len(trading_dates)} days)\n")

    symbols = [e["symbol"] for e in universe]

    # Load features for the simulation window.
    # load_5min_features loads the full 2-year cache internally so that the
    # avg_value20_cr rolling window has its 20-day warmup, then filters
    # the returned day_cache to [trading_dates[0], trading_dates[-1]].
    print("  Loading 5-minute features …", end=" ", flush=True)
    _, day_cache = load_5min_features(symbols, trading_dates[0], trading_dates[-1])
    print(f"done ({len(day_cache)} (date,symbol) days loaded)")
    print()
    print(f"  {'Date':<12} {'Signals':>8} {'Rejected':>9} {'Trades':>7} {'Wins':>5} "
          f"{'Net%':>8} {'Slip%':>8}  Notes")
    print("  " + "-" * 62)

    all_trades = []
    for date_str in trading_dates:
        trades = simulate_day(date_str, day_cache, universe_map, validator, verbose=True)
        persist_day(date_str, trades)
        all_trades.extend(trades)

    print()
    print("  " + "=" * 62)
    _print_cumulative_summary(all_trades)

    # Rerun stage validator
    print("\n\nRunning Stage Validator …\n")
    from pkscreener.classes.StageValidator import StageValidator
    sv = StageValidator()
    # lookahead_clean=False: confirmed bug in best_signal_fast() (argmax picks intraday peak).
    # Fixed in research_kite_intraday.py; prior paper CSVs were generated with the bug.
    result = sv.evaluate(
        "late_day_reversal", days=n_days, target_stage=1,
        tier0_flags={"lookahead_clean": False},
    )
    print(StageValidator.format_report(result))

    # Save cumulative report
    from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader
    from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator
    trader   = IntradayPaperTrader()
    reporter = IntradayPaperReportGenerator(trader, validator)
    cumul = reporter.cumulative_report(days=n_days)
    cumul_path = REPORT_DIR / f"cumulative_report_{trading_dates[-1]}.txt"
    cumul_path.write_text(cumul)
    print(cumul)
    print(f"\n  Cumulative report saved → {cumul_path}")

    # Final baseline check
    print("\n\nRunning baseline_stage_report.py …")
    os.system(f".venv/bin/python {BASE_DIR}/scripts/baseline_stage_report.py")


def _print_cumulative_summary(trades: List[Dict]) -> None:
    if not trades:
        print("  No trades generated.")
        return
    n      = len(trades)
    wins   = sum(1 for t in trades if t["pnl_after_costs"] > 0)
    pnls   = [t["pnl_after_costs"] for t in trades]
    slips  = [t["fill_slippage_pct"] for t in trades]
    days   = sorted({t["date"] for t in trades})

    # daily avg
    daily: Dict[str, List[float]] = {}
    for t in trades:
        daily.setdefault(t["date"], []).append(t["pnl_after_costs"])
    daily_avgs = [sum(v) / len(v) for v in daily.values()]
    avg_daily  = sum(daily_avgs) / len(daily_avgs) if daily_avgs else 0.0

    print(f"  CUMULATIVE SUMMARY  ({len(days)} days, {n} trades)")
    print(f"  Win rate:          {wins/n*100:.1f}%  ({wins}W / {n-wins}L)")
    print(f"  Avg trade net P&L: {sum(pnls)/n:+.3f}%")
    print(f"  Avg daily net P&L: {avg_daily:+.3f}%  (Stage 1 target: +0.30%)")
    print(f"  Avg fill slippage: {sum(slips)/len(slips):+.4f}%  (assumed: {BACKTEST_SLIPPAGE_PCT:.2f}%)")
    stop_hits   = sum(1 for t in trades if t["stop_outcome"]   == "hit")
    target_hits = sum(1 for t in trades if t["target_outcome"] == "hit")
    print(f"  Stop hit rate:     {stop_hits/n*100:.1f}%")
    print(f"  Target hit rate:   {target_hits/n*100:.1f}%")

    # Flag key concerns
    if avg_daily < 0.30:
        gap = 0.30 - avg_daily
        print(f"\n  [GAP] avg daily {avg_daily:+.3f}% is {gap:.3f}pp below Stage 1 target (+0.30%)")
    slip_avg = sum(slips) / len(slips)
    if slip_avg > BACKTEST_SLIPPAGE_PCT:
        print(f"  [SLIPPAGE] live avg {slip_avg:.4f}% > backtest assumed {BACKTEST_SLIPPAGE_PCT:.2f}%")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="20-day historical paper trading simulation")
    parser.add_argument("--days",      type=int, default=20, help="Number of trading days to simulate")
    parser.add_argument("--date-from", type=str, default=None, metavar="YYYY-MM-DD",
                        help="Simulate from this date forward")
    args = parser.parse_args()
    main(n_days=args.days, date_from=args.date_from)
