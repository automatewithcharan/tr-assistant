#!/usr/bin/env python3
"""
Wealth Basket — Monthly Plan Runner

Usage:
    python scripts/run_wealth_basket.py [options]

Options:
    --universe   nifty50 | nifty100 | nifty500   (default: nifty100)
    --budget     Monthly SIP amount in INR         (default: 200000)
    --no-cache   Force re-fetch all fundamentals
    --dry-run    Generate report only, do not save to DB

Output:
    results/Data/wealth_basket_reports/YYYY_MM_plan.md
    results/Data/wealth_basket_reports/YYYY_MM_plan.csv

Data trust: ALL FUNDAMENTALS ARE LOW TRUST (yfinance source).
Do not auto-execute trades based solely on this report.
Verify against NSE quarterly filings before committing capital.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from PKDevTools.classes import Archiver

from pkscreener.classes.BenchmarkTracker import (
    build_performance_snapshot,
    check_underperformance,
    compute_rolling_returns,
    fetch_benchmark_history,
    get_latest_nifty_values,
    update_benchmark_db,
)
from pkscreener.classes.FundamentalsDataFetcher import (
    fetch_fundamentals_batch,
    get_price_history,
    load_nifty500_universe,
)
from pkscreener.classes.WealthBasketAllocator import DEFAULT_BUDGET, build_allocation_plan
from pkscreener.classes.WealthBasketDB import WealthBasketDB
from pkscreener.classes.WealthBasketReporter import (
    generate_csv_report,
    generate_markdown_report,
    report_paths,
)
from pkscreener.classes.WealthBasketScorer import score_universe

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
_log = logging.getLogger("wealth_basket")

# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).parent.parent
_INDICES_DIR = _PROJECT_ROOT / "results" / "Indices"
_REPORTS_DIR = _PROJECT_ROOT / "results" / "Data" / "wealth_basket_reports"
_DB_PATH = str(Path(Archiver.get_user_outputs_dir()) / "wealth_basket.db")

_UNIVERSE_FILES = {
    "nifty50":  _INDICES_DIR / "ind_nifty50list.csv",
    "nifty100": _INDICES_DIR / "ind_nifty100list.csv",
    "nifty500": _INDICES_DIR / "ind_nifty500list.csv",
}

# ------------------------------------------------------------------
# Universe loading
# ------------------------------------------------------------------

def _load_universe(universe_name: str):
    csv_path = _UNIVERSE_FILES.get(universe_name)
    if not csv_path or not csv_path.exists():
        _log.warning("Universe file not found for '%s', falling back to nifty500", universe_name)
        csv_path = _INDICES_DIR / "ind_nifty500list.csv"
    pairs = load_nifty500_universe(str(csv_path))
    symbols = [s for s, _ in pairs]
    sector_map = {s: ind for s, ind in pairs}
    _log.info("Universe loaded: %d stocks from %s", len(symbols), csv_path.name)
    return symbols, sector_map


# ------------------------------------------------------------------
# Progress helpers
# ------------------------------------------------------------------

def _progress(done: int, total: int, sym: str) -> None:
    pct = int(done / total * 100)
    bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
    print(f"\r  [{bar}] {pct:3d}% ({done}/{total}) {sym:<20}", end="", flush=True)
    if done == total:
        print()


# ------------------------------------------------------------------
# Main pipeline
# ------------------------------------------------------------------

def run(
    universe_name: str = "nifty100",
    monthly_budget: float = DEFAULT_BUDGET,
    force_refresh: bool = False,
    dry_run: bool = False,
    use_screener: bool = False,
) -> str:
    """
    Full monthly wealth basket pipeline.

    Returns path to the generated markdown report.
    """
    plan_date = datetime.now(timezone.utc).date().isoformat()
    _log.info("=== Wealth Basket Monthly Run: %s ===", plan_date)
    _log.info("Universe: %s | Budget: Rs %.0f | DB: %s", universe_name, monthly_budget, _DB_PATH)

    db = WealthBasketDB(_DB_PATH)

    # ------------------------------------------------------------------
    # Step 1: Load stock universe
    # ------------------------------------------------------------------
    _log.info("Step 1/6: Loading universe...")
    symbols, sector_map = _load_universe(universe_name)

    # ------------------------------------------------------------------
    # Step 2: Fetch fundamentals (with cache)
    # ------------------------------------------------------------------
    max_age = 0 if force_refresh else 30
    _log.info("Step 2/6: Fetching fundamentals for %d stocks (cache %dd, screener=%s)...",
              len(symbols), max_age, use_screener)
    fundamentals_map = fetch_fundamentals_batch(
        symbols=symbols,
        delay_sec=0.4,
        db=db,
        max_age_days=max_age,
        progress_callback=_progress,
        use_screener=use_screener,
    )
    fetched_ok = sum(1 for f in fundamentals_map.values() if f.get("trust_level") != "FAILED")
    _log.info("Fundamentals: %d/%d stocks fetched successfully", fetched_ok, len(symbols))

    # ------------------------------------------------------------------
    # Step 3: Fetch price history for technical scoring
    # ------------------------------------------------------------------
    _log.info("Step 3/6: Fetching 1-year price history...")
    price_history_map = {}
    for i, sym in enumerate(symbols):
        df = get_price_history(sym, period="1y")
        price_history_map[sym] = df
        if (i + 1) % 50 == 0 or (i + 1) == len(symbols):
            _log.info("  Price history: %d/%d", i + 1, len(symbols))

    # ------------------------------------------------------------------
    # Step 4: Score all stocks
    # ------------------------------------------------------------------
    _log.info("Step 4/6: Scoring %d stocks...", len(symbols))
    current_holdings = db.get_holdings(active_only=True)
    held_symbols = [h["symbol"] for h in current_holdings]

    scores = score_universe(
        symbols=symbols,
        fundamentals_map=fundamentals_map,
        price_history_map=price_history_map,
        held_symbols=held_symbols,
        sector_map=sector_map,
    )

    # Persist scores to DB
    if not dry_run:
        for s in scores:
            db.save_score(
                score_date=plan_date,
                symbol=s.symbol,
                sector=s.sector,
                quality=s.quality_score,
                momentum=s.momentum_score,
                valuation=s.valuation_score,
                combined=s.combined_score,
                trust=s.trust_level,
                action=s.action,
                reason=s.action_reason,
                risks=s.risks,
                confidence=s.confidence,
                holding_period=s.holding_period,
            )

    add_count = sum(1 for s in scores if s.action in ("ADD", "ADD_NEW"))
    _log.info("Scoring complete: %d ADD, %d HOLD, %d WATCH, %d REDUCE/EXIT",
              add_count,
              sum(1 for s in scores if s.action == "HOLD"),
              sum(1 for s in scores if s.action == "WATCH"),
              sum(1 for s in scores if s.action in ("REDUCE", "EXIT")),
              )

    # ------------------------------------------------------------------
    # Step 5: Benchmark comparison + underperformance check
    # ------------------------------------------------------------------
    _log.info("Step 5/6: Fetching benchmarks and loading portfolio history...")
    performance = None
    underperf_warning = False
    underperf_msg = ""
    bench_history = {"nifty50": [], "nifty500": []}
    try:
        if not dry_run:
            update_benchmark_db(db)

        bench_history = fetch_benchmark_history(period="2y")

        # Load actual portfolio snapshots for basket return calculation
        basket_snapshots = db.get_portfolio_snapshots(limit=24)
        snapshot_count   = len(basket_snapshots)
        _log.info("Portfolio snapshots: %d months of history", snapshot_count)

        rolling = compute_rolling_returns(bench_history, basket_snapshots)
        underperf_warning, underperf_msg = check_underperformance(rolling, window="6m")

        if not dry_run:
            performance = build_performance_snapshot(rolling, db, current_holdings)
        else:
            performance = rolling
            performance["calc_date"] = plan_date

        if underperf_warning:
            _log.warning("UNDERPERFORMANCE WARNING: %s", underperf_msg)
        else:
            _log.info("Underperformance check: %s",
                      underperf_msg if underperf_msg else "OK")
    except Exception as exc:
        _log.warning("Benchmark step failed (non-fatal): %s", exc)
        basket_snapshots = []
        snapshot_count   = 0

    # ------------------------------------------------------------------
    # Step 6: Build allocation plan and generate reports
    # ------------------------------------------------------------------
    _log.info("Step 6/6: Building allocation plan and generating reports...")

    # Build current prices dict from price history
    current_prices = {}
    for sym, df in price_history_map.items():
        if df is not None and len(df) > 0:
            try:
                current_prices[sym] = float(df["Close"].squeeze().dropna().iloc[-1])
            except Exception:
                pass

    # Check if backtest results exist
    _backtest_path = _REPORTS_DIR / "backtest_results.csv"
    backtest_exists = _backtest_path.exists()

    plan = build_allocation_plan(
        scores=scores,
        current_holdings=current_holdings,
        monthly_budget=monthly_budget,
        plan_date=plan_date,
        underperformance_warning=underperf_warning,
        warning_message=underperf_msg,
        current_prices=current_prices,
        snapshot_count=snapshot_count,
        backtest_exists=backtest_exists,
    )

    # Update holding prices
    if not dry_run and current_prices:
        db.update_holding_prices(current_prices)

    # Log cashflow
    if not dry_run:
        db.log_cashflow(
            amount=monthly_budget,
            deployed=plan.total_allocated,
            held_cash=plan.held_cash,
        )

    # Save monthly portfolio snapshot for basket performance tracking
    if not dry_run:
        _active_holdings = db.get_holdings(active_only=True)
        _holdings_value  = sum(float(h.get("current_value") or h.get("total_invested") or 0)
                               for h in _active_holdings)
        _total_invested  = sum(float(h.get("total_invested") or 0) for h in _active_holdings)
        _basket_total    = _holdings_value + plan.held_cash
        _cumulative      = db.get_cumulative_invested()

        # Get today's NIFTY values from benchmark history
        _nifty_vals = get_latest_nifty_values(bench_history)

        db.save_portfolio_snapshot(
            snapshot_date=plan_date,
            total_invested=_total_invested,
            holdings_value=_holdings_value,
            cash_held=plan.held_cash,
            basket_total=_basket_total,
            nifty50_value=_nifty_vals.get("nifty50"),
            nifty500_value=_nifty_vals.get("nifty500"),
            monthly_sip=monthly_budget,
            cumulative_invested=_cumulative,
            holdings=[{
                "symbol": h["symbol"],
                "qty": float(h.get("total_qty") or 0),
                "avg_cost": float(h.get("avg_cost") or 0),
                "current_price": float(h.get("current_price") or 0),
                "current_value": float(h.get("current_value") or 0),
            } for h in _active_holdings],
        )
        _log.info("Portfolio snapshot saved: basket total = Rs %.0f", _basket_total)

    # Generate reports
    md_path, csv_path = report_paths(str(_REPORTS_DIR), plan_date)
    generate_markdown_report(plan, performance, current_holdings, md_path)
    generate_csv_report(plan, csv_path)

    # ------------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------------
    print()
    print("=" * 60)
    print(f"  WEALTH BASKET MONTHLY PLAN — {plan_date}")
    print("=" * 60)
    print(f"  Budget:     ₹{monthly_budget:>10,.0f}")
    print(f"  Deploying:  ₹{plan.total_allocated:>10,.0f}  ({len(plan.items)} stocks)")
    print(f"  Held cash:  ₹{plan.held_cash:>10,.0f}")
    if underperf_warning:
        print(f"  ⚠️  UNDERPERFORMANCE WARNING ACTIVE")
    print()

    if plan.items:
        print("  ALLOCATION:")
        for item in plan.items:
            new_tag = " [NEW]" if item.is_new_position else ""
            tier_tag = f"  [{item.automation_tier}]"
            print(f"    {item.symbol:<16} ₹{item.amount:>9,.0f}  score={item.score:.0f}"
                  f"{tier_tag}{new_tag}")
    else:
        print("  No allocation — full budget held as cash")
        if plan.cash_reason:
            print(f"  Reason: {plan.cash_reason}")

    if plan.reduce_exit_items:
        print()
        print(f"  ACTION REQUIRED ({len(plan.reduce_exit_items)} stocks):")
        for s in plan.reduce_exit_items:
            print(f"    {s.symbol:<16} → {s.action:<8} score={s.combined_score:.0f}")

    print()
    print(f"  Report: {md_path}")
    print(f"  CSV:    {csv_path}")
    print("=" * 60)

    _log.info("Run complete. Report: %s", md_path)
    return md_path


# ------------------------------------------------------------------
# CLI entry point
# ------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Wealth Basket monthly plan generator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--universe",
        choices=["nifty50", "nifty100", "nifty500"],
        default="nifty100",
        help="Stock universe to screen (default: nifty100)",
    )
    parser.add_argument(
        "--budget",
        type=float,
        default=DEFAULT_BUDGET,
        help=f"Monthly SIP budget in INR (default: {DEFAULT_BUDGET:,.0f})",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force re-fetch all fundamentals, ignoring 30-day cache",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate report only, do not write to DB",
    )
    parser.add_argument(
        "--use-screener",
        action="store_true",
        help="Enable screener.in as second data source (slower, improves trust level)",
    )
    args = parser.parse_args()

    report_path = run(
        universe_name=args.universe,
        monthly_budget=args.budget,
        force_refresh=args.no_cache,
        dry_run=args.dry_run,
        use_screener=args.use_screener,
    )
    print(f"\nDone. Open report: {report_path}")


if __name__ == "__main__":
    main()
