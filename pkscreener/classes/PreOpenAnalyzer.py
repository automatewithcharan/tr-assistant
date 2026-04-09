"""
Pre-Open Session Analyzer (9:00 – 9:15 AM IST)
================================================

NSE pre-open session has 3 phases:
  1. 9:00 – 9:08: Order collection   (orders placed/modified/cancelled)
  2. 9:08 – 9:12: Call auction        (equilibrium price determined)
  3. 9:12 – 9:15: Buffer period       (transition to normal session)

Kite WebSocket delivers ticks during pre-open with:
  - last_price      → equilibrium price being discovered
  - total_buy_quantity / total_sell_quantity → order imbalance
  - volume_traded   → pre-open auction volume
  - open            → opening price (set after call auction ~9:08)

This module:
  1. Reads live ticks during 9:00-9:15 via KiteTickerStream snapshot
  2. Scores each symbol on pre-open auction signals
  3. Merges with the existing pre-market watchlist (NR7/compressed setups)
  4. Sends an "AUCTION ALERT" at ~9:12 AM with refined picks

Usage:
  Called from AutoPilotInvestor.run_loop() during the pre-open window.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.log import default_logger


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PreOpenSignal:
    """A single symbol's pre-open auction analysis."""
    symbol: str
    equilibrium_price: float = 0.0
    prev_close: float = 0.0
    gap_pct: float = 0.0
    total_buy_qty: float = 0.0
    total_sell_qty: float = 0.0
    imbalance_ratio: float = 0.0      # buy/sell ratio (>1 = demand, <1 = supply)
    preopen_volume: float = 0.0       # auction volume
    avg_daily_volume: float = 0.0     # average daily volume for context
    volume_vs_avg_pct: float = 0.0    # preopen vol as % of avg daily vol
    auction_score: float = 0.0        # composite score 0-100
    reasons: List[str] = field(default_factory=list)
    phase: str = ""                   # "collection", "auction", "buffer"


@dataclass
class PreOpenSummary:
    """Summary of the pre-open session analysis."""
    timestamp: str = ""
    phase: str = ""
    total_symbols_with_ticks: int = 0
    strong_demand: List[PreOpenSignal] = field(default_factory=list)   # imbalance > 2x buy
    strong_supply: List[PreOpenSignal] = field(default_factory=list)   # imbalance > 2x sell
    gap_ups: List[PreOpenSignal] = field(default_factory=list)         # gap > +1%
    gap_downs: List[PreOpenSignal] = field(default_factory=list)       # gap < -1%
    high_auction_volume: List[PreOpenSignal] = field(default_factory=list)  # vol > 0.5% of avg daily


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Pre-open window boundaries (minutes since midnight IST)
_PREOPEN_START = 9 * 60          # 9:00 AM
_ORDER_COLLECTION_END = 9 * 60 + 8   # 9:08 AM
_AUCTION_END = 9 * 60 + 12      # 9:12 AM
_BUFFER_END = 9 * 60 + 15       # 9:15 AM

# Scoring thresholds
_IMBALANCE_STRONG = 2.5          # buy_qty >= 2.5x sell_qty → strong demand
_IMBALANCE_MODERATE = 1.5        # buy_qty >= 1.5x sell_qty → moderate demand
_GAP_SIGNIFICANT_PCT = 1.0       # gap >= 1% is notable
_PREOPEN_VOL_NOTABLE_PCT = 0.5   # pre-open vol >= 0.5% of avg daily → notable
_PREOPEN_VOL_STRONG_PCT = 2.0    # pre-open vol >= 2% of avg daily → strong signal
_MIN_PRICE = 10.0                # skip penny stocks


# ---------------------------------------------------------------------------
# PreOpenAnalyzer
# ---------------------------------------------------------------------------

