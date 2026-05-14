"""
Core unit tests for the wealth basket engine.
All tests run offline — no yfinance or broker calls.
"""

import tempfile
import os
import pandas as pd
import numpy as np
import pytest
from datetime import date, timedelta


# ---------------------------------------------------------------------------
# WealthBasketDB
# ---------------------------------------------------------------------------

def _make_db():
    from pkscreener.classes.WealthBasketDB import WealthBasketDB
    f = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    f.close()
    return WealthBasketDB(f.name), f.name


def test_db_tables_created():
    db, path = _make_db()
    import sqlite3
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    expected = {"wb_fundamentals","wb_scores","wb_holdings","wb_sip_tranches",
                "wb_cashflows","wb_benchmark","wb_performance"}
    assert expected.issubset(tables)
    os.unlink(path)


def test_db_save_and_get_fundamentals():
    db, path = _make_db()
    data = {
        "source": "yfinance", "trust_level": "LOW",
        "pe_ratio": 22.5, "roe_pct": 18.0, "debt_equity": 0.4,
        "revenue_growth_yoy": 12.0, "profit_growth_yoy": 20.0,
        "net_margin_pct": 14.0, "sector": "Technology", "market_cap_cr": 50000,
    }
    db.save_fundamentals("INFY", data)
    result = db.get_fundamentals("INFY", max_age_days=30)
    assert result is not None
    assert result["symbol"] == "INFY"
    assert result["pe_ratio"] == pytest.approx(22.5)
    os.unlink(path)


def test_db_fundamentals_cache_expiry():
    db, path = _make_db()
    import sqlite3
    data = {"source": "yfinance", "trust_level": "LOW", "pe_ratio": 20.0}
    db.save_fundamentals("TCS", data)
    # Backdate the fetch to 35 days ago
    old_date = (date.today() - timedelta(days=35)).isoformat()
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE wb_fundamentals SET fetch_date=? WHERE symbol='TCS'", (old_date,))
    # Should return None — stale
    result = db.get_fundamentals("TCS", max_age_days=30)
    assert result is None
    os.unlink(path)


def test_db_upsert_holding_new():
    db, path = _make_db()
    db.upsert_holding("RELIANCE", "Energy", qty_delta=10, price=2500.0, action="ADD", score=72.0)
    holdings = db.get_holdings()
    assert len(holdings) == 1
    h = holdings[0]
    assert h["symbol"] == "RELIANCE"
    assert h["total_qty"] == pytest.approx(10)
    assert h["avg_cost"] == pytest.approx(2500.0)
    assert h["total_invested"] == pytest.approx(25000.0)
    os.unlink(path)


def test_db_upsert_holding_adds_to_existing():
    db, path = _make_db()
    db.upsert_holding("TCS", "IT", qty_delta=5, price=3000.0, action="ADD", score=70.0)
    db.upsert_holding("TCS", "IT", qty_delta=5, price=3200.0, action="ADD", score=68.0)
    holdings = db.get_holdings()
    h = holdings[0]
    assert h["total_qty"] == pytest.approx(10)
    assert h["total_invested"] == pytest.approx(5 * 3000 + 5 * 3200)
    expected_avg = (5 * 3000 + 5 * 3200) / 10
    assert h["avg_cost"] == pytest.approx(expected_avg)
    os.unlink(path)


def test_db_benchmark_roundtrip():
    db, path = _make_db()
    db.save_benchmark_day("2026-01-01", nifty50=23000.0, nifty500=22000.0, flat_12pct=10120.0)
    db.save_benchmark_day("2026-01-02", nifty50=23100.0, nifty500=22050.0, flat_12pct=10123.0)
    rows = db.get_benchmark_history()
    assert len(rows) == 2
    assert rows[0]["nifty50"] == pytest.approx(23000.0)
    os.unlink(path)


def test_db_override_log(tmp_path):
    from pkscreener.classes.LivePortfolio import LivePortfolioStore
    db_path = str(tmp_path / "portfolio.db")
    store = LivePortfolioStore(db_path)
    rid = store.log_override(
        symbol="WIPRO",
        manual_action="MANUAL_SELL at 450.00",
        system_recommendation="HOLD (stage=CONFIRMED)",
    )
    assert rid > 0
    stats = store.get_override_stats()
    assert stats["total"] == 1
    assert stats["pending"] == 1


# ---------------------------------------------------------------------------
# FundamentalsDataFetcher — offline unit tests
# ---------------------------------------------------------------------------

def test_load_nifty500_universe():
    from pkscreener.classes.FundamentalsDataFetcher import load_nifty500_universe
    from pathlib import Path
    csv_path = Path(__file__).parent.parent / "results" / "Indices" / "ind_nifty500list.csv"
    if not csv_path.exists():
        pytest.skip("NIFTY 500 CSV not found")
    pairs = load_nifty500_universe(str(csv_path))
    assert len(pairs) > 400
    symbols = [s for s, _ in pairs]
    assert "RELIANCE" in symbols
    assert "TCS" in symbols


# ---------------------------------------------------------------------------
# WealthBasketScorer
# ---------------------------------------------------------------------------

def _make_price_df(n=250, trend="up"):
    """Create a synthetic daily close price series."""
    base = 1000.0
    if trend == "up":
        prices = [base * (1 + i * 0.001) for i in range(n)]
    elif trend == "down":
        prices = [base * (1 - i * 0.001) for i in range(n)]
    else:
        prices = [base + np.sin(i * 0.1) * 50 for i in range(n)]
    idx = pd.date_range(end=date.today(), periods=n, freq="B")
    return pd.DataFrame({"Close": prices}, index=idx)


