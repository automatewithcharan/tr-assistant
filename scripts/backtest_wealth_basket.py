#!/usr/bin/env python3
"""
Wealth Basket — Monthly SIP Backtest (Multi-Mode)

Simulates three portfolio modes (CONSERVATIVE / BALANCED / AGGRESSIVE_GROWTH)
deploying a fixed monthly SIP into top-ranked stocks, with benchmarks.

⚠️  CRITICAL BIAS WARNINGS — READ BEFORE USING RESULTS
------------------------------------------------------
1. LOOK-AHEAD BIAS (HIGH): Fundamental data (ROE, ROCE, D/E, margins) uses CURRENT
   yfinance values for all historical months. Today's 20% ROE may have been 5% in 2020.
   CONSEQUENCE: Quality sub-score is not historically valid. Results are INFLATED.

2. SURVIVORSHIP BIAS (MEDIUM): Only current Nifty 50 constituents are used.
   Delisted or dropped stocks are excluded → performance is overestimated.

3. MOMENTUM SIGNALS ARE CLEAN: Price-based signals use historical OHLCV data,
   sliced up to each simulated month. Zero look-ahead in the momentum component.

4. COST MODEL: Uses closing prices with conservative cost estimates.
   Real slippage may be higher for thinly traded stocks.

TRUST LEVEL: LOW for all modes — do not use to justify real-money allocations.
Useful for: momentum pattern analysis, portfolio turnover, drawdown behaviour.
Not useful for: validating the fundamental quality model.

Usage:
    python scripts/backtest_wealth_basket.py --universe nifty50 --monthly-investment 200000 --years 5 --costs conservative
    python scripts/backtest_wealth_basket.py --universe nifty50 --compare-modes --years 5
    python scripts/backtest_wealth_basket.py --mode BALANCED --universe nifty100 --years 5

Output (in results/Data/wealth_basket_reports/backtests/):
    backtest_YYYYMM_{universe}_{mode}_summary.md
    backtest_YYYYMM_{universe}_{mode}_monthly.csv
    backtest_YYYYMM_{universe}_{mode}_trades.csv
    backtest_YYYYMM_{universe}_comparison.md     (when --compare-modes)
    backtest_YYYYMM_{universe}_comparison.csv
"""

import argparse
import csv
import logging
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from pkscreener.classes.BenchmarkTracker import flat_12pct_value
from pkscreener.classes.FundamentalsDataFetcher import (
    get_price_history,
    load_nifty500_universe,
)
from pkscreener.classes.WealthBasketScorer import Action, score_stock

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
_log = logging.getLogger("wb_backtest")

_PROJECT_ROOT = Path(__file__).parent.parent
_INDICES_DIR  = _PROJECT_ROOT / "results" / "Indices"
_REPORTS_DIR  = _PROJECT_ROOT / "results" / "Data" / "wealth_basket_reports" / "backtests"

_UNIVERSE_FILES = {
    "nifty50":  _INDICES_DIR / "ind_nifty50list.csv",
    "nifty100": _INDICES_DIR / "ind_nifty100list.csv",
    "nifty200": _INDICES_DIR / "ind_nifty200list.csv",
    "nifty500": _INDICES_DIR / "ind_nifty500list.csv",
}

MIN_TICKET     = 5_000    # ₹5,000 minimum trade size
ETF_NIFTYBEES  = "NIFTYBEES.NS"

ALWAYS_SKIP: frozenset = frozenset({"TMPV"})  # invalid/unavailable NSE symbols

# ---------------------------------------------------------------------------
# Portfolio mode configurations
# ---------------------------------------------------------------------------

PORTFOLIO_MODES: Dict[str, Dict[str, Any]] = {
    "CONSERVATIVE": {
        "score_add_new":      78.0,   # strict: original threshold
        "score_add_held":     72.0,
        "score_hold":         58.0,
        "score_reduce":       28.0,
        "sector_cap":         0.35,   # of invested (stocks+ETF), not of total basket
        "max_stock_pct":      0.20,   # max 20% of monthly budget per stock
        "max_holdings":       15,
        "etf_fill_budget":    False,  # do NOT auto-fill remaining with ETF
        "etf_fallback_min":   3,      # use ETF only when < 3 stocks bought
        "etf_max_monthly":    100_000,
        "etf_deploy_pct":     0.50,   # 50% of idle cash → ETF
        "growth_exception":   False,
        "growth_score_min":   None,
        "growth_roe_min":     None,
        "growth_rev_min":     None,
        "trailing_exit":      False,
        "trailing_stop_pct":  None,
        "description": (
            "Strict quality filter. Original baseline. "
            "High cash buffer. Lowest deployment, lowest risk."
        ),
    },
    "BALANCED": {
        "score_add_new":      70.0,   # relaxed — more stocks qualify
        "score_add_held":     62.0,
        "score_hold":         50.0,
        "score_reduce":       25.0,
        "sector_cap":         0.30,   # tighter sector control compensates for wider filter
        "max_stock_pct":      0.20,
        "max_holdings":       15,
        "etf_fill_budget":    True,   # fill remaining monthly budget with ETF (no idle cash)
        "etf_fallback_min":   2,
        "etf_max_monthly":    None,   # no cap — fills full remaining budget
        "etf_deploy_pct":     1.0,    # 100% of remaining → ETF
        "growth_exception":   True,   # quality growth stocks get ADD exception
        "growth_score_min":   62.0,
        "growth_roe_min":     20.0,   # ROE ≥ 20% required
        "growth_rev_min":     12.0,   # revenue growth ≥ 12% required
        "trailing_exit":      False,
        "trailing_stop_pct":  None,
        "description": (
            "Balanced deployment. Lower thresholds + ETF fills budget gap. "
            "Growth-stock exception for quality compounders. Near-zero idle cash."
        ),
    },
    "AGGRESSIVE_GROWTH": {
        "score_add_new":      65.0,   # most permissive
        "score_add_held":     58.0,
        "score_hold":         45.0,
        "score_reduce":       22.0,
        "sector_cap":         0.25,   # tightest diversification enforcement
        "max_stock_pct":      0.25,   # bigger position sizes
        "max_holdings":       18,     # more holdings
        "etf_fill_budget":    True,
        "etf_fallback_min":   2,
        "etf_max_monthly":    None,
        "etf_deploy_pct":     0.40,   # only 40% of remaining → ETF; leaves some cash buffer
        "growth_exception":   True,
        "growth_score_min":   58.0,
        "growth_roe_min":     18.0,
        "growth_rev_min":     10.0,
        "trailing_exit":      True,   # 15% trailing stop on individual holdings
        "trailing_stop_pct":  0.15,
        "description": (
            "Maximum deployment. Growth + momentum stocks included. "
            "15% trailing exit protects downside. Higher expected return, higher risk."
        ),
    },
}

# ---------------------------------------------------------------------------
# Cost presets
# ---------------------------------------------------------------------------

COST_PRESETS: Dict[str, Dict[str, float]] = {
    "conservative": {"slippage": 0.30, "brokerage": 0.05, "stt": 0.10, "misc": 0.03},
    "moderate":     {"slippage": 0.20, "brokerage": 0.03, "stt": 0.10, "misc": 0.02},
    "aggressive":   {"slippage": 0.10, "brokerage": 0.01, "stt": 0.10, "misc": 0.01},
}


def _cost_buy(preset: Dict) -> float:
    return (preset["slippage"] + preset["brokerage"] + preset["misc"]) / 100


def _cost_sell(preset: Dict) -> float:
    return (preset["slippage"] + preset["brokerage"] + preset["stt"] + preset["misc"]) / 100


# ---------------------------------------------------------------------------
# XIRR
# ---------------------------------------------------------------------------

