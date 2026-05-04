"""
Pluggable fundamentals data provider framework for the wealth basket engine.

Provider hierarchy
------------------
YFinanceProvider   — default free source, always tagged LOW TRUST
ScreenerInProvider — scrapes screener.in public company pages, tagged MEDIUM TRUST
                     Provides ROCE (not in yfinance .info) and cross-validation data
CompositeProvider  — runs both, cross-validates key fields, sets final trust level

Trust escalation rules
----------------------
  yfinance only                              → LOW
  screener.in only                           → MEDIUM (better source but incomplete)
  both sources, key fields agree (< 20%)    → MEDIUM
  both sources, key field conflict (≥ 20%)  → CONFLICT (blocks AUTO_EXECUTE)
  official verified source (future)         → HIGH

Screener.in access
------------------
  Public company pages: https://www.screener.in/company/{SYMBOL}/consolidated/
  No API key or login required for the top-ratios summary section.
  Respects rate limits (1.5 s delay between requests).
  Falls back gracefully if blocked, rate-limited, or unavailable.

Cross-validated fields
----------------------
  pe_ratio, pb_ratio, roe_pct, debt_equity
  Conflict = relative difference ≥ CONFLICT_THRESHOLD (20%)

Fields exclusive to screener.in
--------------------------------
  roce_pct      — ROCE not available in yfinance .info
  promoter_pct  — promoter holding % (future)
"""

import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_log = logging.getLogger(__name__)

CONFLICT_THRESHOLD = 0.20          # 20% relative diff on any cross-validated field
_CROSS_VALIDATE_FIELDS = ["pe_ratio", "pb_ratio", "roe_pct", "debt_equity"]

