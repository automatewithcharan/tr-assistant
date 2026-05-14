#!/usr/bin/env python3
"""
EODPaperRunner — automated end-of-day paper validation runner.

Runs after NSE market close (4:15 PM IST).  Responsibilities:
  - Detect whether today is a trading day
  - Prevent duplicate runs (lock file per date)
  - Simulate today's paper trading from Kite 5-min cache
  - Generate daily report, updated cumulative, updated stage report
  - Check alert conditions and return them for notification
  - Store all outputs with timestamps; never overwrite historical files

SAFETY: read-only Kite access only. No real orders ever placed.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BASE_DIR))

RESULTS_DIR  = BASE_DIR / "results" / "Data"
LIQUIDITY_CSV = str(RESULTS_DIR / "kite_intraday_forensic_audit_shortability.csv")
CACHE_FOLDER = RESULTS_DIR / "kite_history_backfill" / "5minute"

# Alert thresholds
ALERT_SLIPPAGE_MULT      = 2.0    # alert if avg slip > 2× backtest assumed
ALERT_MIN_SIGNALS_PER_DAY = 2     # alert if fewer than 2 signals generated
ALERT_DD_WARN_PCT         = 3.0   # warn if cumulative drawdown > 3%
ALERT_PAPER_VS_BT_RATIO   = 0.30  # alert if paper return < 30% of backtest return

BACKTEST_DAILY_RETURN = 1.7085    # baseline backtest daily return % (for divergence check)
BACKTEST_SLIPPAGE_PCT = 0.10      # backtest assumed slippage (for deterioration check)

STAGE1_DAILY_TARGET = 0.30        # Stage 1 minimum daily return target


class AlertCondition:
    """A detected alert condition with severity and description."""

    SEVERITY_INFO     = "INFO"
    SEVERITY_WARN     = "WARN"
    SEVERITY_SHUTDOWN = "SHUTDOWN"

    def __init__(self, code: str, severity: str, message: str, value: float = 0.0):
        self.code     = code
        self.severity = severity
        self.message  = message
        self.value    = value

    def __repr__(self) -> str:
        return f"[{self.severity}] {self.code}: {self.message}"


class EODRunResult:
    """Results from a single EOD run."""

    def __init__(self):
        self.date_str:          str           = ""
        self.is_trading_day:    bool          = False
        self.already_ran:       bool          = False
        self.skipped_reason:    str           = ""
        self.trades_today:      int           = 0
        self.signals_today:     int           = 0
        self.wins_today:        int           = 0
        self.net_pnl_today:     float         = 0.0
        self.avg_slippage_today: float        = 0.0
        self.cumulative_days:   int           = 0
        self.cumulative_trades: int           = 0
        self.cumul_avg_daily:   float         = 0.0
        self.cumul_drawdown:    float         = 0.0
        self.stage_verdict:     str           = "Pre-Stage 1"
        self.alerts:            List[AlertCondition] = []
        self.report_paths:      Dict[str, str] = {}
        self.error:             Optional[str] = None


class EODPaperRunner:
    """
    Orchestrates daily paper-trading evidence collection and reporting.

    Usage:
        runner = EODPaperRunner()
        result = runner.run(date_str="2026-05-14")   # or None for today
        if result.alerts:
            for alert in result.alerts:
                print(alert)
    """

    def __init__(
        self,
        paper_dir:    Optional[Path] = None,
        report_dir:   Optional[Path] = None,
        strategy:     str = "late_day_reversal",
        lookback_days: int = 60,
    ):
        from PKDevTools.classes import Archiver
        self.paper_dir  = paper_dir  or (Path(Archiver.get_user_data_dir()) / "intraday_paper")
        self.report_dir = report_dir or (RESULTS_DIR / "paper_reports")
        self.strategy   = strategy
        self.lookback_days = lookback_days

        self.paper_dir.mkdir(parents=True, exist_ok=True)
        self.report_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        date_str: Optional[str] = None,
        force: bool = False,
        verbose: bool = True,
    ) -> EODRunResult:
        """
        Run the EOD paper validation for the given date (default: today).

        Args:
            date_str: YYYY-MM-DD to simulate (default: today in IST)
            force:    bypass duplicate-run guard
            verbose:  print progress to stdout
        """
        result = EODRunResult()
        result.date_str = date_str or date.today().isoformat()

        try:
            self._run_impl(result, force=force, verbose=verbose)
        except Exception as exc:
            result.error = str(exc)
            result.alerts.append(AlertCondition(
                code="EOD_RUNNER_ERROR",
                severity=AlertCondition.SEVERITY_SHUTDOWN,
                message=f"EOD runner crashed: {exc}",
            ))

        return result

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    def _run_impl(self, result: EODRunResult, force: bool, verbose: bool) -> None:
        date_str = result.date_str

        # 1. Trading day check
        if not self._is_trading_day(date_str):
            result.is_trading_day = False
            result.skipped_reason = f"{date_str} is not a trading day (weekend or holiday)"
            if verbose:
                print(f"  [EOD] Skipping {date_str}: {result.skipped_reason}")
            return
        result.is_trading_day = True

        # 2. Duplicate-run guard
        if not force and self._already_ran(date_str):
            result.already_ran = True
            result.skipped_reason = f"EOD run for {date_str} already completed"
            if verbose:
                print(f"  [EOD] Already ran for {date_str} — use --force to re-run")
            return

        if verbose:
            print(f"\n{'='*66}")
            print(f"  EOD PAPER VALIDATION — {date_str}")
            print(f"  Strategy: {self.strategy}")
            print(f"{'='*66}\n")

        # 3. Load universe and validator
        universe, universe_map = self._load_universe()
        if not universe:
            raise RuntimeError("Liquidity CSV not found. Run forensic_audit_intraday.py first.")

        from pkscreener.classes.ShortabilityValidator import ShortabilityValidator
        validator = ShortabilityValidator()
        validator.load_liquidity_map(LIQUIDITY_CSV)
        validator._cache_date = date.today().isoformat()   # simulation mode — skip NSE HTTP

        # 4. Load 5-min features
        if verbose:
            print("  Loading 5-minute features …", end=" ", flush=True)
        symbols   = [e["symbol"] for e in universe]
        day_cache = self._load_day(symbols, date_str)
        if verbose:
            print(f"done ({len(day_cache)} symbol-days loaded)")

        if not day_cache:
            result.skipped_reason = f"No 5-min data available for {date_str}"
            result.alerts.append(AlertCondition(
                code="NO_DATA",
                severity=AlertCondition.SEVERITY_WARN,
                message=f"No 5-min Kite cache available for {date_str}",
            ))
            self._write_lock(date_str)
            return

        # 5. Simulate the day
        from scripts.simulate_paper_20days import simulate_day, persist_day, CFG
        trades = simulate_day(date_str, day_cache, universe_map, validator, verbose=verbose)
        persist_day(date_str, trades)

        result.trades_today     = len(trades)
        result.wins_today       = sum(1 for t in trades if t["pnl_after_costs"] > 0)
        result.net_pnl_today    = sum(t["pnl_after_costs"] for t in trades)
        result.avg_slippage_today = (
            sum(t["fill_slippage_pct"] for t in trades) / len(trades) if trades else 0.0
        )
        result.signals_today = result.trades_today + sum(
            validator.get_rejection_summary(date_str).values()
        )

        if verbose:
            wr = result.wins_today / result.trades_today * 100 if result.trades_today else 0
            print(f"\n  Today:  {result.trades_today} trades | WR {wr:.0f}% | "
                  f"net {result.net_pnl_today:+.3f}% | slip {result.avg_slippage_today:+.4f}%")

        # 6. Generate daily report
        daily_report = self._generate_daily_report(date_str, validator)
        daily_path   = self.report_dir / f"daily_report_{date_str}.txt"
        daily_path.write_text(daily_report)
        result.report_paths["daily"] = str(daily_path)

        # 7. Cumulative report
        cumul_report, cumul_stats = self._generate_cumulative_report(validator)
        result.cumulative_days   = cumul_stats.get("days",    0)
        result.cumulative_trades = cumul_stats.get("trades",  0)
        result.cumul_avg_daily   = cumul_stats.get("avg_daily", 0.0)
        result.cumul_drawdown    = cumul_stats.get("max_dd",    0.0)

        cumul_path = self.report_dir / f"cumulative_report_{date_str}.txt"
        cumul_path.write_text(cumul_report)
        result.report_paths["cumulative"] = str(cumul_path)

        # 8. Stage validator
        from pkscreener.classes.StageValidator import StageValidator
        sv     = StageValidator()
        sv_res = sv.evaluate(self.strategy, days=self.lookback_days, target_stage=1)
        result.stage_verdict = (
            f"Stage {sv_res.current_stage}" if sv_res.ready else "Pre-Stage 1"
        )
        stage_report = StageValidator.format_report(sv_res)
        stage_path   = self.report_dir / f"stage_report_{date_str}.txt"
        stage_path.write_text(stage_report)
        result.report_paths["stage"] = str(stage_path)

        if verbose:
            print(stage_report)

        # 9. Alert conditions
        self._check_alerts(result, sv_res)

        # 10. Write EOD summary
        self._write_eod_summary(result)

        # 11. Mark as done
        self._write_lock(date_str)

        if verbose:
            self._print_alert_summary(result)

    # ------------------------------------------------------------------
    # Alert logic
    # ------------------------------------------------------------------

    def _check_alerts(self, result: EODRunResult, sv_res) -> None:
        alerts = result.alerts

        # Signal collapse
        if result.signals_today < ALERT_MIN_SIGNALS_PER_DAY:
            alerts.append(AlertCondition(
                code="SIGNAL_COLLAPSE",
                severity=AlertCondition.SEVERITY_WARN,
                message=(
                    f"Only {result.signals_today} signal(s) on {result.date_str}. "
                    "Strategy may have stopped firing — check regime."
                ),
                value=float(result.signals_today),
            ))

        # Slippage deterioration
        if result.avg_slippage_today > ALERT_SLIPPAGE_MULT * BACKTEST_SLIPPAGE_PCT:
            alerts.append(AlertCondition(
                code="SLIPPAGE_DETERIORATION",
                severity=AlertCondition.SEVERITY_WARN,
                message=(
                    f"Avg slippage {result.avg_slippage_today:.4f}% is "
                    f"{result.avg_slippage_today / BACKTEST_SLIPPAGE_PCT:.1f}× backtest assumed "
                    f"({BACKTEST_SLIPPAGE_PCT:.2f}%)."
                ),
                value=result.avg_slippage_today,
            ))

        # Drawdown approaching shutdown
        if result.cumul_drawdown >= ALERT_DD_WARN_PCT:
            severity = (
                AlertCondition.SEVERITY_SHUTDOWN
                if result.cumul_drawdown >= 5.0
                else AlertCondition.SEVERITY_WARN
            )
            alerts.append(AlertCondition(
                code="DRAWDOWN_WARNING" if severity == AlertCondition.SEVERITY_WARN else "DRAWDOWN_SHUTDOWN",
                severity=severity,
                message=(
                    f"Cumulative drawdown {result.cumul_drawdown:.2f}% — "
                    + ("SHUTDOWN threshold reached (5%)." if severity == AlertCondition.SEVERITY_SHUTDOWN
                       else f"approaching 5% shutdown threshold.")
                ),
                value=result.cumul_drawdown,
            ))

        # Paper vs backtest divergence
        if result.cumulative_days >= 10 and result.cumul_avg_daily < (
            BACKTEST_DAILY_RETURN * ALERT_PAPER_VS_BT_RATIO
        ):
            alerts.append(AlertCondition(
                code="PAPER_BACKTEST_DIVERGENCE",
                severity=AlertCondition.SEVERITY_WARN,
                message=(
                    f"Paper avg daily {result.cumul_avg_daily:+.3f}% is below "
                    f"{ALERT_PAPER_VS_BT_RATIO*100:.0f}% of backtest ({BACKTEST_DAILY_RETURN:+.3f}%). "
                    "Either backtest is overfitted or live conditions differ significantly."
                ),
                value=result.cumul_avg_daily,
            ))

        # Stage blocking — below Stage 1 target after sufficient data
        if result.cumulative_days >= 20 and result.cumul_avg_daily < STAGE1_DAILY_TARGET:
            alerts.append(AlertCondition(
                code="BELOW_STAGE1_TARGET",
                severity=AlertCondition.SEVERITY_WARN,
                message=(
                    f"After {result.cumulative_days} days, avg daily {result.cumul_avg_daily:+.3f}% "
                    f"is below Stage 1 target ({STAGE1_DAILY_TARGET:+.2f}%). "
                    "Do not tune parameters — investigate structurally."
                ),
                value=result.cumul_avg_daily,
            ))

        # Hard shutdown: all trades are losses for 3+ consecutive days
        recent_daily = self._get_recent_daily_pnl(days=3)
        if len(recent_daily) >= 3 and all(p < 0 for p in recent_daily[-3:]):
            alerts.append(AlertCondition(
                code="CONSECUTIVE_LOSS_DAYS",
                severity=AlertCondition.SEVERITY_SHUTDOWN,
                message=(
                    f"3 consecutive losing days: "
                    + ", ".join(f"{p:+.3f}%" for p in recent_daily[-3:])
                    + ". Review strategy urgently before continuing."
                ),
                value=min(recent_daily[-3:]),
            ))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_trading_day(self, date_str: str) -> bool:
        """Return True if date_str is a likely NSE trading day (Mon–Fri)."""
        d = datetime.strptime(date_str, "%Y-%m-%d")
        if d.weekday() >= 5:   # Saturday = 5, Sunday = 6
            return False
        # Holiday list: could be extended; for now weekend check only
        KNOWN_NSE_HOLIDAYS_2026 = {
            "2026-01-26", "2026-03-25", "2026-04-02", "2026-04-06",
            "2026-04-10", "2026-04-14", "2026-05-01", "2026-08-15",
            "2026-10-02", "2026-10-21", "2026-11-04", "2026-12-25",
        }
        return date_str not in KNOWN_NSE_HOLIDAYS_2026

    def _already_ran(self, date_str: str) -> bool:
        return self._lock_path(date_str).exists()

    def _lock_path(self, date_str: str) -> Path:
        return self.paper_dir / f".eod_ran_{date_str}"

    def _write_lock(self, date_str: str) -> None:
        self._lock_path(date_str).write_text(
            datetime.utcnow().isoformat() + "Z\n"
        )

    def _load_universe(self) -> Tuple[List[Dict], Dict[str, float]]:
        rows = []
        if not os.path.isfile(LIQUIDITY_CSV):
            return rows, {}
        with open(LIQUIDITY_CSV) as f:
            for r in csv.DictReader(f):
                liq = float(r.get("avg_value20_cr_median") or 0)
                if liq >= 25.0:
                    rows.append({"symbol": r["symbol"].strip().upper(), "avg_value_cr": liq})
        universe_map = {e["symbol"]: e["avg_value_cr"] for e in rows}
        return rows, universe_map

    def _find_cache_folder(self) -> Optional[Path]:
        """Return the most complete 5-minute cache folder (most CSVs)."""
        best: Optional[Path] = None
        best_count = 0
        for folder in CACHE_FOLDER.iterdir():
            if not folder.is_dir():
                continue
            count = sum(1 for _ in folder.glob("*.csv"))
            if count > best_count:
                best_count = count
                best = folder
        return best

    def _load_day(self, symbols: List[str], date_str: str):
        """Load day_cache for a single date, with rolling warmup."""
        import warnings
        warnings.filterwarnings("ignore")

        cache_folder = self._find_cache_folder()
        if cache_folder is None:
            return []

        name   = cache_folder.name
        parts  = name.split("_")
        if len(parts) != 2:
            return []
        cache_start, cache_end = parts[0], parts[1]

        import pandas as pd
        from scripts.research_kite_intraday import (
            read_intraday, add_intraday_features, prepare_day_cache,
        )

        nifty = read_intraday("NIFTY 50", "5minute", cache_start, cache_end)
        parts_frames = []
        for sym in symbols:
            df = read_intraday(sym, "5minute", cache_start, cache_end)
            if not df.empty:
                parts_frames.append(df)
        if not parts_frames:
            return []

        raw      = pd.concat(parts_frames, ignore_index=True)
        features = add_intraday_features(raw, nifty)
        features = features.dropna(
            subset=["prev_close", "gap_pct", "avg_value20_cr", "vwap"]
        ).copy()

        # Filter to the target date only
        target_ts = pd.Timestamp(date_str)
        features  = features[features["datetime"].dt.normalize() == target_ts].copy()
        return prepare_day_cache(features)

    def _generate_daily_report(self, date_str: str, validator) -> str:
        try:
            from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader
            from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator
            trader   = IntradayPaperTrader()
            reporter = IntradayPaperReportGenerator(trader, validator)
            return reporter.daily_report(date_str)
        except Exception as exc:
            return f"[ERROR generating daily report for {date_str}: {exc}]"

    def _generate_cumulative_report(self, validator) -> Tuple[str, Dict]:
        stats: Dict = {}
        try:
            from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader
            from pkscreener.classes.IntradayPaperReport import IntradayPaperReportGenerator
            trader   = IntradayPaperTrader()
            reporter = IntradayPaperReportGenerator(trader, validator)
            text     = reporter.cumulative_report(days=self.lookback_days)

            # Extract key stats from raw trades
            trades = trader.load_recent_trades(days=self.lookback_days)
            if trades:
                import pandas as pd, numpy as np
                df = pd.DataFrame(trades)
                df = df[df.get("strategy", pd.Series()) == self.strategy] if "strategy" in df.columns else df

                daily: Dict = {}
                for _, row in df.iterrows():
                    daily.setdefault(row.get("date", ""), []).append(
                        float(row.get("pnl_after_costs", 0))
                    )
                daily_avgs = [sum(v) / len(v) for v in daily.values() if v]
                avg_daily  = float(np.mean(daily_avgs)) if daily_avgs else 0.0

                # Max drawdown over daily cumulative returns
                daily_returns = [sum(v) for v in daily.values()]
                cumsum = np.cumsum(daily_returns)
                running_max = np.maximum.accumulate(cumsum)
                drawdowns   = running_max - cumsum
                max_dd      = float(np.max(drawdowns)) if len(drawdowns) else 0.0

                stats = {
                    "days":      len(daily),
                    "trades":    len(df),
                    "avg_daily": avg_daily,
                    "max_dd":    max_dd,
                }
            return text, stats
        except Exception as exc:
            return f"[ERROR generating cumulative report: {exc}]", stats

    def _get_recent_daily_pnl(self, days: int = 5) -> List[float]:
        """Return list of net daily P&L (sum across trades) for last N days."""
        try:
            from pkscreener.classes.IntradayPaperTrader import IntradayPaperTrader
            trader = IntradayPaperTrader()
            trades = trader.load_recent_trades(days=days)
            if not trades:
                return []
            daily: Dict = {}
            for t in trades:
                d = t.get("date", "")
                daily.setdefault(d, []).append(float(t.get("pnl_after_costs", 0)))
            return [sum(v) for v in sorted(daily.items(), key=lambda x: x[0])]  # type: ignore
        except Exception:
            return []

    def _write_eod_summary(self, result: EODRunResult) -> None:
        """Append a one-line summary to the EOD history log."""
        log_path = self.report_dir / "eod_history.csv"
        is_new   = not log_path.exists()
        with open(log_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "date", "trades", "wins", "net_pnl_pct", "avg_slip_pct",
                "cumul_days", "cumul_trades", "cumul_avg_daily", "cumul_drawdown",
                "stage_verdict", "alerts",
            ])
            if is_new:
                writer.writeheader()
            writer.writerow({
                "date":             result.date_str,
                "trades":           result.trades_today,
                "wins":             result.wins_today,
                "net_pnl_pct":      round(result.net_pnl_today, 4),
                "avg_slip_pct":     round(result.avg_slippage_today, 4),
                "cumul_days":       result.cumulative_days,
                "cumul_trades":     result.cumulative_trades,
                "cumul_avg_daily":  round(result.cumul_avg_daily, 4),
                "cumul_drawdown":   round(result.cumul_drawdown, 4),
                "stage_verdict":    result.stage_verdict,
                "alerts":           "|".join(a.code for a in result.alerts),
            })

    def _print_alert_summary(self, result: EODRunResult) -> None:
        if not result.alerts:
            print("\n  [EOD] No alerts.")
            return
        print(f"\n  {'─'*64}")
        print(f"  ALERTS ({len(result.alerts)})")
        print(f"  {'─'*64}")
        for a in result.alerts:
            print(f"  [{a.severity:8s}] {a.code}: {a.message}")
        print(f"  {'─'*64}\n")
