"""
Incremental (O(1) per tick) technical indicator state.

Instead of recomputing RSI, MACD, ATR, EMA, SMA, CCI, MFI from 200 candles
every cycle, each symbol maintains running numeric state. On each new tick,
state updates in O(1) via Wilder smoothing / EMA recurrence / circular buffers.

Usage:
    state = SymbolIncrementalState()
    state.warm_up(historical_df)           # once per session, O(n)
    state.update(close, high, low, volume) # each new tick, O(1)
    indicators = state.get_indicators()    # dict of current values
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Dict, Optional


class _EMAState:
    """Standard EMA(n): alpha = 2/(n+1), seeds from first n-bar SMA."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 2.0 / (period + 1)
        self.value: Optional[float] = None
        self._warmup_buf: list = []

    def update(self, price: float) -> float:
        if self.value is None:
            self._warmup_buf.append(float(price))
            if len(self._warmup_buf) >= self.period:
                self.value = sum(self._warmup_buf) / self.period
                self._warmup_buf = []
        else:
            self.value = self.alpha * float(price) + (1 - self.alpha) * self.value
        return self.value if self.value is not None else float(price)

    @property
    def ready(self) -> bool:
        return self.value is not None


class _WilderState:
    """Wilder's smoothed moving average: alpha = 1/n.  Used in RSI and ATR."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.alpha = 1.0 / period
        self.value: Optional[float] = None
        self._warmup_buf: list = []

    def update(self, x: float) -> float:
        if self.value is None:
            self._warmup_buf.append(float(x))
            if len(self._warmup_buf) >= self.period:
                self.value = sum(self._warmup_buf) / self.period
                self._warmup_buf = []
        else:
            self.value = self.alpha * float(x) + (1 - self.alpha) * self.value
        return self.value if self.value is not None else float(x)

    @property
    def ready(self) -> bool:
        return self.value is not None


class _RSIState:
    """RSI(14) via Wilder smoothing. O(1) per tick."""

    def __init__(self, period: int = 14) -> None:
        self._avg_gain = _WilderState(period)
        self._avg_loss = _WilderState(period)
        self._prev_close: Optional[float] = None
        self.value: float = 50.0

    def update(self, close: float) -> float:
        close = float(close)
        if self._prev_close is not None:
            delta = close - self._prev_close
            gain = max(delta, 0.0)
            loss = max(-delta, 0.0)
            ag = self._avg_gain.update(gain)
            al = self._avg_loss.update(loss)
            if self._avg_gain.ready:
                if al == 0.0:
                    self.value = 100.0
                else:
                    self.value = 100.0 - (100.0 / (1.0 + ag / al))
        self._prev_close = close
        return self.value

    @property
    def ready(self) -> bool:
        return self._avg_gain.ready


class _MACDState:
    """MACD(12, 26, 9). Tracks histogram and prev_histogram for crossover. O(1)."""

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        self._ema_fast = _EMAState(fast)
        self._ema_slow = _EMAState(slow)
        self._ema_signal = _EMAState(signal)
        self.macd: float = 0.0
        self.signal_line: float = 0.0
        self.histogram: float = 0.0
        self.prev_histogram: float = 0.0

    def update(self, close: float) -> None:
        f = self._ema_fast.update(close)
        s = self._ema_slow.update(close)
        if self._ema_fast.ready and self._ema_slow.ready:
            self.macd = f - s
            self.prev_histogram = self.histogram
            sig = self._ema_signal.update(self.macd)
            if self._ema_signal.ready:
                self.signal_line = sig
                self.histogram = self.macd - self.signal_line

    @property
    def ready(self) -> bool:
        return self._ema_fast.ready and self._ema_slow.ready and self._ema_signal.ready


class _ATRState:
    """ATR(14) via Wilder smoothing. Tracks trailing stop. O(1)."""

    def __init__(self, period: int = 14, key_value: float = 2.0) -> None:
        self._wilder = _WilderState(period)
        self._key_value = key_value
        self._prev_close: Optional[float] = None
        self.value: float = 0.0
        self.trailing_stop: Optional[float] = None

    def update(self, high: float, low: float, close: float) -> float:
        high, low, close = float(high), float(low), float(close)
        if self._prev_close is not None:
            tr = max(high - low, abs(high - self._prev_close), abs(low - self._prev_close))
        else:
            tr = high - low
        self.value = self._wilder.update(tr)
        if self._wilder.ready:
            new_stop = close - self._key_value * self.value
            if self.trailing_stop is None:
                self.trailing_stop = new_stop
            else:
                self.trailing_stop = max(self.trailing_stop, new_stop)
        self._prev_close = close
        return self.value

    @property
    def ready(self) -> bool:
        return self._wilder.ready


class _SMAState:
    """SMA(n) via O(1) circular buffer / running sum."""

    def __init__(self, period: int) -> None:
        self.period = period
        self._buf: deque = deque(maxlen=period)
        self._total: float = 0.0
        self.value: float = 0.0

    def update(self, price: float) -> float:
        price = float(price)
        if len(self._buf) == self.period:
            self._total -= self._buf[0]
        self._buf.append(price)
        self._total += price
        self.value = self._total / len(self._buf)
        return self.value

    @property
    def ready(self) -> bool:
        return len(self._buf) >= self.period


class _CCIState:
    """CCI(20) via rolling window of typical prices. O(1)."""

    def __init__(self, period: int = 20) -> None:
        self.period = period
        self._buf: deque = deque(maxlen=period)
        self.value: float = 0.0

    def update(self, high: float, low: float, close: float) -> float:
        tp = (float(high) + float(low) + float(close)) / 3.0
        self._buf.append(tp)
        if len(self._buf) < self.period:
            return 0.0
        mean_tp = sum(self._buf) / self.period
        mad = sum(abs(x - mean_tp) for x in self._buf) / self.period
        self.value = (tp - mean_tp) / (0.015 * mad) if mad > 0 else 0.0
        return self.value

    @property
    def ready(self) -> bool:
        return len(self._buf) >= self.period


class _MFIState:
    """MFI(14) via rolling positive/negative money flow. O(1)."""

    def __init__(self, period: int = 14) -> None:
        self.period = period
        self._pos: deque = deque(maxlen=period)
        self._neg: deque = deque(maxlen=period)
        self._prev_tp: Optional[float] = None
        self.value: float = 50.0

    def update(self, high: float, low: float, close: float, volume: float) -> float:
        tp = (float(high) + float(low) + float(close)) / 3.0
        mf = tp * max(float(volume or 0.0), 0.0)
        if self._prev_tp is not None and tp > self._prev_tp:
            self._pos.append(mf)
            self._neg.append(0.0)
        elif self._prev_tp is not None and tp < self._prev_tp:
            self._pos.append(0.0)
            self._neg.append(mf)
        else:
            self._pos.append(0.0)
            self._neg.append(0.0)
        self._prev_tp = tp
        if len(self._pos) >= self.period:
            pos_flow = sum(self._pos)
            neg_flow = sum(self._neg)
            if neg_flow == 0:
                self.value = 100.0
            else:
                self.value = 100.0 - (100.0 / (1.0 + pos_flow / neg_flow))
        return self.value

    @property
    def ready(self) -> bool:
        return len(self._pos) >= self.period


class SymbolIncrementalState:
    """
    Per-symbol aggregated incremental state for all indicators used in TradingSignals.

    Lifecycle:
        state = SymbolIncrementalState()
        state.warm_up(historical_df)           # O(n) — once per session
        # Each new live tick:
        state.update(close, high, low, volume) # O(1)
        indicators = state.get_indicators()    # O(1) dict lookup
    """

    def __init__(self) -> None:
        self.rsi = _RSIState(14)
        self.macd = _MACDState(12, 26, 9)
        self.atr = _ATRState(14, key_value=2.0)
        self.ema20 = _EMAState(20)
        self.ema50 = _EMAState(50)
        self.sma200 = _SMAState(200)
        self.cci = _CCIState(20)
        self.mfi = _MFIState(14)
        self.candles_processed: int = 0
        self._prev_ema20: Optional[float] = None
        self._prev_ema50: Optional[float] = None
        self._warmed_up: bool = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def warm_up(self, df) -> None:
        """Process a full historical OHLCV DataFrame to initialise state. O(n)."""
        with self._lock:
            for _, row in df.iterrows():
                self._process_candle(
                    float(row["close"]),
                    float(row["high"]),
                    float(row["low"]),
                    float(row.get("volume") or 0.0),
                )
            self._warmed_up = True

    def update(self, close: float, high: float, low: float, volume: float) -> None:
        """Advance all indicators by exactly one new candle. O(1)."""
        with self._lock:
            self._process_candle(float(close), float(high), float(low), float(volume or 0.0))

    def get_indicators(self) -> Dict[str, float]:
        """Return a snapshot of all current indicator values. Thread-safe."""
        with self._lock:
            return {
                "rsi": self.rsi.value,
                "macd_hist": self.macd.histogram,
                "macd_hist_prev": self.macd.prev_histogram,
                "atr_value": self.atr.value,
                "atr_trailing_stop": self.atr.trailing_stop,
                "ema20": self.ema20.value if self.ema20.ready else None,
                "ema50": self.ema50.value if self.ema50.ready else None,
                "sma200": self.sma200.value if self.sma200.ready else None,
                "prev_ema20": self._prev_ema20,
                "prev_ema50": self._prev_ema50,
                "cci": self.cci.value,
                "mfi": self.mfi.value,
            }

    @property
    def is_ready(self) -> bool:
        """True once all slow-starting indicators (EMA50, RSI, MACD) are warm."""
        return (
            self._warmed_up
            and self.rsi.ready
            and self.macd.ready
            and self.atr.ready
            and self.ema20.ready
            and self.ema50.ready
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_candle(self, close: float, high: float, low: float, volume: float) -> None:
        self._prev_ema20 = self.ema20.value
        self._prev_ema50 = self.ema50.value
        self.rsi.update(close)
        self.macd.update(close)
        self.atr.update(high, low, close)
        self.ema20.update(close)
        self.ema50.update(close)
        self.sma200.update(close)
        self.cci.update(high, low, close)
        self.mfi.update(high, low, close, volume)
        self.candles_processed += 1


class IncrementalStateRegistry:
    """
    Thread-safe registry of per-symbol incremental states.
    Holds one SymbolIncrementalState per symbol across all monitor cycles.
    """

    def __init__(self) -> None:
        self._states: Dict[str, SymbolIncrementalState] = {}
        self._lock = threading.Lock()

    def get_or_create(self, symbol: str) -> SymbolIncrementalState:
        key = str(symbol).strip().upper()
        with self._lock:
            if key not in self._states:
                self._states[key] = SymbolIncrementalState()
            return self._states[key]

    def reset(self, symbol: str) -> None:
        key = str(symbol).strip().upper()
        with self._lock:
            self._states.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._states.clear()
