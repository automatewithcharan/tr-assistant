"""
Multi-factor scoring engine for the wealth basket.

Scores each stock across three dimensions:
  1. Quality    — fundamental health (7 factors, 100 pts)
  2. Momentum   — technical strength (5 factors, 100 pts)
  3. Valuation  — rough attractiveness (3 factors, 100 pts)

Combined score = 0.40 × quality + 0.40 × momentum + 0.20 × valuation

Quality score breakdown (100 pts total)
-----------------------------------------
  ROE              15 pts   return quality
  ROCE             15 pts   capital efficiency (NEW — key for India)
  Profit growth    15 pts   earnings trajectory
  Revenue growth   12 pts   top-line growth
  Net margin       13 pts   profitability
  D/E ratio        15 pts   financial leverage
  Current ratio    15 pts   liquidity / short-term health

Trust levels
------------
  MEDIUM  — two sources agree (yfinance + screener.in), or ≥ 6 of 10 fields populated
  LOW     — single source or < 6/10 fields
  CONFLICT — two sources disagree on a key field (blocks AUTO_EXECUTE regardless of score)
  FAILED  — data fetch failed entirely

When trust is LOW: cap action at WATCH for non-held stocks.
When trust is CONFLICT: cap at WATCH regardless; flag prominently in report.
AUTO_EXECUTE requires MEDIUM or HIGH trust AND score ≥ 78.

Action thresholds — held stocks
---------------------------------
  score ≥ 72  → ADD
  score 58–71 → HOLD
  score 42–57 → WATCH
  score 28–41 → REDUCE
  score < 28  → EXIT

Action thresholds — new positions (stricter)
---------------------------------------------
  score ≥ 78  → ADD_NEW
  score 65–77 → WATCH
  score < 65  → SKIP

Hard filters (override score — held stocks only get REDUCE/EXIT, non-held get SKIP)
-------------------------------------------------------------------------------------
  D/E > 2.5                    → REDUCE/SKIP (high leverage)
  revenue_growth < -15%        → REDUCE/SKIP (revenue collapsing)
  profit_growth < -30%         → REDUCE/SKIP (profits collapsing)
  price < 200DMA by > 20%      → REDUCE/SKIP (deep downtrend)
  current_ratio < 0.5          → REDUCE/SKIP (severe liquidity crisis)
  market_cap < ₹2000 Cr        → WATCH (liquidity risk)
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action constants
# ---------------------------------------------------------------------------

class Action:
    ADD     = "ADD"
    ADD_NEW = "ADD_NEW"
    HOLD    = "HOLD"
    WATCH   = "WATCH"
    REDUCE  = "REDUCE"
    EXIT    = "EXIT"
    SKIP    = "SKIP"


# ---------------------------------------------------------------------------
# Score result
# ---------------------------------------------------------------------------

@dataclass
class StockScore:
    symbol: str
    sector: str = ""
    quality_score: float = 0.0
    momentum_score: float = 0.0
    valuation_score: float = 0.0
    combined_score: float = 0.0
    trust_level: str = "LOW"
    action: str = Action.WATCH
    action_reason: str = ""
    risks: str = ""
    confidence: str = "LOW"
    holding_period: str = "3–12 months"
    is_held: bool = False
    hard_filter_triggered: str = ""
    missing_fields: List[str] = field(default_factory=list)
    conflict_fields: str = ""


# ---------------------------------------------------------------------------
# Quality scoring (7 factors, 100 pts)
# ---------------------------------------------------------------------------

def _quality_score(f: Dict) -> tuple[float, str]:
    """
    Score fundamental quality 0–100.

    Weights: ROE(15) + ROCE(15) + profit_growth(15) + rev_growth(12)
             + net_margin(13) + D/E(15) + current_ratio(15) = 100
    """
    scores = []
    reasons = []

    # 1. ROE: 0–15 pts. ROE > 20% is excellent.
    roe = f.get("roe_pct")
    if roe is not None:
        s = min(15, max(0, (roe / 20) * 15)) if roe >= 0 else 0
        scores.append(s)
        reasons.append(f"ROE {roe:.1f}%")
    else:
        scores.append(7)    # neutral when missing
        reasons.append("ROE unknown")

    # 2. ROCE: 0–15 pts. ROCE > 20% = excellent capital efficiency.
    roce = f.get("roce_pct")
    if roce is not None:
        if roce >= 25:
            scores.append(15); reasons.append(f"ROCE {roce:.1f}% excellent")
        elif roce >= 18:
            scores.append(12); reasons.append(f"ROCE {roce:.1f}% good")
        elif roce >= 12:
            scores.append(8);  reasons.append(f"ROCE {roce:.1f}% decent")
        elif roce >= 8:
            scores.append(4);  reasons.append(f"ROCE {roce:.1f}% moderate")
        else:
            scores.append(1);  reasons.append(f"ROCE {roce:.1f}% weak")
    else:
        scores.append(7)    # neutral when missing
        reasons.append("ROCE unknown")

    # 3. Profit growth: 0–15 pts. > 20% YoY is excellent.
    pg = f.get("profit_growth_yoy")
    if pg is not None:
        if pg < -30:
            scores.append(0);  reasons.append(f"profit growth {pg:.0f}% POOR")
        elif pg < 0:
            scores.append(4);  reasons.append(f"profit growth {pg:.0f}%")
        else:
            s = min(15, max(0, (pg / 25) * 15))
            scores.append(s); reasons.append(f"profit growth +{pg:.0f}%")
    else:
        scores.append(7)
        reasons.append("profit growth unknown")

    # 4. Revenue growth: 0–12 pts. > 15% YoY good.
    rg = f.get("revenue_growth_yoy")
    if rg is not None:
        if rg < -15:
            scores.append(0);  reasons.append(f"revenue growth {rg:.0f}% POOR")
        else:
            s = min(12, max(0, (rg / 15) * 12))
            scores.append(s); reasons.append(f"revenue growth +{rg:.0f}%")
    else:
        scores.append(5)
        reasons.append("revenue growth unknown")

    # 5. Net margin: 0–13 pts. > 15% is excellent.
    nm = f.get("net_margin_pct")
    if nm is not None:
        s = min(13, max(0, (nm / 15) * 13)) if nm >= 0 else 0
        scores.append(s); reasons.append(f"net margin {nm:.1f}%")
    else:
        scores.append(6)
        reasons.append("margin unknown")

    # 6. Debt/equity: 0–15 pts. Lower is better.
    de = f.get("debt_equity")
    if de is not None:
        if de <= 0:       scores.append(15); reasons.append("debt-free")
        elif de <= 0.5:   scores.append(13); reasons.append(f"D/E {de:.1f} low")
        elif de <= 1.0:   scores.append(10); reasons.append(f"D/E {de:.1f} moderate")
        elif de <= 2.0:   scores.append(5);  reasons.append(f"D/E {de:.1f} elevated")
        else:             scores.append(1);  reasons.append(f"D/E {de:.1f} HIGH")
    else:
        scores.append(7)
        reasons.append("debt unknown")

    # 7. Current ratio: 0–15 pts. Measures short-term liquidity.
    cr = f.get("current_ratio")
    if cr is not None:
        if cr >= 2.0:     scores.append(15); reasons.append(f"CR {cr:.1f} very liquid")
        elif cr >= 1.5:   scores.append(12); reasons.append(f"CR {cr:.1f} healthy")
        elif cr >= 1.0:   scores.append(8);  reasons.append(f"CR {cr:.1f} adequate")
        elif cr >= 0.75:  scores.append(4);  reasons.append(f"CR {cr:.1f} tight")
        else:             scores.append(0);  reasons.append(f"CR {cr:.1f} LIQUIDITY RISK")
    else:
        scores.append(7)
        reasons.append("current ratio unknown")

    score = round(sum(scores), 1)
    return score, "; ".join(reasons)


# ---------------------------------------------------------------------------
# Momentum scoring (5 factors, 100 pts) — unchanged
# ---------------------------------------------------------------------------

def _momentum_score(price_df, current_price: float = 0) -> tuple[float, str]:
    """Score price momentum 0–100 using daily OHLCV history."""
    if price_df is None or len(price_df) < 20:
        return 50.0, "price history unavailable — neutral"

    try:
        closes = price_df["Close"].squeeze().dropna().values
        if len(closes) < 20:
            return 50.0, "insufficient price history"

        latest = float(closes[-1]) if current_price <= 0 else current_price
        scores = []
        reasons = []

        # 1. Price vs 200DMA (0–30 pts)
        ma200 = float(np.mean(closes[-200:])) if len(closes) >= 200 else float(np.mean(closes))
        pct_vs_200 = (latest - ma200) / ma200 * 100
        if pct_vs_200 > 15:
            scores.append(30); reasons.append(f"+{pct_vs_200:.0f}% above 200DMA")
        elif pct_vs_200 > 5:
            scores.append(22); reasons.append(f"+{pct_vs_200:.0f}% above 200DMA")
        elif pct_vs_200 > 0:
            scores.append(15); reasons.append(f"+{pct_vs_200:.0f}% above 200DMA")
        elif pct_vs_200 > -10:
            scores.append(8);  reasons.append(f"{pct_vs_200:.0f}% below 200DMA")
        else:
            scores.append(0);  reasons.append(f"{pct_vs_200:.0f}% DEEP below 200DMA")

        # 2. 52-week position (0–25 pts)
        high_52w = float(np.max(closes[-252:])) if len(closes) >= 252 else float(np.max(closes))
        low_52w  = float(np.min(closes[-252:])) if len(closes) >= 252 else float(np.min(closes))
        rng = high_52w - low_52w
        pos_52w = ((latest - low_52w) / rng * 100) if rng > 0 else 50
        if pos_52w >= 80:    scores.append(25); reasons.append(f"near 52w high ({pos_52w:.0f}%)")
        elif pos_52w >= 60:  scores.append(18); reasons.append(f"upper 52w range ({pos_52w:.0f}%)")
        elif pos_52w >= 40:  scores.append(12); reasons.append(f"mid 52w range ({pos_52w:.0f}%)")
        elif pos_52w >= 20:  scores.append(6);  reasons.append(f"lower 52w range ({pos_52w:.0f}%)")
        else:                scores.append(0);  reasons.append(f"near 52w low ({pos_52w:.0f}%)")

        # 3. 3-month price trend (0–20 pts)
        closes_3m = closes[-63:] if len(closes) >= 63 else closes
        if len(closes_3m) >= 2:
            ret_3m = (closes_3m[-1] - closes_3m[0]) / closes_3m[0] * 100
            if ret_3m > 15:    scores.append(20); reasons.append(f"3m +{ret_3m:.0f}%")
            elif ret_3m > 5:   scores.append(15); reasons.append(f"3m +{ret_3m:.0f}%")
            elif ret_3m > 0:   scores.append(10); reasons.append(f"3m +{ret_3m:.0f}%")
            elif ret_3m > -5:  scores.append(5);  reasons.append(f"3m {ret_3m:.0f}%")
            else:              scores.append(0);  reasons.append(f"3m {ret_3m:.0f}% weak")
        else:
            scores.append(10)

        # 4. Price vs 50DMA (0–15 pts)
        ma50 = float(np.mean(closes[-50:])) if len(closes) >= 50 else float(np.mean(closes))
        pct_vs_50 = (latest - ma50) / ma50 * 100
        if pct_vs_50 > 5:    scores.append(15); reasons.append(f"+{pct_vs_50:.1f}% above 50DMA")
        elif pct_vs_50 > 0:  scores.append(10); reasons.append(f"+{pct_vs_50:.1f}% above 50DMA")
        elif pct_vs_50 > -5: scores.append(5)
        else:                scores.append(0);  reasons.append(f"{pct_vs_50:.1f}% below 50DMA")

        # 5. Volatility penalty (0–10 pts)
        if len(closes) >= 20:
            _c = closes[-63:] if len(closes) >= 63 else closes
            daily_rets = np.diff(_c) / _c[:-1]
            vol_20d = float(np.std(daily_rets[-20:]) * np.sqrt(252) * 100)
            if vol_20d < 25:    scores.append(10)
            elif vol_20d < 40:  scores.append(6)
            else:               scores.append(2); reasons.append(f"high vol {vol_20d:.0f}%")
        else:
            scores.append(5)

        total = round(sum(scores), 1)
        return min(100, total), "; ".join(reasons[:4])

    except Exception as exc:
        _log.debug("Momentum score error: %s", exc)
        return 50.0, "momentum calc error — neutral"


# ---------------------------------------------------------------------------
# Valuation scoring (3 factors, 100 pts) — unchanged
# ---------------------------------------------------------------------------

def _valuation_score(f: Dict) -> tuple[float, str]:
    """Score valuation attractiveness 0–100. HIGH = attractively valued."""
    scores = []
    reasons = []

    pe = f.get("pe_ratio")
    if pe is not None and pe > 0:
        if pe < 12:    scores.append(50); reasons.append(f"PE {pe:.0f} cheap")
        elif pe < 20:  scores.append(40); reasons.append(f"PE {pe:.0f} fair")
        elif pe < 30:  scores.append(30); reasons.append(f"PE {pe:.0f} moderate")
        elif pe < 50:  scores.append(15); reasons.append(f"PE {pe:.0f} expensive")
        else:          scores.append(5);  reasons.append(f"PE {pe:.0f} very expensive")
    else:
        scores.append(25); reasons.append("PE unknown")

    pb = f.get("pb_ratio")
    if pb is not None and pb > 0:
        if pb < 1.5:   scores.append(30); reasons.append(f"P/B {pb:.1f} low")
        elif pb < 3:   scores.append(22); reasons.append(f"P/B {pb:.1f}")
        elif pb < 6:   scores.append(15); reasons.append(f"P/B {pb:.1f}")
        elif pb < 15:  scores.append(8)
        else:          scores.append(2);  reasons.append(f"P/B {pb:.0f} very high")
    else:
        scores.append(15)

    nm = f.get("net_margin_pct")
    if nm is not None and nm > 10:   scores.append(20); reasons.append("profitable")
    elif nm is not None and nm > 0:  scores.append(12)
    else:                            scores.append(5)

    return min(100, round(sum(scores), 1)), "; ".join(reasons[:3])


# ---------------------------------------------------------------------------
# Hard filters
# ---------------------------------------------------------------------------

def _apply_hard_filters(
    f: Dict, price_df, is_financial: bool = False
) -> tuple[Optional[str], str]:
    """
    Hard filters that override the score.
    Returns (forced_action, reason) or (None, '') if clear.

    D/E and current_ratio filters are SKIPPED for financial-sector companies
    (banks, NBFCs, insurance). Their balance sheets are structurally different
    — high leverage is normal and expected in those businesses.
    All other filters (revenue collapse, profit collapse, 200DMA) apply to all.
    """
    if not is_financial:
        de = f.get("debt_equity")
        if de is not None and de > 2.5:
            return Action.REDUCE, f"D/E {de:.2f} exceeds 2.5 — high leverage risk"

    rg = f.get("revenue_growth_yoy")
    if rg is not None and rg < -15:
        return Action.REDUCE, f"revenue declining {rg:.0f}% YoY"

    pg = f.get("profit_growth_yoy")
    if pg is not None and pg < -30:
        return Action.REDUCE, f"profits collapsing {pg:.0f}% YoY"

    if not is_financial:
        cr = f.get("current_ratio")
        if cr is not None and cr < 0.5:
            return Action.REDUCE, f"current ratio {cr:.2f} — severe liquidity crisis"

    mktcap = f.get("market_cap_cr")
    if mktcap is not None and mktcap < 2000:
        return Action.WATCH, f"market cap Rs {mktcap:.0f} Cr — liquidity risk"

    if price_df is not None and len(price_df) >= 200:
        try:
            closes = price_df["Close"].squeeze().dropna().values
            latest = float(closes[-1])
            ma200  = float(np.mean(closes[-200:]))
            pct_vs_200 = (latest - ma200) / ma200 * 100
            if pct_vs_200 < -20:
                return Action.REDUCE, f"price {pct_vs_200:.0f}% below 200DMA — deep downtrend"
        except Exception:
            pass

    return None, ""


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def score_stock(
    symbol: str,
    fundamentals: Optional[Dict],
    price_df,
    is_held: bool = False,
    sector_from_csv: str = "",
) -> StockScore:
    """
    Score a single stock across quality, momentum, and valuation.

    Args:
        symbol:          NSE trading symbol
        fundamentals:    Dict from FundamentalsDataFetcher (may be None/FAILED)
        price_df:        pandas DataFrame with Close prices (may be None)
        is_held:         True if stock is already in the basket
        sector_from_csv: Sector from index CSV (fallback if yfinance sector missing)
    """
    f = fundamentals or {}
    fund_ok = f.get("trust_level") not in (None, "FAILED", "")
    sector   = f.get("sector") or sector_from_csv or "Unknown"
    conflict = f.get("trust_level") == "CONFLICT"
    missing  = f.get("missing_fields", [])
    conflict_fields = f.get("conflict_fields", "")

    # Detect financial sector — D/E and CR hard filters / quality sub-scores
    # do not apply to banks, NBFCs, and insurance companies.
    is_financial = bool(f.get("is_financial", False))
    if not is_financial and f.get("sector"):
        from pkscreener.classes.FundamentalsDataFetcher import _is_financial_sector
        is_financial = _is_financial_sector(f.get("sector", ""), f.get("industry", ""))

    # For quality scoring of financial companies: null out D/E and CR so the
    # scoring functions give neutral (7 pts each) instead of penalising them.
    f_score = f
    if is_financial and fund_ok:
        f_score = dict(f)
        f_score["debt_equity"]    = None   # neutral: 7 pts
        f_score["current_ratio"]  = None   # neutral: 7 pts

    # Quality score
    if fund_ok:
        quality, q_reason = _quality_score(f_score)
    else:
        quality, q_reason = 50.0, "fundamentals unavailable — neutral"

    # Momentum score
    momentum, m_reason = _momentum_score(price_df)

    # Valuation score
    if fund_ok:
        valuation, v_reason = _valuation_score(f_score)
    else:
        valuation, v_reason = 50.0, "valuation unknown"

    # Trust level — MEDIUM requires real two-source agreement (CompositeProvider).
    # fields_populated alone does NOT upgrade to MEDIUM; that created a false
    # impression that single-source yfinance data was cross-validated.
    raw_trust = f.get("trust_level", "LOW")
    if not fund_ok:
        trust = "LOW"
    elif raw_trust == "CONFLICT":
        trust = "CONFLICT"
    elif raw_trust == "MEDIUM":
        trust = "MEDIUM"
    else:
        trust = "LOW"

    # Combined score
    combined = round(0.40 * quality + 0.40 * momentum + 0.20 * valuation, 1)

    # Hard filters — financial companies skip D/E and CR checks
    forced_action, filter_reason = _apply_hard_filters(f, price_df, is_financial=is_financial)

    # Action classification
    hard_filter = ""
    if forced_action:
        # CONFLICT overrides even hard filters — always WATCH, never auto-reduce on bad data
        if conflict:
            action = Action.WATCH
        elif not is_held and forced_action in (Action.REDUCE, Action.EXIT):
            action = Action.SKIP   # can't reduce something we don't own
        else:
            action = forced_action
        hard_filter = filter_reason
        action_reason = filter_reason

    elif trust == "CONFLICT":
        # Data conflict — do not act; manual review only
        action = Action.WATCH
        action_reason = f"DATA CONFLICT on {conflict_fields} — manual verification required"

    elif trust == "LOW":
        if is_held:
            action = Action.HOLD if combined >= 50 else Action.WATCH
        else:
            action = Action.WATCH
        action_reason = f"LOW TRUST data — capped. Score: {combined:.0f}/100"

    elif is_held:
        if combined >= 72:   action = Action.ADD
        elif combined >= 58: action = Action.HOLD
        elif combined >= 42: action = Action.WATCH
        elif combined >= 28: action = Action.REDUCE
        else:                action = Action.EXIT
        action_reason = f"Score {combined:.0f}/100"

    else:
        if combined >= 78:   action = Action.ADD_NEW
        elif combined >= 65: action = Action.WATCH
        else:                action = Action.SKIP
        action_reason = f"Score {combined:.0f}/100 (new position)"

    # Confidence level
    if trust in ("LOW", "CONFLICT") or forced_action:
        confidence = "LOW"
    elif trust == "MEDIUM" and combined >= 65:
        confidence = "MEDIUM"
    elif trust == "HIGH" and combined >= 72:
        confidence = "HIGH"
    else:
        confidence = "LOW"

    # Build reasons and risks
    reasons_parts = [f"Q:{quality:.0f} M:{momentum:.0f} V:{valuation:.0f}"]
    if q_reason and q_reason != "fundamentals unavailable — neutral":
        reasons_parts.append(q_reason)
    reasons_parts.append(m_reason)

    risks = []
    de = f.get("debt_equity")
    if is_financial:
        risks.append("Financial sector — D/E and liquidity filters not applicable; verify banking metrics separately")
    elif de and de > 1.0:
        risks.append(f"leverage D/E {de:.2f}")
    if trust == "LOW":
        risks.append("fundamentals unverified (yfinance only)")
    if trust == "CONFLICT":
        risks.append(f"DATA CONFLICT: {conflict_fields}")
    if forced_action:
        risks.append(filter_reason)
    if missing:
        risks.append(f"missing: {', '.join(missing[:3])}")

    # Holding period
    if action in (Action.REDUCE, Action.EXIT):
        hp = "Exit planned"
    elif action == Action.WATCH:
        hp = "Review next month"
    elif combined >= 72:
        hp = "12–36 months"
    else:
        hp = "6–18 months"

    return StockScore(
        symbol=symbol.upper(),
        sector=sector,
        quality_score=quality,
        momentum_score=momentum,
        valuation_score=valuation,
        combined_score=combined,
        trust_level=trust,
        action=action,
        action_reason=" | ".join(reasons_parts),
        risks="; ".join(risks) if risks else "None identified",
        confidence=confidence,
        holding_period=hp,
        is_held=is_held,
        hard_filter_triggered=hard_filter,
        missing_fields=missing,
        conflict_fields=conflict_fields,
    )


def score_universe(
    symbols: List[str],
    fundamentals_map: Dict[str, Dict],
    price_history_map: Dict[str, object],
    held_symbols: Optional[List[str]] = None,
    sector_map: Optional[Dict[str, str]] = None,
) -> List[StockScore]:
    """Score a list of stocks and return sorted by combined_score descending."""
    held    = set(s.upper() for s in (held_symbols or []))
    sectors = sector_map or {}
    results: List[StockScore] = []

    for sym in symbols:
        sym_upper = sym.strip().upper()
        try:
            score = score_stock(
                symbol=sym_upper,
                fundamentals=fundamentals_map.get(sym_upper),
                price_df=price_history_map.get(sym_upper),
                is_held=sym_upper in held,
                sector_from_csv=sectors.get(sym_upper, ""),
            )
            results.append(score)
        except Exception as exc:
            _log.warning("Scoring failed for %s: %s", sym_upper, exc)

    results.sort(key=lambda s: s.combined_score, reverse=True)
    return results
