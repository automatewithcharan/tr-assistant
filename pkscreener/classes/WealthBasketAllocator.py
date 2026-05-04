"""
Monthly capital allocation engine for the wealth basket.

Given a list of scored stocks, current holdings, and a monthly budget,
this module decides how to deploy capital for the month.

Allocation principles
---------------------
1. Only stocks with action ADD or ADD_NEW receive new capital.
2. No single stock gets more than MAX_STOCK_PCT of monthly budget (default 20%).
3. No single sector gets more than MAX_SECTOR_PCT of total basket value (default 35%).
4. Stocks classified REDUCE/EXIT get no new capital and flag for manual action.
5. If fewer than MIN_ADD_COUNT quality stocks exist, hold partial cash.
6. If underperformance warning is active, tighten allocation conservatively.
7. Minimum allocation per stock = MIN_TICKET (default ₹5,000) to keep meaningful size.
8. Total allocation never exceeds monthly_budget. Remainder is held as cash.
9. Allocation is proportional to (combined_score - score_floor) among eligible stocks.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from pkscreener.classes.WealthBasketScorer import Action, StockScore

_log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Constants (all configurable)
# ------------------------------------------------------------------

MAX_STOCK_PCT = 0.20        # max 20% of monthly budget to one stock
MAX_SECTOR_PCT = 0.35       # max 35% of total basket value to one sector
MIN_TICKET = 5_000          # minimum ₹5,000 per stock
MIN_ADD_COUNT = 3           # need at least 3 ADD candidates to deploy fully
SCORE_FLOOR = 55.0          # scores below this get zero allocation weight
CASH_HOLD_THRESHOLD = 0.30  # hold ≥30% as cash if underperformance warning active
DEFAULT_BUDGET = 200_000    # ₹2 lakh default monthly SIP


@dataclass
class AllocationItem:
    symbol: str
    sector: str
    action: str
    amount: float           # ₹ to deploy this month
    approx_qty: float       # estimated qty at current price
    current_price: float
    score: float
    confidence: str
    reason: str
    risks: str
    holding_period: str
    is_new_position: bool
    automation_tier: str    # AUTO_EXECUTE / MANUAL_REVIEW / WATCH / REJECT / HOLD_CASH


@dataclass
class EtfSuggestion:
    symbol: str         # e.g. "NIFTYBEES"
    amount: float       # suggested allocation (₹)
    reason: str


@dataclass
class AllocationPlan:
    plan_date: str
    monthly_budget: float
    total_allocated: float
    held_cash: float
    items: List[AllocationItem] = field(default_factory=list)
    reduce_exit_items: List[StockScore] = field(default_factory=list)
    watch_items: List[StockScore] = field(default_factory=list)
    hold_items: List[StockScore] = field(default_factory=list)
    underperformance_warning: bool = False
    warning_message: str = ""
    cash_reason: str = ""
    etf_suggestions: List["EtfSuggestion"] = field(default_factory=list)
    readiness_status: str = "EARLY_WEALTH_BASKET"
    conflict_count: int = 0
    low_trust_count: int = 0


def _automation_tier(score: StockScore, underperf: bool) -> str:
    """Assign automation tier based on confidence and score."""
    if score.hard_filter_triggered:
        return "REJECT"
    if score.action in (Action.REDUCE, Action.EXIT):
        return "MANUAL_REVIEW"
    if score.trust_level in ("LOW", "CONFLICT"):
        return "MANUAL_REVIEW"
    if underperf:
        return "MANUAL_REVIEW"
    if score.confidence == "HIGH" and score.combined_score >= 78:
        return "AUTO_EXECUTE"
    if score.confidence in ("MEDIUM", "HIGH") and score.combined_score >= 65:
        return "MANUAL_REVIEW"
    return "WATCH"


def compute_readiness_status(
    scores: List[StockScore],
    snapshot_count: int,
    backtest_exists: bool,
) -> str:
    """
    Classify current readiness of the wealth basket engine.

    Levels (ascending):
      NOT_READY             — no fundamentals, no scoring
      EARLY_WEALTH_BASKET   — scoring works, but single data source only
      DATA_ONLY             — second source active, but no snapshots or backtest
      READY_FOR_MANUAL_REVIEW — two sources, ≥ 3 months snapshots, backtest exists
      READY_FOR_PILOT       — above + no CONFLICT stocks in ADD list (future)
      READY_FOR_AUTO_EXECUTE — not implemented; requires execution layer
    """
    if not scores:
        return "NOT_READY"

    has_medium_trust  = any(s.trust_level == "MEDIUM" for s in scores)
    has_conflict      = any(s.trust_level == "CONFLICT" for s in scores)
    all_low_trust     = all(s.trust_level in ("LOW", "FAILED") for s in scores)

    if all_low_trust and not has_medium_trust:
        return "EARLY_WEALTH_BASKET"

    if not has_medium_trust:
        return "EARLY_WEALTH_BASKET"

    # Has at least some MEDIUM trust data
    if snapshot_count < 3 or not backtest_exists:
        return "DATA_ONLY"

    # Two sources + snapshots + backtest exist
    add_list = [s for s in scores if s.action in ("ADD", "ADD_NEW")]
    if add_list and has_conflict:
        return "DATA_ONLY"    # conflicts in candidates = not safe yet

    return "READY_FOR_MANUAL_REVIEW"


def build_allocation_plan(
    scores: List[StockScore],
    current_holdings: List[Dict],
    monthly_budget: float = DEFAULT_BUDGET,
    plan_date: str = "",
    underperformance_warning: bool = False,
    warning_message: str = "",
    current_prices: Optional[Dict[str, float]] = None,
    snapshot_count: int = 0,
    backtest_exists: bool = False,
) -> AllocationPlan:
    """
    Build a monthly allocation plan from scored stocks.

    Args:
        scores:                   Scored stocks (from WealthBasketScorer)
        current_holdings:         Current basket holdings (from WealthBasketDB)
        monthly_budget:           Total capital to deploy this month (₹)
        plan_date:                YYYY-MM-DD string for the plan
        underperformance_warning: True if basket is trailing benchmark
        warning_message:          Human-readable reason for warning
        current_prices:           Dict of symbol → current price (for qty calculation)
        snapshot_count:           Number of monthly portfolio snapshots (for readiness)
        backtest_exists:          Whether a backtest has been run (for readiness)
    """
    from datetime import datetime, timezone
    if not plan_date:
        plan_date = datetime.now(timezone.utc).date().isoformat()

    prices = current_prices or {}

    # Build held symbol → holding data map
    held_map: Dict[str, Dict] = {h["symbol"].upper(): h for h in current_holdings}

    # Total current basket value (for sector concentration check)
    total_basket_value = sum(
        float(h.get("current_value") or h.get("total_invested") or 0)
        for h in current_holdings
    )
    # Current sector value map
    sector_values: Dict[str, float] = {}
    for h in current_holdings:
        sec = str(h.get("sector") or "Unknown")
        val = float(h.get("current_value") or h.get("total_invested") or 0)
        sector_values[sec] = sector_values.get(sec, 0) + val

    # If underperformance warning, tighten budget
    effective_budget = monthly_budget
    cash_reason = ""
    if underperformance_warning:
        max_deploy = monthly_budget * (1 - CASH_HOLD_THRESHOLD)
        effective_budget = min(monthly_budget, max_deploy)
        cash_reason = f"Underperformance warning active — holding {CASH_HOLD_THRESHOLD*100:.0f}% as cash"
        _log.info("Underperformance warning: reducing deploy budget to ₹%.0f", effective_budget)

    # Separate scores by action
    add_candidates: List[StockScore] = []
    hold_list: List[StockScore] = []
    watch_list: List[StockScore] = []
    reduce_exit_list: List[StockScore] = []

    for s in scores:
        if s.action in (Action.ADD, Action.ADD_NEW):
            add_candidates.append(s)
        elif s.action == Action.HOLD:
            hold_list.append(s)
        elif s.action == Action.WATCH:
            watch_list.append(s)
        elif s.action in (Action.REDUCE, Action.EXIT):
            reduce_exit_list.append(s)

    # If too few ADD candidates, hold more cash
    if len(add_candidates) < MIN_ADD_COUNT:
        old = effective_budget
        effective_budget *= (len(add_candidates) / MIN_ADD_COUNT)
        if not cash_reason:
            cash_reason = f"Only {len(add_candidates)} ADD candidate(s) — deploying proportionally"
        _log.info("Few ADD candidates (%d): reducing budget ₹%.0f → ₹%.0f",
                  len(add_candidates), old, effective_budget)

    # Compute allocation weights (proportional to score above floor)
    weights: Dict[str, float] = {}
    for s in add_candidates:
        w = max(0, s.combined_score - SCORE_FLOOR)
        # Boost slightly for existing holdings (conviction already demonstrated)
        if s.symbol in held_map:
            w *= 1.15
        weights[s.symbol] = w

    total_weight = sum(weights.values())
    if total_weight <= 0:
        # No valid weights → hold everything as cash
        _ETF_THRESHOLD = 0.30
        etf_suggestions_early: List[EtfSuggestion] = []
        if monthly_budget > monthly_budget * _ETF_THRESHOLD and not underperformance_warning:
            etf_amt = round(monthly_budget * 0.50, 0)
            etf_suggestions_early.append(EtfSuggestion(
                symbol="NIFTYBEES",
                amount=etf_amt,
                reason=(
                    f"₹{etf_amt:,.0f} of held cash could be deployed into NIFTYBEES "
                    f"(NIFTY 50 ETF) as a low-cost core holding while waiting for "
                    f"quality stock opportunities. Do NOT auto-execute — manual decision required."
                ),
            ))
        conflict_count  = sum(1 for s in scores if s.trust_level == "CONFLICT")
        low_trust_count = sum(1 for s in scores if s.trust_level == "LOW")
        readiness = compute_readiness_status(scores, snapshot_count, backtest_exists)
        return AllocationPlan(
            plan_date=plan_date,
            monthly_budget=monthly_budget,
            total_allocated=0,
            held_cash=monthly_budget,
            watch_items=watch_list,
            hold_items=hold_list,
            reduce_exit_items=reduce_exit_list,
            underperformance_warning=underperformance_warning,
            warning_message=warning_message,
            cash_reason="No stocks above allocation threshold — holding all cash",
            etf_suggestions=etf_suggestions_early,
            readiness_status=readiness,
            conflict_count=conflict_count,
            low_trust_count=low_trust_count,
        )

    # First pass: compute uncapped allocations
    raw_alloc: Dict[str, float] = {}
    for sym, w in weights.items():
        raw_alloc[sym] = (w / total_weight) * effective_budget

    # Apply per-stock cap: max MAX_STOCK_PCT of monthly budget
    stock_cap = monthly_budget * MAX_STOCK_PCT
    overflow = 0.0
    capped: Dict[str, float] = {}
    uncapped_syms = []
    for sym, amt in raw_alloc.items():
        if amt > stock_cap:
            capped[sym] = stock_cap
            overflow += amt - stock_cap
        else:
            capped[sym] = amt
            uncapped_syms.append(sym)

    # Redistribute overflow to uncapped stocks
    if overflow > 0 and uncapped_syms:
        uncapped_weight = sum(weights[s] for s in uncapped_syms)
        for sym in uncapped_syms:
            extra = (weights[sym] / uncapped_weight) * overflow
            new_amt = capped[sym] + extra
            if new_amt > stock_cap:
                extra_overflow = new_amt - stock_cap
                overflow += extra_overflow
                capped[sym] = stock_cap
            else:
                capped[sym] = new_amt

    # Apply sector concentration cap
    final_alloc: Dict[str, float] = {}
    for s in add_candidates:
        sym = s.symbol
        amt = capped.get(sym, 0)
        if amt < MIN_TICKET:
            # Drop below minimum ticket
            continue
        sec = s.sector
        sec_current = sector_values.get(sec, 0)
        sec_limit = (total_basket_value + effective_budget) * MAX_SECTOR_PCT
        sec_headroom = max(0, sec_limit - sec_current)
        if sec_headroom < MIN_TICKET:
            _log.info("%s skipped: sector %s at concentration limit", sym, sec)
            watch_list.append(s)
            continue
        amt = min(amt, sec_headroom)
        if amt >= MIN_TICKET:
            final_alloc[sym] = amt
            sector_values[sec] = sector_values.get(sec, 0) + amt  # update running total

    # Build allocation items
    items: List[AllocationItem] = []
    total_allocated = 0.0

    for s in sorted(add_candidates, key=lambda x: x.combined_score, reverse=True):
        sym = s.symbol
        amt = final_alloc.get(sym, 0)
        if amt < MIN_TICKET:
            continue

        price = prices.get(sym, 0.0)
        approx_qty = (amt / price) if price > 0 else 0.0

        tier = _automation_tier(s, underperformance_warning)

        items.append(AllocationItem(
            symbol=sym,
            sector=s.sector,
            action=s.action,
            amount=round(amt, 2),
            approx_qty=round(approx_qty, 2),
            current_price=round(price, 2),
            score=s.combined_score,
            confidence=s.confidence,
            reason=s.action_reason,
            risks=s.risks,
            holding_period=s.holding_period,
            is_new_position=sym not in held_map,
            automation_tier=tier,
        ))
        total_allocated += amt

    held_cash = monthly_budget - total_allocated
    if not cash_reason and held_cash > 0.05 * monthly_budget:
        cash_reason = f"₹{held_cash:,.0f} held — no qualifying stocks to absorb full budget"

    # ETF fallback suggestions for significant undeployed cash
    etf_suggestions: List[EtfSuggestion] = []
    ETF_THRESHOLD = 0.30  # suggest ETF when > 30% cash remains
    if held_cash > monthly_budget * ETF_THRESHOLD and not underperformance_warning:
        etf_amt = round(held_cash * 0.50, 0)   # suggest half the idle cash
        etf_suggestions.append(EtfSuggestion(
            symbol="NIFTYBEES",
            amount=etf_amt,
            reason=(
                f"₹{etf_amt:,.0f} of held cash could be deployed into NIFTYBEES "
                f"(NIFTY 50 ETF) as a low-cost core holding while waiting for "
                f"quality stock opportunities. Do NOT auto-execute — manual decision required."
            ),
        ))

    # Data quality counts for reporting
    conflict_count  = sum(1 for s in scores if s.trust_level == "CONFLICT")
    low_trust_count = sum(1 for s in scores if s.trust_level == "LOW")

    # Readiness classification
    readiness = compute_readiness_status(scores, snapshot_count, backtest_exists)

    return AllocationPlan(
        plan_date=plan_date,
        monthly_budget=monthly_budget,
        total_allocated=round(total_allocated, 2),
        held_cash=round(held_cash, 2),
        items=items,
        reduce_exit_items=reduce_exit_list,
        watch_items=watch_list,
        hold_items=hold_list,
        underperformance_warning=underperformance_warning,
        warning_message=warning_message,
        cash_reason=cash_reason,
        etf_suggestions=etf_suggestions,
        readiness_status=readiness,
        conflict_count=conflict_count,
        low_trust_count=low_trust_count,
    )
