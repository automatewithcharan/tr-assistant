#!/usr/bin/env python3
"""
Intraday short-only paper trading session runner.

Connects to Kite (live quotes) and feeds signals from the forensic-audit
strategy universe into the paper trader.  No real orders are ever placed.

Usage:
    # Start paper session (market hours only — runs until 3:15 PM)
    python scripts/run_intraday_paper_session.py

    # Print today's report only (no live session)
    python scripts/run_intraday_paper_session.py --report

    # Print cumulative N-day report
    python scripts/run_intraday_paper_session.py --report --days 20

    # Check approval criteria
    python scripts/run_intraday_paper_session.py --approval

    # Dry-run: process one past day from Kite history cache
    python scripts/run_intraday_paper_session.py --simulate 2026-05-09

SAFETY: This script is paper-trading ONLY. The Kite session is opened in
read-only mode (quotes + history). Order placement is permanently disabled.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import logging
import os
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, List, Optional

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

RESULTS_DIR   = BASE_DIR / "results" / "Data"
LIQUIDITY_CSV = str(RESULTS_DIR / "kite_intraday_forensic_audit_shortability.csv")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger("paper_session")


# ---------------------------------------------------------------------------
# Strategy config (mirrors forensic audit best-strategy params)
# ---------------------------------------------------------------------------

# These match the winning strategy from the 2-year backtest forensic audit.
# Adjust only after a full re-audit — never tune from paper results alone.
STRATEGY_NAME      = "gap_fade_short"
ENTRY_GAP_MIN_PCT  = 2.0    # % gap-up required to trigger short candidate
ENTRY_GAP_MAX_PCT  = 8.0    # % — larger gap = too risky, often spikes higher
MIN_LIQUIDITY_CR   = 10.0   # avg daily value gate (₹ Crore)
STOP_LOSS_PCT      = 1.5    # % above entry as stop
TARGET_PCT         = 2.5    # % below entry as target
MAX_SIGNAL_HOUR    = 11     # only take signals before 11 AM
MIN_VOL_RATIO      = 2.0    # volume must be at least 2× avg at signal time


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ist_now() -> datetime:
    try:
        import pytz
        return datetime.now(pytz.timezone("Asia/Kolkata"))
    except Exception:
        return datetime.now()


def _market_open() -> bool:
    now = _ist_now()
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= t <= 15 * 60 + 30


def _market_close_soon() -> bool:
    now = _ist_now()
    t   = now.hour * 60 + now.minute
    return t >= 15 * 60 + 10   # after 3:10 PM


def _load_kite() -> Optional[object]:
    """Return an authenticated KiteConnect client, or None if unavailable."""
    try:
        from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager
        mgr = KiteConnectSessionManager()
        kite = mgr.get_kite_client()
        _log.info("Kite connected successfully (paper / read-only mode)")
        return kite
    except Exception as exc:
        _log.warning(f"Kite connection failed: {exc}. Continuing without live quotes.")
        return None


def _fetch_quotes(kite, symbols: List[str]) -> Dict[str, dict]:
    """Fetch live quotes for a list of NSE symbols. Returns {} on failure."""
    if kite is None or not symbols:
        return {}
    try:
        instrument_ids = [f"NSE:{s}" for s in symbols]
        raw = kite.quote(instrument_ids)
        return {k.replace("NSE:", "").strip().upper(): v for k, v in (raw or {}).items()}
    except Exception as exc:
        _log.debug(f"Quote fetch error: {exc}")
        return {}


def _load_universe() -> List[dict]:
    """Load the shortability-confirmed universe from the forensic audit CSV."""
    if not os.path.isfile(LIQUIDITY_CSV):
        _log.error(f"Liquidity CSV not found: {LIQUIDITY_CSV}")
        return []
    rows = []
    with open(LIQUIDITY_CSV) as f:
        for row in csv.DictReader(f):
            liq = float(row.get("avg_value20_cr_median") or 0)
            if liq >= MIN_LIQUIDITY_CR:
                rows.append({
                    "symbol": str(row["symbol"]).strip().upper(),
                    "avg_value_cr": liq,
                })
    _log.info(f"Universe loaded: {len(rows)} symbols with ≥₹{MIN_LIQUIDITY_CR}Cr avg daily value")
    return rows


# ---------------------------------------------------------------------------
# Signal detection (gap-fade short logic)
# ---------------------------------------------------------------------------

def detect_short_signals(
    quotes: Dict[str, dict],
    universe: List[dict],
    now: datetime,
) -> List[dict]:
    """
    Detect intraday short signals from live quotes.

    Logic: stock opened with a gap-up of ENTRY_GAP_MIN..MAX %,
    is now trading near the open (fading / pulling back),
    and volume is elevated.

    Returns list of signal dicts.
    """
    signals = []
    for entry in universe:
        sym = entry["symbol"]
        q   = quotes.get(sym)
        if not q:
            continue

        ohlc      = q.get("ohlc", {}) or {}
        prev_close = float(ohlc.get("close") or 0)
        day_open   = float(ohlc.get("open")  or 0)
        last_price = float(q.get("last_price") or 0)
        volume     = int(q.get("volume") or 0)
        avg_vol    = int(q.get("average_traded_price") or 0)  # approximate

        if prev_close <= 0 or day_open <= 0 or last_price <= 0:
            continue

        gap_pct = (day_open - prev_close) / prev_close * 100

        # Gap-up check
        if not (ENTRY_GAP_MIN_PCT <= gap_pct <= ENTRY_GAP_MAX_PCT):
            continue

        # Price must still be near or below open (fade in progress)
        price_vs_open_pct = (last_price - day_open) / day_open * 100
        if price_vs_open_pct > 0.5:  # already bouncing back up — skip
            continue

        # Time gate: only morning window
        hour = now.hour
        if hour >= MAX_SIGNAL_HOUR:
            continue

        # Volume check (crude — Kite doesn't give 20-day avg directly in quote)
        vol_ratio = 1.0
        if avg_vol > 0 and volume > 0:
            # approximate: if current vol is high vs avg_traded_price proxy
            vol_ratio = volume / avg_vol if avg_vol > 0 else 1.0

        bid  = float(q.get("depth", {}).get("buy", [{}])[0].get("price", last_price))
        ask  = float(q.get("depth", {}).get("sell", [{}])[0].get("price", last_price))
        spread = ask - bid if ask > bid else last_price * 0.001

        signals.append({
            "symbol":         sym,
            "signal_time":    now.strftime("%H:%M:%S"),
            "intended_entry": last_price,
            "stop_loss":      round(last_price * (1 + STOP_LOSS_PCT / 100), 2),
            "target":         round(last_price * (1 - TARGET_PCT / 100), 2),
            "strategy":       STRATEGY_NAME,
            "avg_value_cr":   entry["avg_value_cr"],
            "spread":         round(spread, 2),
            "volume_ratio":   round(vol_ratio, 2),
            "gap_pct":        round(gap_pct, 2),
            "prev_close":     prev_close,
        })

    return signals


# ---------------------------------------------------------------------------
# Main session
# ---------------------------------------------------------------------------

def run_live_session(args) -> None:
    from pkscreener.classes.ShortabilityValidator import ShortabilityValidator
    from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader, IntradayShortSignal
    from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator

    validator = ShortabilityValidator()
    validator.load_liquidity_map(LIQUIDITY_CSV)

    trader    = IntradayPaperTrader()
    reporter  = IntradayPaperReportGenerator(trader, validator)
    kite      = _load_kite()
    universe  = _load_universe()

    if not universe:
        _log.error("Empty universe — aborting.")
        return

    _log.info(
        f"Paper session started — PAPER ONLY, NO REAL ORDERS\n"
        f"Strategy: {STRATEGY_NAME} | Universe: {len(universe)} symbols"
    )

    # Refresh ASM/GSM immediately at session start
    validator._refresh_surveillance_lists()

    prev_quotes: Dict[str, dict] = {}
    scan_interval = 60  # seconds between quote refreshes

    while _market_open():
        now       = _ist_now()
        symbols   = [e["symbol"] for e in universe]
        quotes    = _fetch_quotes(kite, symbols)

        if not quotes:
            _log.debug("No quotes (market closed or Kite unavailable) — waiting...")
            time.sleep(scan_interval)
            continue

        # Detect signals
        signals = detect_short_signals(quotes, universe, now)
        for sig_dict in signals:
            sym  = sig_dict["symbol"]

            # Validate shortability
            short_result = validator.validate(
                sym,
                avg_value_cr=sig_dict["avg_value_cr"],
                kite_client=kite,
            )

            if not short_result.allowed:
                _log.info(f"[REJECTED] {sym}: {short_result.reason}")
                validator.log_rejection(
                    sym,
                    sig_dict["signal_time"],
                    short_result.reason,
                    short_result.status,
                    price=sig_dict["intended_entry"],
                    strategy=sig_dict["strategy"],
                )
                continue

            # Try to accept into paper book
            signal = IntradayShortSignal(
                symbol=sym,
                signal_time=sig_dict["signal_time"],
                intended_entry=sig_dict["intended_entry"],
                stop_loss=sig_dict["stop_loss"],
                target=sig_dict["target"],
                strategy=sig_dict["strategy"],
                avg_value_cr=sig_dict["avg_value_cr"],
                spread=sig_dict["spread"],
                volume_ratio=sig_dict["volume_ratio"],
            )
            accepted, reason = trader.accept_signal(
                signal,
                shortability_allowed=True,
                shortability_reason="",
            )
            if accepted:
                _log.info(
                    f"[SHORT SIGNAL] {sym} @ ₹{sig_dict['intended_entry']:.2f} "
                    f"gap={sig_dict['gap_pct']:+.1f}% "
                    f"SL=₹{sig_dict['stop_loss']:.2f} T=₹{sig_dict['target']:.2f}"
                )
            else:
                _log.debug(f"[SKIP] {sym}: {reason}")

        # Simulate fills for accepted-but-unfilled positions using current quote
        for trade in trader.get_today_trades():
            if trade.simulated_fill > 0 or trade.closed:
                continue
            q = quotes.get(trade.symbol, {})
            next_open = float(q.get("last_price") or 0)
            spread    = trade.spread_at_signal
            if next_open > 0:
                trader.update_fill(trade.symbol, next_open, spread)

        # Check and log exit conditions
        for trade in trader.get_today_trades():
            if trade.closed or trade.simulated_fill <= 0:
                continue
            q         = quotes.get(trade.symbol, {})
            last      = float(q.get("last_price") or 0)
            day_high  = float((q.get("ohlc") or {}).get("high") or 0)
            day_low   = float((q.get("ohlc") or {}).get("low") or 0)

            if last <= 0:
                continue

            exit_reason = None
            if day_high >= trade.stop_loss:
                exit_reason = "stop_hit"
            elif day_low <= trade.target:
                exit_reason = "target_hit"

            if exit_reason:
                exit_price = trade.stop_loss if exit_reason == "stop_hit" else trade.target
                trader.update_exit(
                    trade.symbol, exit_price, now.strftime("%H:%M:%S"),
                    exit_reason, day_high=day_high, day_low=day_low,
                )
                result_pct = trade.pnl_after_costs
                _log.info(
                    f"[EXIT {exit_reason.upper()}] {trade.symbol} "
                    f"net P&L: {result_pct:+.2f}%"
                )

        # Log slippage alerts
        for alert in trader.get_slippage_alerts():
            _log.warning(f"[SLIPPAGE ALERT] {alert}")

        # Daily status
        status = trader.get_daily_status()
        if status["halted"]:
            _log.warning(f"[CIRCUIT BREAKER] {status['halt_reason']} — halting for today")
            break

        # Approach EOD — close all open positions
        if _market_close_soon():
            _log.info("Approaching market close — closing all open paper positions")
            eod_prices = {
                sym: float(quotes.get(sym, {}).get("last_price") or 0)
                for sym in [e["symbol"] for e in universe]
            }
            summary = trader.close_day(eod_prices)
            if summary:
                _log.info(summary)
            break

        prev_quotes = quotes
        time.sleep(scan_interval)

    _log.info("Session ended. Generating daily report...")
    print(reporter.daily_report())


# ---------------------------------------------------------------------------
# Historical simulation (dry-run against cached Kite minute data)
# ---------------------------------------------------------------------------

def run_simulate(date_str: str) -> None:
    """
    Simulate a past trading day using cached Kite 1-minute data.
    This lets you validate the paper trading layer against known outcomes.
    """
    from pkscreener.classes.ShortabilityValidator import ShortabilityValidator
    from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader, IntradayShortSignal
    from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator

    MINUTE_CACHE = BASE_DIR / "results" / "Data" / "kite_history_cache" / "minute"

    validator = ShortabilityValidator()
    validator.load_liquidity_map(LIQUIDITY_CSV)
    trader    = IntradayPaperTrader()
    reporter  = IntradayPaperReportGenerator(trader, validator)
    universe  = _load_universe()

    _log.info(f"Simulating {date_str} with {len(universe)} symbols from cache")

    signals_generated = 0
    signals_rejected  = 0

    for entry in universe:
        sym = entry["symbol"]
        cache_file = MINUTE_CACHE / f"{sym}_1minute.parquet"
        if not cache_file.exists():
            cache_file = MINUTE_CACHE / f"{sym}.parquet"
        if not cache_file.exists():
            continue

        try:
            import pandas as pd
            df = pd.read_parquet(str(cache_file))
            df.index = pd.to_datetime(df.index, utc=True)
            df = df[df.index.date == pd.Timestamp(date_str).date()]
            if df.empty:
                continue

            # Compute gap
            prev_close = float(df["close"].iloc[-1]) if len(df) > 0 else 0.0
            day_open   = float(df["open"].iloc[0])   if len(df) > 0 else 0.0
            if prev_close <= 0 or day_open <= 0:
                continue

            # We need actual prev day close — skip if can't compute
            all_df = pd.read_parquet(str(cache_file))
            all_df.index = pd.to_datetime(all_df.index, utc=True)
            all_df = all_df.sort_index()
            target_date   = pd.Timestamp(date_str, tz="Asia/Kolkata").date()
            prior_rows    = all_df[all_df.index.date < target_date]
            if prior_rows.empty:
                continue
            prev_close = float(prior_rows["close"].iloc[-1])
            gap_pct    = (day_open - prev_close) / prev_close * 100

            if not (ENTRY_GAP_MIN_PCT <= gap_pct <= ENTRY_GAP_MAX_PCT):
                continue

            # Signal at bar 2 (9:30 AM approx)
            if len(df) < 2:
                continue
            signal_bar   = df.iloc[1]
            signal_price = float(signal_bar["open"])
            signal_time  = str(signal_bar.name.strftime("%H:%M:%S"))

            # Validate shortability
            short_r = validator.validate(sym, avg_value_cr=entry["avg_value_cr"])
            if not short_r.allowed:
                validator.log_rejection(sym, signal_time, short_r.reason, short_r.status,
                                        price=signal_price, strategy=STRATEGY_NAME)
                signals_rejected += 1
                continue

            # Accept signal
            signal = IntradayShortSignal(
                symbol=sym,
                signal_time=signal_time,
                intended_entry=signal_price,
                stop_loss=round(signal_price * (1 + STOP_LOSS_PCT / 100), 2),
                target=round(signal_price * (1 - TARGET_PCT / 100), 2),
                strategy=STRATEGY_NAME,
                avg_value_cr=entry["avg_value_cr"],
                spread=signal_price * 0.001,
                volume_ratio=1.0,
            )
            accepted, _ = trader.accept_signal(signal, shortability_allowed=True)
            if not accepted:
                continue
            signals_generated += 1

            # Simulate fill at next bar open
            if len(df) > 2:
                next_bar_open = float(df.iloc[2]["open"])
                trader.update_fill(sym, next_bar_open)

            # Find exit in remaining bars
            entry_fill = trader._trades.get(sym)
            if not entry_fill or entry_fill.simulated_fill <= 0:
                continue

            fill_price = entry_fill.simulated_fill
            sl = entry_fill.stop_loss
            tp = entry_fill.target
            day_high = day_low = fill_price

            for _, bar in df.iloc[3:].iterrows():
                bar_high = float(bar["high"])
                bar_low  = float(bar["low"])
                day_high = max(day_high, bar_high)
                day_low  = min(day_low, bar_low)

                if bar_high >= sl:
                    trader.update_exit(sym, sl, str(bar.name.strftime("%H:%M:%S")),
                                       "stop_hit", day_high=day_high, day_low=day_low)
                    break
                if bar_low <= tp:
                    trader.update_exit(sym, tp, str(bar.name.strftime("%H:%M:%S")),
                                       "target_hit", day_high=day_high, day_low=day_low)
                    break
            else:
                # EOD exit
                eod = float(df.iloc[-1]["close"])
                trader.update_exit(sym, eod, "15:25:00", "eod",
                                   day_high=day_high, day_low=day_low)

        except Exception as exc:
            _log.debug(f"Simulation error for {sym}: {exc}")
            continue

    _log.info(f"Simulation complete: {signals_generated} signals, {signals_rejected} rejected")
    print(reporter.daily_report(date_str))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Intraday short paper trading session")
    parser.add_argument("--report",    action="store_true",  help="Print report only (no live session)")
    parser.add_argument("--days",      type=int, default=20, help="Days for cumulative report")
    parser.add_argument("--approval",  action="store_true",  help="Check approval criteria")
    parser.add_argument("--simulate",  type=str, default="", metavar="DATE",
                        help="Simulate a past day (YYYY-MM-DD) from Kite cache")
    args = parser.parse_args()

    if args.simulate:
        run_simulate(args.simulate)
        return

    from pkscreener.classes.ShortabilityValidator import ShortabilityValidator
    from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader
    from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator

    validator = ShortabilityValidator()
    validator.load_liquidity_map(LIQUIDITY_CSV)
    trader    = IntradayPaperTrader()
    reporter  = IntradayPaperReportGenerator(trader, validator)

    if args.approval:
        print(reporter.check_approval_criteria(days=args.days))
        return

    if args.report:
        if args.days > 1:
            print(reporter.cumulative_report(days=args.days))
        else:
            print(reporter.daily_report())
        return

    # Default: run live session
    if not _market_open():
        _log.info("Market is not open. Use --report or --simulate for offline use.")
        print(reporter.daily_report())
        return

    run_live_session(args)


if __name__ == "__main__":
    main()
