"""
Report generator for the wealth basket engine.

Produces two outputs per monthly run:
  1. Markdown report  → results/Data/wealth_basket_reports/YYYY_MM_plan.md
  2. CSV allocation   → results/Data/wealth_basket_reports/YYYY_MM_plan.csv

Report sections
---------------
A. Header + run metadata
B. Benchmark comparison (rolling 1m / 3m / 6m / 12m)
C. Underperformance warning (if active)
D. Monthly allocation plan  — ADD / ADD_NEW stocks with amounts
E. Existing holdings summary — HOLD stocks
F. Action required           — REDUCE / EXIT stocks
G. Watch list                — WATCH stocks (on radar)
H. Risk summary
I. Trust level disclaimer (always shown — yfinance data is LOW TRUST)
"""

import csv
import logging
import os
from datetime import datetime, timezone
from typing import Dict, List, Optional

from pkscreener.classes.WealthBasketAllocator import AllocationPlan, AllocationItem
from pkscreener.classes.WealthBasketScorer import Action, StockScore

_log = logging.getLogger(__name__)

_ACTION_EMOJI = {
    Action.ADD:     "🟢",
    Action.ADD_NEW: "🆕",
    Action.HOLD:    "🔵",
    Action.WATCH:   "👀",
    Action.REDUCE:  "🟡",
    Action.EXIT:    "🔴",
    Action.SKIP:    "⬜",
}


def _fmt_inr(amount: float) -> str:
    if amount >= 1e7:
        return f"₹{amount/1e7:.2f} Cr"
    if amount >= 1e5:
        return f"₹{amount/1e5:.2f} L"
    return f"₹{amount:,.0f}"


def _pct(val: Optional[float], default: str = "—") -> str:
    if val is None:
        return default
    sign = "+" if val > 0 else ""
    return f"{sign}{val:.1f}%"


def _confidence_bar(score: float) -> str:
    filled = int(round(score / 10))
    return "█" * filled + "░" * (10 - filled) + f" {score:.0f}/100"


