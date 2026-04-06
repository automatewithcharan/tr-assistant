"""
    The MIT License (MIT)

    Copyright (c) 2023 pkjmesra

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

    Trading Signals Module
    ======================
    
    This module provides comprehensive buy/sell signal detection using multiple
    technical indicators and strategies. It aggregates various signal sources
    to produce strong buy/sell recommendations.
"""

from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
import numpy as np
import pandas as pd

from PKDevTools.classes.ColorText import colorText
from PKDevTools.classes.log import default_logger


class SignalStrength(Enum):
    """Signal strength levels."""
    STRONG_BUY = 5
    BUY = 4
    WEAK_BUY = 3
    NEUTRAL = 2
    WEAK_SELL = 1
    SELL = 0
    STRONG_SELL = -1


@dataclass
class SignalResult:
    """Container for a signal analysis result."""
    signal: SignalStrength
    confidence: float  # 0-100%
    reasons: List[str] = field(default_factory=list)
    indicators: Dict[str, float] = field(default_factory=dict)
    
    @property
    def is_buy(self) -> bool:
        return self.signal.value >= SignalStrength.WEAK_BUY.value
    
    @property
    def is_sell(self) -> bool:
        return self.signal.value <= SignalStrength.WEAK_SELL.value
    
    @property
    def is_strong_buy(self) -> bool:
        return self.signal == SignalStrength.STRONG_BUY
    
    @property
    def is_strong_sell(self) -> bool:
        return self.signal == SignalStrength.STRONG_SELL