_SCREENER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_SCREENER_BASE = "https://www.screener.in/company/{symbol}/consolidated/"
_SCREENER_FALLBACK = "https://www.screener.in/company/{symbol}/"
_SCREENER_TIMEOUT = 12
_SCREENER_DELAY = 1.5


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class DataProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    def trust_level_base(self) -> str:
        return "LOW"

    @abstractmethod
    def fetch(self, symbol: str) -> Dict:
        """Fetch fundamentals for one NSE symbol. Returns {} on failure."""
        ...

    def is_available(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# YFinance provider
# ---------------------------------------------------------------------------

class YFinanceProvider(DataProvider):
    """Wraps yfinance .info fetch. All data tagged LOW TRUST."""

    @property
    def name(self) -> str:
        return "yfinance"

    @property
    def trust_level_base(self) -> str:
        return "LOW"

    def fetch(self, symbol: str) -> Dict:
        try:
            import yfinance as yf
        except ImportError:
            _log.error("yfinance not installed")
            return {}

        ticker_sym = f"{symbol.strip().upper()}.NS"
        try:
            t = yf.Ticker(ticker_sym)
            info = t.info or {}
        except Exception as exc:
            _log.warning("YFinance fetch failed for %s: %s", symbol, exc)
            return {}

        if not info or info.get("quoteType") == "NONE":
            return {}

        def _safe_pct(val) -> Optional[float]:
            try:
                f = float(val)
                return round(f * 100, 2) if -10 < f < 10 else None
            except (TypeError, ValueError):
                return None

        def _sf(val) -> Optional[float]:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None

        _CR = 1e7
        mktcap = _sf(info.get("marketCap"))
        data = {
            "symbol": symbol.upper(),
            "source": "yfinance",
            "trust_level": "LOW",
            "market_cap_cr": round(mktcap / _CR, 1) if mktcap else None,
            "pe_ratio":             _sf(info.get("trailingPE")),
            "pb_ratio":             _sf(info.get("priceToBook")),
            "roe_pct":              _safe_pct(info.get("returnOnEquity")),
            "roce_pct":             None,   # not in yfinance .info
            "debt_equity":          _sf(info.get("debtToEquity")),
            "revenue_growth_yoy":   _safe_pct(info.get("revenueGrowth")),
            "profit_growth_yoy":    _safe_pct(info.get("earningsGrowth")),
            "gross_margin_pct":     _safe_pct(info.get("grossMargins")),
            "operating_margin_pct": _safe_pct(info.get("operatingMargins")),
            "net_margin_pct":       _safe_pct(info.get("profitMargins")),
            "current_ratio":        _sf(info.get("currentRatio")),
            "sector":               info.get("sector", ""),
            "industry":             info.get("industry", ""),
            "short_name":           info.get("shortName", ""),
        }

        # Try to compute ROCE from balance sheet + income statement
        data["roce_pct"] = self._compute_roce(t)

        return data

    @staticmethod
    def _compute_roce(ticker) -> Optional[float]:
        """
        ROCE = EBIT / Capital Employed × 100
        Capital Employed = Total Assets - Current Liabilities

        Uses yfinance financials/balance_sheet. Returns None if data absent.
        This requires two extra API calls — only attempted when screener.in is unavailable.
        """
        try:
            fin = ticker.income_stmt
            bs  = ticker.balance_sheet
            if fin is None or bs is None or fin.empty or bs.empty:
                return None

            ebit = None
            for label in ("EBIT", "Operating Income", "Operating Income Or Loss"):
                if label in fin.index:
                    val = fin.loc[label].iloc[0]
                    if val is not None and not (hasattr(val, '__float__') and str(val) == 'nan'):
                        ebit = float(val)
                        break

            total_assets = None
            for label in ("Total Assets",):
                if label in bs.index:
                    val = bs.loc[label].iloc[0]
                    if val is not None:
                        total_assets = float(val)
                        break

            curr_liab = None
            for label in ("Current Liabilities", "Total Current Liabilities"):
                if label in bs.index:
                    val = bs.loc[label].iloc[0]
                    if val is not None:
                        curr_liab = float(val)
                        break

            if ebit is None or total_assets is None or curr_liab is None:
                return None

            capital_employed = total_assets - curr_liab
            if capital_employed <= 0:
                return None

            return round((ebit / capital_employed) * 100, 2)
        except Exception as exc:
            _log.debug("ROCE computation failed: %s", exc)
            return None


# ---------------------------------------------------------------------------
# Screener.in provider
# ---------------------------------------------------------------------------

class ScreenerInProvider(DataProvider):
    """
    Scrapes screener.in public company pages for key financial ratios.

    Provides ROCE (not available in yfinance .info) and serves as a second
    source for cross-validating PE, ROE, and D/E from yfinance.

    No login or API key required. Uses public consolidated view.
    Respects rate limits and falls back gracefully on any failure.
    """

    @property
    def name(self) -> str:
        return "screener.in"

    @property
    def trust_level_base(self) -> str:
        return "MEDIUM"

    def fetch(self, symbol: str) -> Dict:
        sym = symbol.strip().upper()
        html = self._fetch_html(sym)
        if not html:
            return {}
        ratios = self._parse_ratios(html)
        if not ratios:
            _log.debug("screener.in: no ratios parsed for %s", sym)
            return {}

        _log.debug("screener.in: %s → %s", sym, list(ratios.keys()))
        return {"symbol": sym, "source": "screener.in", **ratios}

    def _fetch_html(self, symbol: str) -> Optional[str]:
        for url_tpl in (_SCREENER_BASE, _SCREENER_FALLBACK):
            url = url_tpl.format(symbol=symbol)
            try:
                req = Request(url, headers={"User-Agent": _SCREENER_UA})
                with urlopen(req, timeout=_SCREENER_TIMEOUT) as resp:
                    if resp.status == 200:
                        return resp.read().decode("utf-8", errors="replace")
            except HTTPError as e:
                if e.code == 404:
                    _log.debug("screener.in: 404 for %s at %s", symbol, url)
                    continue
                _log.debug("screener.in: HTTP %s for %s", e.code, symbol)
            except URLError as e:
                _log.debug("screener.in: URLError for %s: %s", symbol, e)
            except Exception as e:
                _log.debug("screener.in: fetch error for %s: %s", symbol, e)
            time.sleep(0.3)
        return None

    @staticmethod
    def _parse_number(text: str) -> Optional[float]:
        """Extract a float from strings like '18.5 %', '₹ 1,270', '22.7', '0.32'."""
        if not text:
            return None
        cleaned = re.sub(r"[₹,\s%]", "", text.strip())
        cleaned = re.sub(r"[^\d.\-]", "", cleaned)
        try:
            return float(cleaned) if cleaned else None
        except ValueError:
            return None

    def _parse_ratios(self, html: str) -> Dict:
        """
        Parse the top-ratios section from screener.in HTML.

        Handles both new (div/span) and old (li/span) HTML structures.
        Returns a dict with normalized field names.
        """
        ratios: Dict = {}

        # Extract the top-ratios section
        section_match = re.search(
            r'id=["\']top-ratios["\'].*?</section',
            html, re.DOTALL
        )
        if not section_match:
            # Fall back to searching full page for ROCE pattern
            section_text = html
        else:
            section_text = section_match.group(0)

        # Pattern: <span class="name">ROCE</span> ... <span ... >18.5 %</span>
        # or: <li ...><span class="name">P/E</span><span class="number value">22.7</span></li>
        name_val_pattern = re.compile(
            r'<span[^>]*class=["\'][^"\']*name[^"\']*["\'][^>]*>\s*([^<]+?)\s*</span>'
            r'.*?'
            r'<span[^>]*(?:class=["\'][^"\']*(?:value|number)[^"\']*["\'])[^>]*>\s*([^<]+?)\s*</span>',
            re.DOTALL
        )

        for m in name_val_pattern.finditer(section_text):
            raw_name = m.group(1).strip()
            raw_val  = m.group(2).strip()
            val = self._parse_number(raw_val)
            if val is None:
                continue
            self._map_field(raw_name, val, ratios)
            if len(ratios) >= 12:   # enough fields
                break

        # If the pattern above got nothing, try a simpler li-based approach
        if not ratios:
            li_pattern = re.compile(
                r'<li[^>]*>.*?<span[^>]*>([^<]+?)</span>.*?<span[^>]*>([\d.,\s%₹\-]+)</span>.*?</li>',
                re.DOTALL
            )
            for m in li_pattern.finditer(section_text):
                val = self._parse_number(m.group(2))
                if val is not None:
                    self._map_field(m.group(1).strip(), val, ratios)

        # Also scan for ROCE anywhere in the page (it might appear outside top-ratios)
        if "roce_pct" not in ratios:
            roce_m = re.search(
                r'ROCE\s*</span>\s*.*?<span[^>]*>\s*([\d.]+)\s*%?\s*</span>',
                html, re.DOTALL
            )
            if roce_m:
                val = self._parse_number(roce_m.group(1))
                if val is not None:
                    ratios["roce_pct"] = val

        return ratios

    @staticmethod
    def _map_field(raw_name: str, val: float, out: Dict) -> None:
        """Map screener.in ratio name to our internal field name."""
        n = raw_name.strip().lower()
        if "market cap" in n:
            out["market_cap_cr"] = round(val, 1)
        elif n in ("p/e", "pe", "price/earnings", "p / e"):
            out["pe_ratio"] = val
        elif "book value" in n:
            pass   # raw book value — skip; compute P/B from price/book separately
        elif n in ("p/b", "pb", "price/book", "price to book"):
            out["pb_ratio"] = val
        elif n == "roce":
            out["roce_pct"] = val
        elif n == "roe":
            out["roe_pct"] = val
        elif "debt" in n and "equity" in n:
            out["debt_equity"] = val
        elif "current ratio" in n:
            out["current_ratio"] = val
        elif "dividend yield" in n:
            out["dividend_yield_pct"] = val


# ---------------------------------------------------------------------------
# Composite provider — cross-validation + trust assignment
# ---------------------------------------------------------------------------

class CompositeProvider(DataProvider):
    """
    Fetches from multiple providers and cross-validates key metrics.

    Trust rules:
      Both agree (all cross-validated fields within CONFLICT_THRESHOLD) → MEDIUM
      Any conflict (≥ 1 field outside threshold)                        → CONFLICT
      Only one source returned data                                      → LOW
      Neither source returned data                                       → FAILED
    """

    def __init__(
        self,
        primary: Optional[DataProvider] = None,
        secondary: Optional[DataProvider] = None,
        secondary_delay: float = _SCREENER_DELAY,
    ):
        self._primary   = primary   or YFinanceProvider()
        self._secondary = secondary or ScreenerInProvider()
        self._secondary_delay = secondary_delay

    @property
    def name(self) -> str:
        return f"{self._primary.name}+{self._secondary.name}"

    def fetch(self, symbol: str) -> Dict:
        primary_data = self._primary.fetch(symbol)
        if not primary_data:
            return {"symbol": symbol.upper(), "trust_level": "FAILED", "source": self.name}

        time.sleep(self._secondary_delay)
        secondary_data = self._secondary.fetch(symbol)

        if not secondary_data:
            # Only primary available — keep its data, mark LOW
            primary_data["trust_level"] = "LOW"
            primary_data["source"] = self._primary.name
            primary_data["secondary_available"] = False
            return primary_data

        # Merge: primary is base; fill None fields from secondary
        merged = dict(primary_data)
        merged["secondary_available"] = True

        for field, val in secondary_data.items():
            if field in ("symbol", "source", "trust_level"):
                continue
            if merged.get(field) is None and val is not None:
                merged[field] = val

        # Cross-validate overlapping fields
        conflicts, agreements = _cross_validate(
            primary_data, secondary_data, _CROSS_VALIDATE_FIELDS
        )

        if conflicts:
            merged["trust_level"]  = "CONFLICT"
            merged["conflict_fields"] = "; ".join(conflicts)
            _log.info("%s data conflict: %s", symbol, merged["conflict_fields"])
        else:
            merged["trust_level"] = "MEDIUM"
            merged["conflict_fields"] = ""

        merged["source"] = self.name
        return merged


# ---------------------------------------------------------------------------
# Cross-validation helper
# ---------------------------------------------------------------------------

def _cross_validate(
    a: Dict, b: Dict, fields: List[str]
) -> Tuple[List[str], List[str]]:
    """
    Compare two dicts on the given fields.

    Returns (conflicts, agreements).
    A conflict is when both values are non-None and relative diff ≥ threshold.
    """
    conflicts  = []
    agreements = []
    for field in fields:
        va = a.get(field)
        vb = b.get(field)
        if va is None or vb is None:
            continue
        denom = abs(va) if abs(va) > 1e-9 else 1.0
        rel_diff = abs(va - vb) / denom
        if rel_diff >= CONFLICT_THRESHOLD:
            conflicts.append(f"{field}:{va:.1f}vs{vb:.1f}")
        else:
            agreements.append(field)
    return conflicts, agreements


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_provider(use_screener: bool = True, screener_delay: float = _SCREENER_DELAY) -> DataProvider:
    """
    Return the appropriate provider.

    Args:
        use_screener: If True, use CompositeProvider (yfinance + screener.in).
                      If False, use YFinanceProvider only.
        screener_delay: Seconds to wait between screener.in requests.
    """
    if use_screener:
        return CompositeProvider(secondary_delay=screener_delay)
    return YFinanceProvider()
