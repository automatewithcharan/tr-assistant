"""
Weighted composite ranking engine for stock screening.

Computes a multi-factor rank score combining:
- Momentum (RSI slope, MACD histogram trend)
- Breakout proximity (52-week high nearness, volume confirmation)
- Volume quality (relative volume, trade value)
- Trend alignment (MA stacking, price position)
- Relative strength (stock vs index performance)

Higher CompositeRank = better trading candidate.
"""

import logging
import math
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger("pkscreener")


class RankingEngine:
    """
    Multi-factor ranking that combines multiple signal dimensions
    into a single CompositeRank (0-100 scale).
    """

    # Default factor weights (sum to 100)
    DEFAULT_WEIGHTS = {
        "momentum": 20,
        "breakout": 25,
        "volume": 15,
        "trend": 20,
        "relative_strength": 10,
        "quality": 10,
    }

    def __init__(self, weights: Optional[Dict[str, float]] = None):
        self.weights = weights or self.DEFAULT_WEIGHTS.copy()
        # Normalize weights to sum to 100
        total = sum(self.weights.values())
        if total > 0 and total != 100:
            self.weights = {k: v * 100 / total for k, v in self.weights.items()}

    def rank_single(self, indicators: Dict, ohlcv_df: Optional[pd.DataFrame] = None) -> Dict[str, float]:
        """
        Compute factor scores and composite rank for a single stock.

        Args:
            indicators: Dict with keys like 'rsi', 'macd_hist', 'cci', 'mfi',
                        'volume_ratio', 'close', 'ema20', 'ema50', 'sma200',
                        'atr', 'atr_trailing_stop', 'change_pct', 'confidence'
            ohlcv_df: Optional OHLCV DataFrame for breakout/RS computation

        Returns:
            Dict with factor scores and 'CompositeRank'
        """
        scores = {}

        scores["momentum_score"] = self._score_momentum(indicators)
        scores["breakout_score"] = self._score_breakout(indicators, ohlcv_df)
        scores["volume_score"] = self._score_volume(indicators)
        scores["trend_score"] = self._score_trend(indicators)
        scores["rs_score"] = self._score_relative_strength(indicators, ohlcv_df)
        scores["quality_score"] = self._score_quality(indicators)

        composite = (
            scores["momentum_score"] * self.weights.get("momentum", 0)
            + scores["breakout_score"] * self.weights.get("breakout", 0)
            + scores["volume_score"] * self.weights.get("volume", 0)
            + scores["trend_score"] * self.weights.get("trend", 0)
            + scores["rs_score"] * self.weights.get("relative_strength", 0)
            + scores["quality_score"] * self.weights.get("quality", 0)
        ) / 100.0

        scores["CompositeRank"] = round(max(0.0, min(100.0, composite)), 1)
        return scores

    def rank_dataframe(self, report_df: pd.DataFrame) -> pd.DataFrame:
        """
        Add CompositeRank column to a report DataFrame.
        Expects columns: RSI, CCI, MFI, VolumeRatio, ChangePct, ConfidencePct
        """
        if report_df is None or report_df.empty:
            return report_df

        ranks = []
        for _, row in report_df.iterrows():
            indicators = {
                "rsi": _safe_float(row.get("RSI")),
                "cci": _safe_float(row.get("CCI")),
                "mfi": _safe_float(row.get("MFI")),
                "volume_ratio": _safe_float(row.get("VolumeRatio", 1.0)),
                "change_pct": _safe_float(row.get("ChangePct", 0)),
                "confidence": _safe_float(row.get("ConfidencePct", 0)),
                "close": _safe_float(row.get("PriceNow", 0)),
                "entry_gap_pct": _safe_float(row.get("EntryGapPct", 0)),
            }
            scores = self.rank_single(indicators)
            ranks.append(scores["CompositeRank"])

        report_df = report_df.copy()
        report_df["CompositeRank"] = ranks
        report_df.sort_values("CompositeRank", ascending=False, inplace=True)
        return report_df

    # ---- Factor scoring methods (each returns 0-100) ----

    def _score_momentum(self, ind: Dict) -> float:
        """RSI + CCI + MFI momentum composite."""
        score = 0.0
        rsi = ind.get("rsi", 50)
        cci = ind.get("cci", 0)
        mfi = ind.get("mfi", 50)

        # RSI: 40-70 is bullish momentum zone
        if 40 <= rsi <= 70:
            score += 40.0
        elif 30 <= rsi < 40:
            score += 25.0  # oversold recovery potential
        elif rsi < 30:
            score += 15.0  # deep oversold, may not recover soon
        elif 70 < rsi <= 80:
            score += 20.0  # strong momentum but overbought risk
        else:
            score += 5.0   # extreme overbought

        # CCI: positive and rising is bullish
        if 0 < cci <= 100:
            score += 30.0
        elif 100 < cci <= 200:
            score += 25.0  # strong but extended
        elif cci > 200:
            score += 10.0  # overextended
        elif -100 <= cci <= 0:
            score += 15.0  # neutral-bearish
        else:
            score += 20.0  # oversold CCI, reversal potential

        # MFI: 20-80 is the useful range
        if 40 <= mfi <= 80:
            score += 30.0
        elif 20 <= mfi < 40:
            score += 20.0
        elif mfi < 20:
            score += 15.0  # oversold
        else:
            score += 5.0   # overbought

        return score

    def _score_breakout(self, ind: Dict, ohlcv_df: Optional[pd.DataFrame]) -> float:
        """52-week high proximity + volume confirmation."""
        score = 0.0
        close = ind.get("close", 0)
        volume_ratio = ind.get("volume_ratio", 1.0)

        if ohlcv_df is not None and not ohlcv_df.empty and close > 0:
            # 52-week high proximity
            high_col = "high" if "high" in ohlcv_df.columns else "High"
            if high_col in ohlcv_df.columns:
                week_52_high = ohlcv_df[high_col].tail(250).max()
                if week_52_high > 0:
                    pct_from_high = (close / week_52_high) * 100.0
                    if pct_from_high >= 97:
                        score += 50.0  # Within 3% of 52w high - breakout zone
                    elif pct_from_high >= 90:
                        score += 35.0  # Within 10% - approaching
                    elif pct_from_high >= 80:
                        score += 20.0  # Within 20%
                    else:
                        score += 5.0   # Far from high

            # Recent higher-highs pattern (last 10 candles)
            if len(ohlcv_df) >= 10:
                recent_highs = ohlcv_df[high_col].tail(10).values
                higher_highs = sum(1 for i in range(1, len(recent_highs)) if recent_highs[i] > recent_highs[i-1])
                score += min(20.0, higher_highs * 4.0)
        else:
            # No OHLCV data: use change_pct as proxy
            change = ind.get("change_pct", 0)
            if change > 3:
                score += 30.0
            elif change > 1:
                score += 15.0

        # Volume confirmation for breakout
        if volume_ratio >= 2.0:
            score += 30.0
        elif volume_ratio >= 1.5:
            score += 20.0
        elif volume_ratio >= 1.0:
            score += 10.0

        return min(100.0, score)

    def _score_volume(self, ind: Dict) -> float:
        """Volume quality and relative volume score."""
        volume_ratio = ind.get("volume_ratio", 1.0)
        change_pct = ind.get("change_pct", 0)

        # Base volume score
        if volume_ratio >= 3.0:
            score = 80.0
        elif volume_ratio >= 2.0:
            score = 65.0
        elif volume_ratio >= 1.5:
            score = 50.0
        elif volume_ratio >= 1.0:
            score = 35.0
        elif volume_ratio >= 0.5:
            score = 15.0
        else:
            score = 5.0

        # Bonus: volume + price moving same direction = conviction
        if volume_ratio > 1.5 and change_pct > 0:
            score += 20.0
        elif volume_ratio > 1.5 and change_pct < -1:
            score -= 10.0  # heavy volume on decline = distribution

        return max(0.0, min(100.0, score))

    def _score_trend(self, ind: Dict) -> float:
        """MA alignment and trend direction score."""
        close = ind.get("close", 0)
        ema20 = ind.get("ema20", 0)
        ema50 = ind.get("ema50", 0)
        sma200 = ind.get("sma200", 0)
        score = 0.0

        if close > 0 and ema20 > 0 and ema50 > 0:
            # Price > EMA20 > EMA50: bullish alignment
            if close > ema20 > ema50:
                score += 50.0
            elif close > ema20:
                score += 35.0
            elif close > ema50:
                score += 20.0
            else:
                score += 5.0  # below both MAs

            # SMA200 trend
            if sma200 > 0:
                if close > sma200:
                    score += 30.0  # above 200 SMA = long-term uptrend
                else:
                    score += 5.0

            # EMA20 slope (crude: compare close vs ema20 distance)
            ema20_gap = (close - ema20) / ema20 * 100.0 if ema20 > 0 else 0
            if ema20_gap > 2:
                score += 20.0  # Strong above EMA20
            elif ema20_gap > 0:
                score += 10.0
        else:
            score = 30.0  # neutral when MAs unavailable

        return min(100.0, score)

    def _score_relative_strength(self, ind: Dict, ohlcv_df: Optional[pd.DataFrame]) -> float:
        """Relative strength vs index (simplified using change_pct)."""
        change_pct = ind.get("change_pct", 0)

        # Without index data, use absolute change as proxy
        if change_pct > 5:
            return 90.0
        elif change_pct > 3:
            return 70.0
        elif change_pct > 1:
            return 55.0
        elif change_pct > 0:
            return 40.0
        elif change_pct > -1:
            return 30.0
        elif change_pct > -3:
            return 15.0
        else:
            return 5.0

    def _score_quality(self, ind: Dict) -> float:
        """Quality gate score from confidence and trigger readiness."""
        confidence = ind.get("confidence", 0)
        entry_gap = abs(ind.get("entry_gap_pct", 0))

        # Confidence contribution (0-60)
        conf_score = min(60.0, max(0.0, confidence * 0.6))

        # Entry trigger proximity (0-40)
        if entry_gap <= 0.3:
            trigger_score = 40.0
        elif entry_gap <= 0.6:
            trigger_score = 30.0
        elif entry_gap <= 1.0:
            trigger_score = 20.0
        elif entry_gap <= 2.0:
            trigger_score = 10.0
        else:
            trigger_score = 0.0

        return min(100.0, conf_score + trigger_score)


def _safe_float(val, default: float = 0.0) -> float:
    """Safely convert to float."""
    try:
        if val is None or (isinstance(val, str) and not val.strip()):
            return default
        return float(val)
    except (ValueError, TypeError):
        return default