def test_scorer_strong_stock_gets_high_score():
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    fund = {
        "trust_level": "LOW", "source": "yfinance",
        "pe_ratio": 22.0, "roe_pct": 22.0, "debt_equity": 0.3,
        "revenue_growth_yoy": 18.0, "profit_growth_yoy": 25.0,
        "net_margin_pct": 16.0, "market_cap_cr": 80000,
        "sector": "Technology", "fields_populated": 6,
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("INFY", fund, df, is_held=False)
    assert result.combined_score >= 50, f"Strong stock should score well, got {result.combined_score}"
    # LOW TRUST caps at WATCH even with high score
    assert result.action in (Action.WATCH, Action.ADD_NEW, Action.ADD)


def test_scorer_high_debt_triggers_hard_filter():
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    fund = {
        "trust_level": "LOW", "source": "yfinance",
        "pe_ratio": 15.0, "roe_pct": 18.0, "debt_equity": 3.2,
        "revenue_growth_yoy": 10.0, "net_margin_pct": 8.0,
        "market_cap_cr": 20000, "fields_populated": 5,
    }
    df = _make_price_df(250, trend="up")
    # Non-held: hard filter → SKIP (can't reduce what we don't own)
    result = score_stock("DEBTCO", fund, df, is_held=False)
    assert result.action == Action.SKIP, f"Non-held high D/E should SKIP, got {result.action}"
    assert "D/E" in result.hard_filter_triggered
    # Held: hard filter → REDUCE
    result_held = score_stock("DEBTCO", fund, df, is_held=True)
    assert result_held.action == Action.REDUCE, f"Held high D/E should force REDUCE, got {result_held.action}"


def test_scorer_falling_revenue_triggers_reduce():
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    fund = {
        "trust_level": "LOW", "source": "yfinance",
        "pe_ratio": 10.0, "roe_pct": 12.0, "debt_equity": 0.5,
        "revenue_growth_yoy": -20.0, "profit_growth_yoy": -35.0,
        "net_margin_pct": 5.0, "market_cap_cr": 15000, "fields_populated": 6,
    }
    df = _make_price_df(250, trend="down")
    # Non-held: hard filter → SKIP
    result = score_stock("WEAKCO", fund, df, is_held=False)
    assert result.action == Action.SKIP, f"Non-held collapsing revenue should SKIP, got {result.action}"
    # Held: hard filter → REDUCE or EXIT
    result_held = score_stock("WEAKCO", fund, df, is_held=True)
    assert result_held.action in (Action.REDUCE, Action.EXIT)


def test_scorer_missing_fundamentals_gives_low_trust():
    from pkscreener.classes.WealthBasketScorer import score_stock
    df = _make_price_df(250, trend="up")
    result = score_stock("NODATA", None, df)
    assert result.trust_level == "LOW"
    # Should still produce a momentum-based score
    assert result.momentum_score > 0


def test_scorer_no_price_data_does_not_crash():
    from pkscreener.classes.WealthBasketScorer import score_stock
    fund = {"trust_level": "LOW", "pe_ratio": 20.0, "market_cap_cr": 10000, "fields_populated": 2}
    result = score_stock("NOPRICE", fund, None)
    assert result.momentum_score == pytest.approx(50.0)


def test_scorer_downtrend_below_200dma_triggers_filter():
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    # Build a price series that is 25% below where 200DMA would be
    n = 250
    prices = [1000.0] * 200 + [700.0] * 50   # sharp drop at end
    idx = pd.date_range(end=date.today(), periods=n, freq="B")
    df = pd.DataFrame({"Close": prices}, index=idx)
    fund = {
        "trust_level": "LOW", "pe_ratio": 18.0, "roe_pct": 15.0,
        "debt_equity": 0.5, "market_cap_cr": 20000, "fields_populated": 4,
    }
    # Non-held: deep downtrend → SKIP (don't buy, nothing to reduce)
    result = score_stock("FALLENCO", fund, df, is_held=False)
    assert result.action == Action.SKIP, f"Non-held deep downtrend should SKIP, got {result.action}"
    assert result.hard_filter_triggered != ""
    # Held: deep downtrend → REDUCE
    result_held = score_stock("FALLENCO", fund, df, is_held=True)
    assert result_held.action in (Action.REDUCE, Action.EXIT, Action.WATCH)


def test_score_universe_returns_sorted():
    from pkscreener.classes.WealthBasketScorer import score_universe
    df_up = _make_price_df(250, "up")
    df_down = _make_price_df(250, "down")
    fund_good = {"trust_level": "LOW", "roe_pct": 20.0, "profit_growth_yoy": 25.0,
                 "market_cap_cr": 50000, "fields_populated": 4}
    fund_bad = {"trust_level": "LOW", "roe_pct": 5.0, "profit_growth_yoy": -10.0,
                "market_cap_cr": 50000, "fields_populated": 4}
    results = score_universe(
        symbols=["GOOD", "BAD"],
        fundamentals_map={"GOOD": fund_good, "BAD": fund_bad},
        price_history_map={"GOOD": df_up, "BAD": df_down},
    )
    assert len(results) == 2
    assert results[0].combined_score >= results[1].combined_score


# ---------------------------------------------------------------------------
# WealthBasketAllocator
# ---------------------------------------------------------------------------

def _make_scores(n=5, base_score=72.0):
    from pkscreener.classes.WealthBasketScorer import StockScore, Action
    scores = []
    for i in range(n):
        s = StockScore(
            symbol=f"STOCK{i}",
            sector="Technology" if i < 3 else "Finance",
            quality_score=base_score - i * 2,
            momentum_score=base_score - i * 2,
            valuation_score=50.0,
            combined_score=base_score - i * 2,
            trust_level="MEDIUM",
            action=Action.ADD_NEW,
            action_reason=f"Score {base_score - i*2:.0f}/100",
            risks="None",
            confidence="MEDIUM",
            holding_period="12 months",
            is_held=False,
        )
        scores.append(s)
    return scores


def test_allocator_total_does_not_exceed_budget():
    from pkscreener.classes.WealthBasketAllocator import build_allocation_plan
    scores = _make_scores(5, 75.0)
    plan = build_allocation_plan(scores, [], monthly_budget=200_000)
    assert plan.total_allocated <= 200_000 + 1   # tiny float tolerance


def test_allocator_no_single_stock_exceeds_20pct():
    from pkscreener.classes.WealthBasketAllocator import build_allocation_plan, MAX_STOCK_PCT
    scores = _make_scores(3, 80.0)
    plan = build_allocation_plan(scores, [], monthly_budget=200_000)
    for item in plan.items:
        assert item.amount <= 200_000 * MAX_STOCK_PCT + 1, \
            f"{item.symbol} allocation ₹{item.amount} exceeds 20% cap"


def test_allocator_reduce_stocks_get_no_capital():
    from pkscreener.classes.WealthBasketScorer import StockScore, Action
    from pkscreener.classes.WealthBasketAllocator import build_allocation_plan
    scores = _make_scores(3, 72.0)
    # Add a REDUCE stock
    reduce_s = StockScore(
        symbol="BADSTOCK", sector="Energy", quality_score=20.0,
        momentum_score=20.0, valuation_score=20.0, combined_score=20.0,
        trust_level="LOW", action=Action.REDUCE, action_reason="weak",
        risks="high debt", confidence="LOW", holding_period="Exit planned",
    )
    scores.append(reduce_s)
    plan = build_allocation_plan(scores, [], monthly_budget=200_000)
    allocated_symbols = {item.symbol for item in plan.items}
    assert "BADSTOCK" not in allocated_symbols, "REDUCE stock must not receive allocation"
    assert any(s.symbol == "BADSTOCK" for s in plan.reduce_exit_items)


def test_allocator_cash_held_when_no_candidates():
    from pkscreener.classes.WealthBasketAllocator import build_allocation_plan
    from pkscreener.classes.WealthBasketScorer import StockScore, Action
    # All stocks below floor
    low_score = StockScore(
        symbol="WEAK", sector="X", quality_score=30.0, momentum_score=30.0,
        valuation_score=30.0, combined_score=30.0, trust_level="LOW",
        action=Action.WATCH, action_reason="low score", risks="—",
        confidence="LOW", holding_period="—",
    )
    plan = build_allocation_plan([low_score], [], monthly_budget=200_000)
    assert plan.total_allocated == 0
    assert plan.held_cash == pytest.approx(200_000)


def test_allocator_underperformance_reduces_deployment():
    from pkscreener.classes.WealthBasketAllocator import build_allocation_plan, CASH_HOLD_THRESHOLD
    scores = _make_scores(5, 78.0)
    plan = build_allocation_plan(
        scores, [], monthly_budget=200_000,
        underperformance_warning=True,
        warning_message="Lagging NIFTY 500 by 5%",
    )
    max_allowed = 200_000 * (1 - CASH_HOLD_THRESHOLD)
    assert plan.total_allocated <= max_allowed + 1, \
        f"Underperformance should cap deployment at {max_allowed}"


# ---------------------------------------------------------------------------
# BenchmarkTracker
# ---------------------------------------------------------------------------

def test_flat_12pct_value():
    from pkscreener.classes.BenchmarkTracker import flat_12pct_value
    result = flat_12pct_value(10000.0, "2025-01-01", "2026-01-01")
    assert result == pytest.approx(11200.0, rel=0.02)


def test_check_underperformance_detects_lag():
    from pkscreener.classes.BenchmarkTracker import check_underperformance
    rolling = {
        "basket_6m": 2.0,
        "nifty50_6m": 8.0,
        "nifty500_6m": 7.0,
        "flat_12pct_6m": 5.8,
    }
    is_under, msg = check_underperformance(rolling, window="6m", threshold=3.0)
    assert is_under is True
    assert "NIFTY 50" in msg


def test_check_underperformance_no_flag_when_ok():
    from pkscreener.classes.BenchmarkTracker import check_underperformance
    rolling = {
        "basket_6m": 9.0,
        "nifty50_6m": 8.0,
        "nifty500_6m": 7.5,
        "flat_12pct_6m": 5.8,
    }
    is_under, msg = check_underperformance(rolling, window="6m", threshold=3.0)
    assert is_under is False


def test_check_underperformance_no_data_returns_false():
    from pkscreener.classes.BenchmarkTracker import check_underperformance
    rolling = {}
    is_under, msg = check_underperformance(rolling, window="6m")
    assert is_under is False


# ---------------------------------------------------------------------------
# WealthBasketReporter
# ---------------------------------------------------------------------------

def test_reporter_generates_markdown_file(tmp_path):
    from pkscreener.classes.WealthBasketAllocator import AllocationPlan, AllocationItem
    from pkscreener.classes.WealthBasketReporter import generate_markdown_report
    from pkscreener.classes.WealthBasketScorer import StockScore, Action

    item = AllocationItem(
        symbol="RELIANCE", sector="Energy", action=Action.ADD,
        amount=40000, approx_qty=16, current_price=2500,
        score=74.0, confidence="MEDIUM", reason="Q:70 M:75 V:60",
        risks="None", holding_period="12 months", is_new_position=False,
        automation_tier="MANUAL_REVIEW",
    )
    hold_stock = StockScore(
        symbol="TCS", sector="IT", quality_score=68.0, momentum_score=65.0,
        valuation_score=55.0, combined_score=64.0, trust_level="LOW",
        action=Action.HOLD, action_reason="Score 64/100", risks="None",
        confidence="LOW", holding_period="12 months",
    )
    plan = AllocationPlan(
        plan_date="2026-05-01",
        monthly_budget=200_000,
        total_allocated=40_000,
        held_cash=160_000,
        items=[item],
        hold_items=[hold_stock],
        reduce_exit_items=[],
        watch_items=[],
    )
    perf = {
        "basket_1m": 2.1, "basket_3m": 5.4, "basket_6m": 9.2, "basket_12m": None,
        "nifty50_1m": 1.8, "nifty50_3m": 4.9, "nifty50_6m": 8.5, "nifty50_12m": None,
        "nifty500_1m": 1.7, "nifty500_3m": 4.7, "nifty500_6m": 8.1, "nifty500_12m": None,
    }
    md_path = str(tmp_path / "2026_05_plan.md")
    generate_markdown_report(plan, perf, [], md_path)
    assert os.path.exists(md_path)
    content = open(md_path).read()
    assert "RELIANCE" in content
    assert "HOLD" in content
    assert "Trust Disclaimer" in content


def test_reporter_generates_csv_file(tmp_path):
    from pkscreener.classes.WealthBasketAllocator import AllocationPlan, AllocationItem
    from pkscreener.classes.WealthBasketReporter import generate_csv_report
    from pkscreener.classes.WealthBasketScorer import Action

    item = AllocationItem(
        symbol="INFY", sector="IT", action=Action.ADD_NEW,
        amount=30000, approx_qty=20, current_price=1500,
        score=76.0, confidence="MEDIUM", reason="Strong momentum",
        risks="None", holding_period="12–24 months", is_new_position=True,
        automation_tier="MANUAL_REVIEW",
    )
    plan = AllocationPlan(
        plan_date="2026-05-01",
        monthly_budget=200_000,
        total_allocated=30_000,
        held_cash=170_000,
        items=[item],
    )
    csv_path = str(tmp_path / "2026_05_plan.csv")
    generate_csv_report(plan, csv_path)
    assert os.path.exists(csv_path)
    import csv
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) >= 1
    assert rows[0]["symbol"] == "INFY"
    assert float(rows[0]["amount_inr"]) == pytest.approx(30000)


