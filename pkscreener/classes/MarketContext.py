"""
Market Context Layer — provides real-time market regime, sector, and breadth
signals to improve anomaly detection quality.

Features:
1. Market regime detection (trending up, down, choppy)
2. Sector strength mapping (which sectors are hot/cold)
3. Breadth analysis (advance/decline ratio from live ticks)
4. Order imbalance detection (buy/sell pressure from depth)
5. Per-stock sector classification

Used by AnomalyDetector to:
- Suppress alerts during market crashes
- Boost alerts when sector confirms
- Detect chain-reaction opportunities (leader/follower)
"""

import csv
import json
import os
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from PKDevTools.classes.log import default_logger


# ---------------------------------------------------------------------------
# Sector mapping from Nifty500 Industry data + manual extensions
# ---------------------------------------------------------------------------
_SECTOR_MAP: Dict[str, str] = {}  # symbol -> sector
_SECTOR_STOCKS: Dict[str, Set[str]] = defaultdict(set)  # sector -> {symbols}

# Coarse sector grouping (map Nifty500 "Industry" → our sector buckets)
_INDUSTRY_TO_SECTOR = {
    "Financial Services": "FINANCE",
    "Capital Goods": "CAPGOODS",
    "Healthcare": "PHARMA",
    "Automobile and Auto Components": "AUTO",
    "Fast Moving Consumer Goods": "FMCG",
    "Information Technology": "IT",
    "Consumer Services": "CONSUMER",
    "Chemicals": "CHEMICALS",
    "Consumer Durables": "CONSUMER",
    "Oil Gas & Consumable Fuels": "ENERGY",
    "Power": "ENERGY",
    "Metals & Mining": "METALS",
    "Services": "SERVICES",
    "Construction": "INFRA",
    "Construction Materials": "INFRA",
    "Realty": "REALTY",
    "Telecommunication": "TELECOM",
    "Textiles": "TEXTILES",
    "Media Entertainment & Publication": "MEDIA",
    "Diversified": "DIVERSIFIED",
    "Forest Materials": "MATERIALS",
    "Sovereign Gold Bond": "OTHER",
}

# Key index-like stocks/ETFs to track for broad market sentiment
MARKET_BENCHMARKS = {"NIFTY50", "NIFTYIETF", "BANKIETF", "JUNIORBEES"}


def _load_sector_map() -> None:
    """Load sector mappings from Nifty500 index file."""
    global _SECTOR_MAP, _SECTOR_STOCKS
    if _SECTOR_MAP:
        return

    csv_path = os.path.join("results", "Indices", "ind_nifty500list.csv")
    if not os.path.exists(csv_path):
        default_logger().debug(f"Sector mapping file not found: {csv_path}")
        return

    try:
        import pandas as pd
        df = pd.read_csv(csv_path)
        if "Symbol" in df.columns and "Industry" in df.columns:
            for _, row in df.iterrows():
                sym = str(row["Symbol"]).strip().upper()
                industry = str(row["Industry"]).strip()
                sector = _INDUSTRY_TO_SECTOR.get(industry, "OTHER")
                _SECTOR_MAP[sym] = sector
                _SECTOR_STOCKS[sector].add(sym)
        default_logger().info(
            f"MarketContext: loaded sector map for {len(_SECTOR_MAP)} stocks "
            f"across {len(_SECTOR_STOCKS)} sectors"
        )
    except Exception as exc:
        default_logger().debug(f"Failed loading sector map: {exc}")


def get_sector(symbol: str) -> str:
    """Return the sector bucket for a symbol, or 'UNKNOWN'."""
    _load_sector_map()
    return _SECTOR_MAP.get(str(symbol).strip().upper(), "UNKNOWN")


def get_sector_peers(symbol: str) -> Set[str]:
    """Return all symbols in the same sector."""
    _load_sector_map()
    sector = _SECTOR_MAP.get(str(symbol).strip().upper())
    if not sector:
        return set()
    return _SECTOR_STOCKS.get(sector, set()) - {str(symbol).strip().upper()}


