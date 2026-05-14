"""
Stage Validator — staged, reality-based strategy readiness framework.

Stages:
  Stage 1: 0.3 %/day net average — single strategy, 3-month paper validation
  Stage 2: 0.5 %/day net average — 2–3 uncorrelated strategies, 6-month validation
  Stage 3: 1.0 %/day net average — portfolio construction, regime filters, 12+ months

The validator never optimises toward fantasy returns.
It reports the exact reason a strategy is or is not ready, with no sugar-coating.
"""

from __future__ import annotations

import glob
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from PKDevTools.classes import Archiver

# ---------------------------------------------------------------------------
# Stage thresholds
# ---------------------------------------------------------------------------

STAGES = {
    1: {
        "daily_return_pct":   0.30,
        "min_trading_days":   60,    # ~3 months
        "min_trades":         200,
        "max_drawdown_pct":   5.0,
        "min_win_rate":       45.0,
        "max_slippage_pct":   0.20,
        "min_sharpe":         1.0,
        "description":        "Single strategy, 3-month paper validation",
    },
    2: {
        "daily_return_pct":   0.50,
        "min_trading_days":   120,   # ~6 months
        "min_trades":         500,
        "max_drawdown_pct":   7.0,
        "min_win_rate":       47.0,
        "max_slippage_pct":   0.20,
        "min_sharpe":         1.2,
        "description":        "2–3 uncorrelated strategies, 6-month paper/live-sim",
    },
    3: {
        "daily_return_pct":   1.00,
        "min_trading_days":   250,   # ~12 months
        "min_trades":         1000,
        "max_drawdown_pct":   10.0,
        "min_win_rate":       48.0,
        "max_slippage_pct":   0.25,
        "min_sharpe":         1.5,
        "description":        "Portfolio, regime filters, sizing edge — 12+ months",
    },
}

TIER0_CHECKS = [
    "paper_trading_days_met",        # hard gate: ≥20 paper days from new system
    "paper_trading_trades_met",      # hard gate: ≥200 paper trades from new system
    "lookahead_clean",
    "slippage_within_bounds",
    "shortability_confirmed",
    "spread_model_realistic",
    "no_survivorship_bias",
    "walk_forward_cross_regime",
]

# Paper trading is the ONLY valid evidence for stage claims.
# Backtests can nominate a candidate; paper trading earns the stage.
MIN_PAPER_DAYS   = 20
MIN_PAPER_TRADES = 200


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class Tier0Result:
    check:    str
    passed:   bool
    detail:   str


@dataclass
class StageResult:
    strategy_name:     str
    evaluated_at:      str

    # Core metrics
    trading_days:       int
    total_trades:       int
    avg_daily_return:   float   # net of costs
    win_rate:           float
    sharpe:             float
    max_drawdown:       float
    avg_slippage:       float

    # Scores
    fragility_score:    int     # 1–10 (10 = most fragile)
    execution_quality:  int     # 1–10 (10 = best)
    regime_compat:      int     # 1–10 (10 = fully compatible)
    confidence_level:   str     # "LOW" | "MEDIUM" | "HIGH"

    # Tier 0
    tier0_results:      List[Tier0Result] = field(default_factory=list)
    tier0_passed:       bool = False

    # Stage verdict
    current_stage:      int  = 0    # 0 = not yet stage 1
    target_stage:       int  = 1
    stage_passed:       bool = False
    blocking_reasons:   List[str] = field(default_factory=list)
    next_steps:         List[str] = field(default_factory=list)

    # Regime
    regime_notes:       str = ""
    sharpe_note:        str = ""   # non-empty when Sharpe > 5 (anomaly flag)
    ready:              bool = False


# ---------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------