# ---------------------------------------------------------------------------
# FundamentalsDataFetcher — missing_fields annotation
# ---------------------------------------------------------------------------

def test_annotate_missing_records_none_fields():
    from pkscreener.classes.FundamentalsDataFetcher import _annotate_missing, _ALL_KEY_FIELDS
    data = {
        "pe_ratio": 22.0,
        "roe_pct": 18.0,
        # everything else is absent → None
    }
    result = _annotate_missing(data)
    missing = result["missing_fields"]
    # pe_ratio and roe_pct are present; all others should be in missing
    assert "pe_ratio" not in missing
    assert "roe_pct" not in missing
    for key in _ALL_KEY_FIELDS:
        if key not in ("pe_ratio", "roe_pct"):
            assert key in missing, f"{key} should be in missing_fields"
    assert result["fields_populated"] == 2
    assert result["fields_total"] == len(_ALL_KEY_FIELDS)


def test_annotate_missing_all_present():
    from pkscreener.classes.FundamentalsDataFetcher import _annotate_missing, _ALL_KEY_FIELDS
    data = {k: 1.0 for k in _ALL_KEY_FIELDS}
    _annotate_missing(data)
    assert data["missing_fields"] == []
    assert data["fields_populated"] == len(_ALL_KEY_FIELDS)