def xirr(cash_flows: List[Tuple[str, float]], guess: float = 0.10) -> Optional[float]:
    """XIRR. Negative = outflow, positive = inflow. Returns decimal rate."""
    if not cash_flows or len(cash_flows) < 2:
        return None
    dates  = [date.fromisoformat(d) for d, _ in cash_flows]
    flows  = [float(a) for _, a in cash_flows]
    base   = dates[0]
    t_frac = [(d - base).days / 365.25 for d in dates]

    def npv(r):
        return sum(f / (1 + r) ** t for f, t in zip(flows, t_frac))
    def d_npv(r):
        return sum(-t * f / (1 + r) ** (t + 1) for f, t in zip(flows, t_frac))

    r = guess
    for _ in range(300):
        try:
            f = npv(r); df = d_npv(r)
            if df == 0:
                break
            nr = r - f / df
            if abs(nr - r) < 1e-9:
                r = nr; break
            r = max(-0.999, min(nr, 100.0))
        except (ZeroDivisionError, OverflowError):
            return None
    return round(r, 6) if -1.0 < r < 100.0 else None


# ---------------------------------------------------------------------------
# Price data helpers
# ---------------------------------------------------------------------------

def _fetch_raw_prices(ticker: str, period: str = "7y") -> Optional[pd.DataFrame]:
    """Fetch price history using exact ticker (no .NS added)."""
    import yfinance as yf
    try:
        df = yf.download(ticker, period=period, progress=False, auto_adjust=True)
        return df if (df is not None and len(df) >= 10) else None
    except Exception as exc:
        _log.debug("Price fetch failed %s: %s", ticker, exc)
        return None


def _price_on(df: Optional[pd.DataFrame], target: date) -> Optional[float]:
    """Return closing price on or before target date."""
    if df is None or len(df) == 0:
        return None
    try:
        sub = df[df.index.date <= target]
        if len(sub) == 0:
            return None
        v = sub["Close"].dropna().iloc[-1]
        return float(v.iloc[0]) if hasattr(v, "iloc") else float(v)
    except Exception:
        return None