class PreOpenAnalyzer:
    """Analyze pre-open auction data from live Kite WebSocket ticks."""

    def __init__(
        self,
        universe: str = "all",
        history_cache_dir: Optional[str] = None,
    ) -> None:
        self.universe = universe
        self._output_dir = Archiver.get_user_data_dir()
        self._history_cache_dir = history_cache_dir or os.path.join(
            "results", "Data", "kite_history_cache", "day"
        )
        self._avg_volumes: Dict[str, float] = {}  # symbol -> avg daily volume (cached)
        self._avg_volumes_loaded = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze(self, ticks: Optional[Dict[str, Dict]] = None) -> PreOpenSummary:
        """Analyze current pre-open tick snapshot.

        Args:
            ticks: Dict of symbol -> tick dict. If None, loads from disk.

        Returns:
            PreOpenSummary with categorized signals.
        """
        if ticks is None:
            ticks = self._load_ticks()

        phase = self._current_phase()
        summary = PreOpenSummary(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            phase=phase,
            total_symbols_with_ticks=len(ticks),
        )

        if not ticks:
            return summary

        if not self._avg_volumes_loaded:
            self._load_avg_volumes()

        signals: List[PreOpenSignal] = []
        for symbol, tick in ticks.items():
            sig = self._analyze_symbol(symbol, tick, phase)
            if sig is not None:
                signals.append(sig)

        # Categorize
        for sig in signals:
            if sig.imbalance_ratio >= _IMBALANCE_STRONG:
                summary.strong_demand.append(sig)
            elif sig.imbalance_ratio > 0 and sig.imbalance_ratio <= 1.0 / _IMBALANCE_STRONG:
                summary.strong_supply.append(sig)

            if sig.gap_pct >= _GAP_SIGNIFICANT_PCT:
                summary.gap_ups.append(sig)
            elif sig.gap_pct <= -_GAP_SIGNIFICANT_PCT:
                summary.gap_downs.append(sig)

            if sig.volume_vs_avg_pct >= _PREOPEN_VOL_NOTABLE_PCT:
                summary.high_auction_volume.append(sig)

        # Sort each category by auction_score descending
        for lst in (summary.strong_demand, summary.strong_supply,
                    summary.gap_ups, summary.gap_downs, summary.high_auction_volume):
            lst.sort(key=lambda s: -s.auction_score)

        # Persist to disk for other modules
        self._persist_summary(summary)

        return summary

    def get_top_picks(
        self,
        summary: Optional[PreOpenSummary] = None,
        watchlist_symbols: Optional[List[str]] = None,
        max_picks: int = 10,
    ) -> List[PreOpenSignal]:
        """Return top pre-open picks, optionally filtered to watchlist symbols.

        These are stocks showing strong demand + positive gap + auction volume,
        ideal for buying at or just after market open.
        """
        if summary is None:
            summary = self.analyze()

        # Merge all signals, deduplicate
        all_signals: Dict[str, PreOpenSignal] = {}
        for lst in (summary.strong_demand, summary.gap_ups, summary.high_auction_volume):
            for sig in lst:
                existing = all_signals.get(sig.symbol)
                if existing is None or sig.auction_score > existing.auction_score:
                    all_signals[sig.symbol] = sig

        # Filter: only bullish signals (gap >= 0, demand > supply)
        bullish = [
            s for s in all_signals.values()
            if s.gap_pct >= 0 and s.imbalance_ratio >= 1.0 and s.auction_score >= 30
        ]

        # If watchlist provided, boost scores for symbols in it
        if watchlist_symbols:
            wl_set = {s.upper() for s in watchlist_symbols}
            for sig in bullish:
                if sig.symbol in wl_set:
                    sig.auction_score += 20  # big boost for being in yesterday's picks
                    sig.reasons.append("📋 In pre-market watchlist")

        bullish.sort(key=lambda s: -s.auction_score)
        return bullish[:max_picks]

    def format_alert(self, picks: List[PreOpenSignal], phase: str = "") -> str:
        """Format picks into a Telegram/Discord alert message."""
        if not picks:
            return ""

        phase_str = phase or self._current_phase()
        phase_label = {
            "collection": "ORDER COLLECTION (9:00-9:08)",
            "auction": "CALL AUCTION (9:08-9:12)",
            "buffer": "BUFFER PERIOD (9:12-9:15)",
        }.get(phase_str, "PRE-OPEN")

        now_str = datetime.now().strftime("%d %b %Y %H:%M")
        lines = [
            f"**🔔 PRE-OPEN AUCTION ALERT — {now_str}**",
            f"Phase: **{phase_label}**",
            f"Top **{len(picks)}** auction signal{'s' if len(picks) != 1 else ''}:\n",
        ]

        for i, p in enumerate(picks, 1):
            gap_emoji = "🟢" if p.gap_pct >= 0 else "🔴"
            imb_str = ""
            if p.imbalance_ratio >= _IMBALANCE_STRONG:
                imb_str = f" | 📥 Buy {p.imbalance_ratio:.1f}x sell"
            elif p.imbalance_ratio >= _IMBALANCE_MODERATE:
                imb_str = f" | 📥 Buy {p.imbalance_ratio:.1f}x sell"

            vol_str = ""
            if p.volume_vs_avg_pct >= _PREOPEN_VOL_NOTABLE_PCT:
                vol_str = f" | 📊 Pre-open vol {p.volume_vs_avg_pct:.1f}% of daily avg"

            lines.append(
                f"**{i}. {p.symbol}** — Score {p.auction_score:.0f}/100\n"
                f"   {gap_emoji} Gap {p.gap_pct:+.1f}% | Eq price `{p.equilibrium_price:.2f}`"
                f"{imb_str}{vol_str}"
            )

            if p.reasons:
                reason_str = " • ".join(p.reasons[:3])
                lines.append(f"   _{reason_str}_")

        lines.append(
            "\n_⚡ Act fast at 9:15 — these stocks have institutional interest before market opens_"
        )
        return "\n".join(lines)

    @staticmethod
    def is_preopen_window() -> bool:
        """Return True if current time is within 9:00-9:15 AM IST."""
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)

        now_min = now_ist.hour * 60 + now_ist.minute
        return _PREOPEN_START <= now_min < _BUFFER_END

    @staticmethod
    def _current_phase() -> str:
        """Return current pre-open phase name."""
        try:
            import pytz
            ist = pytz.timezone("Asia/Kolkata")
            now_ist = datetime.now(ist)
        except Exception:
            from datetime import timezone, timedelta
            ist_offset = timezone(timedelta(hours=5, minutes=30))
            now_ist = datetime.now(ist_offset)

        now_min = now_ist.hour * 60 + now_ist.minute
        if now_min < _ORDER_COLLECTION_END:
            return "collection"
        elif now_min < _AUCTION_END:
            return "auction"
        elif now_min < _BUFFER_END:
            return "buffer"
        return "market_open"

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _analyze_symbol(self, symbol: str, tick: Dict, phase: str) -> Optional[PreOpenSignal]:
        """Score a single symbol's pre-open tick data."""
        last_price = float(tick.get("last_price") or 0)
        if last_price < _MIN_PRICE:
            return None

        prev_close = float(tick.get("close") or 0)  # OHLC 'close' = prev day close in Kite ticks
        if prev_close <= 0:
            return None

        open_price = float(tick.get("open") or 0)
        # During pre-open, 'open' may be 0 until auction completes; use last_price as proxy
        eq_price = open_price if open_price > 0 else last_price

        gap_pct = (eq_price - prev_close) / prev_close * 100.0

        total_buy = float(tick.get("total_buy_quantity") or 0)
        total_sell = float(tick.get("total_sell_quantity") or 0)
        preopen_vol = float(tick.get("volume_traded") or 0)
        avg_vol = self._avg_volumes.get(symbol, 0)

        # Imbalance ratio
        imbalance = 0.0
        if total_sell > 0:
            imbalance = total_buy / total_sell
        elif total_buy > 0:
            imbalance = 99.0  # effectively infinite demand

        # Pre-open volume as % of average daily volume
        vol_vs_avg = (preopen_vol / avg_vol * 100.0) if avg_vol > 0 else 0.0

        # --- Scoring ---
        score = 0.0
        reasons: List[str] = []

        # 1. Order imbalance (0-30 points)
        if imbalance >= _IMBALANCE_STRONG:
            pts = min(30, (imbalance - 1.0) * 10)
            score += pts
            reasons.append(f"Strong demand: buy orders {imbalance:.1f}x sell")
        elif imbalance >= _IMBALANCE_MODERATE:
            pts = min(15, (imbalance - 1.0) * 8)
            score += pts
            reasons.append(f"Moderate demand: buy {imbalance:.1f}x sell")
        elif 0 < imbalance < 1.0 / _IMBALANCE_MODERATE:
            reasons.append(f"Supply pressure: sell heavy ({1/imbalance:.1f}x)")

        # 2. Gap direction (0-25 points)
        if gap_pct >= 3.0:
            score += 25
            reasons.append(f"Strong gap up +{gap_pct:.1f}%")
        elif gap_pct >= 1.0:
            score += 15
            reasons.append(f"Gap up +{gap_pct:.1f}%")
        elif gap_pct >= 0.3:
            score += 8
            reasons.append(f"Slight gap up +{gap_pct:.1f}%")
        elif gap_pct <= -2.0:
            score -= 10  # penalize gap downs

        # 3. Pre-open auction volume (0-25 points)
        if vol_vs_avg >= _PREOPEN_VOL_STRONG_PCT:
            score += 25
            reasons.append(f"Heavy auction volume ({vol_vs_avg:.1f}% of daily avg)")
        elif vol_vs_avg >= _PREOPEN_VOL_NOTABLE_PCT:
            score += 12
            reasons.append(f"Notable auction volume ({vol_vs_avg:.1f}% of daily avg)")
        elif preopen_vol > 0 and avg_vol > 0:
            # Some volume, but below threshold — small bonus
            score += 3

        # 4. Phase-specific bonus (0-20 points)
        # Signals during auction/buffer are more reliable than collection phase
        if phase == "buffer":
            score += 10  # highest confidence — price is locked in
            reasons.append("Price confirmed (buffer phase)")
        elif phase == "auction":
            score += 5   # price being discovered
            reasons.append("Price being discovered (auction phase)")
        # collection phase: no bonus, orders can still be cancelled

        # Clamp score
        score = max(0, min(100, score))

        return PreOpenSignal(
            symbol=symbol,
            equilibrium_price=eq_price,
            prev_close=prev_close,
            gap_pct=round(gap_pct, 2),
            total_buy_qty=total_buy,
            total_sell_qty=total_sell,
            imbalance_ratio=round(imbalance, 2),
            preopen_volume=preopen_vol,
            avg_daily_volume=avg_vol,
            volume_vs_avg_pct=round(vol_vs_avg, 2),
            auction_score=round(score, 1),
            reasons=reasons,
            phase=phase,
        )

    def _load_ticks(self) -> Dict[str, Dict]:
        """Load latest tick snapshot from KiteTickerStream."""
        ticks_path = os.path.join(
            self._output_dir,
            f"kiteticker_{self.universe}_ticks_latest.json",
        )
        if not os.path.isfile(ticks_path):
            return {}
        try:
            with open(ticks_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k).strip().upper(): v for k, v in data.items()} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _load_avg_volumes(self) -> None:
        """Load average daily volumes from historical cache for all symbols."""
        self._avg_volumes_loaded = True
        cache_dir = self._history_cache_dir
        if not os.path.isdir(cache_dir):
            return

        for fname in os.listdir(cache_dir):
            if not fname.endswith(".csv"):
                continue
            symbol = fname.replace(".csv", "").upper()
            fpath = os.path.join(cache_dir, fname)
            try:
                df = pd.read_csv(fpath, usecols=["volume"], nrows=20)
                if not df.empty and "volume" in df.columns:
                    self._avg_volumes[symbol] = float(df["volume"].mean())
            except Exception:
                continue

    def _persist_summary(self, summary: PreOpenSummary) -> None:
        """Save pre-open summary to disk for other modules to read."""
        os.makedirs(self._output_dir, exist_ok=True)
        out_path = os.path.join(self._output_dir, "preopen_auction_latest.json")
        try:
            data = {
                "timestamp": summary.timestamp,
                "phase": summary.phase,
                "total_symbols": summary.total_symbols_with_ticks,
                "strong_demand_count": len(summary.strong_demand),
                "strong_supply_count": len(summary.strong_supply),
                "gap_ups_count": len(summary.gap_ups),
                "gap_downs_count": len(summary.gap_downs),
                "high_vol_count": len(summary.high_auction_volume),
                "top_demand": [
                    {"symbol": s.symbol, "score": s.auction_score, "gap": s.gap_pct,
                     "imbalance": s.imbalance_ratio, "vol_pct": s.volume_vs_avg_pct}
                    for s in summary.strong_demand[:15]
                ],
                "top_gap_ups": [
                    {"symbol": s.symbol, "score": s.auction_score, "gap": s.gap_pct,
                     "imbalance": s.imbalance_ratio}
                    for s in summary.gap_ups[:15]
                ],
            }
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as exc:
            default_logger().debug(f"Failed to persist pre-open summary: {exc}")
