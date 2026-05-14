#!/usr/bin/env python3
"""
Baseline Stage 1 Report — Day 0 honest assessment of the current best
intraday short candidate before paper trading begins.

Evaluates: 5minute | short | late_day_reversal
  late_minutes=270, move_pct=1.2, target=1.1%, stop=0.4%, max_trades=5

Sources used:
  - Live paper trading data (IntradayPaperTrader CSVs)        ← stage evidence
  - Forensic audit CSVs                                       ← context only
  - Strategy research summaries                               ← context only

Backtests provide context. Only paper trading earns a stage.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
RESULTS = BASE_DIR / "results" / "Data"

from pkscreener.classes.StageValidator import StageValidator, STAGES, MIN_PAPER_DAYS, MIN_PAPER_TRADES

STRATEGY_NAME = "late_day_reversal"

# ---------------------------------------------------------------------------
# Load forensic audit context (not used for stage evidence)
# ---------------------------------------------------------------------------

def load_forensic_context() -> dict:
    ctx = {}

    # Variant stress-test table
    summary_path = RESULTS / "kite_intraday_forensic_audit_summary.csv"
    if summary_path.exists():
        variants = {}
        with open(summary_path) as f:
            for row in csv.DictReader(f):
                variants[row["variant"]] = row
        ctx["variants"] = variants

    # Lookahead checks from audit report
    report_path = RESULTS / "kite_intraday_forensic_audit_report.md"
    if report_path.exists():
        text = report_path.read_text()
        ctx["lookahead_section"] = _extract_md_section(text, "## Lookahead Checks")
        ctx["shortability_section"] = _extract_md_section(text, "## Shortability")
        ctx["bug_section"] = _extract_md_section(text, "## Bug / Risk List")
        ctx["paper_decision"] = _extract_md_section(text, "## Paper-Trading Decision")

    # Shortability CSV
    short_path = RESULTS / "kite_intraday_forensic_audit_shortability.csv"
    if short_path.exists():
        with open(short_path) as f:
            rows = list(csv.DictReader(f))
        ctx["shortable_symbols"] = len([r for r in rows if float(r.get("avg_value20_cr_median", 0) or 0) >= 10])
        ctx["total_symbols"] = len(rows)

    return ctx


def _extract_md_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    capture = False
    out = []
    for line in lines:
        if line.strip() == heading:
            capture = True
            continue
        if capture:
            if line.startswith("## "):
                break
            out.append(line)
    return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Assess which Tier 0 checks are actually verified
# ---------------------------------------------------------------------------

def assess_tier0_flags(ctx: dict) -> dict[str, bool]:
    """
    Return only the checks that are ACTUALLY verified by audit evidence.
    Unverified checks are left out (treated as None → unverified in validator).
    """
    flags: dict[str, bool] = {}

    lookahead = ctx.get("lookahead_section", "")
    # The forensic audit explicitly tested and confirmed these three conditions
    if "entry_after_signal_close: True" in lookahead:
        # Bar-level entry lookahead is clean per forensic audit.
        # However: baseline Sharpe of 21.699 remains unexplained — mark as
        # PARTIALLY verified only. We cannot assert full lookahead_clean=True
        # until the anomalous Sharpe is explained.
        flags["lookahead_clean"] = False   # conservatively fail until Sharpe anomaly is explained

    # Shortability: audit explicitly says NOT confirmed
    flags["shortability_confirmed"] = False

    # Survivorship: advanced research explicitly says incomplete removal
    flags["no_survivorship_bias"] = False

    # Spread model: never tested against actual afternoon depth
    flags["spread_model_realistic"] = False

    # Walk-forward cross-regime: latest_3m_holdout shows Sharpe 21.229 —
    # this means all holdout periods had the same regime. Not cross-regime.
    flags["walk_forward_cross_regime"] = False

    return flags


# ---------------------------------------------------------------------------
# Format forensic context block
# ---------------------------------------------------------------------------

def format_context(ctx: dict) -> str:
    variants = ctx.get("variants", {})

    def _vrow(key: str) -> str:
        v = variants.get(key, {})
        if not v:
            return f"  {key:35s}  no data"
        return (
            f"  {key:35s}  "
            f"daily={float(v.get('avg_daily_return_pct',0)):+.4f}%  "
            f"sharpe={float(v.get('sharpe',0)):6.3f}  "
            f"wr={float(v.get('win_rate_pct',0)):.1f}%  "
            f"dd={float(v.get('max_drawdown_pct',0)):.2f}%"
        )

    # Shortability
    short_total = ctx.get("total_symbols", "?")
    short_pass  = ctx.get("shortable_symbols", "?")

    lines = [
        "",
        "╔══════════════════════════════════════════════════════════════════╗",
        "║     FORENSIC AUDIT CONTEXT  (reference only — not stage evidence)║",
        "╚══════════════════════════════════════════════════════════════════╝",
        "",
        "  Strategy: 5min | short | late_day_reversal",
        "  Params:   late_minutes=270, move_pct=1.2, target=1.1%, stop=0.4%, maxd=5",
        "",
        "  ─── BACKTEST STRESS VARIANTS (Kite 2-year data) ────────────────",
        _vrow("baseline"),
        _vrow("fees_1_5x_slip_2x"),
        _vrow("fees_2x_slip_3x_extra_10bp"),
        _vrow("entry_delay_1bar"),
        _vrow("entry_delay_2bar"),
        _vrow("partial_fill_50pct"),
        _vrow("latest_3m_holdout"),
        "",
        "  ─── CRITICAL OBSERVATIONS ──────────────────────────────────────",
        "  [RED FLAG]  Baseline Sharpe = 21.699, win rate = 77.3%.",
        "              No legitimate short strategy produces Sharpe > 5 in live trading.",
        "              This suggests unresolved data quality or subtle lookahead.",
        "              Until explained, the baseline numbers are NOT credible.",
        "",
        "  [KEY FACT]  fees_2x_slip_3x_extra_10bp (realistic stress):",
        "              daily return = +0.0649% — barely above zero.",
        "              At 2× real costs + 3× real slippage + 10bp extra,",
        "              the strategy has no usable edge.",
        "",
        "  [KEY FACT]  entry_delay_2bar (10-min execution delay):",
        "              daily return = +0.5364% — moderate signal survival.",
        "              Kite API latency makes sub-1-bar fills unreliable.",
        "",
        f"  [SHORTABILITY]  {short_pass} of {short_total} symbols pass ₹10Cr liquidity gate.",
        "                  Broker/ASM/GSM shortability is NOT confirmed.",
        "",
        "  [SURVIVORSHIP]  Research note explicitly flags incomplete survivorship",
        "                  removal in the Yahoo Finance data pipeline.",
        "                  Kite-based forensic audit better, but delisted stocks",
        "                  from the training window are not confirmed included.",
        "",
        "  ─── AUDIT LOOKAHEAD CHECKS (forensic audit result) ─────────────",
    ]

    for line in ctx.get("lookahead_section", "").splitlines():
        if line.strip():
            lines.append(f"  {line.strip()}")

    lines += [
        "",
        "  NOTE: bar-level entry is clean (entry_after_signal_close=True).",
        "        But anomalous Sharpe remains unexplained.",
        "        lookahead_clean is conservatively marked UNVERIFIED.",
        "",
        "  ─── FORENSIC AUDIT PAPER-TRADING DECISION ──────────────────────",
    ]
    for line in ctx.get("paper_decision", "").splitlines():
        if line.strip():
            lines.append(f"  {line.strip()}")

    lines += [
        "",
        "══════════════════════════════════════════════════════════════════",
        "  Forensic data is CONTEXT, not stage evidence.",
        "  Stage 1 requires 20 paper days + 200 trades from the live system.",
        "══════════════════════════════════════════════════════════════════",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"\n{'='*66}")
    print(f"  BASELINE STAGE REPORT — {date.today().isoformat()}")
    print(f"  Strategy: {STRATEGY_NAME}")
    print(f"{'='*66}\n")

    ctx        = load_forensic_context()
    tier0_flags = assess_tier0_flags(ctx)

    validator  = StageValidator()
    result     = validator.evaluate(
        strategy_name=STRATEGY_NAME,
        days=90,
        target_stage=1,
        tier0_flags=tier0_flags,
    )

    # Stage validation dashboard
    print(StageValidator.format_report(result))

    # Forensic audit context block
    print(format_context(ctx))

    # Paper trading start instructions
    print("\n" + "─"*66)
    print("  TO START ACCUMULATING PAPER TRADING EVIDENCE:")
    print("─"*66)
    print("  1. Ensure Kite session is active (market hours):")
    print("       python scripts/run_intraday_paper_session.py")
    print()
    print("  2. To simulate past days from Kite cache:")
    print("       python scripts/run_intraday_paper_session.py --simulate YYYY-MM-DD")
    print()
    print(f"  3. Stage 1 gate unlocks after:")
    print(f"       • {MIN_PAPER_DAYS} paper-trading days  ({result.trading_days} / {MIN_PAPER_DAYS} currently)")
    print(f"       • {MIN_PAPER_TRADES} closed simulated trades  ({result.total_trades} / {MIN_PAPER_TRADES} currently)")
    print(f"       • All 8 Tier 0 checks passing")
    print(f"       • avg daily net return ≥ {STAGES[1]['daily_return_pct']:+.2f}%")
    print()
    print("  4. Re-run this report at any time:")
    print("       python scripts/baseline_stage_report.py")
    print("─"*66 + "\n")


if __name__ == "__main__":
    main()