# ---------------------------------------------------------------------------
# WealthBasketScorer — ROCE and current_ratio scoring
# ---------------------------------------------------------------------------

def test_scorer_roce_excellent_gives_full_points():
    from pkscreener.classes.WealthBasketScorer import _quality_score
    fund = {
        "roe_pct": 20.0, "roce_pct": 30.0,
        "profit_growth_yoy": 20.0, "revenue_growth_yoy": 15.0,
        "net_margin_pct": 15.0, "debt_equity": 0.2, "current_ratio": 2.5,
    }
    score, reason = _quality_score(fund)
    # Perfect inputs: ROE(15)+ROCE(15)+profit_growth(12)+rev_growth(12)+margin(13)+D/E(13)+CR(15)=95
    assert score >= 90.0, f"Near-perfect fundamentals should score ≥90, got {score}"
    assert "excellent" in reason.lower() or "30" in reason


def test_scorer_roce_missing_gives_neutral():
    from pkscreener.classes.WealthBasketScorer import _quality_score
    fund = {
        "roe_pct": 20.0,
        # roce_pct missing
        "profit_growth_yoy": 20.0, "revenue_growth_yoy": 15.0,
        "net_margin_pct": 15.0, "debt_equity": 0.2, "current_ratio": 2.0,
    }
    score_with_roce, _ = _quality_score({**fund, "roce_pct": 25.0})
    score_without, _ = _quality_score(fund)
    # Missing ROCE gives 7 pts neutral; excellent ROCE gives 15 pts
    assert score_with_roce > score_without


def test_scorer_current_ratio_low_gives_zero_pts():
    from pkscreener.classes.WealthBasketScorer import _quality_score
    fund = {
        "roe_pct": 20.0, "roce_pct": 25.0,
        "profit_growth_yoy": 20.0, "revenue_growth_yoy": 15.0,
        "net_margin_pct": 15.0, "debt_equity": 0.2,
        "current_ratio": 0.3,   # below 0.75 → 0 pts
    }
    score, reason = _quality_score(fund)
    assert "LIQUIDITY RISK" in reason or "CR 0.3" in reason


