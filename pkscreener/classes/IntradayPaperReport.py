"""
Daily report generator and approval-criteria checker for the intraday
short-only paper trading validation programme.

Report sections (required 5):
  1. Signals generated / rejected (with breakdown by rejection reason)
  2. Simulated trades summary
  3. Fill quality (slippage distribution, spread, liquidity at signal)
  4. P&L summary (gross, after-cost, daily, cumulative)
  5. Slippage vs backtest assumptions
  6. Broker restriction / shortability issues

Approval gate (required 6):
  Minimum 20 trading days
  At least 200 simulated trades
  Positive net P&L after realistic costs
  Max drawdown < 5 %
  Live slippage within 2× backtest assumed range
  No material shortability / rejection problem ( > 30 % rejection rate flags concern)
"""

from __future__ import annotations

import csv
import glob
import logging
import math
import os
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

from pkscreener.classes.IntradayPaperTrader import (
    BACKTEST_SLIPPAGE_PCT,
    BACKTEST_FEE_PCT,
    SLIPPAGE_ALERT_MULT,
    IntradayPaperTrader,
)
from pkscreener.classes.ShortabilityValidator import ShortabilityValidator

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Approval criteria thresholds
# ---------------------------------------------------------------------------

APPROVAL_MIN_DAYS         = 20
APPROVAL_MIN_TRADES       = 200
APPROVAL_MAX_DRAWDOWN_PCT = 5.0
APPROVAL_MAX_REJECTION_RATE = 30.0     # % — more than this signals shortability problem
APPROVAL_SLIPPAGE_MULT    = 2.0        # live slippage must be ≤ 2× backtest assumed


# ---------------------------------------------------------------------------
# Daily report dataclass
# ---------------------------------------------------------------------------

@dataclass
class DailyReportData:
    date:                str
    # Signals
    signals_generated:   int
    signals_rejected:    int
    rejection_breakdown: Dict[str, int]   # status → count
    # Trades
    simulated_trades:    int
    open_trades:         int
    # Fill quality
    avg_slippage_pct:    float
    median_slippage_pct: float
    pct_bad_fills:       float           # % trades where slippage > BAD_FILL_THRESHOLD
    slippage_alerts:     int             # fills > 2× backtest assumption
    # P&L
    gross_pnl_pct:       float
    net_pnl_pct:         float           # after costs
    win_rate:            float
    avg_winner_pct:      float
    avg_loser_pct:       float
    # Stop / target
    stop_hit_rate:       float
    target_hit_rate:     float
    # vs backtest
    backtest_slippage_assumed: float
    live_slippage_excess: float          # live_avg - backtest_assumed (negative = better)
    # Shortability
    shortability_rejection_count: int
    asm_gsm_blocked_count:        int


# ---------------------------------------------------------------------------
# Report generator
# ---------------------------------------------------------------------------