def generate_markdown_report(
    plan: AllocationPlan,
    performance: Optional[Dict],
    holdings: List[Dict],
    output_path: str,
) -> str:
    """
    Write the monthly wealth basket plan as a markdown file.
    Returns the path written.
    """
    lines: List[str] = []
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ----------------------------------------------------------------
    # A. Header
    # ----------------------------------------------------------------
    lines += [
        f"# Wealth Basket — Monthly Plan",
        f"**Plan date:** {plan.plan_date}  |  **Generated:** {now_str}",
        "",
        "---",
        "",
    ]

    # ----------------------------------------------------------------
    # B. Benchmark comparison
    # ----------------------------------------------------------------
    lines += ["## Benchmark Comparison", ""]
    if performance:
        basket_status = performance.get("basket_status", "")
        months_hist   = performance.get("months_of_history", 0)

        if basket_status == "INSUFFICIENT_HISTORY":
            lines += [
                f"> **INSUFFICIENT HISTORY** — {months_hist} portfolio snapshot(s) recorded. "
                f"Need at least 2 months of actual deployment to compute basket returns.",
                f"> Basket returns will show as `—` until sufficient history exists.",
                f"> Benchmark returns (NIFTY, 12% CAGR) are real-time and shown for reference.",
                "",
            ]

        lines += [
            "| Period | Basket | NIFTY 50 | NIFTY 500 | 12% CAGR |",
            "|--------|--------|----------|-----------|----------|",
        ]
        for window in ("1m", "3m", "6m", "12m"):
            b    = _pct(performance.get(f"basket_{window}"))
            n50  = _pct(performance.get(f"nifty50_{window}"))
            n500 = _pct(performance.get(f"nifty500_{window}"))
            flat = _pct(performance.get(f"flat_12pct_{window}"))
            b_cell = f"**{b}**" if basket_status == "OK" else f"*{b}* (no data)"
            lines.append(f"| {window} | {b_cell} | {n50} | {n500} | {flat} |")
        lines.append("")
    else:
        lines += ["> Benchmark history not yet available. Run for at least 1 month to compare.", ""]

    # ----------------------------------------------------------------
    # C. Underperformance warning
    # ----------------------------------------------------------------
    if plan.underperformance_warning:
        lines += [
            "## ⚠️ Underperformance Warning",
            "",
            f"> **{plan.warning_message}**",
            "",
            "**Actions taken automatically:**",
            f"- Monthly deploy budget reduced: {_fmt_inr(plan.total_allocated)} of {_fmt_inr(plan.monthly_budget)}",
            f"- {_fmt_inr(plan.held_cash)} held as cash",
            "- All actions downgraded to MANUAL_REVIEW (no AUTO_EXECUTE this cycle)",
            "- Review REDUCE/EXIT section carefully",
            "",
        ]
    else:
        lines += [
            "## Performance Status",
            "",
            "> ✅ No underperformance warning active.",
            "",
        ]

    # ----------------------------------------------------------------
    # D. Monthly allocation plan
    # ----------------------------------------------------------------
    lines += [
        "## Monthly Allocation Plan",
        f"",
        f"**Budget:** {_fmt_inr(plan.monthly_budget)}  "
        f"**Deploying:** {_fmt_inr(plan.total_allocated)}  "
        f"**Held cash:** {_fmt_inr(plan.held_cash)}",
        "",
    ]
    if plan.cash_reason:
        lines += [f"> {plan.cash_reason}", ""]

    if plan.items:
        lines += [
            "| # | Stock | Sector | Action | Amount | ~Qty | Score | Confidence | Automation | Holding Period |",
            "|---|-------|--------|--------|--------|------|-------|------------|------------|----------------|",
        ]
        for i, item in enumerate(plan.items, 1):
            emoji = _ACTION_EMOJI.get(item.action, "")
            conf_label = {"HIGH": "🟢 HIGH", "MEDIUM": "🟡 MED", "LOW": "🔴 LOW"}.get(item.confidence, item.confidence)
            tier_label = {
                "AUTO_EXECUTE": "✅ AUTO",
                "MANUAL_REVIEW": "👤 MANUAL",
                "WATCH": "👀 WATCH",
            }.get(item.automation_tier, item.automation_tier)
            qty_str = f"{item.approx_qty:.0f}" if item.approx_qty > 0 else "—"
            lines.append(
                f"| {i} | **{item.symbol}** | {item.sector} | {emoji} {item.action} | "
                f"{_fmt_inr(item.amount)} | {qty_str} | {item.score:.0f} | "
                f"{conf_label} | {tier_label} | {item.holding_period} |"
            )
        lines.append("")

        # Detailed reasoning per stock
        lines += ["### Reasoning", ""]
        for item in plan.items:
            emoji = _ACTION_EMOJI.get(item.action, "")
            new_tag = " *(new position)*" if item.is_new_position else ""
            lines += [
                f"#### {emoji} {item.symbol}{new_tag}",
                f"- **Action:** {item.action}  |  **Amount:** {_fmt_inr(item.amount)}  "
                f"|  **Score:** {_confidence_bar(item.score)}",
                f"- **Reason:** {item.reason}",
                f"- **Risks:** {item.risks}",
                f"- **Automation:** {item.automation_tier}",
                "",
            ]
    else:
        lines += ["> No stocks qualify for allocation this month. Full budget held as cash.", ""]

    # ----------------------------------------------------------------
    # E. Existing holdings — HOLD
    # ----------------------------------------------------------------
    if plan.hold_items:
        lines += ["## Holdings — No Action This Month (HOLD)", ""]
        lines += [
            "| Stock | Sector | Score | Reason |",
            "|-------|--------|-------|--------|",
        ]
        for s in plan.hold_items:
            lines.append(f"| {s.symbol} | {s.sector} | {s.combined_score:.0f} | {s.action_reason[:80]} |")
        lines.append("")

    # ----------------------------------------------------------------
    # F. Action required — REDUCE / EXIT
    # ----------------------------------------------------------------
    if plan.reduce_exit_items:
        lines += [
            "## ⚠️ Action Required — Reduce or Exit",
            "",
            "> These holdings are flagged for reduction or exit. "
            "Review carefully before acting.",
            "",
            "| Stock | Sector | Action | Score | Reason | Risks |",
            "|-------|--------|--------|-------|--------|-------|",
        ]
        for s in plan.reduce_exit_items:
            emoji = _ACTION_EMOJI.get(s.action, "")
            lines.append(
                f"| {s.symbol} | {s.sector} | {emoji} {s.action} | "
                f"{s.combined_score:.0f} | {s.action_reason[:70]} | {s.risks[:60]} |"
            )
        lines.append("")

    # ----------------------------------------------------------------
    # G. Watch list
    # ----------------------------------------------------------------
    if plan.watch_items:
        lines += ["## Watch List — Monitor Next Month", ""]
        lines += [
            "| Stock | Sector | Score | Reason |",
            "|-------|--------|-------|--------|",
        ]
        for s in plan.watch_items[:20]:  # cap at 20 rows
            lines.append(f"| {s.symbol} | {s.sector} | {s.combined_score:.0f} | {s.action_reason[:80]} |")
        if len(plan.watch_items) > 20:
            lines.append(f"| *...and {len(plan.watch_items) - 20} more* | | | |")
        lines.append("")

    # ----------------------------------------------------------------
    # H. Current holdings snapshot
    # ----------------------------------------------------------------
    active_holdings = [h for h in holdings if h.get("active", 1)]
    if active_holdings:
        total_invested = sum(float(h.get("total_invested") or 0) for h in active_holdings)
        total_value = sum(float(h.get("current_value") or h.get("total_invested") or 0) for h in active_holdings)
        total_pnl = total_value - total_invested
        pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

        lines += [
            "## Current Portfolio Snapshot",
            "",
            f"**Total invested:** {_fmt_inr(total_invested)}  "
            f"**Current value:** {_fmt_inr(total_value)}  "
            f"**Unrealized P&L:** {_fmt_inr(total_pnl)} ({_pct(pnl_pct)})",
            "",
            "| Stock | Sector | Invested | Avg Cost | Current Price | P&L | P&L% | Action |",
            "|-------|--------|----------|----------|---------------|-----|------|--------|",
        ]
        for h in sorted(active_holdings, key=lambda x: float(x.get("total_invested") or 0), reverse=True):
            pnl = float(h.get("unrealized_pnl") or 0)
            pct = float(h.get("unrealized_pct") or 0)
            pnl_str = f"{_fmt_inr(abs(pnl))} {'gain' if pnl >= 0 else 'loss'}"
            lines.append(
                f"| {h['symbol']} | {h.get('sector','—')} | "
                f"{_fmt_inr(float(h.get('total_invested') or 0))} | "
                f"₹{float(h.get('avg_cost') or 0):.1f} | "
                f"₹{float(h.get('current_price') or 0):.1f} | "
                f"{pnl_str} | {_pct(pct)} | {h.get('last_action','—')} |"
            )
        lines.append("")

    # ----------------------------------------------------------------
    # I. ETF fallback suggestions
    # ----------------------------------------------------------------
    if plan.etf_suggestions:
        lines += ["## Core ETF Fallback Suggestions", ""]
        for etf in plan.etf_suggestions:
            lines += [
                f"> **{etf.symbol}** — {_fmt_inr(etf.amount)}",
                f"> {etf.reason}",
                "",
            ]

    # ----------------------------------------------------------------
    # J. Risk summary + data quality
    # ----------------------------------------------------------------
    total_scored = len(plan.items) + len(plan.hold_items) + len(plan.watch_items) + len(plan.reduce_exit_items)
    conflict_pct = (plan.conflict_count / max(total_scored, 1)) * 100
    low_trust_pct = (plan.low_trust_count / max(total_scored, 1)) * 100

    lines += [
        "## Risk Summary",
        "",
        "| Risk | Status |",
        "|------|--------|",
        f"| Underperformance (6m) | {'⚠️ WARNING' if plan.underperformance_warning else '✅ OK'} |",
        f"| Stocks with REDUCE/EXIT | {len(plan.reduce_exit_items)} |",
        f"| Stocks held this month | {len(plan.hold_items)} |",
        f"| New positions this month | {sum(1 for i in plan.items if i.is_new_position)} |",
        f"| Cash held | {_fmt_inr(plan.held_cash)} ({plan.held_cash/plan.monthly_budget*100:.0f}% of budget) |",
        f"| CONFLICT data stocks | {plan.conflict_count} ({conflict_pct:.0f}% of universe) |",
        f"| LOW TRUST data stocks | {plan.low_trust_count} ({low_trust_pct:.0f}% of universe) |",
        "",
    ]

    # ----------------------------------------------------------------
    # K. Engine readiness status
    # ----------------------------------------------------------------
    _READINESS_DESCRIPTIONS = {
        "NOT_READY":              "⛔ NOT READY — no scoring data available",
        "EARLY_WEALTH_BASKET":    "🟡 EARLY WEALTH BASKET — single data source (yfinance only), LOW TRUST",
        "DATA_ONLY":              "🟠 DATA ONLY — second source active but insufficient history or no backtest",
        "READY_FOR_MANUAL_REVIEW":"🟢 READY FOR MANUAL REVIEW — two sources + history + backtest exist",
        "READY_FOR_PILOT":        "🔵 READY FOR PILOT — clean data, no conflicts in ADD list",
        "READY_FOR_AUTO_EXECUTE": "✅ READY FOR AUTO-EXECUTE — not yet implemented",
    }
    status_desc = _READINESS_DESCRIPTIONS.get(
        plan.readiness_status, plan.readiness_status
    )

    lines += [
        "## Engine Readiness Status",
        "",
        f"**{status_desc}**",
        "",
    ]

    _upgrade_paths = {
        "EARLY_WEALTH_BASKET": (
            "To upgrade to DATA_ONLY: enable screener.in as second source "
            "(`--use-screener`) OR run with balance sheet ROCE fetch enabled."
        ),
        "DATA_ONLY": (
            "To upgrade to READY_FOR_MANUAL_REVIEW: ensure ≥ 3 months of portfolio "
            "snapshots exist AND run `scripts/backtest_wealth_basket.py`."
        ),
        "READY_FOR_MANUAL_REVIEW": (
            "To upgrade to READY_FOR_PILOT: resolve all CONFLICT-flagged stocks "
            "in the ADD/HOLD lists before committing capital."
        ),
    }
    if plan.readiness_status in _upgrade_paths:
        lines += [f"> {_upgrade_paths[plan.readiness_status]}", ""]

    lines += [
        "---",
        "",
        "## ⚠️ Data Trust Disclaimer",
        "",
        "> **All fundamental data is sourced from yfinance and/or screener.in and is LOW–MEDIUM TRUST.**",
        "> yfinance India data is frequently incomplete, stale, or inconsistent.",
        "> Fields like ROE, ROCE, and current ratio are often missing or unreliable.",
        "> CONFLICT trust means two data sources gave materially different values — do not act on these stocks.",
        "> **Do not auto-execute positions based solely on this report.**",
        "> Verify all key fundamentals against NSE quarterly filings before committing capital.",
        "",
        "*Generated by tr-assistant wealth basket engine*",
    ]

    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    _log.info("Markdown report written: %s", output_path)
    return output_path