def test_scorer_low_current_ratio_hard_filter():
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    fund = {
        "trust_level": "LOW", "source": "yfinance",
        "pe_ratio": 20.0, "roe_pct": 18.0, "debt_equity": 0.5,
        "current_ratio": 0.3,   # below 0.5 → hard filter
        "market_cap_cr": 20000, "fields_populated": 5,
    }
    df = _make_price_df(250, trend="up")
    # Non-held: hard filter → SKIP
    result = score_stock("ILLIQUID", fund, df, is_held=False)
    assert result.action == Action.SKIP
    assert result.hard_filter_triggered != ""
    # Held: hard filter → REDUCE
    result_held = score_stock("ILLIQUID", fund, df, is_held=True)
    assert result_held.action == Action.REDUCE


# ---------------------------------------------------------------------------
# WealthBasketScorer — CONFLICT trust level
# ---------------------------------------------------------------------------

def test_scorer_conflict_trust_caps_at_watch():
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    # High-quality fundamentals but marked as CONFLICT
    fund = {
        "trust_level": "CONFLICT",
        "source": "composite",
        "conflict_fields": "pe_ratio:22.0vs30.0|roe_pct:18.0vs25.0",
        "pe_ratio": 22.0, "roe_pct": 18.0, "debt_equity": 0.3,
        "revenue_growth_yoy": 18.0, "profit_growth_yoy": 25.0,
        "net_margin_pct": 16.0, "market_cap_cr": 80000,
        "fields_populated": 8,
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("CONFLICTCO", fund, df, is_held=False)
    assert result.action == Action.WATCH, \
        f"CONFLICT trust should cap at WATCH even with good score, got {result.action}"
    assert result.trust_level == "CONFLICT"


def test_scorer_conflict_overrides_hard_filter():
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    # CONFLICT trust + hard filter (high D/E) — CONFLICT should win → WATCH not SKIP/REDUCE
    fund = {
        "trust_level": "CONFLICT",
        "conflict_fields": "pe_ratio:10.0vs22.0",
        "pe_ratio": 10.0, "roe_pct": 15.0,
        "debt_equity": 3.0,   # would normally trigger hard filter
        "market_cap_cr": 50000, "fields_populated": 5,
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("CONFLICTDEBT", fund, df, is_held=False)
    assert result.action == Action.WATCH, \
        f"CONFLICT should override hard filter → WATCH, got {result.action}"


# ---------------------------------------------------------------------------
# DataProviders — cross-validation conflict detection
# ---------------------------------------------------------------------------

def test_cross_validate_detects_conflict():
    from pkscreener.classes.DataProviders import _cross_validate, CONFLICT_THRESHOLD
    a = {"pe_ratio": 20.0, "roe_pct": 18.0}
    b = {"pe_ratio": 30.0, "roe_pct": 18.5}   # pe_ratio diverges > 20%
    conflicts, agreements = _cross_validate(a, b, ["pe_ratio", "roe_pct"])
    assert "pe_ratio" in " ".join(conflicts), f"pe_ratio should conflict, got {conflicts}"
    assert any("roe_pct" in a for a in agreements)


def test_cross_validate_no_conflict_when_within_threshold():
    from pkscreener.classes.DataProviders import _cross_validate
    a = {"pe_ratio": 20.0, "roe_pct": 18.0}
    b = {"pe_ratio": 21.0, "roe_pct": 18.5}   # both within 20%
    conflicts, agreements = _cross_validate(a, b, ["pe_ratio", "roe_pct"])
    assert len(conflicts) == 0
    assert len(agreements) == 2


def test_cross_validate_skips_none_fields():
    from pkscreener.classes.DataProviders import _cross_validate
    a = {"pe_ratio": 20.0, "roe_pct": None}
    b = {"pe_ratio": 35.0, "roe_pct": 18.0}   # roe_pct only in one source
    conflicts, _ = _cross_validate(a, b, ["pe_ratio", "roe_pct"])
    # Only pe_ratio should be compared (roe_pct None in a → skip)
    assert any("pe_ratio" in c for c in conflicts)
    # roe_pct not compared → not in conflicts
    assert not any("roe_pct" in c for c in conflicts)


# ---------------------------------------------------------------------------
# WealthBasketDB — portfolio snapshots
# ---------------------------------------------------------------------------

def test_db_tables_include_portfolio_snapshots():
    db, path = _make_db()
    import sqlite3
    with sqlite3.connect(path) as conn:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
    assert "wb_portfolio_snapshots" in tables, "wb_portfolio_snapshots table must exist"
    os.unlink(path)


def test_db_save_and_get_portfolio_snapshot():
    db, path = _make_db()
    db.save_portfolio_snapshot(
        snapshot_date="2026-04-01",
        total_invested=100_000,
        holdings_value=105_000,
        cash_held=20_000,
        basket_total=125_000,
        nifty50_value=22000.0,
        nifty500_value=10500.0,
        monthly_sip=20_000,
        cumulative_invested=100_000,
        holdings=[{"symbol": "RELIANCE", "qty": 10, "avg_cost": 2500, "current_price": 2600, "current_value": 26000}],
    )
    snapshots = db.get_portfolio_snapshots(limit=5)
    assert len(snapshots) == 1
    s = snapshots[0]
    assert s["basket_total"] == pytest.approx(125_000)
    assert s["nifty50_value"] == pytest.approx(22000.0)
    assert isinstance(s.get("holdings_json") or s.get("holdings"), (list, str))
    os.unlink(path)


def test_db_portfolio_snapshot_upsert():
    db, path = _make_db()
    db.save_portfolio_snapshot("2026-04-01", 100_000, 105_000, 20_000, 125_000,
                               22000.0, 10500.0, 20_000, 100_000, [])
    db.save_portfolio_snapshot("2026-04-01", 110_000, 115_000, 20_000, 135_000,
                               22500.0, 10800.0, 20_000, 110_000, [])
    snapshots = db.get_portfolio_snapshots(limit=5)
    assert len(snapshots) == 1   # upsert → only one row
    assert snapshots[0]["basket_total"] == pytest.approx(135_000)
    os.unlink(path)


def test_db_get_cumulative_invested():
    db, path = _make_db()
    db.log_cashflow(amount=200_000, deployed=150_000, held_cash=50_000)
    db.log_cashflow(amount=200_000, deployed=180_000, held_cash=20_000)
    total = db.get_cumulative_invested()
    assert total == pytest.approx(400_000)
    os.unlink(path)


# ---------------------------------------------------------------------------
# BenchmarkTracker — basket TWR and INSUFFICIENT_HISTORY
# ---------------------------------------------------------------------------

def test_benchmark_insufficient_history_no_warning():
    from pkscreener.classes.BenchmarkTracker import check_underperformance, INSUFFICIENT_HISTORY
    rolling = {"basket_status": INSUFFICIENT_HISTORY, "months_of_history": 1}
    is_under, msg = check_underperformance(rolling, window="6m")
    assert is_under is False
    assert INSUFFICIENT_HISTORY in msg


def test_benchmark_basket_twr_two_snapshots():
    from pkscreener.classes.BenchmarkTracker import _basket_twr
    # Month 1: put in 200k, basket stays 200k → 0% return
    # Month 2: basket grows to 220k (after another 200k SIP)
    snapshots = [
        {"basket_total": 200_000, "monthly_sip": 200_000},
        {"basket_total": 420_000, "monthly_sip": 200_000},  # 220k grew to 220k + new 200k
    ]
    result = _basket_twr(snapshots)
    # 1m return: (420k - 200k - 200k) / (200k + 200k) = 20k / 400k = 5%
    assert result["basket_1m"] == pytest.approx(5.0, abs=0.1)


def test_benchmark_insufficient_history_less_than_2_snapshots():
    from pkscreener.classes.BenchmarkTracker import compute_rolling_returns, INSUFFICIENT_HISTORY
    bench_history = {"nifty50": [], "nifty500": []}
    rolling = compute_rolling_returns(bench_history, basket_snapshots=[])
    assert rolling["basket_status"] == INSUFFICIENT_HISTORY
    assert rolling["basket_1m"] is None
    assert rolling["basket_6m"] is None


# ---------------------------------------------------------------------------
# WealthBasketAllocator — readiness status and ETF suggestions
# ---------------------------------------------------------------------------

def test_readiness_status_no_scores():
    from pkscreener.classes.WealthBasketAllocator import compute_readiness_status
    assert compute_readiness_status([], snapshot_count=0, backtest_exists=False) == "NOT_READY"


def test_readiness_status_all_low_trust():
    from pkscreener.classes.WealthBasketAllocator import compute_readiness_status
    from pkscreener.classes.WealthBasketScorer import StockScore, Action
    scores = [
        StockScore(symbol="A", trust_level="LOW", action=Action.WATCH,
                   quality_score=50, momentum_score=50, valuation_score=50, combined_score=50),
        StockScore(symbol="B", trust_level="FAILED", action=Action.WATCH,
                   quality_score=50, momentum_score=50, valuation_score=50, combined_score=50),
    ]
    result = compute_readiness_status(scores, snapshot_count=5, backtest_exists=True)
    assert result == "EARLY_WEALTH_BASKET"


def test_readiness_status_medium_trust_no_snapshots():
    from pkscreener.classes.WealthBasketAllocator import compute_readiness_status
    from pkscreener.classes.WealthBasketScorer import StockScore, Action
    scores = [
        StockScore(symbol="A", trust_level="MEDIUM", action=Action.ADD_NEW,
                   quality_score=70, momentum_score=70, valuation_score=60, combined_score=68),
    ]
    result = compute_readiness_status(scores, snapshot_count=0, backtest_exists=False)
    assert result == "DATA_ONLY"


def test_readiness_status_ready_for_manual_review():
    from pkscreener.classes.WealthBasketAllocator import compute_readiness_status
    from pkscreener.classes.WealthBasketScorer import StockScore, Action
    scores = [
        StockScore(symbol="A", trust_level="MEDIUM", action=Action.ADD_NEW,
                   quality_score=70, momentum_score=70, valuation_score=60, combined_score=68),
    ]
    result = compute_readiness_status(scores, snapshot_count=4, backtest_exists=True)
    assert result == "READY_FOR_MANUAL_REVIEW"


def test_allocator_etf_suggestion_when_cash_above_30pct():
    from pkscreener.classes.WealthBasketAllocator import build_allocation_plan
    from pkscreener.classes.WealthBasketScorer import StockScore, Action
    # No ADD candidates → all cash held → ETF suggestion triggered
    low_score = StockScore(
        symbol="WEAK", sector="X", quality_score=30.0, momentum_score=30.0,
        valuation_score=30.0, combined_score=30.0, trust_level="LOW",
        action=Action.WATCH, action_reason="low score", risks="—",
        confidence="LOW", holding_period="—",
    )
    plan = build_allocation_plan([low_score], [], monthly_budget=200_000,
                                 underperformance_warning=False)
    assert plan.held_cash == pytest.approx(200_000)
    assert len(plan.etf_suggestions) >= 1
    etf = plan.etf_suggestions[0]
    assert etf.symbol == "NIFTYBEES"
    assert etf.amount > 0


def test_allocator_no_etf_suggestion_when_underperformance_active():
    from pkscreener.classes.WealthBasketAllocator import build_allocation_plan
    from pkscreener.classes.WealthBasketScorer import StockScore, Action
    low_score = StockScore(
        symbol="WEAK", sector="X", quality_score=30.0, momentum_score=30.0,
        valuation_score=30.0, combined_score=30.0, trust_level="LOW",
        action=Action.WATCH, action_reason="low score", risks="—",
        confidence="LOW", holding_period="—",
    )
    plan = build_allocation_plan([low_score], [], monthly_budget=200_000,
                                 underperformance_warning=True)
    assert len(plan.etf_suggestions) == 0, "ETF suggestion must be suppressed during underperformance"


# ---------------------------------------------------------------------------
# Backtest — XIRR calculation
# ---------------------------------------------------------------------------

def test_xirr_simple_known_rate():
    """Verify XIRR on a single investment + return at a known rate."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "scripts"))
    from backtest_wealth_basket import xirr

    # Invest 10000 on 2025-01-01, get back 11200 on 2026-01-01 → ~12% XIRR
    flows = [("2025-01-01", -10000.0), ("2026-01-01", 11200.0)]
    rate = xirr(flows)
    assert rate is not None
    assert rate == pytest.approx(0.12, abs=0.01), f"Expected ~12% XIRR, got {rate:.4f}"


def test_xirr_zero_gain_gives_zero():
    """Invest and get back exactly the same → 0% XIRR."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "scripts"))
    from backtest_wealth_basket import xirr

    flows = [("2025-01-01", -10000.0), ("2026-01-01", 10000.0)]
    rate = xirr(flows)
    assert rate is not None
    assert abs(rate) < 0.01, f"Zero-gain should give ~0% XIRR, got {rate:.4f}"


def test_xirr_negative_return():
    """Lose money → XIRR should be negative."""
    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).parent.parent / "scripts"))
    from backtest_wealth_basket import xirr

    flows = [("2025-01-01", -10000.0), ("2026-01-01", 9000.0)]
    rate = xirr(flows)
    assert rate is not None
    assert rate < 0, f"Loss should give negative XIRR, got {rate:.4f}"