class IntradayPaperReportGenerator:
    """
    Generates daily and cumulative reports for the paper trading programme.

    Usage:
        rg = IntradayPaperReportGenerator(trader, validator)
        daily_text  = rg.daily_report("2026-05-13")
        cumul_text  = rg.cumulative_report(days=20)
        approval    = rg.check_approval_criteria(days=20)
    """

    def __init__(
        self,
        trader:    IntradayPaperTrader,
        validator: ShortabilityValidator,
    ) -> None:
        self._trader    = trader
        self._validator = validator

    # ------------------------------------------------------------------
    # Daily report
    # ------------------------------------------------------------------

    def daily_report(self, date_str: Optional[str] = None) -> str:
        """Return a formatted daily report string."""
        target = date_str or date.today().isoformat()
        trades = self._trader.load_trades_for_date(target)
        rejections = self._validator.load_rejections_for_date(target)

        data = self._compute_daily(target, trades, rejections)
        return self._format_daily(data)

    # ------------------------------------------------------------------
    # Cumulative report
    # ------------------------------------------------------------------

    def cumulative_report(self, days: int = 20) -> str:
        """Return a formatted multi-day cumulative report."""
        all_trades = self._trader.load_recent_trades(days)
        if not all_trades:
            return "No intraday paper trades found for the requested period."

        closed = [t for t in all_trades if _b(t.get("closed")) and _f(t.get("pnl_after_costs")) != 0.0]
        if not closed:
            n_open = len(all_trades)
            return f"{n_open} trades logged but none closed yet. Keep collecting data."

        pnls   = [_f(t["pnl_after_costs"]) for t in closed]
        n      = len(closed)
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        # Win metrics
        wr       = len(wins) / n * 100 if n else 0.0
        avg_pnl  = sum(pnls) / n if n else 0.0
        avg_win  = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0
        expectancy = (wr / 100 * avg_win) + ((1 - wr / 100) * avg_loss)

        # Slippage analysis
        slippages = [_f(t.get("fill_slippage_pct")) for t in closed]
        avg_slip = sum(slippages) / len(slippages) if slippages else 0.0
        med_slip = sorted(slippages)[len(slippages) // 2] if slippages else 0.0
        pct_bad  = sum(1 for s in slippages if s > 0.25) / len(slippages) * 100 if slippages else 0.0

        # Daily P&L for Sharpe + drawdown
        daily_map: Dict[str, List[float]] = {}
        for t in closed:
            d = t.get("date", "")
            if d:
                daily_map.setdefault(d, []).append(_f(t["pnl_after_costs"]))

        daily_returns = [sum(v) / len(v) for v in daily_map.values()]
        n_days = len(daily_returns)
        sharpe = _sharpe(daily_returns)
        max_dd = _max_drawdown(pnls)

        # Rejection stats (all dates in range)
        rejection_rows = self._load_rejections_for_range(days)
        n_rejected = len(rejection_rows)
        n_asm_gsm  = sum(1 for r in rejection_rows if r.get("status") in {"asm", "gsm"})

        # Slippage excess
        slip_excess = avg_slip - BACKTEST_SLIPPAGE_PCT

        # Per-strategy breakdown
        from collections import Counter
        strat_counter  = Counter(t.get("strategy", "unknown") for t in closed)
        strat_lines    = [
            f"  {s:30s}: {c:3d} trades" for s, c in strat_counter.most_common()
        ]

        # Per-day P&L
        day_lines = []
        for d in sorted(daily_map.keys()):
            dp = daily_map[d]
            dw = sum(1 for p in dp if p > 0)
            day_lines.append(
                f"  {d}: {len(dp):3d} trades | WR {dw/len(dp)*100:5.1f}% | "
                f"Avg {sum(dp)/len(dp):+.2f}%"
            )

        report = (
            f"**INTRADAY SHORT PAPER TRADING — {days}-Day Cumulative**\n"
            f"{'━'*50}\n"
            f"Trading days:       {n_days}\n"
            f"Simulated trades:   {n}\n"
            f"Signals rejected:   {n_rejected} (ASM/GSM: {n_asm_gsm})\n"
            f"\n**P&L**\n"
            f"Win rate:           {wr:.1f}% ({len(wins)}W / {len(losses)}L)\n"
            f"Avg P&L (net):      {avg_pnl:+.2f}%\n"
            f"Avg winner:         {avg_win:+.2f}%\n"
            f"Avg loser:          {avg_loss:+.2f}%\n"
            f"Expectancy:         {expectancy:+.3f}%\n"
            f"Sharpe (ann.):      {sharpe:.2f}\n"
            f"Max Drawdown:       {max_dd:.2f}%\n"
            f"\n**Execution Quality**\n"
            f"Avg slippage:       {avg_slip:+.3f}% (backtest assumed {BACKTEST_SLIPPAGE_PCT:.2f}%)\n"
            f"Median slippage:    {med_slip:+.3f}%\n"
            f"Slippage excess:    {slip_excess:+.3f}% vs backtest\n"
            f"Bad fills (>0.25%): {pct_bad:.1f}% of trades\n"
            f"\n**Strategies:**\n" + "\n".join(strat_lines) +
            f"\n\n**Per Day:**\n" + "\n".join(day_lines) +
            f"\n{'━'*50}"
        )
        return report

    # ------------------------------------------------------------------
    # Approval criteria
    # ------------------------------------------------------------------

    def check_approval_criteria(self, days: int = 20) -> str:
        """
        Check whether the paper trading programme has met all 6 approval
        criteria for transitioning to live trading.

        Returns a formatted report with PASS / FAIL per criterion.
        """
        all_trades = self._trader.load_recent_trades(days)
        closed     = [t for t in all_trades if _b(t.get("closed"))]

        # Deduplicate trading days
        all_dates  = sorted({t.get("date", "") for t in all_trades if t.get("date")})
        n_days     = len(all_dates)
        n_trades   = len(closed)

        pnls       = [_f(t.get("pnl_after_costs")) for t in closed]
        net_pnl    = sum(pnls)
        max_dd     = _max_drawdown(pnls)

        slippages  = [_f(t.get("fill_slippage_pct")) for t in closed]
        avg_slip   = sum(slippages) / len(slippages) if slippages else 0.0

        rejection_rows  = self._load_rejections_for_range(days)
        total_signals   = n_trades + len(rejection_rows)
        rejection_rate  = (len(rejection_rows) / total_signals * 100) if total_signals else 0.0

        # Evaluate each criterion
        c1_pass  = n_days >= APPROVAL_MIN_DAYS
        c2_pass  = n_trades >= APPROVAL_MIN_TRADES
        c3_pass  = net_pnl > 0
        c4_pass  = max_dd < APPROVAL_MAX_DRAWDOWN_PCT
        c5_pass  = avg_slip <= APPROVAL_SLIPPAGE_MULT * BACKTEST_SLIPPAGE_PCT
        c6_pass  = rejection_rate <= APPROVAL_MAX_REJECTION_RATE

        all_pass = all([c1_pass, c2_pass, c3_pass, c4_pass, c5_pass, c6_pass])

        def _line(label: str, value: str, passed: bool) -> str:
            icon = "✓ PASS" if passed else "✗ FAIL"
            return f"  {icon}  {label:45s}  {value}"

        report = (
            f"**PAPER TRADING APPROVAL CRITERIA**\n"
            f"{'━'*60}\n"
            + _line("Min 20 trading days",        f"{n_days} days",           c1_pass) + "\n"
            + _line("Min 200 simulated trades",   f"{n_trades} trades",       c2_pass) + "\n"
            + _line("Positive net P&L (after costs)", f"{net_pnl:+.2f}%",    c3_pass) + "\n"
            + _line("Max drawdown < 5%",          f"{max_dd:.2f}%",           c4_pass) + "\n"
            + _line(f"Live slippage ≤ {APPROVAL_SLIPPAGE_MULT}× backtest ({BACKTEST_SLIPPAGE_PCT:.2f}%)",
                    f"live avg {avg_slip:.3f}%",   c5_pass) + "\n"
            + _line(f"Shortability rejection rate ≤ {APPROVAL_MAX_REJECTION_RATE:.0f}%",
                    f"{rejection_rate:.1f}%",      c6_pass) + "\n"
            + f"{'━'*60}\n"
            + (
                "**VERDICT: APPROVED ✓** — Strategy is ready for live trading consideration.\n"
                "Next step: Get explicit sign-off before enabling any live orders."
                if all_pass else
                "**VERDICT: NOT YET APPROVED ✗** — One or more criteria failed.\n"
                "Continue paper trading and review failed criteria."
            )
        )
        return report

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _compute_daily(
        self,
        date_str: str,
        trades: List[dict],
        rejections: List[dict],
    ) -> DailyReportData:
        closed = [t for t in trades if _b(t.get("closed"))]
        n      = len(closed)
        pnls   = [_f(t.get("pnl_after_costs")) for t in closed]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        slippages = [_f(t.get("fill_slippage_pct")) for t in closed]
        avg_slip  = sum(slippages) / len(slippages) if slippages else 0.0
        med_slip  = sorted(slippages)[len(slippages) // 2] if slippages else 0.0
        bad_fills = sum(1 for s in slippages if s > 0.25)
        slip_alerts = sum(1 for s in slippages if s > SLIPPAGE_ALERT_MULT * BACKTEST_SLIPPAGE_PCT)

        # Rejection breakdown
        rej_breakdown: Dict[str, int] = {}
        for r in rejections:
            status = r.get("status", "unknown")
            rej_breakdown[status] = rej_breakdown.get(status, 0) + 1

        asm_gsm_blocked = rej_breakdown.get("asm", 0) + rej_breakdown.get("gsm", 0)

        return DailyReportData(
            date=date_str,
            signals_generated=len(trades) + len(rejections),
            signals_rejected=len(rejections),
            rejection_breakdown=rej_breakdown,
            simulated_trades=n,
            open_trades=len(trades) - n,
            avg_slippage_pct=round(avg_slip, 4),
            median_slippage_pct=round(med_slip, 4),
            pct_bad_fills=round(bad_fills / n * 100 if n else 0.0, 1),
            slippage_alerts=slip_alerts,
            gross_pnl_pct=round(sum(_f(t.get("pnl_pct")) for t in closed) / n if n else 0.0, 4),
            net_pnl_pct=round(sum(pnls) / n if n else 0.0, 4),
            win_rate=round(len(wins) / n * 100 if n else 0.0, 1),
            avg_winner_pct=round(sum(wins) / len(wins) if wins else 0.0, 4),
            avg_loser_pct=round(sum(losses) / len(losses) if losses else 0.0, 4),
            stop_hit_rate=round(
                sum(1 for t in closed if t.get("stop_outcome") == "hit") / n * 100 if n else 0.0, 1
            ),
            target_hit_rate=round(
                sum(1 for t in closed if t.get("target_outcome") == "hit") / n * 100 if n else 0.0, 1
            ),
            backtest_slippage_assumed=BACKTEST_SLIPPAGE_PCT,
            live_slippage_excess=round(avg_slip - BACKTEST_SLIPPAGE_PCT, 4),
            shortability_rejection_count=len(rejections),
            asm_gsm_blocked_count=asm_gsm_blocked,
        )

    def _format_daily(self, d: DailyReportData) -> str:
        rej_detail = "  " + ", ".join(f"{k}: {v}" for k, v in d.rejection_breakdown.items()) \
            if d.rejection_breakdown else "  none"

        slip_flag = ""
        if d.live_slippage_excess > BACKTEST_SLIPPAGE_PCT:
            slip_flag = f"  ⚠ SLIPPAGE ALERT: live {d.avg_slippage_pct:.3f}% is {d.live_slippage_excess:+.3f}% worse than backtest assumed {BACKTEST_SLIPPAGE_PCT:.2f}%\n"

        return (
            f"**INTRADAY SHORT PAPER — Daily Report {d.date}**\n"
            f"{'━'*50}\n"
            f"**1. Signals**\n"
            f"  Generated:          {d.signals_generated}\n"
            f"  Rejected:           {d.signals_rejected}\n"
            f"  Rejection breakdown:\n{rej_detail}\n"
            f"\n**2. Simulated Trades**\n"
            f"  Closed:             {d.simulated_trades}\n"
            f"  Still open:         {d.open_trades}\n"
            f"\n**3. Fill Quality**\n"
            f"  Avg slippage:       {d.avg_slippage_pct:+.3f}%\n"
            f"  Median slippage:    {d.median_slippage_pct:+.3f}%\n"
            f"  Bad fills (>0.25%): {d.pct_bad_fills:.1f}%\n"
            f"  Slippage alerts:    {d.slippage_alerts}\n"
            f"\n**4. P&L**\n"
            f"  Win rate:           {d.win_rate:.1f}%\n"
            f"  Avg net P&L:        {d.net_pnl_pct:+.2f}%\n"
            f"  Avg gross P&L:      {d.gross_pnl_pct:+.2f}%\n"
            f"  Avg winner:         {d.avg_winner_pct:+.2f}%\n"
            f"  Avg loser:          {d.avg_loser_pct:+.2f}%\n"
            f"  Stop hit rate:      {d.stop_hit_rate:.1f}%\n"
            f"  Target hit rate:    {d.target_hit_rate:.1f}%\n"
            f"\n**5. Slippage vs Backtest**\n"
            f"  Backtest assumed:   {d.backtest_slippage_assumed:.2f}%\n"
            f"  Live average:       {d.avg_slippage_pct:+.3f}%\n"
            f"  Excess vs assumed:  {d.live_slippage_excess:+.3f}%\n"
            + slip_flag +
            f"\n**6. Broker / Shortability**\n"
            f"  Rejected signals:   {d.shortability_rejection_count}\n"
            f"  ASM/GSM blocked:    {d.asm_gsm_blocked_count}\n"
            f"{'━'*50}"
        )

    def _load_rejections_for_range(self, days: int) -> List[dict]:
        """Load all rejection rows from the last *days* CSV files."""
        from PKDevTools.classes import Archiver
        data_dir = os.path.join(Archiver.get_user_data_dir(), "shortability")
        path     = os.path.join(data_dir, "rejected_signals.csv")
        if not os.path.isfile(path):
            return []
        rows: List[dict] = []
        try:
            # Get the set of dates covered by recent trade files
            trade_files = sorted(
                glob.glob(os.path.join(
                    Archiver.get_user_data_dir(), "intraday_paper", "intraday_paper_*.csv"
                )),
                reverse=True,
            )[:days]
            covered_dates = set()
            for fp in trade_files:
                base = os.path.basename(fp)
                # intraday_paper_YYYY-MM-DD.csv
                parts = base.replace("intraday_paper_", "").replace(".csv", "")
                if parts:
                    covered_dates.add(parts)

            with open(path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if not covered_dates or row.get("date", "") in covered_dates:
                        rows.append(dict(row))
        except Exception as exc:
            _log.debug(f"[Report] rejection load error: {exc}")
        return rows


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------

def _f(v) -> float:
    try:
        return 0.0 if v in (None, "", "None") else float(v)
    except Exception:
        return 0.0


def _b(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"1", "true", "yes", "y"}


def _sharpe(daily_returns: List[float]) -> float:
    n = len(daily_returns)
    if n < 2:
        return 0.0
    mean = sum(daily_returns) / n
    variance = sum((r - mean) ** 2 for r in daily_returns) / (n - 1)
    std = math.sqrt(variance)
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0


def _max_drawdown(pnls: List[float]) -> float:
    """Max drawdown on a flat list of P&L percentages (cumulative)."""
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in pnls:
        cum  += p
        peak  = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd
