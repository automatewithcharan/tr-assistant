"""
Advanced signal filters using Kalman Filter and GARCH from Quantopian lectures.

Kalman Filter: Noise-reduced price estimation for cleaner trailing stops and
  trend detection. Replaces noisy close prices with filtered state estimates.

GARCH(1,1): Conditional volatility prediction for adaptive stop-losses.
  Wide stops in volatile regimes, tight stops in calm regimes.

References:
  - docs/quantopian_lectures/research_public/notebooks/lectures/Kalman_Filters/
  - docs/quantopian_lectures/research_public/notebooks/lectures/ARCH_GARCH_and_GMM/
"""

import logging
import math
from typing import Dict, Optional, Tuple

import numpy as np

logger = logging.getLogger("pkscreener")

# ============================================================================
# Kalman Filter - Rolling Mean Estimation (from Quantopian lecture)
# ============================================================================

try:
    from pykalman import KalmanFilter as _PyKalmanFilter
    _KALMAN_AVAILABLE = True
except ImportError:
    _KALMAN_AVAILABLE = False


class KalmanPriceSmoother:
    """
    Kalman-filtered price smoother for noise reduction.

    From Quantopian lecture: uses a 1D state Kalman filter to estimate the
    true underlying price from noisy observations. The transition_covariance
    controls how quickly the filter adapts (lower = smoother, higher = reactive).

    Usage:
        smoother = KalmanPriceSmoother(observation_noise=1.0, transition_noise=0.01)
        filtered_prices = smoother.smooth(close_prices)
        # filtered_prices is a cleaned series with less noise
    """

    def __init__(
        self,
        observation_noise: float = 1.0,
        transition_noise: float = 0.01,
    ):
        self.observation_noise = observation_noise
        self.transition_noise = transition_noise

    def smooth(self, prices: np.ndarray) -> np.ndarray:
        """Apply Kalman filter to price array, return smoothed prices."""
        if not _KALMAN_AVAILABLE:
            return prices  # graceful fallback

        if len(prices) < 10:
            return prices

        try:
            kf = _PyKalmanFilter(
                transition_matrices=[1],
                observation_matrices=[1],
                initial_state_mean=float(prices[0]),
                initial_state_covariance=1,
                observation_covariance=self.observation_noise,
                transition_covariance=self.transition_noise,
            )
            state_means, _ = kf.filter(prices)
            return state_means.flatten()
        except Exception as exc:
            logger.debug(f"Kalman smooth failed: {exc}")
            return prices

    def smooth_and_velocity(self, prices: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        """
        Kalman filter with 2D state: [position, velocity].
        Returns (smoothed_prices, velocity_estimates).
        Velocity > 0 means uptrend, < 0 means downtrend.
        """
        if not _KALMAN_AVAILABLE or len(prices) < 10:
            velocity = np.diff(prices, prepend=prices[0])
            return prices, velocity

        try:
            tau = 1.0  # time step
            kf = _PyKalmanFilter(
                n_dim_obs=1,
                n_dim_state=2,
                initial_state_mean=[float(prices[0]), 0],
                initial_state_covariance=np.eye(2),
                transition_matrices=[[1, tau], [0, 1]],
                observation_matrices=[[1, 0]],
                observation_covariance=self.observation_noise,
                transition_covariance=self.transition_noise * np.eye(2),
            )
            state_means, _ = kf.filter(prices)
            return state_means[:, 0], state_means[:, 1]
        except Exception as exc:
            logger.debug(f"Kalman 2D failed: {exc}")
            velocity = np.diff(prices, prepend=prices[0])
            return prices, velocity


# ============================================================================
# GARCH(1,1) - Conditional Volatility (from Quantopian lecture)
# ============================================================================

try:
    from arch import arch_model
    _GARCH_AVAILABLE = True
except ImportError:
    _GARCH_AVAILABLE = False


class GARCHVolatility:
    """
    GARCH(1,1) volatility estimator for adaptive stop-loss sizing.

    From Quantopian lecture: σ²_t = ω + α₁·ε²_{t-1} + β₁·σ²_{t-1}
    This captures volatility clustering: big moves predict big moves.

    Usage:
        garch = GARCHVolatility()
        current_vol, forecast_vol = garch.estimate(returns_array)
        # Use forecast_vol to set adaptive ATR multiplier
    """

    def __init__(self, p: int = 1, q: int = 1, min_obs: int = 60):
        self.p = p
        self.q = q
        self.min_obs = min_obs

    def estimate(self, returns: np.ndarray) -> Tuple[float, float]:
        """
        Fit GARCH and return (current_volatility, 1-step-ahead forecast).
        Returns are assumed to be percentage returns.
        """
        if not _GARCH_AVAILABLE or len(returns) < self.min_obs:
            return self._fallback_volatility(returns)

        try:
            # Scale returns to percentage for numerical stability
            scaled = returns * 100.0 if np.abs(returns).max() < 1 else returns

            model = arch_model(scaled, vol='Garch', p=self.p, q=self.q, mean='Zero')
            result = model.fit(disp='off', show_warning=False)

            # Current conditional volatility (last fitted value)
            current_vol = float(result.conditional_volatility.iloc[-1]) / 100.0

            # 1-step forecast
            forecast = result.forecast(horizon=1)
            forecast_var = float(forecast.variance.iloc[-1].values[0])
            forecast_vol = math.sqrt(forecast_var) / 100.0

            return current_vol, forecast_vol
        except Exception as exc:
            logger.debug(f"GARCH fit failed: {exc}")
            return self._fallback_volatility(returns)

    def _fallback_volatility(self, returns: np.ndarray) -> Tuple[float, float]:
        """Simple rolling std as fallback."""
        if len(returns) < 5:
            return 0.02, 0.02
        recent = returns[-20:] if len(returns) >= 20 else returns
        vol = float(np.std(recent))
        return vol, vol

    def adaptive_atr_multiplier(self, returns: np.ndarray, base_multiplier: float = 2.0) -> float:
        """
        Scale the ATR multiplier based on GARCH volatility regime.

        Low vol → tighter stops (0.7x-1.0x base)
        Normal vol → base stops (1.0x)
        High vol → wider stops (1.3x-2.0x base)
        """
        current_vol, forecast_vol = self.estimate(returns)
        if current_vol <= 0:
            return base_multiplier

        # Compare forecast to current
        vol_ratio = forecast_vol / current_vol if current_vol > 0 else 1.0

        # Long-term average volatility
        long_vol = float(np.std(returns[-60:])) if len(returns) >= 60 else current_vol
        regime_ratio = current_vol / long_vol if long_vol > 0 else 1.0

        # Scale multiplier
        if regime_ratio < 0.7:
            # Low vol regime: tighten stops
            scale = 0.75
        elif regime_ratio < 1.0:
            scale = 0.9
        elif regime_ratio < 1.5:
            scale = 1.0
        elif regime_ratio < 2.0:
            scale = 1.3
        else:
            # Extreme vol: widen stops significantly
            scale = 1.8

        return round(base_multiplier * scale, 2)


# ============================================================================
# Signal Quality Filter — combines Kalman + GARCH for go/no-go decision
# ============================================================================

class SignalQualityFilter:
    """
    Filters signals using Kalman trend + GARCH volatility.

    Rejects buy signals when:
    - Kalman velocity is negative (downtrend despite noisy price spike)
    - Kalman smoothed price is below current price by >2% (noise spike)
    - GARCH forecasts expanding volatility (regime deterioration)
    - Price is extended >2 ATR above Kalman-smoothed level
    """

    def __init__(self):
        self.kalman = KalmanPriceSmoother(observation_noise=1.0, transition_noise=0.01)
        self.garch = GARCHVolatility()

    def evaluate(self, close_prices: np.ndarray, volumes: Optional[np.ndarray] = None) -> Dict:
        """
        Evaluate signal quality for the latest price point.

        Returns dict with:
            kalman_price: smoothed price estimate
            kalman_velocity: trend direction and speed
            kalman_trend: 'up', 'down', or 'flat'
            garch_current_vol: current conditional volatility
            garch_forecast_vol: 1-step forecast volatility
            vol_regime: 'calm', 'normal', 'elevated', 'extreme'
            noise_spike: True if price is extended above Kalman-smoothed
            quality_score: 0-100 composite quality score
            pass_filter: True if signal is worth acting on
        """
        if len(close_prices) < 30:
            return {"pass_filter": True, "quality_score": 50, "reason": "insufficient data"}

        # Kalman smoothing
        smoothed, velocity = self.kalman.smooth_and_velocity(close_prices)
        kalman_price = float(smoothed[-1])
        kalman_vel = float(velocity[-1])
        current_price = float(close_prices[-1])

        # Trend classification
        if kalman_vel > 0.5:
            kalman_trend = "up"
        elif kalman_vel < -0.5:
            kalman_trend = "down"
        else:
            kalman_trend = "flat"

        # Noise spike detection: price extended above smoothed level
        price_vs_kalman_pct = (current_price - kalman_price) / kalman_price * 100 if kalman_price > 0 else 0
        noise_spike = price_vs_kalman_pct > 2.0

        # GARCH volatility
        returns = np.diff(close_prices) / close_prices[:-1]
        current_vol, forecast_vol = self.garch.estimate(returns)

        # Vol regime
        long_vol = float(np.std(returns[-60:])) if len(returns) >= 60 else current_vol
        if long_vol > 0:
            regime_ratio = current_vol / long_vol
        else:
            regime_ratio = 1.0

        if regime_ratio < 0.7:
            vol_regime = "calm"
        elif regime_ratio < 1.3:
            vol_regime = "normal"
        elif regime_ratio < 2.0:
            vol_regime = "elevated"
        else:
            vol_regime = "extreme"

        # Quality score computation
        score = 50.0  # neutral baseline

        # Trend component (+/- 25)
        if kalman_trend == "up":
            score += 25.0
        elif kalman_trend == "down":
            score -= 25.0

        # Noise penalty (-15 if spike)
        if noise_spike:
            score -= 15.0

        # Vol regime component
        if vol_regime == "calm":
            score += 10.0
        elif vol_regime == "elevated":
            score -= 10.0
        elif vol_regime == "extreme":
            score -= 20.0

        # Expanding vol penalty
        if forecast_vol > current_vol * 1.2:
            score -= 10.0

        score = max(0.0, min(100.0, score))

        # Pass/fail
        pass_filter = score >= 35.0 and kalman_trend != "down"

        return {
            "kalman_price": round(kalman_price, 2),
            "kalman_velocity": round(kalman_vel, 4),
            "kalman_trend": kalman_trend,
            "garch_current_vol": round(current_vol, 6),
            "garch_forecast_vol": round(forecast_vol, 6),
            "vol_regime": vol_regime,
            "noise_spike": noise_spike,
            "price_vs_kalman_pct": round(price_vs_kalman_pct, 2),
            "quality_score": round(score, 1),
            "pass_filter": pass_filter,
        }