# ---------------------------------------------------------------------------
# D/E normalization
# ---------------------------------------------------------------------------

def test_normalize_de_yfinance_percentage_scaled():
    """TCS-like: yfinance returns 10.39 (%) → normalized to 0.1039 ratio."""
    from pkscreener.classes.FundamentalsDataFetcher import normalize_debt_equity
    result = normalize_debt_equity(10.389, source="yfinance", sector="Technology")
    assert result == pytest.approx(0.1039, abs=0.001)


def test_normalize_de_high_percentage_scaled():
    """JSWSTEEL-like: 118.74 → 1.187 after normalization."""
    from pkscreener.classes.FundamentalsDataFetcher import normalize_debt_equity
    result = normalize_debt_equity(118.74, source="yfinance", sector="Basic Materials")
    assert result == pytest.approx(1.1874, abs=0.001)


def test_normalize_de_below_threshold_unchanged():
    """MARUTI-like: 0.096 < 3.0 threshold → kept as-is."""
    from pkscreener.classes.FundamentalsDataFetcher import normalize_debt_equity
    result = normalize_debt_equity(0.096, source="yfinance", sector="Consumer Cyclical")
    assert result == pytest.approx(0.096)


def test_normalize_de_none_preserved():
    from pkscreener.classes.FundamentalsDataFetcher import normalize_debt_equity
    assert normalize_debt_equity(None) is None