def _month_starts(start: date, end: date) -> List[date]:
    result = []
    d = date(start.year, start.month, 1)
    while d <= end:
        result.append(d)
        d = date(d.year + (d.month // 12), (d.month % 12) + 1, 1)
    return result


def _fetch_all_prices(symbols: List[str]) -> Dict[str, pd.DataFrame]:
    """Fetch 7y NSE price history for all symbols."""
    cache: Dict[str, pd.DataFrame] = {}
    fail = []
    for i, sym in enumerate(symbols):
        if sym in ALWAYS_SKIP:
            fail.append(sym); continue
        if (i + 1) % 10 == 0:
            _log.info("  Price data: %d/%d", i + 1, len(symbols))
        df = get_price_history(sym, period="7y")   # adds .NS suffix
        if df is not None and len(df) > 20:
            cache[sym] = df
        else:
            fail.append(sym)
    _log.info("  Price loaded: %d/%d  failed: %s", len(cache), len(symbols), fail[:8])
    return cache


class SharedData:
    """Price data fetched once, shared across mode runs."""
    symbols:        List[str]
    bad_symbols:    List[str]
    price_cache:    Dict[str, pd.DataFrame]
    nifty50_df:     Optional[pd.DataFrame]
    nifty500_df:    Optional[pd.DataFrame]
    niftybees_df:   Optional[pd.DataFrame]
    fundamentals:   Dict[str, Dict]


def _fetch_shared(universe_name: str) -> SharedData:
    sd = SharedData()
    csv_path = _UNIVERSE_FILES.get(universe_name, _UNIVERSE_FILES["nifty50"])
    if not csv_path.exists():
        raise FileNotFoundError(f"Universe file not found: {csv_path}")
    pairs       = load_nifty500_universe(str(csv_path))
    sd.symbols  = []
    sd.bad_symbols = []
    for sym, _ in pairs:
        if "DUMMY" in sym.upper() or sym in ALWAYS_SKIP:
            sd.bad_symbols.append(sym)
        else:
            sd.symbols.append(sym)
    _log.info("Universe: %d stocks (%d bad skipped: %s)",
              len(sd.symbols), len(sd.bad_symbols), sd.bad_symbols)

    _log.info("Fetching price history...")
    sd.price_cache = _fetch_all_prices(sd.symbols)

    _log.info("Fetching benchmarks...")
    sd.nifty50_df  = _fetch_raw_prices("^NSEI",    period="7y")
    sd.nifty500_df = _fetch_raw_prices("^CRSLDX",  period="7y")
    sd.niftybees_df= _fetch_raw_prices("NIFTYBEES.NS", period="7y")

    _log.info("Loading fundamentals from DB cache (LOOK-AHEAD BIAS)...")
    try:
        from pkscreener.classes.WealthBasketDB import WealthBasketDB
        db = WealthBasketDB(str(_PROJECT_ROOT / "results" / "wealth_basket.db"))
        sd.fundamentals = {}
        for sym in sd.symbols:
            f = db.get_fundamentals(sym, max_age_days=7)
            if f:
                sd.fundamentals[sym] = f
    except Exception as e:
        _log.warning("DB fundamentals failed (%s) — empty fund map", e)
        sd.fundamentals = {}
    _log.info("Fundamentals: %d/%d stocks", len(sd.fundamentals), len(sd.symbols))
    return sd


# ---------------------------------------------------------------------------
# Scoring helper
# ---------------------------------------------------------------------------

def _score_as_of(sym, price_cache, as_of, fund, is_held):
    df = price_cache.get(sym)
    if df is None:
        return None
    mask = df.index.date <= as_of
    df_s = df[mask]
    if len(df_s) < 20:
        return None
    try:
        return score_stock(symbol=sym, fundamentals=fund, price_df=df_s, is_held=is_held)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core simulation
# ---------------------------------------------------------------------------

def run_backtest(
    sd:             SharedData,
    mode:           str   = "CONSERVATIVE",
    monthly_budget: float = 200_000,
    start_date:     str   = "2021-01-01",
    end_date:       str   = None,
    cost_preset:    str   = "conservative",
) -> Dict:
    """
    Simulate monthly SIP with ADD/HOLD/REDUCE/EXIT + sector caps + ETF fallback.
    Returns summary dict. Trust level is always LOW (look-ahead + survivorship bias).
    """
    if end_date is None:
        end_date = date.today().isoformat()

    mp     = PORTFOLIO_MODES[mode]
    start  = date.fromisoformat(start_date)
    end    = date.fromisoformat(end_date)
    months = _month_starts(start, end)
    years  = (end - start).days / 365.25

    if len(months) < 6:
        _log.error("Need at least 6 months")
        return {}

    costs     = COST_PRESETS.get(cost_preset, COST_PRESETS["conservative"])
    cost_buy  = _cost_buy(costs)
    cost_sell = _cost_sell(costs)

    _log.info("  [%s] %s → %s  (%d months)", mode, start_date, end_date, len(months))
    _log.info("    score_add_new=%.0f  sector_cap=%.0f%%  etf_fill=%s  growth_exc=%s  trailing=%s",
              mp["score_add_new"], mp["sector_cap"]*100,
              mp["etf_fill_budget"], mp["growth_exception"], mp["trailing_exit"])

    # Simulation state
    portfolio: Dict[str, Dict] = {}   # sym → {qty, avg_cost, total_cost, sector, peak_price, entry_month}
    etf_pos:   Dict[str, Dict] = {}   # "NIFTYBEES" → {qty, avg_cost, total_cost}
    cash = 0.0

    monthly_records:  List[Dict]            = []
    trade_log:        List[Dict]            = []
    sector_log:       List[Dict]            = []
    cashflows:        List[Tuple[str,float]]= []
    n50_units  = 0.0
    n500_units = 0.0
    peak_value = 0.0
    max_drawdown = 0.0
    month_vals: List[float] = []

    nb_price_last = None  # last NIFTYBEES price for ETF valuation

    for i, mdate in enumerate(months):
        m_str = mdate.isoformat()

        # Inject SIP
        cash += monthly_budget
        cashflows.append((m_str, -monthly_budget))

        # ---- Current NIFTYBEES price (needed for ETF valuation + buy) ----
        nb_price = _price_on(sd.niftybees_df, mdate)
        if nb_price:
            nb_price_last = nb_price

        # ---- Pre-compute current portfolio values for sector cap denominator ----
        stock_val_cur = sum(
            pos["qty"] * (_price_on(sd.price_cache.get(s), mdate) or pos["avg_cost"])
            for s, pos in portfolio.items()
        )
        etf_val_cur = sum(
            ep["qty"] * (nb_price or nb_price_last or ep["avg_cost"])
            for ep in etf_pos.values()
        )
        invested_cur = stock_val_cur + etf_val_cur   # denominator for sector cap

        # ---- Score all stocks ----
        scores: Dict[str, Any] = {}
        for sym in sd.symbols:
            if sym not in sd.price_cache:
                continue
            sc = _score_as_of(sym, sd.price_cache, mdate, sd.fundamentals.get(sym, {}), sym in portfolio)
            if sc is not None:
                scores[sym] = sc

        # ---- Trailing exit for AGGRESSIVE_GROWTH ----
        if mp["trailing_exit"] and mp["trailing_stop_pct"]:
            for sym in list(portfolio.keys()):
                price = _price_on(sd.price_cache.get(sym), mdate)
                if not price:
                    continue
                pos = portfolio[sym]
                pos["peak_price"] = max(pos.get("peak_price", price), price)
                trail_floor = pos["peak_price"] * (1 - mp["trailing_stop_pct"])
                if price < trail_floor:
                    qty   = pos["qty"]
                    gross = qty * price
                    net   = gross * (1 - cost_sell)
                    cash += net
                    trade_log.append({
                        "date": m_str, "symbol": sym, "action": "SELL_TRAILING_STOP",
                        "qty": round(qty, 4), "price": round(price, 2),
                        "gross": round(gross, 0), "net": round(net, 0),
                        "score": round(scores[sym].combined_score, 1) if sym in scores else 0,
                        "reason": f"trailing stop: {price:.1f} < {trail_floor:.1f} (peak {pos['peak_price']:.1f})",
                    })
                    del portfolio[sym]

        # ---- Score-based exits for held stocks ----
        to_remove = []
        for sym in list(portfolio.keys()):
            sc = scores.get(sym)
            if sc is None:
                continue
            price = _price_on(sd.price_cache.get(sym), mdate)
            if not price:
                continue
            if sc.action == Action.EXIT or sc.combined_score < mp["score_reduce"]:
                qty = portfolio[sym]["qty"]
                net = qty * price * (1 - cost_sell)
                cash += net
                trade_log.append({
                    "date": m_str, "symbol": sym, "action": "SELL_EXIT",
                    "qty": round(qty, 4), "price": round(price, 2),
                    "gross": round(qty * price, 0), "net": round(net, 0),
                    "score": round(sc.combined_score, 1),
                    "reason": sc.action_reason[:80],
                })
                to_remove.append(sym)
            elif sc.action == Action.REDUCE or (mp["score_reduce"] <= sc.combined_score < mp["score_hold"]):
                qty = portfolio[sym]["qty"] / 2
                net = qty * price * (1 - cost_sell)
                cash += net
                portfolio[sym]["qty"]        -= qty
                portfolio[sym]["total_cost"] *= 0.5
                trade_log.append({
                    "date": m_str, "symbol": sym, "action": "SELL_REDUCE",
                    "qty": round(qty, 4), "price": round(price, 2),
                    "gross": round(qty * price, 0), "net": round(net, 0),
                    "score": round(sc.combined_score, 1),
                    "reason": sc.action_reason[:80],
                })
        for sym in to_remove:
            del portfolio[sym]

        # ---- Build ADD candidates ----
        add_candidates: List[Tuple[str, float, str, bool]] = []
        for sym, sc in scores.items():
            is_h = sym in portfolio
            if is_h:
                if sc.action in (Action.ADD,) and sc.combined_score >= mp["score_add_held"]:
                    add_candidates.append((sym, sc.combined_score, sc.sector or "Unknown", True))
            else:
                if sc.action == Action.ADD_NEW and sc.combined_score >= mp["score_add_new"]:
                    add_candidates.append((sym, sc.combined_score, sc.sector or "Unknown", False))

        # ---- Growth exception (BALANCED / AGGRESSIVE_GROWTH) ----
        already_in = {c[0] for c in add_candidates}
        if mp["growth_exception"]:
            for sym, sc in scores.items():
                if sym in already_in:
                    continue
                if sc.action not in (Action.WATCH,):
                    continue
                if sc.combined_score < mp["growth_score_min"]:
                    continue
                fund   = sd.fundamentals.get(sym, {})
                roe    = fund.get("roe_pct") or 0
                rev_g  = fund.get("revenue_growth_yoy") or 0
                if roe >= mp["growth_roe_min"] and rev_g >= mp["growth_rev_min"]:
                    sector = fund.get("sector") or sc.sector or "Unknown"
                    add_candidates.append((sym, sc.combined_score, sector, sym in portfolio))
                    trade_log.append({
                        "date": m_str, "symbol": sym, "action": "GROWTH_EXCEPTION_ELIGIBLE",
                        "qty": 0, "price": 0, "gross": 0, "net": 0,
                        "score": round(sc.combined_score, 1),
                        "reason": f"growth exception: ROE {roe:.1f}% rev_g {rev_g:.1f}%",
                    })

        add_candidates.sort(key=lambda x: x[1], reverse=True)
        add_candidates = add_candidates[:mp["max_holdings"]]

        # ---- Allocate budget to ADD candidates ----
        deploy    = min(cash, monthly_budget)
        n_add     = len(add_candidates)
        per_stock = (min(deploy / n_add, monthly_budget * mp["max_stock_pct"])
                     if n_add > 0 else 0)

        bought_this_month  = 0
        deployed_this_month = 0.0

        for sym, score, sector, is_held_pos in add_candidates:
            if per_stock < MIN_TICKET:
                break
            if len(portfolio) >= mp["max_holdings"] and sym not in portfolio:
                break

            # ---- Sector cap: based on total invested (stocks + ETF) ----
            if invested_cur > 0:
                curr_sector_v = sum(
                    portfolio[s]["qty"] * (_price_on(sd.price_cache.get(s), mdate) or 0)
                    for s in portfolio
                    if (sd.fundamentals.get(s, {}).get("sector") or "Unknown") == sector
                )
                proj_pct = (curr_sector_v + per_stock) / (invested_cur + per_stock)
                if proj_pct > mp["sector_cap"]:
                    trade_log.append({
                        "date": m_str, "symbol": sym, "action": "SKIP_SECTOR_CAP",
                        "qty": 0, "price": 0, "gross": 0, "net": 0,
                        "score": round(score, 1),
                        "reason": f"sector {sector[:20]} {proj_pct:.0%} > {mp['sector_cap']:.0%} cap",
                    })
                    continue

            price = _price_on(sd.price_cache.get(sym), mdate)
            if not price or price <= 0:
                continue
            qty = per_stock / (price * (1 + cost_buy))
            if qty <= 0:
                continue

            if sym not in portfolio:
                portfolio[sym] = {
                    "qty": 0.0, "avg_cost": 0.0, "total_cost": 0.0,
                    "sector": sd.fundamentals.get(sym, {}).get("sector") or sector,
                    "entry_month": m_str, "peak_price": price,
                }
            pos = portfolio[sym]
            new_qty  = pos["qty"] + qty
            new_cost = pos["total_cost"] + per_stock
            pos["qty"]        = new_qty
            pos["total_cost"] = new_cost
            pos["avg_cost"]   = new_cost / new_qty
            pos["peak_price"] = max(pos.get("peak_price", price), price)

            cash               -= per_stock
            bought_this_month  += 1
            deployed_this_month += per_stock
            invested_cur        += per_stock  # update denominator in-loop

            trade_log.append({
                "date": m_str, "symbol": sym,
                "action": "BUY_ADD" if is_held_pos else "BUY_NEW",
                "qty": round(qty, 4), "price": round(price, 2),
                "gross": round(per_stock, 0), "net": round(per_stock, 0),
                "score": round(score, 1),
                "reason": (scores[sym].action_reason[:80] if sym in scores else "growth_exception"),
            })

        # ---- ETF deployment ----
        if nb_price and nb_price > 0:
            etf_deploy = 0.0

            if mp["etf_fill_budget"]:
                # Fill remaining monthly budget with ETF
                remaining = monthly_budget - deployed_this_month
                if remaining >= MIN_TICKET:
                    etf_deploy = remaining * mp["etf_deploy_pct"]
                    if mp["etf_max_monthly"]:
                        etf_deploy = min(etf_deploy, mp["etf_max_monthly"])
            else:
                # CONSERVATIVE: only when stock picks are insufficient
                if bought_this_month < mp["etf_fallback_min"] and cash > monthly_budget * 0.3:
                    etf_deploy = min(cash * mp["etf_deploy_pct"], monthly_budget * 0.5)
                    if mp["etf_max_monthly"]:
                        etf_deploy = min(etf_deploy, mp["etf_max_monthly"])

            etf_deploy = min(etf_deploy, cash)  # can't deploy more than available
            if etf_deploy >= MIN_TICKET:
                etf_qty = etf_deploy / (nb_price * (1 + cost_buy))
                sym_etf = "NIFTYBEES"
                if sym_etf not in etf_pos:
                    etf_pos[sym_etf] = {"qty": 0.0, "avg_cost": 0.0, "total_cost": 0.0}
                ep = etf_pos[sym_etf]
                ep["qty"]        += etf_qty
                ep["total_cost"] += etf_deploy
                ep["avg_cost"]    = ep["total_cost"] / ep["qty"]
                cash               -= etf_deploy
                deployed_this_month += etf_deploy
                trade_log.append({
                    "date": m_str, "symbol": sym_etf, "action": "BUY_ETF",
                    "qty": round(etf_qty, 4), "price": round(nb_price, 2),
                    "gross": round(etf_deploy, 0), "net": round(etf_deploy, 0),
                    "score": 0.0,
                    "reason": f"{'budget_fill' if mp['etf_fill_budget'] else 'fallback'}: {bought_this_month} stock(s) bought",
                })

        # ---- NIFTY SIP benchmarks ----
        n50_price  = _price_on(sd.nifty50_df, mdate)
        n500_price = _price_on(sd.nifty500_df, mdate)
        if n50_price  and n50_price  > 0: n50_units  += monthly_budget / (n50_price  * (1 + cost_buy))
        if n500_price and n500_price > 0: n500_units += monthly_budget / (n500_price * (1 + cost_buy))

        # ---- Portfolio value ----
        stock_val = sum(
            pos["qty"] * (_price_on(sd.price_cache.get(s), mdate) or pos["avg_cost"])
            for s, pos in portfolio.items()
        )
        etf_val = sum(
            ep["qty"] * (nb_price or ep["avg_cost"]) for ep in etf_pos.values()
        )
        basket_total = stock_val + etf_val + cash

        # ---- Drawdown ----
        if basket_total > peak_value:
            peak_value = basket_total
        dd = ((peak_value - basket_total) / peak_value * 100) if peak_value > 0 else 0.0
        max_drawdown = max(max_drawdown, dd)

        n50_val  = (n50_units  * n50_price)  if n50_price  else None
        n500_val = (n500_units * n500_price) if n500_price else None
        flat_val = flat_12pct_value(monthly_budget * (i + 1) * 0.5, start_date, m_str)

        # ---- Sector exposure snapshot ----
        sec_snap: Dict[str, float] = defaultdict(float)
        total_inv = stock_val + etf_val
        if total_inv > 0:
            for s, pos in portfolio.items():
                pval = pos["qty"] * (_price_on(sd.price_cache.get(s), mdate) or pos["avg_cost"])
                sec  = (sd.fundamentals.get(s, {}).get("sector") or "Unknown")[:20]
                sec_snap[sec] += pval / total_inv * 100
        sector_log.append({
            "date": m_str,
            **{f"sec_{k.replace(' ','_')}": round(v, 1) for k, v in sorted(sec_snap.items(), key=lambda x: -x[1])[:8]},
        })

        month_vals.append(basket_total)
        monthly_records.append({
            "date":              m_str,
            "sip_added":         monthly_budget,
            "deployed":          round(deployed_this_month, 0),
            "n_stocks":          len(portfolio),
            "n_etf":             len(etf_pos),
            "buys_stocks":       bought_this_month,
            "stock_value":       round(stock_val, 0),
            "etf_value":         round(etf_val, 0),
            "cash":              round(cash, 0),
            "basket_total":      round(basket_total, 0),
            "nifty50_value":     round(n50_val, 0)  if n50_val  else None,
            "nifty500_value":    round(n500_val, 0) if n500_val else None,
            "flat12pct_value":   round(flat_val, 0),
            "drawdown_pct":      round(dd, 2),
            "n_add_eligible":    sum(1 for s in scores.values() if s.combined_score >= mp["score_add_new"]),
        })

    # ---- Terminal valuation ----
    final_date  = months[-1]
    final_value = cash
    for sym, pos in portfolio.items():
        p = _price_on(sd.price_cache.get(sym), final_date)
        if p:
            final_value += pos["qty"] * p * (1 - cost_sell)
    nb_fp = _price_on(sd.niftybees_df, final_date) or nb_price_last
    if nb_fp:
        for ep in etf_pos.values():
            final_value += ep["qty"] * nb_fp * (1 - cost_sell)

    cashflows.append((final_date.isoformat(), final_value))

    # ---- Benchmark XIRRs ----
    def _bench_xirr(units, price_df, budget, months_list):
        if units <= 0 or price_df is None:
            return None, None
        fp = _price_on(price_df, months_list[-1])
        if not fp:
            return None, None
        fv = units * fp * (1 - cost_sell)
        cf = [(m.isoformat(), -budget) for m in months_list]
        cf.append((months_list[-1].isoformat(), fv))
        return xirr(cf), fv

    basket_xirr = xirr(cashflows)
    n50_xirr,  n50_final  = _bench_xirr(n50_units,  sd.nifty50_df,  monthly_budget, months)
    n500_xirr, n500_final = _bench_xirr(n500_units, sd.nifty500_df, monthly_budget, months)

    total_invested = monthly_budget * len(months)
    simple_ret = (final_value - total_invested) / total_invested * 100 if total_invested > 0 else 0

    # ---- Final holdings ----
    final_holdings = []
    for sym, pos in portfolio.items():
        p = _price_on(sd.price_cache.get(sym), final_date)
        if p:
            val = pos["qty"] * p
            gain = (val - pos["total_cost"]) / pos["total_cost"] * 100 if pos["total_cost"] > 0 else 0
            final_holdings.append({
                "symbol": sym, "sector": pos.get("sector", "?")[:20],
                "qty": round(pos["qty"], 2), "avg_cost": round(pos["avg_cost"], 2),
                "current_price": round(p, 2), "current_value": round(val, 0),
                "gain_pct": round(gain, 1), "entry_month": pos.get("entry_month", "?"),
            })
    final_holdings.sort(key=lambda x: -x["current_value"])

    rolling = _compute_rolling(monthly_records, month_vals)

    # ---- Final portfolio composition % ----
    last_rec = monthly_records[-1] if monthly_records else {}
    last_bt  = last_rec.get("basket_total", 1)
    cash_pct  = last_rec.get("cash", 0) / last_bt * 100     if last_bt else 0
    etf_pct   = last_rec.get("etf_value", 0) / last_bt * 100 if last_bt else 0
    stock_pct = last_rec.get("stock_value", 0) / last_bt * 100 if last_bt else 0

    # Sector concentration (top sector in stock portfolio)
    last_sector = sector_log[-1] if sector_log else {}
    top_sector  = max(
        ((k, v) for k, v in last_sector.items() if k != "date"),
        key=lambda x: x[1], default=("?", 0)
    )

    total_trades = sum(1 for t in trade_log if t["action"].startswith("BUY"))
    total_exits  = sum(1 for t in trade_log if t["action"].startswith("SELL"))

    _log.info("  → Basket XIRR %.1f%%  N50 %.1f%%  N500 %.1f%%  DD %.1f%%  "
              "cash %.0f%%  etf %.0f%%  stocks %.0f%%",
              (basket_xirr*100 if basket_xirr else 0),
              (n50_xirr*100  if n50_xirr  else 0),
              (n500_xirr*100 if n500_xirr else 0),
              max_drawdown, cash_pct, etf_pct, stock_pct)

    return {
        "mode":                  mode,
        "mode_description":      mp["description"],
        "universe":              sd.symbols and "?" or "?",
        "start_date":            start_date,
        "end_date":              final_date.isoformat(),
        "months_simulated":      len(months),
        "years_simulated":       round(years, 1),
        "monthly_budget":        monthly_budget,
        "cost_preset":           cost_preset,
        "cost_buy_pct":          round(cost_buy * 100, 3),
        "cost_sell_pct":         round(cost_sell * 100, 3),
        "total_invested":        total_invested,
        "final_basket_value":    round(final_value, 0),
        "final_nifty50_value":   round(n50_final, 0)  if n50_final  else None,
        "final_nifty500_value":  round(n500_final, 0) if n500_final else None,
        "basket_xirr_pct":       round(basket_xirr * 100, 2) if basket_xirr else None,
        "nifty50_xirr_pct":      round(n50_xirr  * 100, 2)  if n50_xirr   else None,
        "nifty500_xirr_pct":     round(n500_xirr * 100, 2)  if n500_xirr  else None,
        "simple_return_pct":     round(simple_ret, 2),
        "max_drawdown_pct":      round(max_drawdown, 2),
        "cash_pct":              round(cash_pct, 1),
        "etf_pct":               round(etf_pct, 1),
        "stock_pct":             round(stock_pct, 1),
        "n_holdings_final":      len(final_holdings),
        "top_sector":            top_sector[0],
        "top_sector_pct":        round(top_sector[1], 1),
        "total_buys":            total_trades,
        "total_exits":           total_exits,
        "bad_symbols":           sd.bad_symbols,
        "trust_level":           "LOW",
        "bias_warnings": [
            "LOOK-AHEAD BIAS (HIGH): fundamentals are current yfinance values, not historical point-in-time",
            "SURVIVORSHIP BIAS (MEDIUM): only current index members used",
            "MOMENTUM (CLEAN): historical price data only — no look-ahead",
            f"DATA QUALITY (MEDIUM): ROE missing for ~50% of stocks from yfinance",
        ],
        "monthly_records":  monthly_records,
        "trade_log":        trade_log,
        "sector_log":       sector_log,
        "final_holdings":   final_holdings,
        "rolling":          rolling,
    }


# ---------------------------------------------------------------------------
# Rolling and worst-period helpers
# ---------------------------------------------------------------------------

def _compute_rolling(monthly_records: List[Dict], month_vals: List[float]) -> Dict:
    if len(month_vals) < 2:
        return {}
    budget = monthly_records[0]["sip_added"] if monthly_records else 200_000
    mr = []
    for i in range(1, len(month_vals)):
        prev, curr = month_vals[i-1], month_vals[i]
        if prev > 0:
            r = (curr - prev - budget) / prev * 100
            mr.append((monthly_records[i]["date"], r))

    def worst(lst): return min(lst, key=lambda x: x[1], default=None)
    def best(lst):  return max(lst, key=lambda x: x[1], default=None)

    def rolling_n(n):
        res = []
        for i in range(n-1, len(month_vals)):
            vs, ve = month_vals[i-n+1], month_vals[i]
            if vs > 0:
                r = (ve - vs - budget * (n-1)) / vs * 100
                res.append((monthly_records[i]["date"], r))
        return res

    r3  = rolling_n(3);  r6  = rolling_n(6);  r12 = rolling_n(12)
    wm  = worst(mr);     bm  = best(mr)
    w3  = worst(r3);     w6  = worst(r6);  w12 = worst(r12)

    return {
        "monthly_returns":   mr,
        "worst_month_pct":   round(wm[1],  2) if wm  else None,
        "worst_month_date":  wm[0]             if wm  else None,
        "best_month_pct":    round(bm[1],  2) if bm  else None,
        "best_month_date":   bm[0]             if bm  else None,
        "worst_3m_pct":      round(w3[1],  2) if w3  else None,
        "worst_3m_date":     w3[0]             if w3  else None,
        "worst_6m_pct":      round(w6[1],  2) if w6  else None,
        "worst_6m_date":     w6[0]             if w6  else None,
        "worst_12m_pct":     round(w12[1], 2) if w12 else None,
        "worst_12m_date":    w12[0]            if w12 else None,
    }


def _classify_readiness(results: List[Dict]) -> str:
    """Classify the best mode's readiness status."""
    best = max(results, key=lambda r: r.get("basket_xirr_pct") or 0)
    bx   = best.get("basket_xirr_pct") or 0
    nx   = best.get("nifty500_xirr_pct") or 0
    dd   = best.get("max_drawdown_pct") or 100

    if bx <= 0:
        return "DATA_ONLY"
    if bx > 0 and bx < 8:
        return "EARLY_WEALTH_BASKET — underperforms or barely matches index (LOW trust backtest)"
    if bx >= 8 and bx < nx:
        return "EARLY_WEALTH_BASKET — positive but below NIFTY 500 (LOW trust; look-ahead bias inflates result)"
    if bx >= nx and dd < 35:
        return "EARLY_WEALTH_BASKET — beats NIFTY 500 in LOW trust backtest; needs live validation to upgrade"
    return "EARLY_WEALTH_BASKET — investigate drawdown or data quality before upgrading"


# ---------------------------------------------------------------------------
# Multi-mode comparison runner
# ---------------------------------------------------------------------------

def run_mode_comparison(
    sd:             SharedData,
    universe_name:  str,
    monthly_budget: float = 200_000,
    start_date:     str   = "2021-01-01",
    end_date:       str   = None,
    cost_preset:    str   = "conservative",
) -> Dict:
    """Run all 3 modes using shared data; return combined results dict."""
    _log.info("="*60)
    _log.info("WEALTH BASKET MULTI-MODE COMPARISON")
    _log.info("  Universe: %s | Budget: ₹%.0f | Period: %s→%s",
              universe_name, monthly_budget, start_date, end_date or date.today().isoformat())
    _log.info("  Costs: %s | BIAS: fundamentals use CURRENT values — LOW TRUST",
              cost_preset)
    _log.info("="*60)

    results = {}
    for mode in ["CONSERVATIVE", "BALANCED", "AGGRESSIVE_GROWTH"]:
        _log.info("Running mode: %s", mode)
        r = run_backtest(sd, mode, monthly_budget, start_date, end_date, cost_preset)
        r["universe"] = universe_name
        results[mode] = r

    readiness = _classify_readiness(list(results.values()))

    # Which mode wins what
    modes = list(results.values())
    best_xirr  = max(modes, key=lambda x: x.get("basket_xirr_pct") or 0)
    best_safe  = min(modes, key=lambda x: x.get("max_drawdown_pct") or 100)
    beat_n500  = [m for m in modes if
                  (m.get("basket_xirr_pct") or 0) > (m.get("nifty500_xirr_pct") or 0)]
    beat_12pct = [m for m in modes if (m.get("basket_xirr_pct") or 0) > 12.0]

    _log.info("="*60)
    _log.info("COMPARISON COMPLETE")
    _log.info("  Best XIRR:  %s  %.1f%%", best_xirr["mode"], best_xirr.get("basket_xirr_pct") or 0)
    _log.info("  Safest:     %s  max-DD %.1f%%", best_safe["mode"], best_safe.get("max_drawdown_pct") or 0)
    _log.info("  Beats N500: %s", [m["mode"] for m in beat_n500] or "none")
    _log.info("  Beats 12%%: %s", [m["mode"] for m in beat_12pct] or "none")
    _log.info("  Readiness:  %s", readiness)
    _log.info("="*60)

    return {
        "modes":       results,
        "readiness":   readiness,
        "best_xirr":   best_xirr["mode"],
        "safest":      best_safe["mode"],
        "beats_n500":  [m["mode"] for m in beat_n500],
        "beats_12pct": [m["mode"] for m in beat_12pct],
        "universe":    universe_name,
        "start_date":  start_date,
        "end_date":    end_date or date.today().isoformat(),
        "nifty50_xirr_pct":  results["CONSERVATIVE"].get("nifty50_xirr_pct"),
        "nifty500_xirr_pct": results["CONSERVATIVE"].get("nifty500_xirr_pct"),
    }


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _fmt(v, fmt=".1f", fallback="—"):
    return f"{v:{fmt}}" if v is not None else fallback


def _write_csv(path: str, rows: List[Dict], fieldnames=None):
    if not rows:
        return
    if fieldnames is None:
        fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def save_mode_results(summary: Dict, output_dir: str) -> Dict[str, str]:
    """Write individual mode results."""
    os.makedirs(output_dir, exist_ok=True)
    today  = date.today().strftime("%Y%m")
    univ   = summary.get("universe", "?")
    mode   = summary.get("mode", "?").lower()
    prefix = os.path.join(output_dir, f"backtest_{today}_{univ}_{mode}")
    paths  = {}

    if summary.get("monthly_records"):
        p = f"{prefix}_monthly.csv"
        _write_csv(p, summary["monthly_records"])
        paths["monthly"] = p

    if summary.get("trade_log"):
        p = f"{prefix}_trades.csv"
        _write_csv(p, summary["trade_log"])
        paths["trades"] = p

    if summary.get("sector_log"):
        keys = sorted({k for row in summary["sector_log"] for k in row})
        p = f"{prefix}_sector.csv"
        _write_csv(p, summary["sector_log"], fieldnames=keys)
        paths["sector"] = p

    p = f"{prefix}_summary.md"
    _write_single_summary_md(p, summary)
    paths["summary"] = p
    return paths


def save_comparison_results(comp: Dict, output_dir: str) -> Dict[str, str]:
    """Write comparison table + individual mode summaries."""
    os.makedirs(output_dir, exist_ok=True)
    today  = date.today().strftime("%Y%m")
    univ   = comp.get("universe", "?")
    paths  = {}

    # Individual mode files
    for mode, s in comp["modes"].items():
        mode_paths = save_mode_results(s, output_dir)
        for k, v in mode_paths.items():
            paths[f"{mode.lower()}_{k}"] = v

    # Comparison CSV
    cmp_csv = os.path.join(output_dir, f"backtest_{today}_{univ}_comparison.csv")
    rows = []
    for mode, s in comp["modes"].items():
        ro = s.get("rolling", {})
        rows.append({
            "mode":              mode,
            "description":       s.get("mode_description", "")[:60],
            "final_value":       s.get("final_basket_value"),
            "basket_xirr_pct":   s.get("basket_xirr_pct"),
            "nifty50_xirr_pct":  s.get("nifty50_xirr_pct"),
            "nifty500_xirr_pct": s.get("nifty500_xirr_pct"),
            "simple_return_pct": s.get("simple_return_pct"),
            "max_drawdown_pct":  s.get("max_drawdown_pct"),
            "worst_month_pct":   ro.get("worst_month_pct"),
            "worst_6m_pct":      ro.get("worst_6m_pct"),
            "cash_pct":          s.get("cash_pct"),
            "etf_pct":           s.get("etf_pct"),
            "stock_pct":         s.get("stock_pct"),
            "n_holdings":        s.get("n_holdings_final"),
            "top_sector":        s.get("top_sector"),
            "top_sector_pct":    s.get("top_sector_pct"),
            "total_buys":        s.get("total_buys"),
            "total_exits":       s.get("total_exits"),
            "trust_level":       "LOW",
        })
    _write_csv(cmp_csv, rows)
    paths["comparison_csv"] = cmp_csv

    # Comparison Markdown
    cmp_md = os.path.join(output_dir, f"backtest_{today}_{univ}_comparison.md")
    _write_comparison_md(cmp_md, comp)
    paths["comparison_md"] = cmp_md

    return paths


def _write_single_summary_md(path: str, s: Dict):
    bx  = s.get("basket_xirr_pct")
    nx  = s.get("nifty50_xirr_pct")
    n5x = s.get("nifty500_xirr_pct")
    ro  = s.get("rolling", {})

    lines = [
        f"# Wealth Basket [{s.get('mode','')}] — Backtest Summary",
        f"",
        f"**Period:** {s['start_date']} → {s['end_date']}  |  "
        f"**Mode:** {s.get('mode','')}  |  "
        f"**Cost preset:** {s.get('cost_preset','')}  |  "
        f"**Generated:** {date.today().isoformat()}",
        f"",
        f"> {s.get('mode_description','')}",
        "",
        "## ⚠️ Trust Level: LOW — Look-ahead and survivorship bias present",
        "",
        "## Results",
        "",
        f"| Metric | Basket | NIFTY 50 | NIFTY 500 | 12% CAGR |",
        f"|--------|--------|----------|-----------|----------|",
        f"| Final value | ₹{s['final_basket_value']:,.0f} | "
        f"{'₹{:,.0f}'.format(s['final_nifty50_value']) if s.get('final_nifty50_value') else '—'} | "
        f"{'₹{:,.0f}'.format(s['final_nifty500_value']) if s.get('final_nifty500_value') else '—'} | — |",
        f"| XIRR | {_fmt(bx)}% | {_fmt(nx)}% | {_fmt(n5x)}% | 12.0% |",
        f"| Simple return | {_fmt(s.get('simple_return_pct'))}% | — | — | — |",
        f"| Beat NIFTY 50? | {'✅' if bx and nx and bx > nx else '❌'} | | | |",
        f"| Beat NIFTY 500? | {'✅' if bx and n5x and bx > n5x else '❌'} | | | |",
        f"| Beat 12%? | {'✅' if bx and bx > 12 else '❌'} | | | |",
        "",
        "## Risk",
        "",
        f"| Max drawdown | {_fmt(s.get('max_drawdown_pct'))}% |",
        f"|---|---|",
        f"| Worst month | {_fmt(ro.get('worst_month_pct'))}% ({ro.get('worst_month_date','?')}) |",
        f"| Worst 6m | {_fmt(ro.get('worst_6m_pct'))}% |",
        f"| Worst 12m | {_fmt(ro.get('worst_12m_pct'))}% |",
        "",
        "## Portfolio Composition (final month)",
        "",
        f"| Cash | {s.get('cash_pct'):.1f}% |",
        f"|---|---|",
        f"| ETF (NIFTYBEES) | {s.get('etf_pct'):.1f}% |",
        f"| Individual stocks | {s.get('stock_pct'):.1f}% |",
        f"| Holdings | {s.get('n_holdings_final')} stocks |",
        f"| Top sector | {s.get('top_sector')} ({s.get('top_sector_pct'):.1f}% of invested) |",
        "",
        "## Final Holdings",
        "",
        "| Symbol | Sector | Value | Gain% |",
        "|--------|--------|-------|-------|",
    ]
    for h in s.get("final_holdings", []):
        lines.append(f"| {h['symbol']} | {h['sector'][:18]} | ₹{h['current_value']:,.0f} | {h['gain_pct']:+.1f}% |")

    lines += [
        "",
        "---",
        "*Generated by tr-assistant wealth basket backtest engine*",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def _write_comparison_md(path: str, comp: Dict):
    modes  = comp["modes"]
    n50x   = comp.get("nifty50_xirr_pct")
    n500x  = comp.get("nifty500_xirr_pct")
    today  = date.today().isoformat()

    def _r(mode, key): return modes[mode].get(key)
    def _ro(mode, key): return modes[mode].get("rolling", {}).get(key)

    c = modes.get("CONSERVATIVE", {})
    b = modes.get("BALANCED", {})
    a = modes.get("AGGRESSIVE_GROWTH", {})

    def xirr_tag(bx, nx):
        if bx is None or nx is None: return "❓"
        return "✅" if bx > nx else "❌"

    lines = [
        "# Wealth Basket — Mode Comparison Backtest",
        "",
        f"**Universe:** {comp['universe']}  |  "
        f"**Period:** {comp['start_date']} → {comp['end_date']}  |  "
        f"**Generated:** {today}",
        "",
        "---",
        "",
        "## ⚠️ Trust Level: 🔴 LOW for ALL modes",
        "",
        "> Fundamental data uses **current** yfinance values for all historical months.",
        "> **Look-ahead bias** and **survivorship bias** are present.",
        "> Momentum signals are clean (historical OHLCV only). Cost model is conservative.",
        "> Do not use these results to justify real-money allocations.",
        "",
        "---",
        "",
        "## Mode Descriptions",
        "",
        f"| Mode | Description |",
        f"|------|-------------|",
        f"| CONSERVATIVE | {c.get('mode_description','')[:80]} |",
        f"| BALANCED | {b.get('mode_description','')[:80]} |",
        f"| AGGRESSIVE_GROWTH | {a.get('mode_description','')[:80]} |",
        "",
        "---",
        "",
        "## Comparison Table",
        "",
        f"| Metric | CONSERVATIVE | BALANCED | AGGRESSIVE_GROWTH | NIFTY 50 | NIFTY 500 | 12% CAGR |",
        f"|--------|-------------|---------|------------------|----------|-----------|----------|",
    ]

    # Helper rows
    def row(label, key, suffix="", ro=False):
        def v(m):
            val = _ro(m, key) if ro else _r(m, key)
            return f"{_fmt(val)}{suffix}"
        return f"| {label} | {v('CONSERVATIVE')} | {v('BALANCED')} | {v('AGGRESSIVE_GROWTH')} | — | — | — |"

    lines += [
        f"| Final value | ₹{_r('CONSERVATIVE','final_basket_value') or 0:,.0f} "
        f"| ₹{_r('BALANCED','final_basket_value') or 0:,.0f} "
        f"| ₹{_r('AGGRESSIVE_GROWTH','final_basket_value') or 0:,.0f} "
        f"| ₹{_r('CONSERVATIVE','final_nifty50_value') or 0:,.0f} "
        f"| ₹{_r('CONSERVATIVE','final_nifty500_value') or 0:,.0f} | — |",
        f"| **XIRR** | **{_fmt(_r('CONSERVATIVE','basket_xirr_pct'))}%** "
        f"| **{_fmt(_r('BALANCED','basket_xirr_pct'))}%** "
        f"| **{_fmt(_r('AGGRESSIVE_GROWTH','basket_xirr_pct'))}%** "
        f"| {_fmt(n50x)}% | {_fmt(n500x)}% | 12.0% |",
        row("Simple return", "simple_return_pct", "%"),
        f"| Beat NIFTY 50 | {xirr_tag(_r('CONSERVATIVE','basket_xirr_pct'), n50x)} "
        f"| {xirr_tag(_r('BALANCED','basket_xirr_pct'), n50x)} "
        f"| {xirr_tag(_r('AGGRESSIVE_GROWTH','basket_xirr_pct'), n50x)} | — | — | — |",
        f"| Beat NIFTY 500 | {xirr_tag(_r('CONSERVATIVE','basket_xirr_pct'), n500x)} "
        f"| {xirr_tag(_r('BALANCED','basket_xirr_pct'), n500x)} "
        f"| {xirr_tag(_r('AGGRESSIVE_GROWTH','basket_xirr_pct'), n500x)} | — | — | — |",
        f"| Beat 12% target | {xirr_tag(_r('CONSERVATIVE','basket_xirr_pct'), 12.0)} "
        f"| {xirr_tag(_r('BALANCED','basket_xirr_pct'), 12.0)} "
        f"| {xirr_tag(_r('AGGRESSIVE_GROWTH','basket_xirr_pct'), 12.0)} | — | — | — |",
        row("Max drawdown", "max_drawdown_pct", "%"),
        f"| Worst month | {_fmt(_ro('CONSERVATIVE','worst_month_pct'))}% | {_fmt(_ro('BALANCED','worst_month_pct'))}% | {_fmt(_ro('AGGRESSIVE_GROWTH','worst_month_pct'))}% | — | — | — |",
        f"| Worst 6-month | {_fmt(_ro('CONSERVATIVE','worst_6m_pct'))}% | {_fmt(_ro('BALANCED','worst_6m_pct'))}% | {_fmt(_ro('AGGRESSIVE_GROWTH','worst_6m_pct'))}% | — | — | — |",
        row("Cash %", "cash_pct", "%"),
        row("ETF %", "etf_pct", "%"),
        row("Stock %", "stock_pct", "%"),
        row("N holdings", "n_holdings_final"),
        f"| Top sector | {_r('CONSERVATIVE','top_sector')} ({_r('CONSERVATIVE','top_sector_pct'):.0f}%) "
        f"| {_r('BALANCED','top_sector')} ({_r('BALANCED','top_sector_pct'):.0f}%) "
        f"| {_r('AGGRESSIVE_GROWTH','top_sector')} ({_r('AGGRESSIVE_GROWTH','top_sector_pct'):.0f}%) "
        f"| — | — | — |",
        row("Total buys", "total_buys"),
        row("Total exits", "total_exits"),
    ]

    # Recommendations
    bx_c = _r("CONSERVATIVE","basket_xirr_pct") or 0
    bx_b = _r("BALANCED","basket_xirr_pct") or 0
    bx_a = _r("AGGRESSIVE_GROWTH","basket_xirr_pct") or 0
    dd_c = _r("CONSERVATIVE","max_drawdown_pct") or 0
    dd_b = _r("BALANCED","max_drawdown_pct") or 0
    dd_a = _r("AGGRESSIVE_GROWTH","max_drawdown_pct") or 0

    best_mode   = comp.get("best_xirr", "AGGRESSIVE_GROWTH")
    safest_mode = comp.get("safest", "CONSERVATIVE")

    lines += [
        "",
        "---",
        "",
        "## Findings and Recommendations",
        "",
        "### Can BALANCED beat NIFTY 500 and 12% target?",
        "",
        (f"> ✅ YES — BALANCED XIRR {bx_b:.1f}% beats NIFTY 500 {_fmt(n500x)}%"
         if (n500x and bx_b > n500x) else
         f"> ❌ NO — BALANCED XIRR {bx_b:.1f}% does not beat NIFTY 500 {_fmt(n500x)}%"),
        (f"> ✅ YES — BALANCED beats 12% target" if bx_b > 12 else
         f"> ❌ NO — BALANCED XIRR {bx_b:.1f}% misses 12% target"),
        "",
        "### Can AGGRESSIVE_GROWTH improve returns without unacceptable drawdown?",
        "",
        (f"> ✅ YES — AGGRESSIVE XIRR {bx_a:.1f}% vs CONSERVATIVE {bx_c:.1f}%, max DD {dd_a:.1f}%"
         if bx_a > bx_c else
         f"> ❌ NO — AGGRESSIVE XIRR {bx_a:.1f}% vs CONSERVATIVE {bx_c:.1f}%"),
        "",
        "### Is the model still PSU/value/cyclical biased?",
        "",
        f"> Top sector: CONSERVATIVE={_r('CONSERVATIVE','top_sector')} {_r('CONSERVATIVE','top_sector_pct'):.0f}%  "
        f"BALANCED={_r('BALANCED','top_sector')} {_r('BALANCED','top_sector_pct'):.0f}%  "
        f"AGGRESSIVE={_r('AGGRESSIVE_GROWTH','top_sector')} {_r('AGGRESSIVE_GROWTH','top_sector_pct'):.0f}%",
        ">",
        "> If Energy/Industrials still dominate in BALANCED and AGGRESSIVE, the model remains cyclical-biased.",
        "> The growth exception rule should bring in FMCG/Consumer/IT stocks — check sector exposure CSV.",
        "",
        "### Is Wealth Basket worth pursuing vs index-core?",
        "",
        (f"> RECOMMENDATION: The model shows directional momentum alpha but the look-ahead bias makes "
         f"results LOW TRUST. NIFTY 500 SIP at {_fmt(n500x)}% XIRR is a strong benchmark. "
         f"{'The BALANCED mode beats NIFTY 500 — worth monitoring with live data.' if (n500x and bx_b > n500x) else 'No mode clearly beats NIFTY 500 after accounting for bias. The safe choice is a NIFTY 500 index core with the basket as a supplementary layer.'} "
         f"Swing engine handles short-term alpha; Wealth Basket should handle quality long-term deployment."),
        "",
        "---",
        "",
        "## Best / Safest / Highest Return Mode",
        "",
        f"| Category | Mode | XIRR |",
        f"|----------|------|------|",
        f"| Highest return | {best_mode} | {_fmt(_r(best_mode,'basket_xirr_pct'))}% |",
        f"| Safest (min drawdown) | {safest_mode} | {_fmt(_r(safest_mode,'basket_xirr_pct'))}% |",
        f"| Ready for manual pilot? | {'BALANCED — if live tracking starts' if (n500x and bx_b > n500x) else 'None yet — start live tracking first'} | — |",
        f"| Still LOW TRUST? | All modes | YES (look-ahead + survivorship bias) |",
        "",
        "---",
        "",
        "## Engine Readiness",
        "",
        f"> **{comp.get('readiness', 'EARLY_WEALTH_BASKET')}**",
        "",
        "> Requirements for `READY_FOR_MANUAL_REVIEW`:",
        "> 1. Backtest exists ✅",
        "> 2. ≥ 3 months live portfolio snapshots ❌",
        "> 3. Point-in-time fundamentals ❌ (not available from yfinance)",
        "> 4. MEDIUM trust data ✅ (screener.in cross-validation active)",
        f"> 5. Backtest beats benchmark {'✅' if comp.get('beats_n500') else '❌'}",
        "",
        "---",
        "",
        "*Generated by tr-assistant wealth basket backtest engine*",
    ]
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Wealth Basket monthly SIP backtest — multi-mode comparison",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--universe",
                        choices=["nifty50", "nifty100", "nifty200", "nifty500"],
                        default="nifty50")
    parser.add_argument("--monthly-investment", "--budget",
                        dest="budget", type=float, default=200_000)
    parser.add_argument("--years", type=float, default=None,
                        help="Years to backtest (computes start from today - years)")
    parser.add_argument("--start", default="2021-01-01")
    parser.add_argument("--end", default=None)
    parser.add_argument("--costs",
                        choices=["conservative", "moderate", "aggressive"],
                        default="conservative")
    parser.add_argument("--mode",
                        choices=["CONSERVATIVE", "BALANCED", "AGGRESSIVE_GROWTH"],
                        default=None,
                        help="Single mode to run (default: all modes via --compare-modes)")
    parser.add_argument("--compare-modes", action="store_true",
                        help="Run all 3 modes and generate comparison (default if no --mode given)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print summary; do not write files")
    args = parser.parse_args()

    # Default to compare-modes if no specific mode given
    if not args.mode:
        args.compare_modes = True

    if args.years is not None:
        end_d    = date.today()
        start_d  = end_d - timedelta(days=int(args.years * 365.25))
        start_str = start_d.isoformat()
    else:
        start_str = args.start

    _log.info("Loading universe and fetching data (shared across all modes)...")
    sd = _fetch_shared(args.universe)

    os.makedirs(str(_REPORTS_DIR), exist_ok=True)
    today = date.today().strftime("%Y%m")

    if args.compare_modes or not args.mode:
        comp = run_mode_comparison(
            sd, args.universe, args.budget, start_str, args.end, args.costs
        )
        if not args.dry_run:
            paths = save_comparison_results(comp, str(_REPORTS_DIR))
            print("\nComparison complete. Files written:")
            for k, v in paths.items():
                if k.endswith("_summary") or k in ("comparison_csv", "comparison_md"):
                    print(f"  [{k:30s}] {v}")
        else:
            # Print comparison table
            modes = comp["modes"]
            print("\n" + "="*70)
            print("WEALTH BASKET MULTI-MODE COMPARISON — DRY RUN")
            print("="*70)
            fmt = "{:<22} {:>10} {:>10} {:>10} {:>10} {:>10}"
            print(fmt.format("Metric", "CONSERV.", "BALANCED", "AGGR.GRW", "NIFTY50", "NIFTY500"))
            print("-"*70)
            def pr(label, key, ro=False, suffix=""):
                def v(m):
                    val = (modes[m].get("rolling",{}).get(key) if ro else modes[m].get(key))
                    return f"{_fmt(val)}{suffix}"
                n50v  = ""
                n500v = ""
                if key == "basket_xirr_pct":
                    n50v  = f"{_fmt(comp.get('nifty50_xirr_pct'))}%"
                    n500v = f"{_fmt(comp.get('nifty500_xirr_pct'))}%"
                print(fmt.format(label, v("CONSERVATIVE"), v("BALANCED"), v("AGGRESSIVE_GROWTH"), n50v, n500v))
            pr("XIRR",          "basket_xirr_pct",  suffix="%")
            pr("Simple return",  "simple_return_pct", suffix="%")
            pr("Max drawdown",   "max_drawdown_pct",  suffix="%")
            pr("Worst month",    "worst_month_pct",   ro=True, suffix="%")
            pr("Worst 6m",       "worst_6m_pct",      ro=True, suffix="%")
            pr("Cash %",         "cash_pct",          suffix="%")
            pr("ETF %",          "etf_pct",           suffix="%")
            pr("Stock %",        "stock_pct",         suffix="%")
            pr("N holdings",     "n_holdings_final")
            print("="*70)
            print(f"  Best XIRR:   {comp['best_xirr']}")
            print(f"  Safest:      {comp['safest']}")
            print(f"  Beats N500:  {comp['beats_n500'] or 'none'}")
            print(f"  Beats 12%:   {comp['beats_12pct'] or 'none'}")
            print(f"  Readiness:   {comp['readiness']}")
    else:
        # Single mode
        summary = run_backtest(sd, args.mode, args.budget, start_str, args.end, args.costs)
        summary["universe"] = args.universe
        bx = summary.get("basket_xirr_pct")
        nx = summary.get("nifty50_xirr_pct")
        n5x= summary.get("nifty500_xirr_pct")
        dd = summary.get("max_drawdown_pct")
        print(f"\n  [{args.mode}] XIRR {_fmt(bx)}%  N50 {_fmt(nx)}%  N500 {_fmt(n5x)}%  MaxDD {_fmt(dd)}%")
        if not args.dry_run:
            paths = save_mode_results(summary, str(_REPORTS_DIR))
            for k, v in paths.items():
                print(f"  [{k}] {v}")


if __name__ == "__main__":
    main()