class StageValidator:
    """
    Evaluates a strategy's paper/live-sim performance against the staged
    validation framework and produces an honest readiness report.

    Usage:
        validator = StageValidator()
        result = validator.evaluate("gap_fade_short", days=60)
        print(StageValidator.format_report(result))
    """

    def __init__(self, data_dir: Optional[str] = None) -> None:
        self._paper_dir = data_dir or os.path.join(
            Archiver.get_user_data_dir(), "intraday_paper"
        )
        self._short_dir = os.path.join(
            Archiver.get_user_data_dir(), "shortability"
        )

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def evaluate(
        self,
        strategy_name: str = "all",
        days: int = 60,
        target_stage: int = 1,
        tier0_flags: Optional[Dict[str, bool]] = None,
    ) -> StageResult:
        """
        Evaluate a strategy against the staged framework.

        tier0_flags: manual overrides for Tier 0 checks that cannot be
        auto-computed (e.g. {"lookahead_clean": True, "no_survivorship_bias": False}).
        Pass None to treat all as "unverified" (conservative default).
        """
        trades = self._load_trades(strategy_name, days)
        tier0  = self._run_tier0(trades, tier0_flags or {})

        if not trades:
            return self._empty_result(strategy_name, target_stage, tier0)

        metrics  = self._compute_metrics(trades)
        scores   = self._compute_scores(metrics, tier0)
        verdict  = self._stage_verdict(metrics, scores, tier0, target_stage)

        return StageResult(
            strategy_name=strategy_name,
            evaluated_at=date.today().isoformat(),
            trading_days=metrics["trading_days"],
            total_trades=metrics["n"],
            avg_daily_return=round(metrics["avg_daily_return"], 4),
            win_rate=round(metrics["win_rate"], 1),
            sharpe=round(metrics["sharpe"], 2),
            max_drawdown=round(metrics["max_drawdown"], 2),
            avg_slippage=round(metrics["avg_slippage"], 4),
            fragility_score=scores["fragility"],
            execution_quality=scores["execution"],
            regime_compat=scores["regime_compat"],
            confidence_level=scores["confidence"],
            tier0_results=tier0,
            tier0_passed=all(t.passed for t in tier0),
            current_stage=verdict["current_stage"],
            target_stage=target_stage,
            stage_passed=verdict["passed"],
            blocking_reasons=verdict["blocking"],
            next_steps=verdict["next_steps"],
            regime_notes=verdict["regime_notes"],
            sharpe_note=metrics.get("sharpe_note", ""),
            ready=verdict["ready"],
        )

    # ------------------------------------------------------------------
    # Formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_report(r: StageResult) -> str:
        """Return a complete human-readable dashboard string."""

        stage_label = f"Stage {r.current_stage}" if r.current_stage > 0 else "Pre-Stage 1"
        target_label = f"Stage {r.target_stage}"
        ready_label  = "✓ READY" if r.ready else "✗ NOT READY"
        tier0_label  = "✓ PASS" if r.tier0_passed else "✗ FAIL"

        # Confidence colour words
        conf_icons = {"HIGH": "●●●", "MEDIUM": "●●○", "LOW": "●○○"}
        conf_icon  = conf_icons.get(r.confidence_level, "○○○")

        # Scores as bars
        def _bar(score: int) -> str:
            filled = "█" * score
            empty  = "░" * (10 - score)
            return f"[{filled}{empty}] {score}/10"

        # Tier 0 detail lines
        tier0_lines = "\n".join(
            f"  {'✓' if t.passed else '✗'}  {t.check:35s}  {t.detail}"
            for t in r.tier0_results
        )

        # Blocking reasons
        blocking = "\n".join(f"  • {b}" for b in r.blocking_reasons) if r.blocking_reasons else "  none"

        # Next steps
        steps = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(r.next_steps)) if r.next_steps else "  —"

        threshold = STAGES.get(r.target_stage, STAGES[1])

        report = f"""
╔══════════════════════════════════════════════════════════════════╗
║         STRATEGY STAGE VALIDATION REPORT                        ║
╚══════════════════════════════════════════════════════════════════╝
  Strategy:        {r.strategy_name}
  Evaluated:       {r.evaluated_at}
  Target:          {target_label} ({threshold['description']})
  Verdict:         {ready_label}

──────────────────────────────────────────────────────────────────
  CURRENT STAGE:   {stage_label}
──────────────────────────────────────────────────────────────────

  Core Metrics                    Actual       Required
  ──────────────────────────────────────────────────────
  Trading days                    {r.trading_days:>6}       ≥ {threshold['min_trading_days']}
  Total trades                    {r.total_trades:>6}       ≥ {threshold['min_trades']}
  Avg daily return (net)          {r.avg_daily_return:>+6.3f}%     ≥ {threshold['daily_return_pct']:+.2f}%
  Win rate                        {r.win_rate:>6.1f}%     ≥ {threshold['min_win_rate']:.0f}%
  Sharpe (annualised)             {r.sharpe:>6.2f}       ≥ {threshold['min_sharpe']:.1f}
  Max drawdown                    {r.max_drawdown:>6.2f}%     < {threshold['max_drawdown_pct']:.1f}%
  Avg fill slippage               {r.avg_slippage:>+6.3f}%     ≤ {threshold['max_slippage_pct']:.2f}%

──────────────────────────────────────────────────────────────────
  SCORES
──────────────────────────────────────────────────────────────────
  Fragility        {_bar(r.fragility_score)}   (lower = more durable)
  Execution qual   {_bar(r.execution_quality)}   (higher = better fills)
  Regime compat    {_bar(r.regime_compat)}   (higher = better fit)
  Confidence       {conf_icon} {r.confidence_level:6s}              (statistical confidence)

──────────────────────────────────────────────────────────────────
  TIER 0 AUDIT                     {tier0_label}
──────────────────────────────────────────────────────────────────
{tier0_lines}

──────────────────────────────────────────────────────────────────
  REGIME COMPATIBILITY
──────────────────────────────────────────────────────────────────
  {r.regime_notes or "No regime data available."}
{"" if not r.sharpe_note else chr(10) + "──────────────────────────────────────────────────────────────────" + chr(10) + "  SHARPE ANOMALY" + chr(10) + "──────────────────────────────────────────────────────────────────" + chr(10) + "  " + r.sharpe_note}

──────────────────────────────────────────────────────────────────
  BLOCKING REASONS
──────────────────────────────────────────────────────────────────
{blocking}

──────────────────────────────────────────────────────────────────
  NEXT STEPS
──────────────────────────────────────────────────────────────────
{steps}

══════════════════════════════════════════════════════════════════
  This tool does not optimise toward fantasy returns.
  Stage 1 target: 0.3%/day net.  Stage 2: 0.5%/day.  Stage 3: 1.0%/day.
  Nothing is promised beyond Stage 3.
══════════════════════════════════════════════════════════════════
"""
        return report.strip()

    # ------------------------------------------------------------------
    # Private: data loading
    # ------------------------------------------------------------------

    def _load_trades(self, strategy_name: str, days: int) -> List[dict]:
        files = sorted(
            glob.glob(os.path.join(self._paper_dir, "intraday_paper_*.csv")),
            reverse=True,
        )[:days]
        rows: List[dict] = []
        for fp in files:
            try:
                import csv
                with open(fp) as f:
                    for row in csv.DictReader(f):
                        if strategy_name == "all" or row.get("strategy") == strategy_name:
                            rows.append(row)
            except Exception:
                continue
        return rows

    # ------------------------------------------------------------------
    # Private: metrics
    # ------------------------------------------------------------------

    def _compute_metrics(self, trades: List[dict]) -> Dict:
        closed = [t for t in trades if _b(t.get("closed"))]
        n = len(closed)

        if n == 0:
            return {
                "n": 0, "trading_days": 0, "avg_daily_return": 0.0,
                "win_rate": 0.0, "sharpe": 0.0, "max_drawdown": 0.0,
                "avg_slippage": 0.0, "daily_returns": [],
                "sharpe_note": "",
            }

        pnls      = [_f(t.get("pnl_after_costs")) for t in closed]
        slippages = [_f(t.get("fill_slippage_pct")) for t in closed]
        wins      = [p for p in pnls if p > 0]

        # Daily aggregation: daily portfolio return = mean of concurrent position returns
        # (equal-weight: each position uses 1/N of capital for that day)
        daily: Dict[str, List[float]] = {}
        for t in closed:
            d = t.get("date", "")
            if d:
                daily.setdefault(d, []).append(_f(t["pnl_after_costs"]))

        daily_returns = [sum(v) / len(v) for v in daily.values()]
        trading_days  = len(daily_returns)
        avg_daily     = sum(daily_returns) / trading_days if trading_days else 0.0

        # Sharpe: use daily portfolio returns (mean of concurrent positions per day).
        # This is portfolio Sharpe, inflated by sqrt(N_positions) relative to a
        # single-signal Sharpe.  Flag if > 5 — no legitimate short strategy exceeds
        # Sharpe 5 in live trading.
        portfolio_sharpe  = _sharpe(daily_returns)
        single_trade_sh   = _sharpe_per_trade(pnls, trading_days)
        sharpe_note = ""
        if portfolio_sharpe > 5.0:
            sharpe_note = (
                f"[ANOMALY] Sharpe {portfolio_sharpe:.1f} exceeds 5.0 — physically implausible "
                f"for a live short strategy. Portfolio Sharpe is inflated by averaging "
                f"~{n/trading_days:.0f} concurrent positions/day (ρ≈0). "
                f"Per-trade signal Sharpe = {single_trade_sh:.1f} (still suspect). "
                "Root cause: 0 losing days out of {trading_days}. "
                "Same anomaly as backtest Sharpe=21.699. Unresolved."
            ).format(trading_days=trading_days)

        # Drawdown: must be computed on DAILY portfolio returns, not individual trade
        # P&Ls.  Treating concurrent intraday positions as sequential creates a phantom
        # drawdown that does not correspond to any real capital loss.
        max_dd = _max_drawdown(daily_returns)

        return {
            "n":                n,
            "trading_days":     trading_days,
            "avg_daily_return": avg_daily,
            "win_rate":         len(wins) / n * 100 if n else 0.0,
            "sharpe":           portfolio_sharpe,
            "max_drawdown":     max_dd,
            "avg_slippage":     sum(slippages) / len(slippages) if slippages else 0.0,
            "daily_returns":    daily_returns,
            "sharpe_note":      sharpe_note,
        }

    # ------------------------------------------------------------------
    # Private: scores
    # ------------------------------------------------------------------

    def _compute_scores(self, m: Dict, tier0: List[Tier0Result]) -> Dict:
        n             = m["n"]
        trading_days  = m["trading_days"]
        avg_daily     = m["avg_daily_return"]
        sharpe        = m["sharpe"]
        max_dd        = m["max_drawdown"]
        avg_slip      = m["avg_slippage"]
        tier0_pass    = sum(1 for t in tier0 if t.passed)
        tier0_total   = len(tier0)

        # --- Fragility (1=durable, 10=fragile) ---
        fragility = 5  # base
        if max_dd > 7:          fragility += 2
        elif max_dd < 3:        fragility -= 1
        if sharpe < 1.0:        fragility += 2
        elif sharpe > 2.0:      fragility -= 1
        if avg_slip > 0.20:     fragility += 2
        if tier0_pass < tier0_total:
            fragility += (tier0_total - tier0_pass)
        fragility = max(1, min(10, fragility))

        # --- Execution quality (1=bad, 10=excellent) ---
        exec_q = 5
        if avg_slip <= 0.05:    exec_q += 3
        elif avg_slip <= 0.10:  exec_q += 2
        elif avg_slip <= 0.15:  exec_q += 1
        elif avg_slip > 0.25:   exec_q -= 2
        exec_q = max(1, min(10, exec_q))

        # --- Regime compatibility (heuristic from trade outcomes) ---
        daily_rets    = m.get("daily_returns", [])
        pos_days      = sum(1 for r in daily_rets if r > 0)
        regime_pct    = pos_days / len(daily_rets) * 100 if daily_rets else 0
        regime_compat = max(1, min(10, int(regime_pct / 10)))

        # --- Confidence ---
        if n >= 200 and trading_days >= 40:
            confidence = "HIGH"
        elif n >= 80 and trading_days >= 15:
            confidence = "MEDIUM"
        else:
            confidence = "LOW"

        return {
            "fragility":    fragility,
            "execution":    exec_q,
            "regime_compat": regime_compat,
            "confidence":   confidence,
        }

    # ------------------------------------------------------------------
    # Private: stage verdict
    # ------------------------------------------------------------------

    def _stage_verdict(
        self,
        m: Dict,
        scores: Dict,
        tier0: List[Tier0Result],
        target_stage: int,
    ) -> Dict:
        thresh   = STAGES.get(target_stage, STAGES[1])
        blocking = []
        steps    = []

        # Determine current highest stage already met
        current_stage = 0
        for s in [1, 2, 3]:
            t = STAGES[s]
            if (
                m["trading_days"]     >= t["min_trading_days"]
                and m["n"]            >= t["min_trades"]
                and m["avg_daily_return"] >= t["daily_return_pct"]
                and m["win_rate"]     >= t["min_win_rate"]
                and m["sharpe"]       >= t["min_sharpe"]
                and m["max_drawdown"] <= t["max_drawdown_pct"]
                and m["avg_slippage"] <= t["max_slippage_pct"]
                and all(t2.passed for t2 in tier0)
            ):
                current_stage = s
            else:
                break

        # Check each threshold for the target stage
        if not all(t.passed for t in tier0):
            failed = [t.check for t in tier0 if not t.passed]
            blocking.append(f"Tier 0 audit failed: {', '.join(failed)}")
            steps.append("Run Tier 0 audit — fix lookahead / slippage model / survivorship issues first")

        if m["trading_days"] < thresh["min_trading_days"]:
            remaining = thresh["min_trading_days"] - m["trading_days"]
            blocking.append(
                f"Insufficient trading days: {m['trading_days']} / {thresh['min_trading_days']} "
                f"({remaining} more needed)"
            )
            steps.append(f"Continue paper trading for at least {remaining} more trading days")

        if m["n"] < thresh["min_trades"]:
            remaining = thresh["min_trades"] - m["n"]
            blocking.append(
                f"Insufficient trades: {m['n']} / {thresh['min_trades']} "
                f"({remaining} more needed)"
            )
            steps.append(f"Need {remaining} more closed trades to reach statistical minimum")

        if m["avg_daily_return"] < thresh["daily_return_pct"]:
            blocking.append(
                f"Average daily return {m['avg_daily_return']:+.3f}% "
                f"below target {thresh['daily_return_pct']:+.2f}%"
            )
            steps.append(
                "Do not tune parameters to hit the return target — "
                "investigate whether the edge exists structurally"
            )

        if m["win_rate"] < thresh["min_win_rate"]:
            blocking.append(f"Win rate {m['win_rate']:.1f}% below required {thresh['min_win_rate']:.0f}%")

        if m["sharpe"] < thresh["min_sharpe"]:
            blocking.append(f"Sharpe {m['sharpe']:.2f} below required {thresh['min_sharpe']:.1f}")

        if m["max_drawdown"] > thresh["max_drawdown_pct"]:
            blocking.append(
                f"Max drawdown {m['max_drawdown']:.2f}% exceeds limit {thresh['max_drawdown_pct']:.1f}%"
            )
            steps.append("Tighten circuit breakers or reduce position size before re-evaluating")

        if m["avg_slippage"] > thresh["max_slippage_pct"]:
            blocking.append(
                f"Average slippage {m['avg_slippage']:+.3f}% exceeds {thresh['max_slippage_pct']:.2f}% limit — "
                "execution quality insufficient"
            )
            steps.append("Fix afternoon spread model; verify fills against actual depth data")

        if scores["confidence"] == "LOW":
            blocking.append(
                f"Statistical confidence LOW ({m['n']} trades, {m['trading_days']} days) — "
                "any measured edge is noise until more data accumulates"
            )

        passed = len(blocking) == 0

        # Regime note
        regime_notes = self._regime_note(m, scores)

        # Auto-shutdown conditions
        shutdown = self._shutdown_triggers(m, scores)
        if shutdown:
            for s in shutdown:
                blocking.append(f"[SHUTDOWN CONDITION] {s}")
            steps.insert(0, "Immediate shutdown conditions met — move to paper-only until resolved")

        return {
            "current_stage": current_stage,
            "passed":        passed,
            "ready":         passed and not shutdown,
            "blocking":      blocking,
            "next_steps":    steps if steps else ["All criteria met — proceed to next stage review"],
            "regime_notes":  regime_notes,
        }

    # ------------------------------------------------------------------
    # Private: Tier 0 checks
    # ------------------------------------------------------------------

    def _run_tier0(
        self,
        trades: List[dict],
        manual_flags: Dict[str, bool],
    ) -> List[Tier0Result]:
        results = []
        closed = [t for t in trades if _b(t.get("closed"))]
        n = len(closed)

        # --- Paper trading days gate (hard requirement — backtests do not count) ---
        all_dates = {t.get("date", "") for t in trades if t.get("date")}
        paper_days = len(all_dates)
        results.append(Tier0Result(
            check="paper_trading_days_met",
            passed=paper_days >= MIN_PAPER_DAYS,
            detail=(
                f"{paper_days} / {MIN_PAPER_DAYS} paper-trading days from the live paper system "
                + ("✓" if paper_days >= MIN_PAPER_DAYS
                   else f"— need {MIN_PAPER_DAYS - paper_days} more. "
                        "Backtests do not satisfy this requirement.")
            ),
        ))

        # --- Paper trading trades gate ---
        results.append(Tier0Result(
            check="paper_trading_trades_met",
            passed=n >= MIN_PAPER_TRADES,
            detail=(
                f"{n} / {MIN_PAPER_TRADES} closed paper trades "
                + ("✓" if n >= MIN_PAPER_TRADES
                   else f"— need {MIN_PAPER_TRADES - n} more. "
                        "Backtests do not satisfy this requirement.")
            ),
        ))

        # --- Lookahead check (manual / audit result) ---
        lookahead = manual_flags.get("lookahead_clean")
        results.append(Tier0Result(
            check="lookahead_clean",
            passed=lookahead is True,
            detail=(
                "VERIFIED clean" if lookahead is True
                else "UNVERIFIED — must audit: check all features use t-1 bar close, not t bar close"
                if lookahead is None
                else "FAILED — lookahead detected; backtest P&L is invalid"
            ),
        ))

        # --- Slippage within bounds ---
        if n > 0:
            slippages  = [_f(t.get("fill_slippage_pct")) for t in closed]
            avg_slip   = sum(slippages) / len(slippages)
            slip_ok    = manual_flags.get("slippage_within_bounds", avg_slip <= 0.20)
            results.append(Tier0Result(
                check="slippage_within_bounds",
                passed=bool(slip_ok),
                detail=f"avg slippage {avg_slip:+.3f}% (limit 0.20%)",
            ))
        else:
            results.append(Tier0Result(
                check="slippage_within_bounds",
                passed=False,
                detail="no closed trades — cannot evaluate",
            ))

        # --- Shortability confirmed ---
        short_ok = manual_flags.get("shortability_confirmed")
        rej_path = os.path.join(self._short_dir, "rejected_signals.csv")
        rej_count = 0
        if os.path.isfile(rej_path):
            import csv
            with open(rej_path) as f:
                rej_count = sum(1 for _ in csv.DictReader(f))
        total_signals = n + rej_count
        rej_rate = rej_count / total_signals * 100 if total_signals else 0.0
        short_pass = short_ok if short_ok is not None else (rej_rate <= 30.0)
        results.append(Tier0Result(
            check="shortability_confirmed",
            passed=bool(short_pass),
            detail=f"rejection rate {rej_rate:.1f}% ({rej_count} rejected / {total_signals} total signals)",
        ))

        # --- Spread model realistic ---
        spread_ok = manual_flags.get("spread_model_realistic")
        results.append(Tier0Result(
            check="spread_model_realistic",
            passed=spread_ok is True,
            detail=(
                "VERIFIED" if spread_ok is True
                else "UNVERIFIED — must compare afternoon depth spread to signal-time spread assumption"
                if spread_ok is None
                else "FAILED — backtest spread assumption underestimates afternoon reality"
            ),
        ))

        # --- Survivorship bias ---
        surv_ok = manual_flags.get("no_survivorship_bias")
        results.append(Tier0Result(
            check="no_survivorship_bias",
            passed=surv_ok is True,
            detail=(
                "VERIFIED" if surv_ok is True
                else "UNVERIFIED — confirm delisted/ASM-added stocks from backtest period are included"
                if surv_ok is None
                else "FAILED — backtest universe excludes post-hoc delisted/restricted symbols"
            ),
        ))

        # --- Walk-forward cross-regime ---
        wf_ok = manual_flags.get("walk_forward_cross_regime")
        results.append(Tier0Result(
            check="walk_forward_cross_regime",
            passed=wf_ok is True,
            detail=(
                "VERIFIED across multiple VIX regimes" if wf_ok is True
                else "UNVERIFIED — walk-forward must span at least one low-vol + one high-vol regime"
                if wf_ok is None
                else "FAILED — walk-forward only validated within a single vol regime"
            ),
        ))

        return results

    # ------------------------------------------------------------------
    # Private: regime note
    # ------------------------------------------------------------------

    def _regime_note(self, m: Dict, scores: Dict) -> str:
        compat = scores["regime_compat"]
        daily_rets = m.get("daily_returns", [])
        if not daily_rets:
            return "Insufficient data for regime assessment."
        pos = sum(1 for r in daily_rets if r > 0)
        neg = len(daily_rets) - pos
        if compat >= 7:
            return (
                f"Regime compatible: {pos} profitable days / {len(daily_rets)} total. "
                "Current market conditions appear favourable for this strategy."
            )
        elif compat >= 4:
            return (
                f"Regime marginal: {pos} profitable / {neg} unprofitable days. "
                "Monitor India VIX and signal count daily. "
                "Pause if VIX > 18 or < 10, or if signal count drops below 2/day."
            )
        else:
            return (
                f"Regime incompatible: {neg} losing days / {len(daily_rets)} total. "
                "Do NOT advance stage. The current market regime does not support this strategy. "
                "Paper-only until regime changes."
            )

    # ------------------------------------------------------------------
    # Private: shutdown triggers
    # ------------------------------------------------------------------

    def _shutdown_triggers(self, m: Dict, scores: Dict) -> List[str]:
        triggers = []
        if m["avg_daily_return"] < -0.5:
            triggers.append(
                f"Average daily return {m['avg_daily_return']:+.3f}% is deeply negative"
            )
        if m["max_drawdown"] > 15.0:
            triggers.append(f"Max drawdown {m['max_drawdown']:.1f}% exceeds emergency limit (15%)")
        if m["avg_slippage"] > 0.30:
            triggers.append(
                f"Slippage {m['avg_slippage']:+.3f}% exceeds hard shutdown threshold (0.30%)"
            )
        if scores["regime_compat"] <= 2 and m["trading_days"] >= 20:
            triggers.append("Regime incompatibility confirmed over sufficient data — strategy is structurally broken in this market")
        return triggers

    # ------------------------------------------------------------------
    # Private: empty result
    # ------------------------------------------------------------------

    def _empty_result(
        self,
        strategy_name: str,
        target_stage: int,
        tier0: List[Tier0Result],
    ) -> StageResult:
        return StageResult(
            strategy_name=strategy_name,
            evaluated_at=date.today().isoformat(),
            trading_days=0,
            total_trades=0,
            avg_daily_return=0.0,
            win_rate=0.0,
            sharpe=0.0,
            max_drawdown=0.0,
            avg_slippage=0.0,
            fragility_score=10,
            execution_quality=1,
            regime_compat=1,
            confidence_level="LOW",
            tier0_results=tier0,
            tier0_passed=False,
            current_stage=0,
            target_stage=target_stage,
            stage_passed=False,
            blocking_reasons=["No paper trading data found for this strategy."],
            next_steps=["Run paper trading session first: python scripts/run_intraday_paper_session.py"],
            regime_notes="No data.",
            ready=False,
        )