def test_normalize_de_negative_invalid():
    from pkscreener.classes.FundamentalsDataFetcher import normalize_debt_equity
    assert normalize_debt_equity(-5.0, source="yfinance", sector="Technology") is None


def test_normalize_de_financial_sector_not_normalized():
    """Financial companies: D/E returned raw regardless of magnitude."""
    from pkscreener.classes.FundamentalsDataFetcher import normalize_debt_equity
    # SHRIRAMFIN-like: 274.99 → kept as 274.99 (financial sector, skip normalization)
    result = normalize_debt_equity(274.99, source="yfinance", sector="Financial Services",
                                   industry="Credit Services")
    assert result == pytest.approx(274.99)


def test_normalize_de_screener_not_normalized():
    """screener.in already returns a ratio — do not divide."""
    from pkscreener.classes.FundamentalsDataFetcher import normalize_debt_equity
    result = normalize_debt_equity(1.5, source="screener", sector="Technology")
    assert result == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Financial sector hard-filter bypass
# ---------------------------------------------------------------------------

def test_financial_sector_skips_de_hard_filter():
    """Bank/NBFC with high D/E must not be falsely SKIP-ped."""
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    fund = {
        "trust_level": "LOW", "source": "yfinance",
        "sector": "Financial Services", "industry": "Credit Services",
        "is_financial": True,
        "pe_ratio": 15.0, "roe_pct": 16.0,
        "debt_equity": 274.99,   # raw yfinance NBFC value — must NOT trigger filter
        "revenue_growth_yoy": 15.0, "profit_growth_yoy": 20.0,
        "net_margin_pct": 18.0, "market_cap_cr": 80000, "fields_populated": 7,
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("BAJFINANCE", fund, df, is_held=False)
    assert result.action != Action.SKIP, \
        f"Financial company with high raw D/E must not SKIP — got {result.action}"
    assert result.hard_filter_triggered == "", \
        f"No hard filter should fire for financial sector, got: {result.hard_filter_triggered}"


def test_financial_sector_skips_current_ratio_filter():
    """Insurance company with low current_ratio must not be blocked."""
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    fund = {
        "trust_level": "LOW", "source": "yfinance",
        "sector": "Financial Services", "industry": "Insurance - Life",
        "is_financial": True,
        "pe_ratio": 20.0, "roe_pct": 14.0,
        "current_ratio": 0.3,   # low, but irrelevant for insurance
        "market_cap_cr": 50000, "fields_populated": 5,
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("HDFCLIFE", fund, df, is_held=False)
    assert result.hard_filter_triggered == "", \
        f"CR filter must not fire for financial sector, got: {result.hard_filter_triggered}"


def test_non_financial_high_real_de_still_blocked():
    """A genuinely over-leveraged non-financial company must still be blocked."""
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    fund = {
        "trust_level": "LOW", "source": "yfinance",
        "sector": "Industrials", "industry": "Engineering & Construction",
        "is_financial": False,
        "pe_ratio": 20.0, "roe_pct": 12.0,
        "debt_equity": 3.5,   # normalized ratio already above threshold
        "market_cap_cr": 30000, "fields_populated": 6,
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("HIGHDEBT", fund, df, is_held=False)
    assert result.action == Action.SKIP, \
        f"Non-financial with D/E 3.5 should SKIP, got {result.action}"
    assert "D/E" in result.hard_filter_triggered


def test_it_company_normalized_de_not_falsely_blocked():
    """TCS-like IT company: after D/E normalization 10.39→0.10, must not be SKIPped."""
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    from pkscreener.classes.FundamentalsDataFetcher import normalize_debt_equity
    raw_de = 10.389
    norm_de = normalize_debt_equity(raw_de, source="yfinance", sector="Technology")
    fund = {
        "trust_level": "LOW", "source": "yfinance",
        "sector": "Technology", "industry": "Information Technology Services",
        "is_financial": False,
        "pe_ratio": 30.0, "roe_pct": 48.0, "roce_pct": 55.0,
        "debt_equity": norm_de,   # 0.1039 after normalization
        "revenue_growth_yoy": 10.0, "profit_growth_yoy": 12.0,
        "net_margin_pct": 20.0, "market_cap_cr": 1400000, "fields_populated": 10,
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("TCS", fund, df, is_held=False)
    assert result.action != Action.SKIP, \
        f"TCS with normalized D/E 0.10 must not be SKIPped, got {result.action}"
    assert result.hard_filter_triggered == "", \
        f"No hard filter should fire, got: {result.hard_filter_triggered}"


# ---------------------------------------------------------------------------
# Trust level consistency — LOW when only yfinance used
# ---------------------------------------------------------------------------

def test_trust_stays_low_with_yfinance_only_even_if_all_fields_populated():
    """10/10 fields populated from yfinance alone must stay LOW trust."""
    from pkscreener.classes.WealthBasketScorer import score_stock
    fund = {
        "trust_level": "LOW",   # fetch-level trust
        "source": "yfinance",
        "pe_ratio": 22.0, "pb_ratio": 3.0, "roe_pct": 20.0, "roce_pct": 25.0,
        "debt_equity": 0.3, "revenue_growth_yoy": 15.0, "profit_growth_yoy": 20.0,
        "net_margin_pct": 15.0, "current_ratio": 2.0, "market_cap_cr": 50000,
        "fields_populated": 10,  # all 10 populated — must NOT auto-upgrade to MEDIUM
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("FULLDATA", fund, df, is_held=False)
    assert result.trust_level == "LOW", \
        f"Single-source yfinance must remain LOW trust even with 10/10 fields, got {result.trust_level}"


def test_trust_medium_only_from_composite_source():
    """trust_level=MEDIUM in data dict (from CompositeProvider) must propagate correctly."""
    from pkscreener.classes.WealthBasketScorer import score_stock
    fund = {
        "trust_level": "MEDIUM",   # set by CompositeProvider after cross-validation
        "source": "composite",
        "secondary_available": True,
        "pe_ratio": 22.0, "roe_pct": 20.0, "debt_equity": 0.3,
        "revenue_growth_yoy": 15.0, "profit_growth_yoy": 20.0,
        "net_margin_pct": 15.0, "market_cap_cr": 50000, "fields_populated": 8,
    }
    df = _make_price_df(250, trend="up")
    result = score_stock("TWODATA", fund, df, is_held=False)
    assert result.trust_level == "MEDIUM", \
        f"CompositeProvider MEDIUM trust must propagate, got {result.trust_level}"


# ---------------------------------------------------------------------------
# Bad symbol handling
# ---------------------------------------------------------------------------

def test_failed_trust_symbol_handled_gracefully():
    """A FAILED fetch must return a valid StockScore without crashing."""
    from pkscreener.classes.WealthBasketScorer import score_stock, Action
    failed_fund = {
        "trust_level": "FAILED",
        "source": "yfinance",
        "missing_fields": ["pe_ratio", "roe_pct", "debt_equity"],
        "fields_populated": 0,
    }
    result = score_stock("BADSYM", failed_fund, None, is_held=False)
    # Should not crash; trust stays LOW, no hard filter, action = WATCH or SKIP
    assert result is not None
    assert result.trust_level == "LOW"
    assert result.hard_filter_triggered == ""


def test_screener_failure_falls_back_to_yfinance(monkeypatch):
    """If CompositeProvider raises, fetch_fundamentals_with_screener must fallback gracefully."""
    from pkscreener.classes import FundamentalsDataFetcher

    def _mock_fetch(sym):
        return {
            "symbol": sym, "trust_level": "LOW", "source": "yfinance",
            "pe_ratio": 20.0, "roe_pct": 15.0, "debt_equity": 0.5,
            "fields_populated": 3, "missing_fields": [],
        }

    monkeypatch.setattr(FundamentalsDataFetcher, "fetch_fundamentals", _mock_fetch)

    import sys
    # Patch CompositeProvider to raise
    import unittest.mock as mock
    with mock.patch(
        "pkscreener.classes.FundamentalsDataFetcher.fetch_fundamentals_with_screener",
        side_effect=lambda sym, **kw: _mock_fetch(sym),
    ):
        result = FundamentalsDataFetcher.fetch_fundamentals_with_screener("TESTCO")
    assert result is not None
    assert result.get("trust_level") in ("LOW", "MEDIUM")
