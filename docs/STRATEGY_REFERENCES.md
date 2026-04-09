# Strategy References & Research Sources

External repos, papers, and strategies evaluated for potential integration
into the V2 intelligence engine.

---

## Integrated Ideas

### NR7 (Narrow Range 7) — from NSE-Stock-Scanner
- **Source**: https://github.com/deshwalmahesh/NSE-Stock-Scanner
- **Concept**: If today's candle range (high - low) is the narrowest of the
  last 7 days, it signals a "coiled spring" about to break out.
- **Our implementation**: Added `nr7` flag to `SymbolVolumeProfile`. Awards
  +5 quality points to anomaly candidates exhibiting NR7 compression.
- **Where**: `AnomalyDetector.py` → `load_historical_profiles()`, quality score
- **Reference**: Toby Crabel's "Day Trading with Short Term Price Patterns"

### TICK/TRIN (Market Internals) — from NSE-Stock-Scanner
- **Source**: https://github.com/deshwalmahesh/NSE-Stock-Scanner
- **Concept**: TICK = net advancing stocks. TRIN (Arms Index) =
  (advancing/declining) ÷ (advancing_volume/declining_volume). TRIN < 1.0
  means money flowing into advancing stocks (bullish); > 1.0 = bearish.
- **Our implementation**: Added `tick_value` and `trin` to `MarketRegime`.
  TRIN contributes ±10 to regime_score (bullish TRIN < 0.7 → +10, bearish
  TRIN > 1.5 → -10). This flows into the quality score of every anomaly
  candidate via the market regime component.
- **Where**: `MarketContext.py` → `update()`, regime classification

---

## Reference Strategies (Not Integrated — Future Potential)

### Opening Range Breakout / Dual Thrust
- **Source**: https://github.com/je-suis-tm/quant-trading (strategy #7)
- **Concept**: Establish upper/lower thresholds from previous day's OHLC,
  trade breakouts in the first 30 minutes. Useful for intraday entry timing.
- **Potential use**: Could refine our anomaly entry timing — instead of
  entering at open+2%, use a dynamic range breakout threshold.

### Bollinger Bands Pattern Recognition
- **Source**: https://github.com/je-suis-tm/quant-trading (strategy #9)
- **Concept**: Bollinger squeeze (contraction) precedes breakout. The width
  of the bands signals volatility compression similar to our NR7/range
  compression, but adds statistical thresholds (standard deviation bands).
- **Potential use**: Could add Bollinger squeeze as a secondary pre-move
  filter alongside range compression ratio.

### Pair Trading / Cointegration
- **Source**: https://github.com/je-suis-tm/quant-trading (strategy #2)
- **Concept**: Use Engle-Granger cointegration to find pairs. Trade mean
  reversion when spread exceeds threshold.
- **Potential use**: Could identify sector pairs. If stock A breaks out but
  its cointegrated partner doesn't, signal quality is lower.

### Sector Rotation / Momentum
- **Source**: paperswithbacktest — "Sector Momentum – Rotational System"
  (Sharpe 0.401, 14.1% annualized)
- **Concept**: Monthly rotation into strongest sectors.
- **Potential use**: Our `MarketContext.sector_strength` already ranks sectors
  in real-time. Could add a "hot sector bonus" for stocks in the top 2-3
  sectors for the day.

### Short-Term Reversal in Stocks
- **Source**: paperswithbacktest — Sharpe 0.816, 21.4% annualized, weekly
- **Concept**: Stocks that dropped last week tend to bounce. Contrarian
  strategy.
- **Potential use**: Could add a reversal signal for stocks near 20d lows
  (we already give a low_bonus in quality scoring).

### Trend-Following in Stocks
- **Source**: paperswithbacktest — Sharpe 0.569, 15.2% annualized, daily
- **Concept**: Simple trend-following using 200-day MA.
- **Potential use**: Could filter anomaly candidates to only those above
  their 200 DMA for higher conviction.

### Heikin-Ashi Candlestick
- **Source**: https://github.com/je-suis-tm/quant-trading (strategy #3)
- **Concept**: Smoothed candlestick that filters noise in momentum trading.
- **Potential use**: Could use Heikin-Ashi on the 5-min chart for
  follow-through confirmation (instead of raw price comparison).

---

## Screener Repos (Upstream Knowledge)

### Screeni-py (PKScreener upstream)
- **Source**: https://github.com/pranjal-joshi/Screeni-py (677 stars)
- **Relevance**: We fork/extend this. It provides: breakout level detection,
  consolidation %, Stage 2 filtering, MA crossovers, RSI, trend strength.
- **Status**: Already fully inherited in our codebase.

### NSE-Stock-Scanner
- **Source**: https://github.com/deshwalmahesh/NSE-Stock-Scanner (301 stars)
- **Relevance**: India-focused NSE scanner with Zerodha Kite integration.
  Features: narrow range, market mood, intraday strategies, pivot points,
  option chain analysis.
- **Status**: NR7 and TICK/TRIN ideas integrated. Other features already
  covered by our system.

---

## Backtesting Frameworks (Reference Only)

| Framework | Stars | License | Notes |
|-----------|-------|---------|-------|
| [backtrader](https://github.com/mementum/backtrader) | 21.1K | GPL-3.0 | Event-driven, unmaintained 3yr. Wrong paradigm for tick-based system. |
| [backtesting.py](https://github.com/kernc/backtesting.py) | 8.2K | AGPL-3.0 | Clean vector-based backtester. Our `backtest_anomaly.py` is purpose-built. |
| [vectorbt](https://github.com/polakowo/vectorbt) | — | — | NumPy/Numba-accelerated. Good for high-throughput but adds complexity. |

---

## Curated Lists

- [awesome-systematic-trading](https://github.com/paperswithbacktest/awesome-systematic-trading)
  (7.8K stars): 97 libraries, 40+ strategies, 55 books. Good reference for
  finding new research.

---

## Books Worth Reading (from the repos above)

- **Quantitative Momentum** — Wesley Gray, Jack Vogel
- **Trading Evolved** — Andreas Clenow (Python backtesting)
- **Advances in Financial Machine Learning** — Marcos Lopez de Prado
- **Systematic Trading** — Robert Carver
- **Algorithmic Trading** — Ernest Chan
