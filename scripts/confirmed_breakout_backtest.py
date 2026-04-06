"""
Enhanced Intraday Momentum Backtest v2 — "Confirmed Breakout" Strategy

KEY INSIGHT: A breakout that HOLDS into close (HoldRatio >= 0.8) continues
rising the next 1-3 days 92-96% of the time. This is the confirmation signal.

STRATEGY:
  Day T (Observation): Detect stocks with:
    - Open-to-High >= X% (big intraday surge)
    - Volume >= Y times average (genuine interest)
    - HoldRatio >= Z (close near the high = breakout held, not a false spike)
    - Price above 20-day MA (healthy trend)
    - Market regime not BEAR

  Day T+1 (Entry): Buy at open — the continuation is confirmed
  Hold 1-5 days with adaptive trailing stop

NO look-ahead bias: Day T is fully observed before Day T+1 entry.

FIVE STRATEGIES tested:
  S1: "Loose" — OTH>=3%, HR>=0.5, vol>=1.5x → more trades, lower WR
  S2: "Standard" — OTH>=5%, HR>=0.7, vol>=2x → balanced
  S3: "Strict" — OTH>=7%, HR>=0.8, vol>=2x → high WR
  S4: "Ultra" — OTH>=10%, HR>=0.85, vol>=3x → very high WR, fewer trades
  S5: "Elite" — OTH>=12%, HR>=0.9, vol>=3x → maximum WR
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PKDevTools.classes import Archiver

UNIVERSE_FILE = REPO_ROOT / "results" / "Indices" / "EQUITY_L.csv"

# Pre-defined strategy profiles
STRATEGY_PROFILES = {
    "loose":    {"breakout_pct": 3.0,  "hold_ratio": 0.50, "vol_ratio": 1.5, "top_n": 5},
    "standard": {"breakout_pct": 5.0,  "hold_ratio": 0.70, "vol_ratio": 2.0, "top_n": 5},
    "strict":   {"breakout_pct": 7.0,  "hold_ratio": 0.80, "vol_ratio": 2.0, "top_n": 3},
    "ultra":    {"breakout_pct": 10.0, "hold_ratio": 0.85, "vol_ratio": 3.0, "top_n": 3},
    "elite":    {"breakout_pct": 12.0, "hold_ratio": 0.90, "vol_ratio": 3.0, "top_n": 2},
}


def _load_universe_symbols(limit: int = 0) -> List[str]:
    df = pd.read_csv(UNIVERSE_FILE)
    symbols = df["SYMBOL"].astype(str).str.strip().tolist()
    symbols = [s for s in symbols if s]
    return symbols[:limit] if limit > 0 else symbols


def _download_daily_prices(symbols: List[str], start: str, end: str) -> pd.DataFrame:
    tickers = " ".join(f"{s}.NS" for s in symbols)
    return yf.download(
        tickers=tickers, start=start, end=end,
        auto_adjust=False, progress=False, group_by="ticker", threads=True,
    )


def _extract_symbol_history(raw_df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    ticker = f"{symbol}.NS"
    if raw_df.empty or not isinstance(raw_df.columns, pd.MultiIndex):
        return pd.DataFrame()
    if ticker not in raw_df.columns.get_level_values(0):
        return pd.DataFrame()
    df = raw_df[ticker].copy().dropna(how="all")
    if df.empty:
        return pd.DataFrame()
    df.columns = [str(c).strip().lower() for c in df.columns]
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index)
    return df.dropna()


def _detect_market_regime(nifty_hist: pd.DataFrame, as_of: pd.Timestamp) -> str:
    df = nifty_hist[nifty_hist.index <= as_of].copy()
    if len(df) < 50:
        return "NEUTRAL"
    close = df["close"]
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()
    e10, e20, e50 = float(ema10.iloc[-1]), float(ema20.iloc[-1]), float(ema50.iloc[-1])
    slope = ((e20 - float(ema20.iloc[-6])) / float(ema20.iloc[-6]) * 100) if len(ema20) >= 6 else 0
    if e10 < e20 < e50 and slope < -0.3:
        return "BEAR"
    ret5 = ((float(close.iloc[-1]) - float(close.iloc[-6])) / float(close.iloc[-6]) * 100) if len(close) >= 6 else 0
    if ret5 < -3.0 and slope < -0.5:
        return "BEAR"
    return "BULL" if e10 > e20 > e50 and slope > 0.1 else "NEUTRAL"


def _compute_atr(hist: pd.DataFrame, period: int = 14) -> float:
    """Average True Range."""
    if len(hist) < period + 1:
        return 0.0
    h = hist["high"].values
    l = hist["low"].values
    c = hist["close"].values
    tr = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    if len(tr) < period:
        return float(np.mean(tr)) if len(tr) > 0 else 0.0
    return float(np.mean(tr[-period:]))


def _download_all_data(start: str, end: str, universe_limit: int = 0) -> Tuple[pd.DataFrame, Dict[str, pd.DataFrame], List[pd.Timestamp]]:
    """Download all data once, return (nifty_hist, symbol_histories, all_dates)."""
    print("  Downloading Nifty 50...")
    nifty_raw = yf.download("^NSEI", start=start, end=end, auto_adjust=False, progress=False)
    nifty_hist = pd.DataFrame()
    if nifty_raw is not None and not nifty_raw.empty:
        nifty_hist = nifty_raw.copy()
        nifty_hist.columns = [str(c).strip().lower() if isinstance(c, str) else str(c[0]).strip().lower() for c in nifty_hist.columns]
        nifty_hist.index = pd.to_datetime(nifty_hist.index)
    print(f"  {len(nifty_hist)} Nifty days")

    symbols = _load_universe_symbols(limit=universe_limit)
    print(f"  Downloading {len(symbols)} stocks ({start} → {end})...")
    raw_df = _download_daily_prices(symbols, start=start, end=end)

    print("  Building per-symbol history...")
    symbol_histories: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        hist = _extract_symbol_history(raw_df, symbol)
        if len(hist) >= 30:
            symbol_histories[symbol] = hist
    print(f"    {len(symbol_histories)} symbols loaded")

    all_dates = sorted(set().union(*(set(h.index) for h in symbol_histories.values())))
    return nifty_hist, symbol_histories, all_dates


def run_confirmed_breakout_backtest(
    start: str,
    end: str,
    universe_limit: int = 0,
    strategy: str = "strict",
    # Override profile defaults
    breakout_pct: Optional[float] = None,
    hold_ratio: Optional[float] = None,
    vol_ratio: Optional[float] = None,
    top_n: Optional[int] = None,
    # Hold/exit config
    max_hold_days: int = 5,
    trail_pct: float = 1.5,
    # Filters
    require_above_ma: bool = True,
    require_gap_up: bool = False,
    use_regime_filter: bool = True,
    require_bull_only: bool = False,
    min_trade_value: float = 500000.0,
    # Adaptive stop
    use_atr_stop: bool = True,
    atr_stop_mult: float = 1.5,
    # Pre-downloaded data (to avoid re-downloading)
    _preloaded: Optional[Tuple[pd.DataFrame, Dict[str, pd.DataFrame], List[pd.Timestamp]]] = None,
) -> Dict:

    prof = STRATEGY_PROFILES.get(strategy, STRATEGY_PROFILES["strict"])
    bp = breakout_pct if breakout_pct is not None else prof["breakout_pct"]
    hr = hold_ratio if hold_ratio is not None else prof["hold_ratio"]
    vr = vol_ratio if vol_ratio is not None else prof["vol_ratio"]
    tn = top_n if top_n is not None else prof["top_n"]

    print(f"\n{'='*70}")
    print(f"CONFIRMED BREAKOUT BACKTEST — Strategy: {strategy.upper()}")
    print(f"{'='*70}")
    print(f"  Breakout >= {bp}%, HoldRatio >= {hr}, Volume >= {vr}x, Top {tn}/day")
    print(f"  Hold: {max_hold_days}d, Trail: {trail_pct}%, ATR-stop: {use_atr_stop}")
    print(f"  Filters: above_MA={require_above_ma}, gap_up={require_gap_up}, "
          f"regime={use_regime_filter}, bull_only={require_bull_only}")

    if _preloaded is not None:
        nifty_hist, symbol_histories, all_dates = _preloaded
        print(f"  Using pre-loaded data: {len(symbol_histories)} symbols, {len(all_dates)} dates")
    else:
        nifty_hist, symbol_histories, all_dates = _download_all_data(start, end, universe_limit)
    # We need Day T (observation) and at least max_hold_days after
    eligible_dates = all_dates[25:-(max_hold_days + 2)]
    print(f"    {len(eligible_dates)} eligible observation days")

    trade_rows: List[Dict] = []
    daily_summaries: List[Dict] = []
    regime_stats: Dict[str, int] = {}

    for obs_idx, obs_date in enumerate(eligible_dates):
        obs_str = obs_date.strftime("%Y-%m-%d")

        # Find next trading day (entry day)
        future = [d for d in all_dates if d > obs_date]
        if len(future) < max_hold_days + 1:
            continue
        entry_date = future[0]
        entry_str = entry_date.strftime("%Y-%m-%d")

        # Regime check on observation day
        regime = _detect_market_regime(nifty_hist, obs_date) if use_regime_filter and len(nifty_hist) > 0 else "NEUTRAL"
        regime_stats[regime] = regime_stats.get(regime, 0) + 1

        if regime == "BEAR" and use_regime_filter:
            daily_summaries.append({
                "ObsDate": obs_str, "EntryDate": entry_str,
                "Breakouts": 0, "Qualified": 0, "Traded": 0,
                "Wins": 0, "Losses": 0, "WinRate": 0, "AvgReturnPct": 0,
                "Regime": "BEAR",
            })
            continue

        if require_bull_only and regime != "BULL":
            daily_summaries.append({
                "ObsDate": obs_str, "EntryDate": entry_str,
                "Breakouts": 0, "Qualified": 0, "Traded": 0,
                "Wins": 0, "Losses": 0, "WinRate": 0, "AvgReturnPct": 0,
                "Regime": regime,
            })
            continue

        # OBSERVATION DAY: Find confirmed breakouts
        candidates: List[Dict] = []

        for symbol, hist in symbol_histories.items():
            if obs_date not in hist.index or entry_date not in hist.index:
                continue

            # Need prior data for averages
            prior = hist[hist.index < obs_date].tail(20)
            if len(prior) < 15:
                continue

            row = hist.loc[obs_date]
            day_open = float(row["open"])
            day_high = float(row["high"])
            day_low = float(row["low"])
            day_close = float(row["close"])
            day_volume = float(row["volume"])

            if day_open <= 0 or day_high <= day_open:
                continue

            # --- BREAKOUT DETECTION (fully observable on Day T) ---

            # 1. Big intraday move
            open_to_high_pct = (day_high - day_open) / day_open * 100
            if open_to_high_pct < bp:
                continue

            # 2. HOLD RATIO: close near the high (the critical filter!)
            # HoldRatio = (close - open) / (high - open)
            hold_ratio_val = (day_close - day_open) / (day_high - day_open)
            if hold_ratio_val < hr:
                continue

            open_to_close_pct = (day_close - day_open) / day_open * 100

            # 3. Volume confirmation
            avg_volume = prior["volume"].mean()
            volume_ratio = day_volume / avg_volume if avg_volume > 0 else 0
            if volume_ratio < vr:
                continue

            # 4. Liquidity
            avg_trade_value = (prior["close"] * prior["volume"]).mean()
            if avg_trade_value < min_trade_value:
                continue

            # 5. Above 20-day MA (healthy trend)
            ma20 = prior["close"].mean()
            above_ma20 = day_close > ma20
            if require_above_ma and not above_ma20:
                continue

            # 6. Gap-up from previous close (optional extra confirmation)
            prev_close = float(prior["close"].iloc[-1])
            gap_pct = (day_open - prev_close) / prev_close * 100 if prev_close > 0 else 0
            if require_gap_up and gap_pct < 0:
                continue

            # 7. ATR for adaptive stop
            longer_prior = hist[hist.index < obs_date].tail(20)
            atr = _compute_atr(longer_prior)

            # 8. Prior consolidation (low volatility before breakout = better)
            # ATR-normalized breakout = breakout relative to normal daily range
            atr_norm_breakout = open_to_high_pct / (atr / day_open * 100) if atr > 0 else open_to_high_pct

            # 9. Multi-day trend: was this already moving up?
            if len(prior) >= 5:
                close_5d_ago = float(prior["close"].iloc[-5]) if len(prior) >= 5 else float(prior["close"].iloc[0])
                trend_5d = (day_close - close_5d_ago) / close_5d_ago * 100
            else:
                trend_5d = 0.0

            # COMPOSITE QUALITY SCORE
            score = (
                hold_ratio_val * 30 +                # Breakout hold quality (most important)
                min(open_to_high_pct, 30) * 1.0 +    # Breakout size (capped)
                min(volume_ratio, 10) * 3 +           # Volume confirmation
                (5 if above_ma20 else 0) +            # Trend alignment
                min(gap_pct, 5) * 2 +                 # Gap-up bonus
                min(atr_norm_breakout, 5) * 3 +       # Abnormal move
                (3 if trend_5d > 2 else 0) +          # Multi-day trend
                0
            )

            candidates.append({
                "Symbol": symbol,
                "ObsDate": obs_str,
                "Open": day_open,
                "High": day_high,
                "Low": day_low,
                "Close": day_close,
                "Volume": day_volume,
                "OpenToHighPct": round(open_to_high_pct, 2),
                "OpenToClosePct": round(open_to_close_pct, 2),
                "HoldRatio": round(hold_ratio_val, 3),
                "VolumeRatio": round(volume_ratio, 2),
                "GapPct": round(gap_pct, 2),
                "AboveMA20": above_ma20,
                "ATR": round(atr, 2),
                "ATRNormBreakout": round(atr_norm_breakout, 2),
                "Trend5D": round(trend_5d, 2),
                "Score": round(score, 1),
            })

        total_breakouts = len(candidates)

        # Rank by quality score and take top N
        candidates.sort(key=lambda x: x["Score"], reverse=True)
        selected = candidates[:tn]

        day_trades: List[Dict] = []
        for cand in selected:
            symbol = cand["Symbol"]
            hist = symbol_histories[symbol]

            # ENTRY: Buy at Day T+1 open
            entry_row = hist.loc[entry_date]
            buy_price = float(entry_row["open"])

            if buy_price <= 0:
                continue

            # Check if gap-up is too extreme (avoid chasing)
            entry_gap = (buy_price - cand["Close"]) / cand["Close"] * 100
            if entry_gap > 10:  # Don't chase stocks gapping up 10%+
                continue

            # ADAPTIVE STOP LOSS
            if use_atr_stop and cand["ATR"] > 0:
                atr_stop = buy_price - atr_stop_mult * cand["ATR"]
                pct_stop = buy_price * (1 - trail_pct / 100)
                stop_loss = max(atr_stop, pct_stop)
            else:
                stop_loss = buy_price * (1 - trail_pct / 100)

            # Targets
            target1 = buy_price * 1.02
            target2 = buy_price * 1.04

            # MULTI-DAY HOLDING from entry day
            hold_dates = [d for d in all_dates if d >= entry_date][:max_hold_days + 1]
            trail_stop = stop_loss
            peak_price = buy_price
            t1_hit = False
            t2_hit = False
            exit_price = buy_price
            exit_date = entry_str
            exit_reason = "same_day"
            hold_days = 0

            for hd_idx, hd in enumerate(hold_dates):
                if hd not in hist.index:
                    continue

                hrow = hist.loc[hd]
                hhigh = float(hrow["high"])
                hlow = float(hrow["low"])
                hclose = float(hrow["close"])
                hold_days = hd_idx

                # Update peak and trailing stop
                if hhigh > peak_price:
                    peak_price = hhigh
                    new_trail = peak_price * (1 - trail_pct / 100)
                    trail_stop = max(trail_stop, new_trail)

                # Check stop hit
                if hlow <= trail_stop:
                    exit_price = trail_stop
                    exit_date = hd.strftime("%Y-%m-%d")
                    exit_reason = "trailing_stop" if trail_stop > stop_loss else "stop_loss"
                    break

                # Check T2
                if hhigh >= target2:
                    exit_price = target2
                    exit_date = hd.strftime("%Y-%m-%d")
                    exit_reason = "target2"
                    t2_hit = True
                    t1_hit = True
                    break

                # Check T1 → tighten trail
                if hhigh >= target1 and not t1_hit:
                    t1_hit = True
                    trail_stop = max(trail_stop, buy_price * 1.005)

                # Last hold day
                if hold_days == max_hold_days:
                    exit_price = hclose
                    exit_date = hd.strftime("%Y-%m-%d")
                    exit_reason = "max_hold"
                    break

            total_return = round((exit_price - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0.0
            won = total_return > 0

            # Same-day entry performance (close of entry day vs buy)
            entry_close = float(entry_row["close"]) if "close" in entry_row.index else buy_price
            same_day_return = round((entry_close - buy_price) / buy_price * 100, 2)

            trade = {
                "ObsDate": obs_str,
                "EntryDate": entry_str,
                "Symbol": symbol,
                "Regime": regime,
                "ObsOTH": cand["OpenToHighPct"],
                "ObsOTC": cand["OpenToClosePct"],
                "ObsHoldRatio": cand["HoldRatio"],
                "ObsVolumeRatio": cand["VolumeRatio"],
                "ObsGapPct": cand["GapPct"],
                "ObsTrend5D": cand["Trend5D"],
                "Score": cand["Score"],
                "BuyPrice": round(buy_price, 2),
                "EntryGapPct": round(entry_gap, 2),
                "StopLoss": round(stop_loss, 2),
                "ExitPrice": round(exit_price, 2),
                "ExitDate": exit_date,
                "HoldDays": hold_days,
                "ExitReason": exit_reason,
                "SameDayReturnPct": same_day_return,
                "TradeReturnPct": total_return,
                "Won": won,
                "T1Hit": t1_hit,
                "T2Hit": t2_hit,
            }
            trade_rows.append(trade)
            day_trades.append(trade)

        wins = sum(1 for t in day_trades if t["Won"])
        avg_ret = round(np.mean([t["TradeReturnPct"] for t in day_trades]), 2) if day_trades else 0.0
        daily_summaries.append({
            "ObsDate": obs_str,
            "EntryDate": entry_str,
            "Breakouts": total_breakouts,
            "Qualified": len(selected),
            "Traded": len(day_trades),
            "Wins": wins,
            "Losses": len(day_trades) - wins,
            "WinRate": round(100 * wins / len(day_trades), 1) if day_trades else 0.0,
            "AvgReturnPct": avg_ret,
            "Regime": regime,
        })

    # --- RESULTS ---
    trades_df = pd.DataFrame(trade_rows)
    summary_df = pd.DataFrame(daily_summaries)

    print(f"\n{'='*70}")
    print(f"RESULTS — {strategy.upper()}")
    print(f"{'='*70}")

    if trades_df.empty:
        print("  No trades generated.")
        return {"strategy": strategy, "trades": 0, "wr": 0}

    total = len(trades_df)
    wins = int(trades_df["Won"].sum())
    losses = total - wins
    wr = round(100 * wins / total, 1)
    avg_ret = round(trades_df["TradeReturnPct"].mean(), 2)
    med_ret = round(trades_df["TradeReturnPct"].median(), 2)
    avg_win = round(trades_df[trades_df["Won"]]["TradeReturnPct"].mean(), 2) if wins > 0 else 0
    avg_loss = round(trades_df[~trades_df["Won"]]["TradeReturnPct"].mean(), 2) if losses > 0 else 0
    avg_hold = round(trades_df["HoldDays"].mean(), 1)
    active = int((summary_df["Traded"] > 0).sum())
    skipped = int((summary_df["Traded"] == 0).sum())

    print(f"  Period: {eligible_dates[0].strftime('%Y-%m-%d')} → {eligible_dates[-1].strftime('%Y-%m-%d')}")
    print(f"  Days: {len(eligible_dates)} | Active: {active} | Inactive: {skipped}")
    print(f"  Regime distribution: {regime_stats}")
    print(f"  Filters: OTH>={bp}%, HR>={hr}, Vol>={vr}x")
    print(f"  Total trades: {total}")
    print(f"  Wins: {wins} | Losses: {losses}")
    print(f"  ***  WIN RATE: {wr}%  ***")
    print(f"  Avg Return/trade: {avg_ret:+.2f}%")
    print(f"  Median Return/trade: {med_ret:+.2f}%")
    print(f"  Avg Winner: {avg_win:+.2f}% | Avg Loser: {avg_loss:+.2f}%")
    print(f"  Avg Hold Days: {avg_hold}")

    # Same-day performance
    sd_wr = round(100 * (trades_df["SameDayReturnPct"] > 0).sum() / total, 1)
    sd_avg = round(trades_df["SameDayReturnPct"].mean(), 2)
    print(f"  Same-day WR (entry close vs buy): {sd_wr}%, avg {sd_avg:+.2f}%")

    # Exit reasons
    print(f"\n  --- Exit Reasons ---")
    for reason in trades_df["ExitReason"].value_counts().index:
        sub = trades_df[trades_df["ExitReason"] == reason]
        r_wr = 100 * sub["Won"].mean()
        print(f"    {reason:18s}: {len(sub):4d} ({100*len(sub)/total:.0f}%), WR={r_wr:.0f}%, avg={sub['TradeReturnPct'].mean():+.2f}%")

    # Entry gap analysis
    print(f"\n  --- Entry Gap (T+1 open vs T close) ---")
    for lo, hi in [(-5, 0), (0, 1), (1, 3), (3, 5), (5, 20)]:
        sub = trades_df[(trades_df["EntryGapPct"] >= lo) & (trades_df["EntryGapPct"] < hi)]
        if len(sub) >= 3:
            eg_wr = 100 * sub["Won"].mean()
            print(f"    Gap {lo:+d}% to {hi:+d}%: {len(sub):3d} trades, WR={eg_wr:.1f}%")

    # Portfolio simulation
    print(f"\n  --- Portfolio (start Rs 1,00,000) ---")
    portfolio = 100000.0
    for _, day in summary_df.iterrows():
        if day["Traded"] > 0:
            portfolio *= (1 + day["AvgReturnPct"] / 100)
    total_ret = round((portfolio / 100000 - 1) * 100, 1)
    months = max(1, (pd.to_datetime(eligible_dates[-1]) - pd.to_datetime(eligible_dates[0])).days / 30.44)
    monthly = round(((portfolio / 100000) ** (1 / months) - 1) * 100, 2)
    print(f"  Final: Rs {portfolio:,.0f}")
    print(f"  Return: {total_ret:+.1f}% over {months:.1f} months")
    print(f"  Monthly: {monthly:+.2f}%")

    # Monthly breakdown
    summary_df["Month"] = pd.to_datetime(summary_df["ObsDate"]).dt.strftime("%Y-%m")
    monthly_df = summary_df[summary_df["Traded"] > 0].groupby("Month").agg(
        Days=("ObsDate", "count"),
        TotalTrades=("Traded", "sum"),
        TotalWins=("Wins", "sum"),
        TotalLosses=("Losses", "sum"),
        AvgReturn=("AvgReturnPct", "mean"),
    ).reset_index()
    monthly_df["WR"] = (100 * monthly_df["TotalWins"] / monthly_df["TotalTrades"].clip(lower=1)).round(1)
    monthly_df["AvgReturn"] = monthly_df["AvgReturn"].round(2)
    print(f"\n  --- Monthly Breakdown ---")
    print(monthly_df.to_string(index=False))

    # HoldRatio analysis
    print(f"\n  --- Win Rate by Observation Hold Ratio ---")
    for lo, hi in [(0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]:
        sub = trades_df[(trades_df["ObsHoldRatio"] >= lo) & (trades_df["ObsHoldRatio"] < hi)]
        if len(sub) >= 3:
            print(f"    HR {lo:.1f}-{hi:.1f}: {len(sub):3d} trades, WR={100*sub['Won'].mean():.1f}%, avg={sub['TradeReturnPct'].mean():+.2f}%")

    # Save
    output_dir = Path(Archiver.get_user_data_dir())
    tag = f"confirmed_{strategy}"
    dr = f"{eligible_dates[0].strftime('%Y-%m-%d')}_{eligible_dates[-1].strftime('%Y-%m-%d')}"
    trades_path = output_dir / f"{tag}_trades_{dr}.csv"
    summary_path = output_dir / f"{tag}_summary_{dr}.csv"
    trades_df.to_csv(trades_path, index=False)
    summary_df.to_csv(summary_path, index=False)
    print(f"\n  Saved: {trades_path.name}")

    return {
        "strategy": strategy,
        "trades": total,
        "wins": wins,
        "losses": losses,
        "wr": wr,
        "avg_return": avg_ret,
        "avg_winner": avg_win,
        "avg_loser": avg_loss,
        "portfolio_final": round(portfolio),
        "monthly_return": monthly,
    }


def main():
    p = argparse.ArgumentParser(description="Confirmed Breakout Backtest v2")
    p.add_argument("--start", default="2025-04-01")
    p.add_argument("--end", default="2026-04-06")
    p.add_argument("--limit", type=int, default=0, help="Limit universe to N stocks")
    p.add_argument("--strategy", default="all",
                   choices=["loose", "standard", "strict", "ultra", "elite", "all"])
    p.add_argument("--breakout-pct", type=float, default=None)
    p.add_argument("--hold-ratio", type=float, default=None)
    p.add_argument("--vol-ratio", type=float, default=None)
    p.add_argument("--top-n", type=int, default=None)
    p.add_argument("--hold-days", type=int, default=5)
    p.add_argument("--trail-pct", type=float, default=1.5)
    p.add_argument("--bull-only", action="store_true")
    p.add_argument("--gap-up", action="store_true")
    p.add_argument("--no-regime", action="store_true")
    p.add_argument("--no-ma", action="store_true")
    p.add_argument("--no-atr-stop", action="store_true")
    a = p.parse_args()

    strategies = list(STRATEGY_PROFILES.keys()) if a.strategy == "all" else [a.strategy]
    all_results = []

    # Download data ONCE
    print("=" * 70)
    print("DOWNLOADING DATA (once for all strategies)")
    print("=" * 70)
    preloaded = _download_all_data(a.start, a.end, a.limit)

    for strat in strategies:
        result = run_confirmed_breakout_backtest(
            start=a.start, end=a.end,
            universe_limit=a.limit,
            strategy=strat,
            breakout_pct=a.breakout_pct, hold_ratio=a.hold_ratio,
            vol_ratio=a.vol_ratio, top_n=a.top_n,
            max_hold_days=a.hold_days, trail_pct=a.trail_pct,
            require_above_ma=not a.no_ma,
            require_gap_up=a.gap_up,
            use_regime_filter=not a.no_regime,
            require_bull_only=a.bull_only,
            use_atr_stop=not a.no_atr_stop,
            _preloaded=preloaded,
        )
        all_results.append(result)

    if len(all_results) > 1:
        print(f"\n\n{'='*70}")
        print("STRATEGY COMPARISON")
        print(f"{'='*70}")
        print(f"{'Strategy':12s} {'Trades':>7s} {'Wins':>5s} {'Loss':>5s} {'WR%':>6s} {'AvgRet':>7s} {'AvgW':>7s} {'AvgL':>7s} {'Port':>12s} {'Mo%':>6s}")
        print("-" * 80)
        for r in all_results:
            print(f"{r['strategy']:12s} {r.get('trades',0):7d} {r.get('wins',0):5d} {r.get('losses',0):5d} "
                  f"{r.get('wr',0):6.1f} {r.get('avg_return',0):+7.2f} {r.get('avg_winner',0):+7.2f} "
                  f"{r.get('avg_loser',0):+7.2f} {r.get('portfolio_final',100000):>12,} {r.get('monthly_return',0):+6.2f}")


if __name__ == "__main__":
    main()
