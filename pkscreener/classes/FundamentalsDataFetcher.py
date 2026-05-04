"""
Fundamentals data fetcher for the wealth basket engine.

Default data source: yfinance (.NS suffix for NSE stocks)
Optional second source: screener.in (via DataProviders.CompositeProvider)

ALL DATA FROM THIS MODULE IS TAGGED LOW TRUST UNLESS:
  - Two sources agree within 20% on key fields → MEDIUM TRUST
  - A conflicting signal is detected → CONFLICT (blocks AUTO_EXECUTE)

yfinance India data quality issues (documented):
  - ROE and currentRatio frequently missing for some stocks
  - Revenue/earnings growth uses trailing 12-month, not clean annual
  - Data can be stale by days or weeks
  - No adjustments for Indian accounting differences
  - ROCE not available in .info (computed from balance sheet if possible)
  - debtToEquity is returned as a percentage × 100 for most Indian (.NS) stocks
    when the actual ratio exceeds ~3% (i.e., 10.39 means 10.39% = 0.104x).
    Values ≤ 3 are already in ratio form. normalize_debt_equity() handles this.

Do not auto-execute positions based solely on this module.
Verify key claims against NSE quarterly filings or screener.in before committing capital.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

# All fields we track for data quality purposes
_ALL_KEY_FIELDS = [
    "pe_ratio", "pb_ratio", "roe_pct", "roce_pct", "debt_equity",
    "revenue_growth_yoy", "profit_growth_yoy", "net_margin_pct",
    "current_ratio", "market_cap_cr",
]

_CR = 1e7

# Keywords that identify financial sector companies (banks, NBFCs, insurance).
# These businesses have structurally different balance sheets; D/E and
# current_ratio hard filters must not apply to them.
_FINANCIAL_SECTOR_KEYWORDS = frozenset(("bank", "financ", "insur", "nbfc", "credit"))

# yfinance debtToEquity normalization:
# For NSE Indian stocks, yfinance returns the value as percentage (× 100) when
# the underlying ratio is above roughly 3%. Below this threshold the value is
# already a decimal ratio. Confirmed pattern across NIFTY 50 (May 2026):
#   TCS 10.389 → ÷100 → 0.104   JSWSTEEL 118.74 → ÷100 → 1.19
#   MARUTI 0.096 → kept as-is    BEL 0.273 → kept as-is
_YFINANCE_DE_SCALE_THRESHOLD = 3.0


def _safe_pct(val) -> Optional[float]:
    """Convert a fraction (0.12) to percentage (12.0). Returns None if out of range."""
    try:
        f = float(val)
        return round(f * 100, 2) if -10 < f < 10 else None
    except (TypeError, ValueError):
        return None


def _safe_float(val) -> Optional[float]:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _is_financial_sector(sector: str, industry: str = "") -> bool:
    """Return True if sector/industry represents a financial services business.

    Financial companies (banks, NBFCs, insurance) use different balance sheet
    conventions — D/E and current_ratio hard filters must not apply to them.
    """
    combined = (sector + " " + industry).lower()
    return any(kw in combined for kw in _FINANCIAL_SECTOR_KEYWORDS)


def normalize_debt_equity(
    raw_value,
    source: str = "yfinance",
    sector: str = "",
    industry: str = "",
) -> Optional[float]:
    """
    Normalize debt/equity ratio from yfinance to a consistent decimal ratio.

    Problem: yfinance returns debtToEquity in inconsistent scales for Indian stocks.
    Values above _YFINANCE_DE_SCALE_THRESHOLD are percentage-scaled (÷100 needed).
    Values at or below the threshold are already decimal ratios.

    Examples (NIFTY 50, confirmed May 2026):
      TCS 10.389  → 0.1039  (IT, nearly debt-free)
      INFY  9.827 → 0.0983
      JSWSTEEL 118.74 → 1.187  (steel, moderate leverage)
      ADANIENT 177.77 → 1.778
      INDIGO 866.46 → 8.665  (airlines, high lease liabilities — real)
      MARUTI 0.096  → 0.096  (already a ratio, kept as-is)
      BEL    0.273  → 0.273  (kept as-is)
      CIPLA  1.415  → 1.415  (kept as-is; slightly high but not over threshold)

    Rules:
      - None → None (missing data preserved)
      - Negative → None (invalid)
      - Financial sector → return raw without normalization (their D/E is
        structurally high; scoring handles this via is_financial flag)
      - yfinance + non-financial + value > 3.0 → divide by 100
      - Everything else → return as-is
      - screener.in → already a ratio, return as-is

    Args:
        raw_value:  Raw value from data source (may be None)
        source:     "yfinance" or "screener"
        sector:     Sector string from yfinance/screener
        industry:   Industry string (used together with sector for classification)

    Returns:
        Normalized D/E as a decimal ratio, or None if invalid/missing.
    """
    if raw_value is None:
        return None
    try:
        v = float(raw_value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None

    # Financial companies: D/E has different semantics — skip normalization.
    if _is_financial_sector(sector, industry):
        return v

    # yfinance-specific: values above threshold are percentage-scaled.
    if source == "yfinance" and v > _YFINANCE_DE_SCALE_THRESHOLD:
        normalized = round(v / 100, 4)
        _log.debug("D/E normalize [%s/%s]: raw=%.3f → ratio=%.4f", sector, industry, v, normalized)
        return normalized

    return v


def _annotate_missing(data: Dict) -> Dict:
    """
    Add missing_fields list and update fields_populated count.
    Operates in-place on data dict, also returns it.
    """
    missing = [f for f in _ALL_KEY_FIELDS if data.get(f) is None]
    populated = len(_ALL_KEY_FIELDS) - len(missing)
    data["missing_fields"] = missing
    data["fields_populated"] = populated
    data["fields_total"] = len(_ALL_KEY_FIELDS)
    return data


def fetch_fundamentals(symbol: str, retries: int = 2) -> Dict:
    """
    Fetch fundamental data for one NSE stock via yfinance.

    Returns a dict with normalized fields and trust_level = 'LOW'.
    ROCE is attempted via balance sheet; marked missing if unavailable.
    debt_equity is normalized from yfinance's percentage-scaled format.
    On failure returns minimal dict with trust_level = 'FAILED'.
    """
    import yfinance as yf

    ticker_sym = f"{symbol.strip().upper()}.NS"
    info: Dict = {}

    for attempt in range(retries + 1):
        try:
            t = yf.Ticker(ticker_sym)
            info = t.info or {}
            break
        except Exception as exc:
            if attempt < retries:
                time.sleep(2 ** attempt)
            else:
                _log.warning("yfinance fetch failed for %s: %s", symbol, exc)
                return {
                    "symbol": symbol, "trust_level": "FAILED",
                    "source": "yfinance", "raw": {},
                    "missing_fields": _ALL_KEY_FIELDS[:], "fields_populated": 0,
                    "fields_total": len(_ALL_KEY_FIELDS),
                }

    if not info or info.get("quoteType") == "NONE":
        _log.warning("yfinance returned empty info for %s", symbol)
        return {
            "symbol": symbol, "trust_level": "FAILED",
            "source": "yfinance", "raw": {},
            "missing_fields": _ALL_KEY_FIELDS[:], "fields_populated": 0,
            "fields_total": len(_ALL_KEY_FIELDS),
        }

    mktcap      = _safe_float(info.get("marketCap"))
    sector_val  = info.get("sector", "") or ""
    industry_val= info.get("industry", "") or ""
    raw_de      = _safe_float(info.get("debtToEquity"))
    norm_de     = normalize_debt_equity(raw_de, source="yfinance",
                                        sector=sector_val, industry=industry_val)
    is_fin      = _is_financial_sector(sector_val, industry_val)

    if raw_de is not None and norm_de != raw_de:
        _log.debug("%s D/E: yfinance=%.3f → normalized=%.4f", symbol, raw_de, norm_de)

    data = {
        "symbol":               symbol.upper(),
        "source":               "yfinance",
        "trust_level":          "LOW",
        "is_financial":         is_fin,
        "market_cap_cr":        round(mktcap / _CR, 1) if mktcap else None,
        "pe_ratio":             _safe_float(info.get("trailingPE")),
        "pb_ratio":             _safe_float(info.get("priceToBook")),
        "roe_pct":              _safe_pct(info.get("returnOnEquity")),
        "roce_pct":             None,       # filled below from balance sheet
        "raw_debt_equity":      raw_de,     # original yfinance value (for audit)
        "debt_equity":          norm_de,    # normalized decimal ratio (used for scoring)
        "revenue_growth_yoy":   _safe_pct(info.get("revenueGrowth")),
        "profit_growth_yoy":    _safe_pct(info.get("earningsGrowth")),
        "gross_margin_pct":     _safe_pct(info.get("grossMargins")),
        "operating_margin_pct": _safe_pct(info.get("operatingMargins")),
        "net_margin_pct":       _safe_pct(info.get("profitMargins")),
        "current_ratio":        _safe_float(info.get("currentRatio")),
        "sector":               sector_val,
        "industry":             industry_val,
        "short_name":           info.get("shortName", ""),
        "conflict_fields":      "",
        "secondary_available":  False,
        "raw": {k: info.get(k) for k in (
            "trailingPE", "priceToBook", "returnOnEquity", "debtToEquity",
            "revenueGrowth", "earningsGrowth", "grossMargins", "operatingMargins",
            "profitMargins", "currentRatio", "marketCap", "sector", "industry",
        )},
    }

    # Try ROCE from balance sheet (extra network call)
    data["roce_pct"] = _compute_roce_yfinance(t)

    _annotate_missing(data)
    _log.debug("%s: %d/%d fields populated, missing: %s",
               symbol, data["fields_populated"], data["fields_total"],
               data["missing_fields"])
    return data


def _compute_roce_yfinance(ticker) -> Optional[float]:
    """
    Compute ROCE = EBIT / Capital Employed × 100 from yfinance income_stmt + balance_sheet.
    Returns None if required inputs are unavailable. Does not fake the value.
    """
    try:
        fin = ticker.income_stmt
        bs  = ticker.balance_sheet
        if fin is None or bs is None or fin.empty or bs.empty:
            return None

        ebit = None
        for label in ("EBIT", "Operating Income", "Operating Income Or Loss"):
            if label in fin.index:
                raw = fin.loc[label].iloc[0]
                try:
                    v = float(raw)
                    if v == v:   # nan check
                        ebit = v
                        break
                except (TypeError, ValueError):
                    pass

        total_assets = None
        for label in ("Total Assets",):
            if label in bs.index:
                try:
                    total_assets = float(bs.loc[label].iloc[0])
                    break
                except (TypeError, ValueError):
                    pass

        curr_liab = None
        for label in ("Current Liabilities", "Total Current Liabilities"):
            if label in bs.index:
                try:
                    curr_liab = float(bs.loc[label].iloc[0])
                    break
                except (TypeError, ValueError):
                    pass

        if ebit is None or total_assets is None or curr_liab is None:
            return None

        capital_employed = total_assets - curr_liab
        if capital_employed <= 0:
            return None

        return round((ebit / capital_employed) * 100, 2)
    except Exception as exc:
        _log.debug("ROCE yfinance computation failed: %s", exc)
        return None


def fetch_fundamentals_with_screener(
    symbol: str,
    screener_delay: float = 1.5,
) -> Dict:
    """
    Fetch fundamentals using CompositeProvider (yfinance + screener.in).

    Cross-validates key fields. Sets trust_level to MEDIUM on agreement,
    CONFLICT on disagreement, LOW if screener.in is unavailable.

    Slower than fetch_fundamentals() — adds ~1.5s screener delay.
    """
    try:
        from pkscreener.classes.DataProviders import CompositeProvider
        provider = CompositeProvider(secondary_delay=screener_delay)
        data = provider.fetch(symbol)
        if not data or data.get("trust_level") == "FAILED":
            return fetch_fundamentals(symbol)   # fallback
        # Apply D/E normalization to composite data too
        sector_val   = data.get("sector", "") or ""
        industry_val = data.get("industry", "") or ""
        raw_de       = data.get("debt_equity")
        # CompositeProvider may set debt_equity from screener (already ratio) or
        # yfinance (may be percentage-scaled). Normalize based on source.
        data["raw_debt_equity"] = raw_de
        # Composite provider's debt_equity comes from yfinance (percentage-scaled) —
        # always normalize regardless of source label.
        data["debt_equity"] = normalize_debt_equity(raw_de, source="yfinance",
                                                     sector=sector_val, industry=industry_val)
        data["is_financial"] = _is_financial_sector(sector_val, industry_val)
        _annotate_missing(data)
        return data
    except Exception as exc:
        _log.warning("CompositeProvider failed for %s, falling back to yfinance: %s", symbol, exc)
        return fetch_fundamentals(symbol)


def fetch_fundamentals_batch(
    symbols: List[str],
    delay_sec: float = 0.5,
    db=None,
    max_age_days: int = 30,
    progress_callback=None,
    use_screener: bool = False,
    screener_delay: float = 1.5,
) -> Dict[str, Dict]:
    """
    Fetch fundamentals for a list of symbols, using DB cache when fresh.

    Args:
        symbols:           NSE symbols
        delay_sec:         Delay between yfinance calls
        db:                WealthBasketDB instance for caching (optional)
        max_age_days:      Skip fetch if cached data is fresher than this
        progress_callback: Called as (done, total, symbol)
        use_screener:      If True, use CompositeProvider for uncached stocks
        screener_delay:    Seconds between screener.in requests (default 1.5)

    Returns:
        dict mapping symbol → fundamentals dict
    """
    results: Dict[str, Dict] = {}
    total = len(symbols)

    for idx, sym in enumerate(symbols):
        sym = sym.strip().upper()

        # Check cache first
        if db is not None:
            cached = db.get_fundamentals(sym, max_age_days=max_age_days)
            if cached:
                results[sym] = cached
                if progress_callback:
                    progress_callback(idx + 1, total, sym)
                continue

        # Fetch from source(s)
        if use_screener:
            data = fetch_fundamentals_with_screener(sym, screener_delay=screener_delay)
        else:
            data = fetch_fundamentals(sym)

        results[sym] = data

        if db is not None and data.get("trust_level") not in ("FAILED", None):
            try:
                db.save_fundamentals(sym, data)
            except Exception as exc:
                _log.warning("Failed to cache fundamentals for %s: %s", sym, exc)

        if progress_callback:
            progress_callback(idx + 1, total, sym)

        if idx < total - 1:
            time.sleep(delay_sec)

    return results


def load_nifty500_universe(csv_path: str) -> List[Tuple[str, str]]:
    """
    Load NSE symbols and industries from NIFTY 500 constituent CSV.

    CSV format (NSE official): Company Name, Industry, Symbol, Series, ISIN Code
    Returns list of (symbol, industry) tuples, EQ series only.
    Skips rows where symbol contains "DUMMY" (placeholder entries).
    """
    import csv
    result = []
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                series = str(row.get("Series", "") or "").strip().upper()
                sym    = str(row.get("Symbol", "") or "").strip().upper()
                industry = str(row.get("Industry", "") or "").strip()
                if sym and series == "EQ":
                    if "DUMMY" in sym:
                        _log.warning("Skipping placeholder symbol '%s' in universe CSV", sym)
                        continue
                    result.append((sym, industry))
    except Exception as exc:
        _log.error("Failed to load universe from %s: %s", csv_path, exc)
    return result


def load_nifty50_universe(csv_path: str) -> List[Tuple[str, str]]:
    return load_nifty500_universe(csv_path)


def get_price_history(symbol: str, period: str = "1y") -> Optional[object]:
    """
    Fetch daily price history for a stock via yfinance.
    Returns a pandas DataFrame with OHLCV. Returns None on failure.
    """
    import yfinance as yf
    try:
        ticker_sym = f"{symbol.strip().upper()}.NS"
        df = yf.download(ticker_sym, period=period, progress=False, auto_adjust=True)
        if df is None or len(df) < 10:
            return None
        return df
    except Exception as exc:
        _log.warning("Price history fetch failed for %s: %s", symbol, exc)
        return None