def generate_csv_report(plan: AllocationPlan, output_path: str) -> str:
    """
    Write the allocation plan as a CSV for further analysis.
    Returns the path written.
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    all_rows = []

    for item in plan.items:
        all_rows.append({
            "plan_date": plan.plan_date,
            "symbol": item.symbol,
            "sector": item.sector,
            "action": item.action,
            "amount_inr": item.amount,
            "approx_qty": item.approx_qty,
            "current_price": item.current_price,
            "score": item.score,
            "confidence": item.confidence,
            "automation_tier": item.automation_tier,
            "is_new_position": item.is_new_position,
            "holding_period": item.holding_period,
            "reason": item.reason,
            "risks": item.risks,
        })

    for s in plan.hold_items:
        all_rows.append({
            "plan_date": plan.plan_date,
            "symbol": s.symbol,
            "sector": s.sector,
            "action": Action.HOLD,
            "amount_inr": 0,
            "approx_qty": 0,
            "current_price": 0,
            "score": s.combined_score,
            "confidence": s.confidence,
            "automation_tier": "HOLD",
            "is_new_position": False,
            "holding_period": s.holding_period,
            "reason": s.action_reason,
            "risks": s.risks,
        })

    for s in plan.reduce_exit_items:
        all_rows.append({
            "plan_date": plan.plan_date,
            "symbol": s.symbol,
            "sector": s.sector,
            "action": s.action,
            "amount_inr": 0,
            "approx_qty": 0,
            "current_price": 0,
            "score": s.combined_score,
            "confidence": s.confidence,
            "automation_tier": "MANUAL_REVIEW",
            "is_new_position": False,
            "holding_period": s.holding_period,
            "reason": s.action_reason,
            "risks": s.risks,
        })

    if not all_rows:
        all_rows.append({
            "plan_date": plan.plan_date,
            "symbol": "CASH",
            "sector": "—",
            "action": "HOLD_CASH",
            "amount_inr": plan.held_cash,
            "approx_qty": 0,
            "current_price": 0,
            "score": 0,
            "confidence": "—",
            "automation_tier": "HOLD_CASH",
            "is_new_position": False,
            "holding_period": "—",
            "reason": plan.cash_reason,
            "risks": "—",
        })

    fieldnames = [
        "plan_date", "symbol", "sector", "action", "amount_inr",
        "approx_qty", "current_price", "score", "confidence",
        "automation_tier", "is_new_position", "holding_period", "reason", "risks",
    ]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    _log.info("CSV report written: %s", output_path)
    return output_path


def report_paths(output_dir: str, plan_date: str) -> tuple[str, str]:
    """Return (markdown_path, csv_path) for a given plan date."""
    month_tag = plan_date[:7].replace("-", "_")
    md = os.path.join(output_dir, f"{month_tag}_plan.md")
    csv_p = os.path.join(output_dir, f"{month_tag}_plan.csv")
    return md, csv_p