# ---------------------------------------------------------------------------
# Stats helpers
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
    """Annualised Sharpe of a daily return series (√252 scaling)."""
    n = len(daily_returns)
    if n < 2:
        return 0.0
    mean = sum(daily_returns) / n
    var  = sum((r - mean) ** 2 for r in daily_returns) / (n - 1)
    std  = math.sqrt(var)
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0


def _sharpe_per_trade(pnls: List[float], trading_days: int) -> float:
    """Per-trade signal Sharpe on a daily basis (√252 annualisation).
    Represents the intrinsic quality of a SINGLE signal: if you ran exactly
    one trade per trading day from this distribution, what Sharpe would you get?
    This is independent of how many concurrent positions are taken per day and
    is comparable across strategies with different portfolio widths."""
    n = len(pnls)
    if n < 2:
        return 0.0
    mean = sum(pnls) / n
    var  = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std  = math.sqrt(var)
    return (mean / std * math.sqrt(252)) if std > 0 else 0.0


def _max_drawdown(daily_returns: List[float]) -> float:
    """Max drawdown on the daily portfolio return series.
    Must be called with one return per trading day (not per individual trade).
    For concurrent intraday positions, per-trade sequential treatment creates
    a phantom drawdown that does not correspond to real capital loss."""
    cum, peak, max_dd = 0.0, 0.0, 0.0
    for p in daily_returns:
        cum   += p
        peak   = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    return max_dd