class TradingSignals:
    """
    Comprehensive trading signal detector.
    
    Combines multiple technical indicators and strategies to generate
    strong buy/sell signals with confidence scores.
    
    Indicators used:
    - RSI (Relative Strength Index)
    - MACD (Moving Average Convergence Divergence)
    - ATR Trailing Stops
    - Volume Analysis
    - Moving Average Crossovers
    - Price Action Patterns
    
    Example:
        >>> from pkscreener.classes.screening.signals import TradingSignals
        >>> signals = TradingSignals(configManager)
        >>> result = signals.analyze(df)
        >>> if result.is_strong_buy:
        >>>     print(f"Strong Buy: {result.confidence}% confidence")
    """
    
    # Signal weight configuration
    WEIGHTS = {
        'rsi': 15,
        'macd': 15,
        'atr_trailing': 20,
        'volume': 15,
        'ma_crossover': 15,
        'price_action': 10,
        'momentum': 10,
    }
    
    def __init__(self, configManager=None):
        """
        Initialize TradingSignals analyzer.
        
        Args:
            configManager: Configuration manager instance
        """
        self.configManager = configManager
        self.logger = default_logger()
    
    def analyze(self, df: pd.DataFrame, saveDict: Dict = None,
                screenDict: Dict = None,
                incr_indicators: Dict = None) -> SignalResult:
        """
        Analyze a stock's DataFrame for trading signals.

        Args:
            df: OHLCV DataFrame with at least ['open', 'high', 'low', 'close', 'volume']
            saveDict: Dictionary to save results for persistence
            screenDict: Dictionary for screen display results
            incr_indicators: Optional pre-computed indicator dict from IncrementalIndicators.
                             When provided, skips all TA-Lib calls for max speed.
            
        Returns:
            SignalResult with overall signal, confidence, and reasons
        """
        if df is None or len(df) < 20:
            return SignalResult(
                signal=SignalStrength.NEUTRAL,
                confidence=0,
                reasons=["Insufficient data for analysis"]
            )

        # ------------------------------------------------------------------ #
        # Fast path: use pre-computed incremental indicator values            #
        # Skips all TA-Lib calls for symbols whose state is already warmed.   #
        # ------------------------------------------------------------------ #
        if incr_indicators is not None:
            return self._analyze_from_incremental(df, incr_indicators, saveDict, screenDict)

        try:
            from pkscreener.classes.Pktalib import pktalib
        except ImportError:
            return SignalResult(
                signal=SignalStrength.NEUTRAL,
                confidence=0,
                reasons=["Technical analysis library not available"]
            )
        
        signals = []
        reasons = []
        indicators = {}
        
        # 1. RSI Analysis
        rsi_signal, rsi_reason, rsi_value = self._analyze_rsi(df, pktalib)
        signals.append(('rsi', rsi_signal))
        if rsi_reason:
            reasons.append(rsi_reason)
        indicators['RSI'] = rsi_value
        
        # 2. MACD Analysis
        macd_signal, macd_reason = self._analyze_macd(df, pktalib)
        signals.append(('macd', macd_signal))
        if macd_reason:
            reasons.append(macd_reason)
        
        # 3. ATR Trailing Stop Analysis
        atr_signal, atr_reason = self._analyze_atr_trailing(df, pktalib)
        signals.append(('atr_trailing', atr_signal))
        if atr_reason:
            reasons.append(atr_reason)
        
        # 4. Volume Analysis
        volume_signal, volume_reason = self._analyze_volume(df)
        signals.append(('volume', volume_signal))
        if volume_reason:
            reasons.append(volume_reason)
        
        # 5. Moving Average Crossover Analysis
        ma_signal, ma_reason = self._analyze_ma_crossover(df, pktalib)
        signals.append(('ma_crossover', ma_signal))
        if ma_reason:
            reasons.append(ma_reason)
        
        # 6. Price Action Analysis
        pa_signal, pa_reason = self._analyze_price_action(df)
        signals.append(('price_action', pa_signal))
        if pa_reason:
            reasons.append(pa_reason)
        
        # 7. Momentum Analysis
        mom_signal, mom_reason, cci_value, mfi_value = self._analyze_momentum(df, pktalib)
        signals.append(('momentum', mom_signal))
        if mom_reason:
            reasons.append(mom_reason)
        indicators['CCI'] = cci_value
        indicators['MFI'] = mfi_value
        
        # Calculate weighted score
        total_weight = sum(self.WEIGHTS.values())
        weighted_score = 0
        
        for indicator, signal in signals:
            weight = self.WEIGHTS.get(indicator, 10)
            weighted_score += (signal * weight)
        
        # Normalize to 0-100 scale
        normalized_score = (weighted_score / total_weight) * 100
        
        # Determine signal strength
        overall_signal = self._score_to_signal(normalized_score)
        
        # Calculate confidence: how many analyzers agree on the direction
        # plus how strongly. Better than abs(score-50)*2 which gave weak numbers.
        bullish_count = sum(1 for _, s in signals if s > 0.55)
        bearish_count = sum(1 for _, s in signals if s < 0.45)
        total_analyzers = len(signals)
        # Direction agreement: what % of analyzers agree on the dominant side
        dominant_count = max(bullish_count, bearish_count)
        agreement_ratio = dominant_count / total_analyzers if total_analyzers > 0 else 0
        # Directional strength: how far from neutral is the weighted score
        directional_strength = abs(normalized_score - 50) / 50  # 0..1
        # Combine: 60% agreement + 40% strength, scale to 0-100
        confidence = min(100, round((agreement_ratio * 60 + directional_strength * 40) * 100 / 100, 1))
        
        # Update save/screen dicts
        signal_text = self._format_signal_text(overall_signal)
        if saveDict is not None:
            saveDict['Signal'] = overall_signal.name
            saveDict['Confidence'] = f"{confidence:.1f}%"
        if screenDict is not None:
            screenDict['Signal'] = signal_text
            screenDict['Confidence'] = f"{confidence:.1f}%"
        
        return SignalResult(
            signal=overall_signal,
            confidence=confidence,
            reasons=reasons,
            indicators=indicators
        )

    # ------------------------------------------------------------------ #
    # Incremental fast path                                               #
    # ------------------------------------------------------------------ #

    def _analyze_from_incremental(
        self,
        df: pd.DataFrame,
        incr: Dict,
        saveDict,
        screenDict,
    ) -> SignalResult:
        """Build a SignalResult from pre-computed incremental indicator values.

        Replaces all TA-Lib calls.  Volume and price-action analyses still use
        the (small) tail of the DataFrame since they are pure-Python and cheap.
        """
        signals = []
        reasons = []
        indicators = {}

        # 1. RSI
        rsi_val = float(incr.get("rsi") or 50.0)
        rsi_signal, rsi_reason = self._score_rsi(rsi_val)
        signals.append(("rsi", rsi_signal))
        if rsi_reason:
            reasons.append(rsi_reason)
        indicators["RSI"] = rsi_val

        # 2. MACD
        macd_hist = float(incr.get("macd_hist") or 0.0)
        macd_hist_prev = float(incr.get("macd_hist_prev") or 0.0)
        macd_signal_val, macd_reason = self._score_macd(macd_hist, macd_hist_prev)
        signals.append(("macd", macd_signal_val))
        if macd_reason:
            reasons.append(macd_reason)

        # 3. ATR Trailing Stop
        atr_val = float(incr.get("atr_value") or 0.0)
        trailing_stop = incr.get("atr_trailing_stop")
        close = float(df["close"].iloc[-1])
        atr_signal_val, atr_reason = self._score_atr_trailing(close, trailing_stop)
        signals.append(("atr_trailing", atr_signal_val))
        if atr_reason:
            reasons.append(atr_reason)

        # 4. Volume (pure-Python, O(21) rows — no TA-Lib)
        volume_signal, volume_reason = self._analyze_volume(df)
        signals.append(("volume", volume_signal))
        if volume_reason:
            reasons.append(volume_reason)

        # 5. MA Crossover
        ema20 = incr.get("ema20")
        ema50 = incr.get("ema50")
        sma200 = incr.get("sma200")
        prev_ema20 = incr.get("prev_ema20")
        prev_ema50 = incr.get("prev_ema50")
        ma_signal_val, ma_reason = self._score_ma_crossover(
            close, ema20, ema50, sma200, prev_ema20, prev_ema50
        )
        signals.append(("ma_crossover", ma_signal_val))
        if ma_reason:
            reasons.append(ma_reason)

        # 6. Price Action (pure-Python, O(5) rows — no TA-Lib)
        pa_signal, pa_reason = self._analyze_price_action(df)
        signals.append(("price_action", pa_signal))
        if pa_reason:
            reasons.append(pa_reason)

        # 7. Momentum
        cci_val = float(incr.get("cci") or 0.0)
        mfi_val = float(incr.get("mfi") or 50.0)
        mom_signal_val, mom_reason = self._score_momentum(cci_val, mfi_val)
        signals.append(("momentum", mom_signal_val))
        if mom_reason:
            reasons.append(mom_reason)
        indicators["CCI"] = cci_val
        indicators["MFI"] = mfi_val

        # Scoring (identical to full path)
        total_weight = sum(self.WEIGHTS.values())
        weighted_score = sum(self.WEIGHTS.get(ind, 10) * sig for ind, sig in signals)
        normalized_score = (weighted_score / total_weight) * 100
        overall_signal = self._score_to_signal(normalized_score)

        bullish_count = sum(1 for _, s in signals if s > 0.55)
        bearish_count = sum(1 for _, s in signals if s < 0.45)
        total_analyzers = len(signals)
        dominant_count = max(bullish_count, bearish_count)
        agreement_ratio = dominant_count / total_analyzers if total_analyzers > 0 else 0
        directional_strength = abs(normalized_score - 50) / 50
        confidence = min(100, round((agreement_ratio * 60 + directional_strength * 40) * 100 / 100, 1))

        signal_text = self._format_signal_text(overall_signal)
        if saveDict is not None:
            saveDict["Signal"] = overall_signal.name
            saveDict["Confidence"] = f"{confidence:.1f}%"
        if screenDict is not None:
            screenDict["Signal"] = signal_text
            screenDict["Confidence"] = f"{confidence:.1f}%"

        return SignalResult(
            signal=overall_signal,
            confidence=confidence,
            reasons=reasons,
            indicators=indicators,
        )

    # ------------------------------------------------------------------ #
    # Score helpers (numeric values → float signal 0..1)                 #
    # ------------------------------------------------------------------ #

    def _score_rsi(self, rsi_val: float) -> Tuple[float, str]:
        if rsi_val < 30:
            return 0.8, f"RSI oversold ({rsi_val:.1f})"
        elif rsi_val < 40:
            return 0.65, f"RSI approaching oversold ({rsi_val:.1f})"
        elif rsi_val > 70:
            return 0.2, f"RSI overbought ({rsi_val:.1f})"
        elif rsi_val > 60:
            return 0.35, f"RSI approaching overbought ({rsi_val:.1f})"
        return 0.5, None

    def _score_macd(self, hist: float, prev_hist: float) -> Tuple[float, str]:
        if hist > 0 and prev_hist <= 0:
            return 0.85, "MACD bullish crossover"
        elif hist < 0 and prev_hist >= 0:
            return 0.15, "MACD bearish crossover"
        elif hist > 0 and hist > prev_hist:
            return 0.7, "MACD histogram increasing"
        elif hist < 0 and hist < prev_hist:
            return 0.3, "MACD histogram decreasing"
        return 0.5, None

    def _score_atr_trailing(self, close: float, trailing_stop) -> Tuple[float, str]:
        if trailing_stop is None:
            return 0.5, None
        ts = float(trailing_stop)
        if close > ts * 1.02:
            return 0.75, "Price above ATR trailing stop"
        elif close < ts * 0.98:
            return 0.25, "Price below ATR trailing stop"
        return 0.5, None

    def _score_ma_crossover(
        self, close, ema20, ema50, sma200, prev_ema20, prev_ema50
    ) -> Tuple[float, str]:
        if ema20 is None or ema50 is None:
            return 0.5, None
        if prev_ema20 is not None and prev_ema50 is not None:
            if prev_ema20 <= prev_ema50 and ema20 > ema50:
                return 0.9, "Golden cross (EMA20 > EMA50)"
            elif prev_ema20 >= prev_ema50 and ema20 < ema50:
                return 0.1, "Death cross (EMA20 < EMA50)"
        if sma200 is not None:
            if close > ema20 > ema50 > sma200:
                return 0.75, "Price above all major MAs (bullish alignment)"
            elif close < ema20 < ema50 < sma200:
                return 0.25, "Price below all major MAs (bearish alignment)"
        return 0.5, None

    def _score_momentum(self, cci_val: float, mfi_val: float) -> Tuple[float, str]:
        sigs = []
        if cci_val < -100:
            sigs.append((0.75, "CCI oversold"))
        elif cci_val > 100:
            sigs.append((0.25, "CCI overbought"))
        if mfi_val < 20:
            sigs.append((0.8, "MFI oversold"))
        elif mfi_val > 80:
            sigs.append((0.2, "MFI overbought"))
        if sigs:
            avg = sum(s[0] for s in sigs) / len(sigs)
            return avg, "; ".join(s[1] for s in sigs)
        return 0.5, None

    def _analyze_rsi(self, df: pd.DataFrame, pktalib) -> Tuple[float, str, float]:
        """Analyze RSI for buy/sell signals."""
        try:
            rsi = pktalib.RSI(df['close'], timeperiod=14)
            if rsi is None or len(rsi) == 0:
                return 0.5, None, 50
            
            current_rsi = rsi.iloc[-1] if hasattr(rsi, 'iloc') else rsi[-1]
            
            # Calculate signal (0 = sell, 0.5 = neutral, 1 = buy)
            if current_rsi < 30:
                return 0.8, f"RSI oversold ({current_rsi:.1f})", current_rsi
            elif current_rsi < 40:
                return 0.65, f"RSI approaching oversold ({current_rsi:.1f})", current_rsi
            elif current_rsi > 70:
                return 0.2, f"RSI overbought ({current_rsi:.1f})", current_rsi
            elif current_rsi > 60:
                return 0.35, f"RSI approaching overbought ({current_rsi:.1f})", current_rsi
            else:
                return 0.5, None, current_rsi
        except Exception as e:
            self.logger.debug(f"RSI analysis error: {e}")
            return 0.5, None, 50
    
    def _analyze_macd(self, df: pd.DataFrame, pktalib) -> Tuple[float, str]:
        """Analyze MACD for buy/sell signals."""
        try:
            macd, signal, hist = pktalib.MACD(df['close'])
            if macd is None or len(macd) == 0:
                return 0.5, None
            
            current_macd = macd.iloc[-1] if hasattr(macd, 'iloc') else macd[-1]
            current_signal = signal.iloc[-1] if hasattr(signal, 'iloc') else signal[-1]
            current_hist = hist.iloc[-1] if hasattr(hist, 'iloc') else hist[-1]
            
            prev_hist = hist.iloc[-2] if hasattr(hist, 'iloc') else hist[-2]
            
            # MACD crossover detection
            if current_hist > 0 and prev_hist <= 0:
                return 0.85, "MACD bullish crossover"
            elif current_hist < 0 and prev_hist >= 0:
                return 0.15, "MACD bearish crossover"
            elif current_hist > 0 and current_hist > prev_hist:
                return 0.7, "MACD histogram increasing"
            elif current_hist < 0 and current_hist < prev_hist:
                return 0.3, "MACD histogram decreasing"
            else:
                return 0.5, None
        except Exception as e:
            self.logger.debug(f"MACD analysis error: {e}")
            return 0.5, None
    
    def _analyze_atr_trailing(self, df: pd.DataFrame, pktalib) -> Tuple[float, str]:
        """Analyze ATR Trailing Stop for buy/sell signals."""
        try:
            atr = pktalib.ATR(df['high'], df['low'], df['close'], timeperiod=14)
            if atr is None or len(atr) == 0:
                return 0.5, None
            
            close = df['close'].iloc[-1]
            current_atr = atr.iloc[-1] if hasattr(atr, 'iloc') else atr[-1]
            
            # Calculate ATR trailing stop
            key_value = 2
            trailing_stop = close - (key_value * current_atr)
            
            # Check if price is above trailing stop
            if close > trailing_stop * 1.02:  # 2% above trailing stop
                return 0.75, "Price above ATR trailing stop"
            elif close < trailing_stop * 0.98:
                return 0.25, "Price below ATR trailing stop"
            else:
                return 0.5, None
        except Exception as e:
            self.logger.debug(f"ATR analysis error: {e}")
            return 0.5, None
    
    def _analyze_volume(self, df: pd.DataFrame) -> Tuple[float, str]:
        """Analyze volume for buy/sell confirmation."""
        try:
            if 'volume' not in df.columns:
                return 0.5, None
            
            current_volume = df['volume'].iloc[-1]
            avg_volume = df['volume'].rolling(window=20).mean().iloc[-1]
            
            if avg_volume == 0:
                return 0.5, None
            
            volume_ratio = current_volume / avg_volume
            price_change = (df['close'].iloc[-1] - df['close'].iloc[-2]) / df['close'].iloc[-2]
            
            # High volume with positive price = bullish
            if volume_ratio > 2 and price_change > 0.01:
                return 0.85, f"Volume surge ({volume_ratio:.1f}x) with price increase"
            elif volume_ratio > 1.5 and price_change > 0:
                return 0.7, f"Above average volume ({volume_ratio:.1f}x) with gain"
            # High volume with negative price = bearish
            elif volume_ratio > 2 and price_change < -0.01:
                return 0.15, f"Volume surge ({volume_ratio:.1f}x) with price decrease"
            elif volume_ratio > 1.5 and price_change < 0:
                return 0.3, f"Above average volume ({volume_ratio:.1f}x) with loss"
            else:
                return 0.5, None
        except Exception as e:
            self.logger.debug(f"Volume analysis error: {e}")
            return 0.5, None
    
    def _analyze_ma_crossover(self, df: pd.DataFrame, pktalib) -> Tuple[float, str]:
        """Analyze moving average crossovers."""
        try:
            ema_20 = pktalib.EMA(df['close'], timeperiod=20)
            ema_50 = pktalib.EMA(df['close'], timeperiod=50)
            sma_200 = pktalib.SMA(df['close'], timeperiod=200)
            
            if ema_20 is None or ema_50 is None:
                return 0.5, None
            
            current_ema20 = ema_20.iloc[-1] if hasattr(ema_20, 'iloc') else ema_20[-1]
            current_ema50 = ema_50.iloc[-1] if hasattr(ema_50, 'iloc') else ema_50[-1]
            prev_ema20 = ema_20.iloc[-2] if hasattr(ema_20, 'iloc') else ema_20[-2]
            prev_ema50 = ema_50.iloc[-2] if hasattr(ema_50, 'iloc') else ema_50[-2]
            
            close = df['close'].iloc[-1]
            
            # Golden cross (EMA20 crosses above EMA50)
            if prev_ema20 <= prev_ema50 and current_ema20 > current_ema50:
                return 0.9, "Golden cross (EMA20 > EMA50)"
            # Death cross (EMA20 crosses below EMA50)
            elif prev_ema20 >= prev_ema50 and current_ema20 < current_ema50:
                return 0.1, "Death cross (EMA20 < EMA50)"
            # Price above all MAs
            elif sma_200 is not None:
                sma_200_val = sma_200.iloc[-1] if hasattr(sma_200, 'iloc') else sma_200[-1]
                if close > current_ema20 > current_ema50 > sma_200_val:
                    return 0.75, "Price above all major MAs (bullish alignment)"
                elif close < current_ema20 < current_ema50 < sma_200_val:
                    return 0.25, "Price below all major MAs (bearish alignment)"
            
            return 0.5, None
        except Exception as e:
            self.logger.debug(f"MA crossover analysis error: {e}")
            return 0.5, None
    
    def _analyze_price_action(self, df: pd.DataFrame) -> Tuple[float, str]:
        """Analyze price action patterns."""
        try:
            if len(df) < 5:
                return 0.5, None
            
            # Check for higher highs and higher lows
            highs = df['high'].tail(5).values
            lows = df['low'].tail(5).values
            closes = df['close'].tail(5).values
            
            higher_highs = all(highs[i] >= highs[i-1] for i in range(1, len(highs)))
            higher_lows = all(lows[i] >= lows[i-1] for i in range(1, len(lows)))
            lower_highs = all(highs[i] <= highs[i-1] for i in range(1, len(highs)))
            lower_lows = all(lows[i] <= lows[i-1] for i in range(1, len(lows)))
            
            if higher_highs and higher_lows:
                return 0.8, "Higher highs and higher lows (uptrend)"
            elif lower_highs and lower_lows:
                return 0.2, "Lower highs and lower lows (downtrend)"
            elif higher_lows:
                return 0.65, "Higher lows (potential reversal)"
            elif lower_highs:
                return 0.35, "Lower highs (weakening momentum)"
            else:
                return 0.5, None
        except Exception as e:
            self.logger.debug(f"Price action analysis error: {e}")
            return 0.5, None
    
    def _analyze_momentum(self, df: pd.DataFrame, pktalib) -> Tuple[float, str]:
        """Analyze momentum indicators."""
        try:
            # CCI (Commodity Channel Index)
            cci = pktalib.CCI(df['high'], df['low'], df['close'], timeperiod=20)
            
            # MFI (Money Flow Index)
            if 'volume' in df.columns:
                mfi = pktalib.MFI(df['high'], df['low'], df['close'], df['volume'], timeperiod=14)
            else:
                mfi = None
            
            signals = []
            cci_value = 0.0
            mfi_value = 50.0
            
            if cci is not None and len(cci) > 0:
                current_cci = cci.iloc[-1] if hasattr(cci, 'iloc') else cci[-1]
                cci_value = float(current_cci) if current_cci == current_cci else 0.0
                if current_cci < -100:
                    signals.append((0.75, "CCI oversold"))
                elif current_cci > 100:
                    signals.append((0.25, "CCI overbought"))
            
            if mfi is not None and len(mfi) > 0:
                current_mfi = mfi.iloc[-1] if hasattr(mfi, 'iloc') else mfi[-1]
                mfi_value = float(current_mfi) if current_mfi == current_mfi else 50.0
                if current_mfi < 20:
                    signals.append((0.8, "MFI oversold"))
                elif current_mfi > 80:
                    signals.append((0.2, "MFI overbought"))
            
            if signals:
                avg_signal = sum(s[0] for s in signals) / len(signals)
                reasons = [s[1] for s in signals]
                return avg_signal, "; ".join(reasons), cci_value, mfi_value
            
            return 0.5, None, cci_value, mfi_value
        except Exception as e:
            self.logger.debug(f"Momentum analysis error: {e}")
            return 0.5, None, 0.0, 50.0
    
    def _score_to_signal(self, score: float) -> SignalStrength:
        """Convert normalized score to signal strength."""
        if score >= 80:
            return SignalStrength.STRONG_BUY
        elif score >= 65:
            return SignalStrength.BUY
        elif score >= 55:
            return SignalStrength.WEAK_BUY
        elif score >= 45:
            return SignalStrength.NEUTRAL
        elif score >= 35:
            return SignalStrength.WEAK_SELL
        elif score >= 20:
            return SignalStrength.SELL
        else:
            return SignalStrength.STRONG_SELL
    
    def _format_signal_text(self, signal: SignalStrength) -> str:
        """Format signal for display with colors."""
        color_map = {
            SignalStrength.STRONG_BUY: colorText.GREEN,
            SignalStrength.BUY: colorText.GREEN,
            SignalStrength.WEAK_BUY: colorText.GREEN,
            SignalStrength.NEUTRAL: colorText.WARN,
            SignalStrength.WEAK_SELL: colorText.FAIL,
            SignalStrength.SELL: colorText.FAIL,
            SignalStrength.STRONG_SELL: colorText.FAIL,
        }
        color = color_map.get(signal, colorText.END)
        return f"{color}{signal.name.replace('_', ' ')}{colorText.END}"
    
    def find_strong_buys(self, df: pd.DataFrame, saveDict: Dict = None,
                         screenDict: Dict = None) -> bool:
        """
        Check if stock qualifies as a Strong Buy signal.
        
        Args:
            df: OHLCV DataFrame
            saveDict: Dictionary for saving results
            screenDict: Dictionary for screen display
            
        Returns:
            True if stock is a Strong Buy, False otherwise
        """
        result = self.analyze(df, saveDict, screenDict)
        return result.is_strong_buy and result.confidence >= 60
    
    def find_strong_sells(self, df: pd.DataFrame, saveDict: Dict = None,
                          screenDict: Dict = None) -> bool:
        """
        Check if stock qualifies as a Strong Sell signal.
        
        Args:
            df: OHLCV DataFrame
            saveDict: Dictionary for saving results
            screenDict: Dictionary for screen display
            
        Returns:
            True if stock is a Strong Sell, False otherwise
        """
        result = self.analyze(df, saveDict, screenDict)
        return result.is_strong_sell and result.confidence >= 60
    
    def find_buy_signals(self, df: pd.DataFrame, saveDict: Dict = None,
                         screenDict: Dict = None) -> bool:
        """
        Check if stock qualifies for any buy signal (including weak).
        
        Args:
            df: OHLCV DataFrame
            saveDict: Dictionary for saving results
            screenDict: Dictionary for screen display
            
        Returns:
            True if stock has a buy signal, False otherwise
        """
        result = self.analyze(df, saveDict, screenDict)
        return result.is_buy
    
    def find_sell_signals(self, df: pd.DataFrame, saveDict: Dict = None,
                          screenDict: Dict = None) -> bool:
        """
        Check if stock qualifies for any sell signal (including weak).
        
        Args:
            df: OHLCV DataFrame
            saveDict: Dictionary for saving results
            screenDict: Dictionary for screen display
            
        Returns:
            True if stock has a sell signal, False otherwise
        """
        result = self.analyze(df, saveDict, screenDict)
        return result.is_sell