@dataclass
class MarketRegime:
    """Snapshot of current market conditions."""
    # Breadth
    advancing: int = 0
    declining: int = 0
    unchanged: int = 0
    breadth_ratio: float = 1.0     # advancing / declining

    # Benchmark
    nifty_change_pct: float = 0.0  # NIFTY50 or proxy change % today

    # Regime classification
    regime: str = "neutral"        # "strong_bull", "bull", "neutral", "bear", "crash"
    regime_score: float = 0.0      # -100 (crash) to +100 (strong rally)

    # Sector heat
    sector_strength: Dict[str, float] = field(default_factory=dict)
    # sector -> avg change% of its stocks

    # Order flow
    total_buy_pressure: float = 0.0   # Sum of total_buy_quantity across all ticks
    total_sell_pressure: float = 0.0  # Sum of total_sell_quantity

    # TICK/TRIN (market internals from NSE-Stock-Scanner concept)
    tick_value: int = 0               # net advancing - declining (positive = bullish)
    trin: float = 1.0                 # (adv/decl) / (adv_vol/decl_vol); <1 = bullish, >1 = bearish


class MarketContextEngine:
    """Computes real-time market context from the live tick stream.

    Updated every tick cycle. Provides:
    - market_regime: overall market direction & strength
    - sector_heat(symbol): how strong is this stock's sector right now
    - is_safe_to_trade(): whether market conditions allow new entries
    - sector_confirmation(symbol): True if sector peers are also moving
    """

    def __init__(self):
        self._regime = MarketRegime()
        self._lock = threading.Lock()
        _load_sector_map()

    def update(self, ticks: Dict[str, Dict]) -> MarketRegime:
        """Recompute market context from current tick snapshot."""
        if not ticks:
            return self._regime

        advancing = 0
        declining = 0
        unchanged = 0
        advancing_volume = 0.0
        declining_volume = 0.0
        sector_changes: Dict[str, List[float]] = defaultdict(list)
        total_buy = 0.0
        total_sell = 0.0
        nifty_change = 0.0

        for sym, tick in ticks.items():
            sym_upper = str(sym).strip().upper()
            try:
                close = float(tick.get("close", 0) or 0)  # prev day close
                ltp = float(tick.get("last_price", 0) or 0)
                if close <= 0 or ltp <= 0:
                    continue
                change_pct = (ltp - close) / close * 100.0
            except (ValueError, TypeError):
                continue

            # Breadth + volume-weighted breadth (for TRIN)
            vol = float(tick.get("volume_traded", 0) or tick.get("volume", 0) or 0)
            if change_pct > 0.1:
                advancing += 1
                advancing_volume += vol
            elif change_pct < -0.1:
                declining += 1
                declining_volume += vol
            else:
                unchanged += 1

            # Sector heat
            sector = _SECTOR_MAP.get(sym_upper)
            if sector:
                sector_changes[sector].append(change_pct)

            # Order flow
            try:
                total_buy += float(tick.get("total_buy_quantity", 0) or 0)
                total_sell += float(tick.get("total_sell_quantity", 0) or 0)
            except (ValueError, TypeError):
                pass

            # Benchmark detection
            if sym_upper in ("NIFTYIETF", "NIFTY50", "NIFTYBEES"):
                nifty_change = change_pct

        # Breadth ratio
        breadth_ratio = advancing / max(declining, 1)

        # TICK: net advancing stocks (positive = bullish momentum)
        tick_value = advancing - declining

        # TRIN (Arms Index): (adv/decl) / (adv_vol/decl_vol)
        # < 1.0 = money flowing into advancing stocks (bullish)
        # > 1.0 = money flowing into declining stocks (bearish)
        if declining > 0 and declining_volume > 0 and advancing_volume > 0:
            trin = (advancing / declining) / (advancing_volume / declining_volume)
        else:
            trin = 1.0

        # Sector strength
        sector_strength = {}
        for sector, changes in sector_changes.items():
            if changes:
                sector_strength[sector] = sum(changes) / len(changes)

        # Regime classification
        regime_score = 0.0
        # Breadth component (±40)
        if breadth_ratio > 2.0:
            regime_score += 40
        elif breadth_ratio > 1.2:
            regime_score += 20
        elif breadth_ratio < 0.5:
            regime_score -= 40
        elif breadth_ratio < 0.8:
            regime_score -= 20

        # Benchmark component (±40)
        regime_score += max(-40, min(40, nifty_change * 15))

        # Order flow component (±20)
        if total_sell > 0:
            flow_ratio = total_buy / total_sell
            if flow_ratio > 1.5:
                regime_score += 20
            elif flow_ratio > 1.1:
                regime_score += 10
            elif flow_ratio < 0.7:
                regime_score -= 20
            elif flow_ratio < 0.9:
                regime_score -= 10

        # TRIN confirmation (±10): reinforces breadth signal
        if trin < 0.7:
            regime_score += 10   # strongly bullish flow
        elif trin > 1.5:
            regime_score -= 10   # strongly bearish flow

        regime_score = max(-100, min(100, regime_score))

        if regime_score >= 50:
            regime = "strong_bull"
        elif regime_score >= 15:
            regime = "bull"
        elif regime_score <= -50:
            regime = "crash"
        elif regime_score <= -15:
            regime = "bear"
        else:
            regime = "neutral"

        with self._lock:
            self._regime = MarketRegime(
                advancing=advancing,
                declining=declining,
                unchanged=unchanged,
                breadth_ratio=round(breadth_ratio, 2),
                nifty_change_pct=round(nifty_change, 2),
                regime=regime,
                regime_score=round(regime_score, 1),
                sector_strength=sector_strength,
                total_buy_pressure=total_buy,
                total_sell_pressure=total_sell,
                tick_value=tick_value,
                trin=round(trin, 3),
            )
        return self._regime

    @property
    def regime(self) -> MarketRegime:
        with self._lock:
            return self._regime

    def is_safe_to_trade(self) -> bool:
        """Returns False during crash/heavy-bear conditions."""
        r = self.regime
        return r.regime not in ("crash",)

    def sector_confirmation(self, symbol: str, ticks: Dict[str, Dict], min_peers: int = 2) -> Tuple[bool, float]:
        """Check if sector peers are also moving positively.

        Returns (confirmed: bool, sector_avg_change: float).
        """
        sector = _SECTOR_MAP.get(str(symbol).strip().upper())
        if not sector:
            return False, 0.0

        peers = _SECTOR_STOCKS.get(sector, set()) - {str(symbol).strip().upper()}
        if len(peers) < min_peers:
            return False, 0.0

        positive_peers = 0
        changes = []
        for peer in peers:
            tick = ticks.get(peer) or ticks.get(peer.lower()) or ticks.get(peer.upper())
            if not tick:
                continue
            try:
                close = float(tick.get("close", 0) or 0)
                ltp = float(tick.get("last_price", 0) or 0)
                if close > 0 and ltp > 0:
                    chg = (ltp - close) / close * 100
                    changes.append(chg)
                    if chg > 0.5:
                        positive_peers += 1
            except (ValueError, TypeError):
                continue

        avg_change = sum(changes) / len(changes) if changes else 0.0
        confirmed = positive_peers >= min_peers and avg_change > 0.5
        return confirmed, round(avg_change, 2)

    def get_leader_followers(self, ticks: Dict[str, Dict], min_change_pct: float = 3.0) -> Dict[str, List[str]]:
        """Identify sector leaders (stocks up > min_change_pct) and their lagging peers.

        Returns: {leader_symbol: [follower symbols that haven't moved yet]}
        """
        result = {}
        for sym, tick in ticks.items():
            sym_upper = str(sym).strip().upper()
            try:
                close = float(tick.get("close", 0) or 0)
                ltp = float(tick.get("last_price", 0) or 0)
                if close <= 0 or ltp <= 0:
                    continue
                change = (ltp - close) / close * 100
            except (ValueError, TypeError):
                continue

            if change < min_change_pct:
                continue

            # This stock is a leader — find lagging peers
            peers = get_sector_peers(sym_upper)
            followers = []
            for peer in peers:
                ptick = ticks.get(peer) or ticks.get(peer.lower())
                if not ptick:
                    continue
                try:
                    pc = float(ptick.get("close", 0) or 0)
                    pl = float(ptick.get("last_price", 0) or 0)
                    if pc > 0 and pl > 0:
                        peer_change = (pl - pc) / pc * 100
                        # Follower: same sector, hasn't moved much yet, volume building
                        if -0.5 < peer_change < 1.5:
                            followers.append(peer)
                except (ValueError, TypeError):
                    continue

            if followers:
                result[sym_upper] = followers

        return result
