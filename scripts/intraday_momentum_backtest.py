"""
Intraday Momentum Backtest — The CORRECT test.

The live system detects breakouts DURING market hours, not before open.
This backtest simulates that by:

1. Use DAILY OHLCV data but detect intraday breakouts:
   - Signal: stock moved up X% from open (open-to-high >= threshold)
   - This means during the day, the stock was visibly breaking out
   
2. Simulate buying at the "detection point":
   - We'd detect around mid-day when price is between open and high
   - Approximate buy price = open + 60% of (high - open) — we catch it partway
   
3. Check: does the stock hold gains into close? Into next day?

This tests the REAL question: "If you buy a stock that's currently surging,
do you make money?"
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from PKDevTools.classes import Archiver

UNIVERSE_FILE = REPO_ROOT / "results" / "Indices" / "EQUITY_L.csv"


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
    """Simple regime: BULL/NEUTRAL/BEAR based on Nifty 50 EMAs."""
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


def run_intraday_momentum_backtest(
    sample_days: int,
    universe_limit: int,
    start: str,
    end: str,
    breakout_pct: float = 1.5,
    min_volume_ratio: float = 1.5,
    min_avg_trade_value: float = 500000.0,
    top_n: int = 5,
    max_hold_days: int = 3,
    trail_pct: float = 1.5,
    use_regime_filter: bool = True,
) -> Dict[str, Path]:

    # Download Nifty 50
    print("Downloading Nifty 50...")
    nifty_raw = yf.download("^NSEI", start=start, end=end, auto_adjust=False, progress=False)
    nifty_hist = pd.DataFrame()
    if nifty_raw is not None and not nifty_raw.empty:
        nifty_hist = nifty_raw.copy()
        nifty_hist.columns = [str(c).strip().lower() if isinstance(c, str) else str(c[0]).strip().lower() for c in nifty_hist.columns]
        nifty_hist.index = pd.to_datetime(nifty_hist.index)
    print(f"  {len(nifty_hist)} Nifty days")

    symbols = _load_universe_symbols(limit=universe_limit)
    print(f"Downloading {len(symbols)} stocks from {start} to {end}...")
    print(f"Strategy: breakout>={breakout_pct}%, vol_ratio>={min_volume_ratio}, "
          f"top_n={top_n}, hold={max_hold_days}d, trail={trail_pct}%")
    raw_df = _download_daily_prices(symbols, start=start, end=end)

    print("Building per-symbol history...")
    symbol_histories: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        hist = _extract_symbol_history(raw_df, symbol)
        if len(hist) >= 30:
            symbol_histories[symbol] = hist
    print(f"  {len(symbol_histories)} symbols loaded")

    all_dates = sorted(set().union(*(set(h.index) for h in symbol_histories.values())))
    eligible_dates = all_dates[25:-(max_hold_days + 1)]

    if sample_days < len(eligible_dates):
        step = max(1, len(eligible_dates) // sample_days)
        sampled_dates = eligible_dates[::step][:sample_days]
    else:
        sampled_dates = eligible_dates
    print(f"  Testing {len(sampled_dates)} days")

    trade_rows: List[Dict] = []
    daily_summaries: List[Dict] = []
    regime_stats: Dict[str, int] = {}

    for day_idx, trade_date in enumerate(sampled_dates):
        t0 = time.time()
        date_str = trade_date.strftime("%Y-%m-%d")

        # Regime check
        regime = _detect_market_regime(nifty_hist, trade_date) if use_regime_filter and len(nifty_hist) > 0 else "NEUTRAL"
        regime_stats[regime] = regime_stats.get(regime, 0) + 1

        if regime == "BEAR" and use_regime_filter:
            daily_summaries.append({
                "TradeDate": date_str, "Breakouts": 0, "Qualified": 0,
                "Traded": 0, "Wins": 0, "Losses": 0,
                "WinRate": 0, "AvgReturnPct": 0, "Regime": "BEAR",
            })
            print(f"  [{day_idx+1}/{len(sampled_dates)}] {date_str}: BEAR — SKIPPED  (0.0s)")
            continue

        # Find stocks breaking out TODAY
        breakout_candidates: List[Dict] = []

        for symbol, hist in symbol_histories.items():
            if trade_date not in hist.index:
                continue

            # Need 20 prior days for volume average
            prior = hist[hist.index < trade_date].tail(20)
            if len(prior) < 15:
                continue

            row = hist.loc[trade_date]
            day_open = float(row["open"])
            day_high = float(row["high"])
            day_low = float(row["low"])
            day_close = float(row["close"])
            day_volume = float(row["volume"])

            if day_open <= 0:
                continue

            # INTRADAY BREAKOUT DETECTION
            open_to_high_pct = (day_high - day_open) / day_open * 100
            if open_to_high_pct < breakout_pct:
                continue

            # Volume confirmation
            avg_volume = prior["volume"].mean()
            volume_ratio = day_volume / avg_volume if avg_volume > 0 else 0
            if volume_ratio < min_volume_ratio:
                continue

            # Liquidity check
            avg_trade_value = (prior["close"] * prior["volume"]).mean()
            if avg_trade_value < min_avg_trade_value:
                continue

            # Additional quality checks
            avg_close_20 = prior["close"].mean()
            above_20_avg = day_close > avg_close_20  # trending above average

            # Open-to-close: did it actually close up?
            open_to_close_pct = (day_close - day_open) / day_open * 100

            # Breakout quality score
            score = (
                open_to_high_pct * 10 +        # bigger breakout = better
                min(volume_ratio, 5) * 8 +      # higher volume = better
                (10 if above_20_avg else 0) +   # above MA = better
                (5 if open_to_close_pct > 0 else -5)  # holding gains = better
            )

            breakout_candidates.append({
                "Symbol": symbol,
                "Open": day_open,
                "High": day_high,
                "Low": day_low,
                "Close": day_close,
                "Volume": day_volume,
                "OpenToHighPct": round(open_to_high_pct, 2),
                "OpenToClosePct": round(open_to_close_pct, 2),
                "VolumeRatio": round(volume_ratio, 2),
                "AvgTradeValue": round(avg_trade_value, 0),
                "AboveAvg20": above_20_avg,
                "Score": round(score, 1),
            })

        total_breakouts = len(breakout_candidates)

        # Rank and select top N
        breakout_candidates.sort(key=lambda x: x["Score"], reverse=True)
        selected = breakout_candidates[:top_n]

        day_trades: List[Dict] = []
        for cand in selected:
            symbol = cand["Symbol"]
            hist = symbol_histories[symbol]

            # SIMULATE BUY PRICE:
            # We detect the breakout mid-day, buy at ~60% of the open-to-high range
            # (we catch it partway through the move, not at the very top)
            buy_price = cand["Open"] + 0.6 * (cand["High"] - cand["Open"])

            # Set stop loss at today's low (worst case intraday) or 1.5% below buy
            stop_loss = max(cand["Low"], buy_price * (1 - trail_pct / 100))

            # Targets: 2% and 4% above buy
            target1 = buy_price * 1.02
            target2 = buy_price * 1.04

            # SAME-DAY exit: close vs buy
            same_day_return = (cand["Close"] - buy_price) / buy_price * 100

            # MULTI-DAY holding: simulate next N days
            future_dates = [d for d in all_dates if d > trade_date][:max_hold_days]
            trail_stop = stop_loss
            peak_price = max(buy_price, cand["High"])
            t1_hit = False
            t2_hit = False
            exit_price = cand["Close"]  # default: same-day close
            exit_date = date_str
            exit_reason = "same_day_close"
            hold_days = 0

            for fd_idx, fd in enumerate(future_dates):
                if fd not in hist.index:
                    continue
                frow = hist.loc[fd]
                fhigh = float(frow["high"])
                flow = float(frow["low"])
                fclose = float(frow["close"])
                hold_days = fd_idx + 1

                # Update peak and trail
                if fhigh > peak_price:
                    peak_price = fhigh
                    trail_stop = max(trail_stop, peak_price * (1 - trail_pct / 100))

                # Check trail/SL hit
                if flow <= trail_stop:
                    exit_price = trail_stop
                    exit_date = fd.strftime("%Y-%m-%d")
                    exit_reason = "trailing_stop" if trail_stop > stop_loss else "stop_loss"
                    break

                # Check T2 hit
                if fhigh >= target2:
                    exit_price = target2
                    exit_date = fd.strftime("%Y-%m-%d")
                    exit_reason = "target2"
                    t2_hit = True
                    t1_hit = True
                    break

                # Check T1 hit → tighten trail
                if fhigh >= target1 and not t1_hit:
                    t1_hit = True
                    trail_stop = max(trail_stop, buy_price * 1.005)

                # Last hold day
                if hold_days == max_hold_days:
                    exit_price = fclose
                    exit_date = fd.strftime("%Y-%m-%d")
                    exit_reason = "max_hold"
                    break

            total_return = round((exit_price - buy_price) / buy_price * 100, 2) if buy_price > 0 else 0.0
            won = total_return > 0

            trade = {
                "TradeDate": date_str,
                "Symbol": symbol,
                "Regime": regime,
                "OpenToHighPct": cand["OpenToHighPct"],
                "OpenToClosePct": cand["OpenToClosePct"],
                "VolumeRatio": cand["VolumeRatio"],
                "Score": cand["Score"],
                "BuyPrice": round(buy_price, 2),
                "StopLoss": round(stop_loss, 2),
                "Target1": round(target1, 2),
                "Target2": round(target2, 2),
                "ExitPrice": round(exit_price, 2),
                "ExitDate": exit_date,
                "HoldDays": hold_days,
                "ExitReason": exit_reason,
                "SameDayReturnPct": round(same_day_return, 2),
                "TradeReturnPct": total_return,
                "SLHit": exit_reason == "stop_loss",
                "T1Hit": t1_hit,
                "T2Hit": t2_hit,
                "Won": won,
            }
            trade_rows.append(trade)
            day_trades.append(trade)

        elapsed = round(time.time() - t0, 1)
        wins = sum(1 for t in day_trades if t["Won"])
        avg_ret = round(np.mean([t["TradeReturnPct"] for t in day_trades]), 2) if day_trades else 0.0
        daily_summaries.append({
            "TradeDate": date_str,
            "Breakouts": total_breakouts,
            "Qualified": len(selected),
            "Traded": len(day_trades),
            "Wins": wins,
            "Losses": len(day_trades) - wins,
            "WinRate": round(100 * wins / len(day_trades), 1) if day_trades else 0.0,
            "AvgReturnPct": avg_ret,
            "Regime": regime,
        })
        regime_tag = f" [{regime}]" if regime != "NEUTRAL" else ""
        print(f"  [{day_idx+1}/{len(sampled_dates)}] {date_str}{regime_tag}: "
              f"{total_breakouts} breakouts, top {len(day_trades)} traded, "
              f"{wins}W/{len(day_trades)-wins}L, avg {avg_ret:+.2f}%  ({elapsed}s)")

    # --- RESULTS ---
    trades_df = pd.DataFrame(trade_rows)
    summary_df = pd.DataFrame(daily_summaries)

    print("\n" + "=" * 70)
    print("INTRADAY MOMENTUM BACKTEST RESULTS")
    print("=" * 70)

    if trades_df.empty:
        print("No trades generated.")
        return {}

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
    skipped = int((summary_df["Regime"] == "BEAR").sum())

    print(f"  Period: {sampled_dates[0].strftime('%Y-%m-%d')} to {sampled_dates[-1].strftime('%Y-%m-%d')}")
    print(f"  Days: {len(sampled_dates)} | Active: {active} | Bear-skipped: {skipped}")
    print(f"  Regime: {regime_stats}")
    print(f"  Breakout threshold: >={breakout_pct}% open-to-high, vol>={min_volume_ratio}x")
    print(f"  Avg breakouts/day: {summary_df[summary_df['Traded']>0]['Breakouts'].mean():.1f}")
    print(f"  Top {top_n} selected/day")
    print(f"  Total trades: {total}")
    print(f"  Wins: {wins} | Losses: {losses}")
    print(f"  Win Rate: {wr}%")
    print(f"  Avg Return/trade: {avg_ret:+.2f}%")
    print(f"  Median Return/trade: {med_ret:+.2f}%")
    print(f"  Avg Winner: {avg_win:+.2f}% | Avg Loser: {avg_loss:+.2f}%")
    print(f"  Avg Hold Days: {avg_hold}")

    # Same-day returns (without multi-day hold)
    same_day_wr = round(100 * (trades_df["SameDayReturnPct"] > 0).sum() / total, 1)
    same_day_avg = round(trades_df["SameDayReturnPct"].mean(), 2)
    print(f"\n  Same-day only: WR={same_day_wr}%, avg={same_day_avg:+.2f}%")

    # Exit reasons
    print(f"\n  --- Exit Reasons ---")
    for reason in trades_df["ExitReason"].value_counts().index:
        sub = trades_df[trades_df["ExitReason"] == reason]
        print(f"    {reason:18s}: {len(sub):4d} ({100*len(sub)/total:.0f}%), WR={100*sub['Won'].mean():.0f}%, avg={sub['TradeReturnPct'].mean():+.2f}%")

    # Portfolio sim
    print(f"\n  --- Portfolio (start Rs 1,00,000) ---")
    portfolio = 100000.0
    for _, day in summary_df.iterrows():
        if day["Traded"] > 0:
            portfolio *= (1 + day["AvgReturnPct"] / 100)
    total_ret = round((portfolio / 100000 - 1) * 100, 1)
    actual_months = max(1, (pd.to_datetime(sampled_dates[-1]) - pd.to_datetime(sampled_dates[0])).days / 30.44)
    monthly = round(((portfolio / 100000) ** (1 / actual_months) - 1) * 100, 2)
    print(f"  Final: Rs {portfolio:,.0f}")
    print(f"  Return: {total_ret:+.1f}% over {actual_months:.1f} months")
    print(f"  Monthly: {monthly:+.2f}%")

    # Monthly breakdown
    summary_df["Month"] = pd.to_datetime(summary_df["TradeDate"]).dt.strftime("%Y-%m")
    monthly_df = summary_df[summary_df["Traded"] > 0].groupby("Month").agg(
        Days=("TradeDate", "count"),
        AvgBreakouts=("Breakouts", "mean"),
        TotalTrades=("Traded", "sum"),
        TotalWins=("Wins", "sum"),
        TotalLosses=("Losses", "sum"),
        AvgReturn=("AvgReturnPct", "mean"),
    ).reset_index()
    monthly_df["WR"] = (100 * monthly_df["TotalWins"] / monthly_df["TotalTrades"].clip(lower=1)).round(1)
    monthly_df["AvgBreakouts"] = monthly_df["AvgBreakouts"].round(0)
    monthly_df["AvgReturn"] = monthly_df["AvgReturn"].round(2)
    print(f"\n  --- Monthly Breakdown ---")
    print(monthly_df.to_string(index=False))

    # Breakout size analysis
    print(f"\n  --- Win Rate by Breakout Size ---")
    for lo, hi in [(1.0, 2.0), (2.0, 3.0), (3.0, 5.0), (5.0, 100.0)]:
        sub = trades_df[(trades_df["OpenToHighPct"] >= lo) & (trades_df["OpenToHighPct"] < hi)]
        if len(sub) >= 5:
            print(f"    {lo:.0f}-{hi:.0f}%: {len(sub)} trades, WR={100*sub['Won'].mean():.1f}%, avg={sub['TradeReturnPct'].mean():+.2f}%")

    # Save
    output_dir = Path(Archiver.get_user_data_dir())
    dr = f"{sampled_dates[0].strftime('%Y-%m-%d')}_{sampled_dates[-1].strftime('%Y-%m-%d')}"
    trades_df.to_csv(output_dir / f"intraday_momentum_trades_{dr}.csv", index=False)
    summary_df.to_csv(output_dir / f"intraday_momentum_summary_{dr}.csv", index=False)
    print(f"\n  Saved to: {output_dir}")
    return {}


def main():
    p = argparse.ArgumentParser(description="Intraday momentum backtest")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--start", default="2025-04-01")
    p.add_argument("--end", default="2026-04-06")
    p.add_argument("--breakout-pct", type=float, default=1.5, help="Min open-to-high %% (default 1.5)")
    p.add_argument("--min-vol", type=float, default=1.5, help="Min volume ratio (default 1.5)")
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--hold-days", type=int, default=3)
    p.add_argument("--trail-pct", type=float, default=1.5)
    p.add_argument("--no-regime", action="store_true")
    a = p.parse_args()
    run_intraday_momentum_backtest(
        sample_days=a.days, universe_limit=a.limit,
        start=a.start, end=a.end,
        breakout_pct=a.breakout_pct, min_volume_ratio=a.min_vol,
        top_n=a.top_n, max_hold_days=a.hold_days,
        trail_pct=a.trail_pct, use_regime_filter=not a.no_regime,
    )


if __name__ == "__main__":
    main()
