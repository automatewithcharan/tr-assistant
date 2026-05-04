"""
Benchmark tracker for the wealth basket.

Tracks three benchmarks:
  1. NIFTY 50       (^NSEI)   — large-cap India benchmark
  2. NIFTY 500      (^CRSLDX) — broad market benchmark
  3. Flat 12% CAGR  — conservative mutual fund baseline

Basket performance:
  Computed from wb_portfolio_snapshots (monthly portfolio value records).
  Returns "INSUFFICIENT_HISTORY" when fewer than 2 snapshots exist.

Rolling returns are computed over 1m / 3m / 6m / 12m periods.

Underperformance rule:
  If basket return < best_benchmark_return - UNDERPERF_THRESHOLD over the
  UNDERPERF_WINDOW months → trigger warning.
  Default: -3% vs best benchmark over 6 months.
  If basket history is insufficient, underperformance check returns
  (False, "INSUFFICIENT_HISTORY") — the warning does NOT trigger.
"""

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

_NIFTY50_TICKER  = "^NSEI"
_NIFTY500_TICKER = "^CRSLDX"

UNDERPERF_THRESHOLD = 3.0
UNDERPERF_WINDOW_M  = 6

INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pct_return(prices: List[float], lookback_days: int) -> Optional[float]:
    """Return % return over the last lookback_days calendar days."""
    if not prices or len(prices) < 2:
        return None
    vals = [p for p in prices if p is not None]
    if len(vals) < 2:
        return None
    start = vals[max(0, len(vals) - lookback_days)]
    end   = vals[-1]
    if start <= 0:
        return None
    return round((end - start) / start * 100, 2)


