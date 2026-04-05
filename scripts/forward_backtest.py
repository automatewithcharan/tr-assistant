"""
Forward Backtest for Trade Assistant.

Unlike the backward backtest (analyze_top_gainers.py) which checks "did we flag
stocks that became winners?", this script answers the REAL question:

    "If you bought every 'Buy Now' signal, what would your actual returns be?"

For each sampled trading day:
1. Run the signal engine on ALL stocks using data up to that evening
2. Collect every stock flagged as "Buy Now"
3. Look at what happened the NEXT trading day (open → close, entry → target/SL)
4. Compute real win rate, average return, and simulated portfolio growth
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pkscreener.classes.ConfigManager as ConfigManager
from PKDevTools.classes import Archiver
from pkscreener.classes.ActionableReport import ActionableReportRunner

UNIVERSE_FILE = REPO_ROOT / "results" / "Indices" / "EQUITY_L.csv"


def _load_universe_symbols(limit: int = 0) -> List[str]:
    df = pd.read_csv(UNIVERSE_FILE)
    symbols = df["SYMBOL"].astype(str).str.strip().tolist()
    symbols = [s for s in symbols if s]
    if limit > 0:
        symbols = symbols[:limit]
    return symbols


def _download_daily_prices(symbols: List[str], start: str, end: str) -> pd.DataFrame:
    tickers = " ".join(f"{s}.NS" for s in symbols)
    return yf.download(
        tickers=tickers,
        start=start,
        end=end,
        auto_adjust=False,
        progress=False,
        group_by="ticker",
        threads=True,
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
    rename_map = {c: str(c).strip().lower() for c in df.columns}
    df = df.rename(columns=rename_map)
    keep = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index)
    return df.dropna()


def _build_runner() -> ActionableReportRunner:
    config_manager = ConfigManager.tools()
    if not config_manager.checkConfigFile():
        config_manager.setConfig(ConfigManager.parser, default=True, showFileCreatedText=False)
    config_manager.getConfig(ConfigManager.parser)
    config_manager.period = "1y"
    config_manager.duration = "1d"
    return ActionableReportRunner(
        config_manager=config_manager,
        universe="allstocks",
        limit=0,
        min_confidence=10.0,
        budget_amount=None,
        risk_percent=1.0,
        min_trade_value=0.0,
        min_volume_ratio=0.0,
        max_workers=1,
        use_live_kite=False,
        strong_buy_only=False,
    )


def run_forward_backtest(
    sample_days: int,
    universe_limit: int,
    start: str,
    end: str,
) -> Dict[str, Path]:
    symbols = _load_universe_symbols(limit=universe_limit)
    print(f"Downloading prices for {len(symbols)} symbols from {start} to {end}...")
    raw_df = _download_daily_prices(symbols, start=start, end=end)

    # Build per-symbol history lookup
    print("Building per-symbol history...")
    symbol_histories: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        hist = _extract_symbol_history(raw_df, symbol)
        if len(hist) >= 35:  # need 30 for signal + a few buffer
            symbol_histories[symbol] = hist
    print(f"  {len(symbol_histories)} symbols have enough history")

    # Get all available trading dates (need at least 30 prior days + 1 next day)
    all_dates = sorted(set().union(*(set(h.index) for h in symbol_histories.values())))
    # We need dates where: (a) at least 30 prior days exist, (b) a next trading day exists
    eligible_dates = all_dates[35:-1]  # skip first 35 for warmup, last 1 for next-day check
    if not eligible_dates:
        raise RuntimeError("Not enough trading days in the window.")

    # Sample evenly across the date range
    if sample_days < len(eligible_dates):
        step = max(1, len(eligible_dates) // sample_days)
        sampled_dates = eligible_dates[::step][:sample_days]
    else:
        sampled_dates = eligible_dates
    print(f"  Testing {len(sampled_dates)} signal days out of {len(eligible_dates)} eligible")

    runner = _build_runner()
    trade_rows: List[Dict] = []
    daily_summaries: List[Dict] = []

    for day_idx, signal_date in enumerate(sampled_dates):
        t0 = time.time()
        signal_date_str = signal_date.strftime("%Y-%m-%d")

        # Find the next trading day
        later_dates = [d for d in all_dates if d > signal_date]
        if not later_dates:
            continue
        next_day = later_dates[0]
        next_day_str = next_day.strftime("%Y-%m-%d")

        buy_now_count = 0
        day_trades: List[Dict] = []

        for symbol, hist in symbol_histories.items():
            # Data up to and including signal_date
            prior_df = hist[hist.index <= signal_date].copy()
            if len(prior_df) < 30:
                continue

            analyzed = runner.analyze_prepared_data(
                symbol=symbol,
                data=prior_df,
                run_at_text=f"{signal_date_str} 15:30:00",
                live_tick_used=False,
            )
            if analyzed is None:
                continue

            now_action = str(analyzed.get("NowAction", ""))
            if now_action not in ["Buy Now", "Strong Buy Now"]:
                continue

            buy_now_count += 1
            entry = float(analyzed.get("Entry", 0.0) or 0.0)
            stop_loss = float(analyzed.get("StopLoss", 0.0) or 0.0)
            target1 = float(analyzed.get("Target1", 0.0) or 0.0)
            target2 = float(analyzed.get("Target2", 0.0) or 0.0)
            confidence = float(analyzed.get("ConfidencePct", 0.0) or 0.0)
            reason = str(analyzed.get("Reason", ""))

            # Check next-day performance
            if next_day not in hist.index:
                continue
            next_row = hist.loc[next_day]
            next_open = float(next_row["open"])
            next_high = float(next_row["high"])
            next_low = float(next_row["low"])
            next_close = float(next_row["close"])

            # Simulate: buy at entry if next day hits it, else buy at open
            if entry > 0 and next_high >= entry:
                buy_price = entry
            else:
                buy_price = next_open

            # Did stop loss get hit?
            sl_hit = stop_loss > 0 and next_low <= stop_loss and buy_price > stop_loss
            # Did target get hit?
            t1_hit = target1 > 0 and next_high >= target1
            t2_hit = target2 > 0 and next_high >= target2

            # Intraday return: close vs buy price
            close_return_pct = round((next_close - buy_price) * 100 / buy_price, 2) if buy_price > 0 else 0.0
            # Best case: target1 hit
            if t1_hit and not sl_hit:
                exit_price = target1
            elif sl_hit:
                exit_price = stop_loss
            else:
                exit_price = next_close
            trade_return_pct = round((exit_price - buy_price) * 100 / buy_price, 2) if buy_price > 0 else 0.0

            trade = {
                "SignalDate": signal_date_str,
                "TradeDate": next_day_str,
                "Symbol": symbol,
                "NowAction": now_action,
                "ConfidencePct": confidence,
                "Entry": entry,
                "StopLoss": stop_loss,
                "Target1": target1,
                "BuyPrice": round(buy_price, 2),
                "NextOpen": round(next_open, 2),
                "NextHigh": round(next_high, 2),
                "NextLow": round(next_low, 2),
                "NextClose": round(next_close, 2),
                "ExitPrice": round(exit_price, 2),
                "CloseReturnPct": close_return_pct,
                "TradeReturnPct": trade_return_pct,
                "SLHit": sl_hit,
                "T1Hit": t1_hit,
                "Won": trade_return_pct > 0,
                "Reason": reason,
            }
            trade_rows.append(trade)
            day_trades.append(trade)

        elapsed = round(time.time() - t0, 1)
        wins = sum(1 for t in day_trades if t["TradeReturnPct"] > 0)
        avg_ret = round(np.mean([t["TradeReturnPct"] for t in day_trades]), 2) if day_trades else 0.0
        daily_summaries.append({
            "SignalDate": signal_date_str,
            "TradeDate": next_day_str,
            "BuyNowCount": buy_now_count,
            "TradesExecuted": len(day_trades),
            "Wins": wins,
            "Losses": len(day_trades) - wins,
            "WinRate": round(100 * wins / len(day_trades), 1) if day_trades else 0.0,
            "AvgReturnPct": avg_ret,
            "TotalReturnPct": round(sum(t["TradeReturnPct"] for t in day_trades), 2) if day_trades else 0.0,
        })
        print(f"  [{day_idx+1}/{len(sampled_dates)}] {signal_date_str} → {next_day_str}: "
              f"{buy_now_count} Buy Now, {len(day_trades)} traded, "
              f"{wins}W/{len(day_trades)-wins}L, avg {avg_ret:+.2f}%  ({elapsed}s)")

    # Build results
    trades_df = pd.DataFrame(trade_rows)
    summary_df = pd.DataFrame(daily_summaries)

    # Print final statistics
    print("\n" + "=" * 70)
    print("FORWARD BACKTEST RESULTS")
    print("=" * 70)

    if trades_df.empty:
        print("No trades were generated.")
        return {}

    total_trades = len(trades_df)
    total_wins = int(trades_df["Won"].sum())
    total_losses = total_trades - total_wins
    win_rate = round(100 * total_wins / total_trades, 1)
    avg_return = round(trades_df["TradeReturnPct"].mean(), 2)
    median_return = round(trades_df["TradeReturnPct"].median(), 2)
    avg_win = round(trades_df[trades_df["Won"]]["TradeReturnPct"].mean(), 2) if total_wins > 0 else 0.0
    avg_loss = round(trades_df[~trades_df["Won"]]["TradeReturnPct"].mean(), 2) if total_losses > 0 else 0.0
    sl_hit_count = int(trades_df["SLHit"].sum())
    t1_hit_count = int(trades_df["T1Hit"].sum())
    avg_buynow_per_day = round(summary_df["BuyNowCount"].mean(), 1)
    avg_trades_per_day = round(summary_df["TradesExecuted"].mean(), 1)

    print(f"  Period: {sampled_dates[0].strftime('%Y-%m-%d')} to {sampled_dates[-1].strftime('%Y-%m-%d')}")
    print(f"  Days sampled: {len(sampled_dates)}")
    print(f"  Avg 'Buy Now' signals/day: {avg_buynow_per_day}")
    print(f"  Avg trades/day: {avg_trades_per_day}")
    print(f"  Total trades: {total_trades}")
    print(f"  Wins: {total_wins} | Losses: {total_losses}")
    print(f"  Win Rate: {win_rate}%")
    print(f"  Avg Return/trade: {avg_return:+.2f}%")
    print(f"  Median Return/trade: {median_return:+.2f}%")
    print(f"  Avg Winner: {avg_win:+.2f}% | Avg Loser: {avg_loss:+.2f}%")
    print(f"  Stop Loss hit: {sl_hit_count}/{total_trades} ({100*sl_hit_count/total_trades:.1f}%)")
    print(f"  Target 1 hit: {t1_hit_count}/{total_trades} ({100*t1_hit_count/total_trades:.1f}%)")

    # Simulated portfolio: start with 100K, invest equally across all Buy Now picks each day
    print(f"\n  --- Simulated Portfolio (start ₹1,00,000) ---")
    portfolio = 100000.0
    for _, day in summary_df.iterrows():
        if day["TradesExecuted"] > 0:
            per_trade_pct = day["AvgReturnPct"] / 100.0
            portfolio *= (1 + per_trade_pct)
    total_return = round((portfolio - 100000) * 100 / 100000, 1)
    print(f"  Final value: ₹{portfolio:,.0f}")
    print(f"  Total return: {total_return:+.1f}%")
    months = len(sampled_dates) / 22  # ~22 trading days per month
    if months > 0:
        monthly_return = round(((portfolio / 100000) ** (1 / months) - 1) * 100, 2)
        print(f"  Approx monthly return: {monthly_return:+.2f}%")

    # Monthly breakdown
    if len(summary_df) > 1:
        summary_df["Month"] = pd.to_datetime(summary_df["SignalDate"]).dt.strftime("%Y-%m")
        monthly = summary_df.groupby("Month").agg(
            Days=("SignalDate", "count"),
            AvgBuyNow=("BuyNowCount", "mean"),
            TotalTrades=("TradesExecuted", "sum"),
            TotalWins=("Wins", "sum"),
            TotalLosses=("Losses", "sum"),
            AvgDailyReturn=("AvgReturnPct", "mean"),
        ).reset_index()
        monthly["WinRate"] = (100 * monthly["TotalWins"] / monthly["TotalTrades"].clip(lower=1)).round(1)
        monthly["AvgBuyNow"] = monthly["AvgBuyNow"].round(1)
        monthly["AvgDailyReturn"] = monthly["AvgDailyReturn"].round(2)
        print(f"\n  --- Monthly Breakdown ---")
        print(monthly.to_string(index=False))

    # Save
    output_dir = Path(Archiver.get_user_data_dir())
    date_range = f"{sampled_dates[0].strftime('%Y-%m-%d')}_{sampled_dates[-1].strftime('%Y-%m-%d')}"
    trades_path = output_dir / f"forward_backtest_trades_{date_range}.csv"
    summary_path = output_dir / f"forward_backtest_summary_{date_range}.csv"
    trades_df.to_csv(trades_path, index=False)
    summary_df.to_csv(summary_path, index=False)
    print(f"\n  Saved trades to: {trades_path}")
    print(f"  Saved daily summary to: {summary_path}")
    return {"trades_path": trades_path, "summary_path": summary_path}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Forward backtest: simulate buying every 'Buy Now' signal and measure next-day returns."
    )
    parser.add_argument("--days", type=int, default=60,
                        help="Number of trading days to sample (default: 60)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Optional universe symbol limit (0 = all)")
    parser.add_argument("--start", type=str, default="2025-04-01",
                        help="Download window start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-04-06",
                        help="Download window end date (YYYY-MM-DD)")
    args = parser.parse_args()

    run_forward_backtest(
        sample_days=max(1, int(args.days)),
        universe_limit=max(0, int(args.limit)),
        start=str(args.start),
        end=str(args.end),
    )


if __name__ == "__main__":
    main()
