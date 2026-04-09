"""
Universe pre-filtering: fast liquidity and quality gates applied BEFORE
expensive per-symbol analysis to cut the scan universe early.

This module filters out stocks that are obviously unfit for trading
(too illiquid, too cheap, no volume) before burning CPU on indicators.
"""

import logging
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger("pkscreener")


class UniversePreFilter:
    """
    Apply fast pre-screening gates to eliminate obviously untradeable stocks.

    These checks use only OHLCV summary data (no indicators needed),
    so they are O(1) per symbol and can be run on the full universe
    before the expensive signal computation.
    """

    def __init__(
        self,
        min_price: float = 10.0,
        max_price: float = 50000.0,
        min_avg_volume: int = 50000,
        min_avg_trade_value: float = 5_000_000.0,
        min_candles: int = 60,
        max_spread_pct: float = 5.0,
    ):
        self.min_price = min_price
        self.max_price = max_price
        self.min_avg_volume = min_avg_volume
        self.min_avg_trade_value = min_avg_trade_value
        self.min_candles = min_candles
        self.max_spread_pct = max_spread_pct

    def is_eligible(self, df: pd.DataFrame) -> Tuple[bool, str]:
        """
        Check if a stock's OHLCV data passes pre-filter gates.

        Returns (eligible, reason) where reason explains rejection.
        """
        if df is None or df.empty:
            return False, "no data"

        n = len(df)
        if n < self.min_candles:
            return False, f"insufficient history ({n} < {self.min_candles})"

        # Use last row for price check
        last = df.iloc[-1]
        close = float(last.get("close", 0) or 0)
        if close < self.min_price:
            return False, f"price too low ({close:.2f} < {self.min_price})"
        if close > self.max_price:
            return False, f"price too high ({close:.2f} > {self.max_price})"

        # Average volume over recent 20 bars
        vol_col = "volume" if "volume" in df.columns else "Volume"
        if vol_col in df.columns:
            recent = df.tail(20)
            avg_vol = recent[vol_col].mean()
            if avg_vol < self.min_avg_volume:
                return False, f"avg volume too low ({avg_vol:.0f} < {self.min_avg_volume})"

            # Average trade value = avg_vol * avg_close
            avg_close = recent["close"].mean() if "close" in recent.columns else close
            avg_trade_value = avg_vol * avg_close
            if avg_trade_value < self.min_avg_trade_value:
                return False, f"avg trade value too low (₹{avg_trade_value:,.0f})"

        # Spread check: reject stocks with extreme high-low range (penny/circuit stocks)
        high = float(last.get("high", 0) or 0)
        low = float(last.get("low", 0) or 0)
        if low > 0:
            spread_pct = (high - low) / low * 100.0
            if spread_pct > self.max_spread_pct * 3:  # 3x the max = likely circuit hit
                return False, f"extreme spread ({spread_pct:.1f}%)"

        return True, "passed"

    def filter_symbols(
        self,
        symbols: List[str],
        data_loader,
        show_progress: bool = False,
    ) -> Tuple[List[str], List[Tuple[str, str]]]:
        """
        Filter a list of symbols, returning (eligible, rejected_with_reasons).

        data_loader: callable(symbol) -> pd.DataFrame (OHLCV data)
        """
        eligible = []
        rejected = []

        for i, symbol in enumerate(symbols):
            try:
                df = data_loader(symbol)
                ok, reason = self.is_eligible(df)
                if ok:
                    eligible.append(symbol)
                else:
                    rejected.append((symbol, reason))
            except Exception as exc:
                rejected.append((symbol, f"error: {exc}"))

            if show_progress and ((i + 1) % 100 == 0 or i + 1 == len(symbols)):
                logger.debug(f"Pre-filter: {i+1}/{len(symbols)} checked, {len(eligible)} eligible")

        return eligible, rejected