def _basket_twr(snapshots: List[Dict]) -> Dict[str, Optional[float]]:
    """
    Compute basket Time-Weighted Returns from monthly portfolio snapshots.

    Uses Modified Dietz approximation for each sub-period.
    Monthly SIP is treated as a cash flow at the start of the month.

    Returns dict with keys basket_1m, basket_3m, basket_6m, basket_12m.
    Any window with insufficient data returns None.
    """
    n = len(snapshots)
    if n < 2:
        return {f"basket_{w}": None for w in ("1m", "3m", "6m", "12m")}

    # Compute sub-period returns using Modified Dietz
    # R = (V_end - V_start - CF) / (V_start + CF)
    # where CF = monthly_sip added at START of period
    monthly_returns = []
    for i in range(1, n):
        v_start = float(snapshots[i - 1].get("basket_total") or 0)
        v_end   = float(snapshots[i].get("basket_total") or 0)
        cf      = float(snapshots[i].get("monthly_sip") or 0)

        # SIP enters at start of period: denominator = V_start + CF
        denominator = v_start + cf
        if denominator <= 0:
            monthly_returns.append(None)
        else:
            r = (v_end - v_start - cf) / denominator
            monthly_returns.append(r)

    def _chain_returns(periods: int) -> Optional[float]:
        if periods > len(monthly_returns):
            return None
        recent = monthly_returns[-periods:]
        if any(r is None for r in recent):
            return None
        product = 1.0
        for r in recent:
            product *= (1 + r)
        return round((product - 1) * 100, 2)

    return {
        "basket_1m":  _chain_returns(1),
        "basket_3m":  _chain_returns(3),
        "basket_6m":  _chain_returns(6),
        "basket_12m": _chain_returns(12),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch_benchmark_history(
    start_date: str = None,
    period: str = "2y",
) -> Dict[str, List[Tuple[str, float]]]:
    """
    Fetch daily closing prices for NIFTY 50 and NIFTY 500.
    Returns dict with keys 'nifty50' and 'nifty500', each a list of
    (date_str, close_price) sorted ascending.
    Returns empty lists on failure.
    """
    import yfinance as yf

    result = {"nifty50": [], "nifty500": []}

    for key, ticker in [("nifty50", _NIFTY50_TICKER), ("nifty500", _NIFTY500_TICKER)]:
        try:
            kwargs = {"progress": False, "auto_adjust": True}
            if start_date:
                kwargs["start"] = start_date
            else:
                kwargs["period"] = period

            df = yf.download(ticker, **kwargs)
            if df is None or len(df) < 5:
                _log.warning("No data for %s (%s)", key, ticker)
                continue

            closes = df["Close"].squeeze().dropna()
            for dt_idx, price in closes.items():
                if hasattr(dt_idx, "strftime"):
                    date_str = dt_idx.strftime("%Y-%m-%d")
                else:
                    date_str = str(dt_idx)[:10]
                result[key].append((date_str, float(price)))
        except Exception as exc:
            _log.warning("Benchmark fetch failed for %s: %s", key, exc)

    return result


def flat_12pct_value(base: float, start_date: str, target_date: str) -> float:
    """Compound a base value at exactly 12% per year from start to target date."""
    try:
        d0 = date.fromisoformat(start_date)
        d1 = date.fromisoformat(target_date)
        years = (d1 - d0).days / 365.25
        return round(base * (1.12 ** years), 2)
    except Exception:
        return base


def compute_rolling_returns(
    benchmark_history: Dict[str, List[Tuple[str, float]]],
    basket_snapshots: List[Dict],
    base_date: str = None,
) -> Dict:
    """
    Compute rolling returns for basket and both benchmarks.

    Args:
        benchmark_history: From fetch_benchmark_history()
        basket_snapshots:  From WealthBasketDB.get_portfolio_snapshots()
                           Empty list → basket returns show as None with
                           basket_status = INSUFFICIENT_HISTORY
        base_date:         First date basket was seeded (for flat-12% baseline)

    Returns dict with 1m/3m/6m/12m returns for basket, nifty50, nifty500, flat12pct,
    plus basket_status ("OK" or "INSUFFICIENT_HISTORY") and months_of_history.
    """
    today = date.today()
    if not base_date:
        base_date = (today - timedelta(days=365)).isoformat()

    windows = {"1m": 30, "3m": 91, "6m": 182, "12m": 365}
    result: Dict = {}

    # Basket TWR from snapshots
    months_available = len(basket_snapshots)
    if months_available >= 2:
        basket_returns = _basket_twr(basket_snapshots)
        result.update(basket_returns)
        result["basket_status"] = "OK"
    else:
        for label in windows:
            result[f"basket_{label}"] = None
        result["basket_status"] = INSUFFICIENT_HISTORY
        result["basket_months_available"] = months_available

    result["months_of_history"] = months_available

    # Benchmark returns
    for bench_key in ("nifty50", "nifty500"):
        prices_data = benchmark_history.get(bench_key, [])
        prices = [v for _, v in prices_data]
        for label, days in windows.items():
            result[f"{bench_key}_{label}"] = _pct_return(prices, days)

    # Flat 12% baseline
    flat_base = 10000.0
    for label, days in windows.items():
        past_date = (today - timedelta(days=days)).isoformat()
        result[f"flat_12pct_{label}"] = round(
            (flat_12pct_value(flat_base, past_date, today.isoformat()) / flat_base - 1) * 100, 2
        )

    return result


def check_underperformance(
    rolling: Dict,
    window: str = "6m",
    threshold: float = UNDERPERF_THRESHOLD,
) -> Tuple[bool, str]:
    """
    Check if basket is underperforming benchmark by more than threshold%.

    Returns (is_underperforming, reason_string).
    If basket history is insufficient, returns (False, INSUFFICIENT_HISTORY).
    The underperformance warning does NOT trigger on missing data —
    it only fires when we have real basket returns to compare.
    """
    if rolling.get("basket_status") == INSUFFICIENT_HISTORY:
        months = rolling.get("months_of_history", 0)
        return False, f"{INSUFFICIENT_HISTORY} ({months} snapshot(s) — need 2+)"

    basket_ret = rolling.get(f"basket_{window}")
    if basket_ret is None:
        return False, f"basket {window} return not available yet"

    benchmarks = {
        "NIFTY 50":  rolling.get(f"nifty50_{window}"),
        "NIFTY 500": rolling.get(f"nifty500_{window}"),
        "12% CAGR":  rolling.get(f"flat_12pct_{window}"),
    }

    lagging_vs = []
    for name, bret in benchmarks.items():
        if bret is not None and basket_ret < (bret - threshold):
            lagging_vs.append(f"{name} ({basket_ret:.1f}% vs {bret:.1f}%)")

    if lagging_vs:
        msg = (
            f"Basket {window} return {basket_ret:.1f}% is lagging "
            + ", ".join(lagging_vs)
            + f" (threshold: -{threshold:.0f}%). "
            "Recommend: review weak holdings, hold extra cash, avoid new risky positions."
        )
        return True, msg

    return False, ""


def build_performance_snapshot(
    rolling: Dict,
    db,
    holdings: List[Dict],
) -> Dict:
    """Build a complete performance snapshot for storage and reporting."""
    snap = dict(rolling)
    snap["calc_date"] = date.today().isoformat()

    is_under_6m,  msg_6m  = check_underperformance(rolling, window="6m")
    is_under_12m, msg_12m = check_underperformance(rolling, window="12m")
    snap["underperform_6m"]  = is_under_6m
    snap["underperform_12m"] = is_under_12m

    notes_parts = []
    if is_under_6m:  notes_parts.append(f"6m: {msg_6m}")
    if is_under_12m: notes_parts.append(f"12m: {msg_12m}")
    snap["notes"] = " | ".join(notes_parts) if notes_parts else "OK"

    if db is not None:
        try:
            db.save_performance(snap)
        except Exception as exc:
            _log.warning("Failed to save performance snapshot: %s", exc)

    return snap


def get_latest_nifty_values(benchmark_history: Dict) -> Dict[str, Optional[float]]:
    """Extract the most recent NIFTY 50 and NIFTY 500 values from benchmark history."""
    result = {"nifty50": None, "nifty500": None}
    for key in ("nifty50", "nifty500"):
        data = benchmark_history.get(key, [])
        if data:
            result[key] = data[-1][1]   # last entry's price
    return result


def update_benchmark_db(db, start_date: str = None) -> int:
    """Fetch latest benchmark prices and persist to DB. Returns new rows saved."""
    history = fetch_benchmark_history(start_date=start_date)
    n50  = dict(history.get("nifty50", []))
    n500 = dict(history.get("nifty500", []))

    all_dates = sorted(set(n50) | set(n500))
    today      = date.today().isoformat()
    count      = 0

    base_date  = all_dates[0] if all_dates else today
    base_nifty = n50.get(base_date, 10000.0)

    for d_str in all_dates:
        nifty50_price  = n50.get(d_str)
        nifty500_price = n500.get(d_str)
        if not nifty50_price and not nifty500_price:
            continue
        flat = flat_12pct_value(base_nifty or 10000.0, base_date, d_str)
        try:
            db.save_benchmark_day(
                price_date=d_str,
                nifty50=nifty50_price or 0,
                nifty500=nifty500_price,
                flat_12pct=flat,
            )
            count += 1
        except Exception as exc:
            _log.debug("Benchmark save error for %s: %s", d_str, exc)

    _log.info("Saved %d benchmark price rows", count)
    return count
