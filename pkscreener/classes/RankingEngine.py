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
        "momentum": 15,
        "breakout": 20,
        "volume": 12,
        "trend": 15,
        "relative_strength": 10,
        "quality": 8,
        "volatility_expansion": 10,
        "setup_quality": 10,
    }

    # Factor key → score column name mapping
    _FACTOR_SCORE_COLS = {
        "momentum": "momentum_score",
        "breakout": "breakout_score",
        "volume": "volume_score",
        "trend": "trend_score",
        "relative_strength": "rs_score",
        "quality": "quality_score",
        "volatility_expansion": "volatility_expansion_score",
        "setup_quality": "setup_quality_score",
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
        scores["volatility_expansion_score"] = self._score_volatility_expansion(indicators, ohlcv_df)
        scores["setup_quality_score"] = self._score_setup_quality(indicators, ohlcv_df)

        composite = sum(
            scores[self._FACTOR_SCORE_COLS[factor]] * self.weights.get(factor, 0)
            for factor in self.weights
            if factor in self._FACTOR_SCORE_COLS
        ) / 100.0

        scores["CompositeRank"] = round(max(0.0, min(100.0, composite)), 1)
        scores["setup_type"] = self._classify_setup(scores, indicators)
        return scores

    def rank_dataframe(self, report_df: pd.DataFrame) -> pd.DataFrame:
        """
        Add CompositeRank and score breakdown columns to a report DataFrame.
        Uses vectorized numpy operations for speed.
        Expects columns: RSI, CCI, MFI, VolumeRatio, ChangePct, ConfidencePct
        """
        if report_df is None or report_df.empty:
            return report_df

        df = report_df.copy()
        n = len(df)

        # Extract arrays (safe float conversion via vectorized helper)
        rsi = _safe_float_series(df.get("RSI"), 50.0, n)
        cci = _safe_float_series(df.get("CCI"), 0.0, n)
        mfi = _safe_float_series(df.get("MFI"), 50.0, n)
        volume_ratio = _safe_float_series(df.get("VolumeRatio"), 1.0, n)
        change_pct = _safe_float_series(df.get("ChangePct"), 0.0, n)
        confidence = _safe_float_series(df.get("ConfidencePct"), 0.0, n)
        entry_gap = np.abs(_safe_float_series(df.get("EntryGapPct"), 0.0, n))
        atr_pct = _safe_float_series(df.get("ATRPct"), 0.0, n)
        avg_trade_value = _safe_float_series(df.get("AvgTradeValue20"), 0.0, n)

        # Vectorized scoring
        mom = _vec_score_momentum(rsi, cci, mfi)
        brk = _vec_score_breakout(volume_ratio, change_pct)
        vol = _vec_score_volume(volume_ratio, change_pct)
        trn = _vec_score_trend_from_indicators(change_pct)
        rs = _vec_score_relative_strength(change_pct)
        qua = _vec_score_quality(confidence, entry_gap)
        vex = _vec_score_volatility_expansion(atr_pct, volume_ratio, change_pct)
        sq = _vec_score_setup_quality(volume_ratio, avg_trade_value, confidence, entry_gap)

        # Composite
        composite = (
            mom * self.weights.get("momentum", 0)
            + brk * self.weights.get("breakout", 0)
            + vol * self.weights.get("volume", 0)
            + trn * self.weights.get("trend", 0)
            + rs * self.weights.get("relative_strength", 0)
            + qua * self.weights.get("quality", 0)
            + vex * self.weights.get("volatility_expansion", 0)
            + sq * self.weights.get("setup_quality", 0)
        ) / 100.0

        df["CompositeRank"] = np.round(np.clip(composite, 0.0, 100.0), 1)
        df["MomentumScore"] = np.round(mom, 1)
        df["BreakoutScore"] = np.round(brk, 1)
        df["VolumeScore"] = np.round(vol, 1)
        df["TrendScore"] = np.round(trn, 1)
        df["RSScore"] = np.round(rs, 1)
        df["QualityScore"] = np.round(qua, 1)
        df["VolExpansionScore"] = np.round(vex, 1)
        df["SetupQualityScore"] = np.round(sq, 1)

        df.sort_values("CompositeRank", ascending=False, inplace=True)
        return df

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

    def _score_volatility_expansion(self, ind: Dict, ohlcv_df: Optional[pd.DataFrame]) -> float:
        """Detect volatility contraction followed by expansion (VCP-like).

        High scores mean the stock was recently quiet and is now expanding —
        classic pre-breakout behavior.
        """
        score = 0.0
        volume_ratio = ind.get("volume_ratio", 1.0)
        change_pct = abs(ind.get("change_pct", 0))

        if ohlcv_df is not None and not ohlcv_df.empty and len(ohlcv_df) >= 20:
            high_col = "high" if "high" in ohlcv_df.columns else "High"
            low_col = "low" if "low" in ohlcv_df.columns else "Low"
            close_col = "close" if "close" in ohlcv_df.columns else "Close"
            if all(c in ohlcv_df.columns for c in [high_col, low_col, close_col]):
                recent = ohlcv_df.tail(20)
                ranges = (recent[high_col] - recent[low_col]).values
                closes = recent[close_col].values
                with np.errstate(divide="ignore", invalid="ignore"):
                    range_pcts = np.where(closes > 0, ranges / closes * 100.0, 0.0)

                if len(range_pcts) >= 10:
                    old_range = float(np.mean(range_pcts[:10]))
                    new_range = float(np.mean(range_pcts[-5:]))
                    today_range = float(range_pcts[-1]) if len(range_pcts) > 0 else 0

                    # Contraction: recent range is smaller than prior range
                    if old_range > 0 and new_range < old_range * 0.7:
                        score += 30.0
                    elif old_range > 0 and new_range < old_range * 0.85:
                        score += 15.0

                    # Today's expansion: current bar wider than recent average
                    if new_range > 0 and today_range > new_range * 1.5:
                        score += 30.0
                    elif new_range > 0 and today_range > new_range * 1.2:
                        score += 15.0
        else:
            atr_pct = ind.get("atr_pct", 0)
            if atr_pct > 0 and change_pct > atr_pct * 1.5:
                score += 30.0
            elif atr_pct > 0 and change_pct > atr_pct:
                score += 15.0

        # Volume expansion confirms volatility expansion
        if volume_ratio >= 2.0:
            score += 25.0
        elif volume_ratio >= 1.5:
            score += 15.0

        # Strong directional move adds conviction
        if change_pct > 3.0:
            score += 15.0
        elif change_pct > 1.5:
            score += 10.0

        return min(100.0, score)

    def _score_setup_quality(self, ind: Dict, ohlcv_df: Optional[pd.DataFrame]) -> float:
        """Combined setup quality: liquidity, execution readiness, clean chart.

        High scores mean the setup is actionable (liquid, near trigger, confirmed).
        """
        score = 0.0
        volume_ratio = ind.get("volume_ratio", 1.0)
        avg_trade_value = ind.get("avg_trade_value", 0)
        confidence = ind.get("confidence", 0)
        entry_gap = abs(ind.get("entry_gap_pct", 0))

        # Liquidity score (0-40)
        if avg_trade_value >= 100_000_000:
            score += 40.0
        elif avg_trade_value >= 50_000_000:
            score += 30.0
        elif avg_trade_value >= 10_000_000:
            score += 20.0
        elif avg_trade_value >= 5_000_000:
            score += 10.0

        # Volume participation (0-20)
        if volume_ratio >= 2.0:
            score += 20.0
        elif volume_ratio >= 1.5:
            score += 15.0
        elif volume_ratio >= 1.0:
            score += 10.0

        # Trigger proximity (0-20)
        if entry_gap <= 0.3:
            score += 20.0
        elif entry_gap <= 0.6:
            score += 15.0
        elif entry_gap <= 1.0:
            score += 10.0

        # Confidence (0-20)
        score += min(20.0, max(0.0, confidence * 0.2))

        return min(100.0, score)

    # ---- Setup classification ----

    def _classify_setup(self, scores: Dict, ind: Dict) -> str:
        """Classify the dominant setup type from score distribution."""
        if scores.get("breakout_score", 0) >= 70 and scores.get("volume_score", 0) >= 50:
            return "breakout"
        if scores.get("momentum_score", 0) >= 70:
            return "momentum"
        if scores.get("volatility_expansion_score", 0) >= 60:
            return "volatility_expansion"
        if scores.get("trend_score", 0) >= 70 and scores.get("rs_score", 0) >= 50:
            return "trend_continuation"
        if scores.get("rs_score", 0) >= 70:
            return "relative_strength"
        if scores.get("volume_score", 0) >= 70 and ind.get("change_pct", 0) > 2:
            return "volume_surge"
        return "composite"


# ---------------------------------------------------------------------------
# Vectorized scoring helpers (operate on numpy arrays for rank_dataframe)
# ---------------------------------------------------------------------------

def _safe_float_series(series, default: float, n: int) -> np.ndarray:
    """Convert a DataFrame column to a float64 numpy array with safe defaults."""
    if series is None:
        return np.full(n, default, dtype=np.float64)
    return pd.to_numeric(series, errors="coerce").fillna(default).to_numpy(dtype=np.float64)


def _vec_score_momentum(rsi: np.ndarray, cci: np.ndarray, mfi: np.ndarray) -> np.ndarray:
    """Vectorized momentum scoring using np.select."""
    r = np.select(
        condlist=[(rsi >= 40) & (rsi <= 70), (rsi >= 30) & (rsi < 40), rsi < 30, (rsi > 70) & (rsi <= 80)],
        choicelist=[40.0, 25.0, 15.0, 20.0],
        default=5.0,
    )
    c = np.select(
        condlist=[(cci > 0) & (cci <= 100), (cci > 100) & (cci <= 200), cci > 200, (cci >= -100) & (cci <= 0)],
        choicelist=[30.0, 25.0, 10.0, 15.0],
        default=20.0,
    )
    m = np.select(
        condlist=[(mfi >= 40) & (mfi <= 80), (mfi >= 20) & (mfi < 40), mfi < 20],
        choicelist=[30.0, 20.0, 15.0],
        default=5.0,
    )
    return r + c + m


def _vec_score_breakout(volume_ratio: np.ndarray, change_pct: np.ndarray) -> np.ndarray:
    """Vectorized breakout scoring (without OHLCV DataFrame — uses change_pct proxy)."""
    change_score = np.select(
        condlist=[change_pct > 3, change_pct > 1],
        choicelist=[30.0, 15.0],
        default=0.0,
    )
    vol_score = np.select(
        condlist=[volume_ratio >= 2.0, volume_ratio >= 1.5, volume_ratio >= 1.0],
        choicelist=[30.0, 20.0, 10.0],
        default=0.0,
    )
    return np.clip(change_score + vol_score, 0.0, 100.0)


def _vec_score_volume(volume_ratio: np.ndarray, change_pct: np.ndarray) -> np.ndarray:
    """Vectorized volume scoring."""
    base = np.select(
        condlist=[volume_ratio >= 3.0, volume_ratio >= 2.0, volume_ratio >= 1.5, volume_ratio >= 1.0, volume_ratio >= 0.5],
        choicelist=[80.0, 65.0, 50.0, 35.0, 15.0],
        default=5.0,
    )
    bonus = np.select(
        condlist=[(volume_ratio > 1.5) & (change_pct > 0), (volume_ratio > 1.5) & (change_pct < -1)],
        choicelist=[20.0, -10.0],
        default=0.0,
    )
    return np.clip(base + bonus, 0.0, 100.0)


def _vec_score_trend_from_indicators(change_pct: np.ndarray) -> np.ndarray:
    """Vectorized trend scoring (simplified — no per-row MA data available)."""
    return np.select(
        condlist=[change_pct > 3, change_pct > 1, change_pct > 0, change_pct > -1],
        choicelist=[80.0, 60.0, 45.0, 30.0],
        default=15.0,
    )


def _vec_score_relative_strength(change_pct: np.ndarray) -> np.ndarray:
    """Vectorized relative strength scoring."""
    return np.select(
        condlist=[change_pct > 5, change_pct > 3, change_pct > 1, change_pct > 0, change_pct > -1, change_pct > -3],
        choicelist=[90.0, 70.0, 55.0, 40.0, 30.0, 15.0],
        default=5.0,
    )


def _vec_score_quality(confidence: np.ndarray, entry_gap: np.ndarray) -> np.ndarray:
    """Vectorized quality scoring."""
    conf_score = np.clip(confidence * 0.6, 0.0, 60.0)
    trigger_score = np.select(
        condlist=[entry_gap <= 0.3, entry_gap <= 0.6, entry_gap <= 1.0, entry_gap <= 2.0],
        choicelist=[40.0, 30.0, 20.0, 10.0],
        default=0.0,
    )
    return np.clip(conf_score + trigger_score, 0.0, 100.0)


def _vec_score_volatility_expansion(atr_pct: np.ndarray, volume_ratio: np.ndarray,
                                     change_pct: np.ndarray) -> np.ndarray:
    """Vectorized volatility expansion scoring (uses ATR% as proxy)."""
    abs_change = np.abs(change_pct)
    atr_score = np.select(
        condlist=[(atr_pct > 0) & (abs_change > atr_pct * 1.5), (atr_pct > 0) & (abs_change > atr_pct)],
        choicelist=[30.0, 15.0],
        default=0.0,
    )
    vol_score = np.select(
        condlist=[volume_ratio >= 2.0, volume_ratio >= 1.5],
        choicelist=[25.0, 15.0],
        default=0.0,
    )
    dir_score = np.select(
        condlist=[abs_change > 3.0, abs_change > 1.5],
        choicelist=[15.0, 10.0],
        default=0.0,
    )
    return np.clip(atr_score + vol_score + dir_score, 0.0, 100.0)


def _vec_score_setup_quality(volume_ratio: np.ndarray, avg_trade_value: np.ndarray,
                              confidence: np.ndarray, entry_gap: np.ndarray) -> np.ndarray:
    """Vectorized setup quality scoring (liquidity + execution readiness)."""
    liq = np.select(
        condlist=[avg_trade_value >= 100_000_000, avg_trade_value >= 50_000_000, avg_trade_value >= 10_000_000, avg_trade_value >= 5_000_000],
        choicelist=[40.0, 30.0, 20.0, 10.0],
        default=0.0,
    )
    vol = np.select(
        condlist=[volume_ratio >= 2.0, volume_ratio >= 1.5, volume_ratio >= 1.0],
        choicelist=[20.0, 15.0, 10.0],
        default=0.0,
    )
    trg = np.select(
        condlist=[entry_gap <= 0.3, entry_gap <= 0.6, entry_gap <= 1.0],
        choicelist=[20.0, 15.0, 10.0],
        default=0.0,
    )
    conf = np.clip(confidence * 0.2, 0.0, 20.0)
    return np.clip(liq + vol + trg + conf, 0.0, 100.0)


def _safe_float(val, default: float = 0.0) -> float:
    """Safely convert to float."""
    try:
        if val is None or (isinstance(val, str) and not val.strip()):
            return default
        return float(val)
    except (ValueError, TypeError):
        return default
