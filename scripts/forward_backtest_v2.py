"""
Forward Backtest v2 — Improved Strategy.

Three key improvements over v1:
1. MARKET REGIME FILTER — Skip trading when Nifty 50 is in a downtrend
2. SIGNAL CONCENTRATION — Rank all Buy Now signals, only take top N per day
3. MULTI-DAY HOLDING    — Hold up to 5 days with trailing stop instead of
                          exiting at day-1 close

Usage:
  python scripts/forward_backtest_v2.py --days 60 --start 2025-04-01 --end 2026-04-06
  python scripts/forward_backtest_v2.py --top-n 3 --hold-days 5
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

import pkscreener.classes.ConfigManager as ConfigManager
from PKDevTools.classes import Archiver
from pkscreener.classes.ActionableReport import ActionableReportRunner

UNIVERSE_FILE = REPO_ROOT / "results" / "Indices" / "EQUITY_L.csv"


# ---------------------------------------------------------------------------
# 1. MARKET REGIME FILTER
# ---------------------------------------------------------------------------

def _detect_market_regime(
    nifty_hist: pd.DataFrame,
    as_of: pd.Timestamp,
) -> Tuple[str, Dict]:
    """
    Classify market regime using Nifty 50 data up to *as_of*.

    Returns (regime, details):
      regime: "BULL" | "NEUTRAL" | "BEAR"
      details: dict with ema20, ema50, slope, adx info
    """
    df = nifty_hist[nifty_hist.index <= as_of].copy()
    if len(df) < 50:
        return "NEUTRAL", {"reason": "insufficient data"}

    close = df["close"]
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema20 = close.ewm(span=20, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    latest_close = float(close.iloc[-1])
    latest_ema10 = float(ema10.iloc[-1])
    latest_ema20 = float(ema20.iloc[-1])
    latest_ema50 = float(ema50.iloc[-1])

    # Slope of EMA20 over last 5 days (annualized direction)
    ema20_5d_ago = float(ema20.iloc[-6]) if len(ema20) >= 6 else latest_ema20
    slope_pct = ((latest_ema20 - ema20_5d_ago) / ema20_5d_ago * 100) if ema20_5d_ago else 0.0

    # How many of last 3 days closed below EMA20?
    last3_below_ema20 = int((close.tail(3) < ema20.tail(3)).sum())

    # Market breadth proxy: is price above or below EMA50?
    above_ema50 = latest_close > latest_ema50

    # EMA alignment: bullish = 10 > 20 > 50, bearish = 10 < 20 < 50
    bull_aligned = latest_ema10 > latest_ema20 > latest_ema50
    bear_aligned = latest_ema10 < latest_ema20 < latest_ema50

    # Recent momentum: 5-day return
    close_5d_ago = float(close.iloc[-6]) if len(close) >= 6 else latest_close
    ret_5d = ((latest_close - close_5d_ago) / close_5d_ago * 100) if close_5d_ago else 0.0

    details = {
        "close": round(latest_close, 1),
        "ema10": round(latest_ema10, 1),
        "ema20": round(latest_ema20, 1),
        "ema50": round(latest_ema50, 1),
        "slope_5d_pct": round(slope_pct, 3),
        "last3_below_ema20": last3_below_ema20,
        "above_ema50": above_ema50,
        "bull_aligned": bull_aligned,
        "bear_aligned": bear_aligned,
        "ret_5d_pct": round(ret_5d, 2),
    }

    # Classification rules
    if bear_aligned and slope_pct < -0.3:
        return "BEAR", details
    if last3_below_ema20 >= 3 and slope_pct < -0.2 and not above_ema50:
        return "BEAR", details
    if ret_5d < -3.0 and slope_pct < -0.5:
        return "BEAR", details
    if bull_aligned and slope_pct > 0.1:
        return "BULL", details
    if above_ema50 and slope_pct > 0 and last3_below_ema20 <= 1:
        return "BULL", details
    return "NEUTRAL", details


# ---------------------------------------------------------------------------
# 2. SIGNAL RANKING + CONCENTRATION
# ---------------------------------------------------------------------------

def _rank_signals(signals: List[Dict], top_n: int = 3) -> List[Dict]:
    """
    Rank Buy Now signals by composite quality score and return top N.

    Score components:
    - Confidence (40%): higher confidence = better signal
    - Volume ratio (25%): higher = more conviction
    - Momentum (20%): higher momentum score = stronger breakout
    - Strong Buy bonus (15%): Strong Buy Now gets extra credit
    """
    if not signals:
        return []

    for s in signals:
        conf_score = min(float(s.get("ConfidencePct", 0)), 60) / 60 * 40
        vol_score = min(float(s.get("VolumeRatio", 0)), 5.0) / 5.0 * 25
        mom_score = min(float(s.get("MomentumScore", 0)), 100) / 100 * 20
        strong_bonus = 15.0 if s.get("NowAction") == "Strong Buy Now" else 0.0
        s["_rank_score"] = round(conf_score + vol_score + mom_score + strong_bonus, 2)

    signals.sort(key=lambda x: x["_rank_score"], reverse=True)
    return signals[:top_n]


# ---------------------------------------------------------------------------
# 3. MULTI-DAY HOLDING SIMULATION
# ---------------------------------------------------------------------------

def _simulate_multiday_trade(
    hist: pd.DataFrame,
    buy_date: pd.Timestamp,
    buy_price: float,
    stop_loss: float,
    target1: float,
    target2: float,
    max_hold_days: int = 5,
    trail_pct: float = 1.5,
) -> Dict:
    """
    Simulate holding a trade for up to max_hold_days with trailing stop.

    Exit conditions (checked each day in order):
    1. Stop loss hit → exit at SL
    2. Target2 hit → exit at T2 (big win)
    3. Target1 hit → tighten trailing stop to breakeven+0.5%, continue
    4. After max_hold_days → exit at close
    5. Trailing stop hit → exit at trail

    Returns dict with exit details.
    """
    dates_after = hist[hist.index > buy_date].index.tolist()
    if not dates_after:
        return {
            "ExitPrice": buy_price, "ExitDate": buy_date.strftime("%Y-%m-%d"),
            "HoldDays": 0, "ExitReason": "no_data", "TradeReturnPct": 0.0,
            "SLHit": False, "T1Hit": False, "T2Hit": False, "Won": False,
            "PeakPrice": buy_price, "TrailStop": stop_loss,
        }

    hold_dates = dates_after[:max_hold_days]
    trail_stop = stop_loss
    peak_price = buy_price
    t1_reached = False

    for day_idx, date in enumerate(hold_dates):
        if date not in hist.index:
            continue
        row = hist.loc[date]
        day_open = float(row["open"])
        day_high = float(row["high"])
        day_low = float(row["low"])
        day_close = float(row["close"])
        hold_day = day_idx + 1

        # Update peak
        if day_high > peak_price:
            peak_price = day_high
            # Update trailing stop: trail_pct% below peak, but never below original SL
            new_trail = peak_price * (1 - trail_pct / 100)
            trail_stop = max(trail_stop, new_trail)

        # Check SL hit (original or trailing)
        if day_low <= trail_stop:
            exit_price = trail_stop
            ret = round((exit_price - buy_price) * 100 / buy_price, 2) if buy_price > 0 else 0.0
            return {
                "ExitPrice": round(exit_price, 2),
                "ExitDate": date.strftime("%Y-%m-%d"),
                "HoldDays": hold_day,
                "ExitReason": "trailing_stop" if trail_stop > stop_loss else "stop_loss",
                "TradeReturnPct": ret,
                "SLHit": trail_stop <= stop_loss * 1.001,
                "T1Hit": t1_reached,
                "T2Hit": False,
                "Won": ret > 0,
                "PeakPrice": round(peak_price, 2),
                "TrailStop": round(trail_stop, 2),
            }

        # Check T2 hit (big win → exit)
        if target2 > 0 and day_high >= target2:
            exit_price = target2
            ret = round((exit_price - buy_price) * 100 / buy_price, 2) if buy_price > 0 else 0.0
            return {
                "ExitPrice": round(exit_price, 2),
                "ExitDate": date.strftime("%Y-%m-%d"),
                "HoldDays": hold_day,
                "ExitReason": "target2",
                "TradeReturnPct": ret,
                "SLHit": False, "T1Hit": True, "T2Hit": True,
                "Won": True,
                "PeakPrice": round(peak_price, 2),
                "TrailStop": round(trail_stop, 2),
            }

        # Check T1 hit → tighten stop to breakeven + 0.5%
        if target1 > 0 and day_high >= target1 and not t1_reached:
            t1_reached = True
            breakeven_plus = buy_price * 1.005  # lock in 0.5% profit minimum
            trail_stop = max(trail_stop, breakeven_plus)

        # Last hold day → exit at close
        if hold_day == max_hold_days:
            exit_price = day_close
            ret = round((exit_price - buy_price) * 100 / buy_price, 2) if buy_price > 0 else 0.0
            return {
                "ExitPrice": round(exit_price, 2),
                "ExitDate": date.strftime("%Y-%m-%d"),
                "HoldDays": hold_day,
                "ExitReason": "max_hold",
                "TradeReturnPct": ret,
                "SLHit": False, "T1Hit": t1_reached, "T2Hit": False,
                "Won": ret > 0,
                "PeakPrice": round(peak_price, 2),
                "TrailStop": round(trail_stop, 2),
            }

    # Fallback: exit at last available close
    last_date = hold_dates[-1] if hold_dates else buy_date
    last_close = float(hist.loc[last_date, "close"]) if last_date in hist.index else buy_price
    ret = round((last_close - buy_price) * 100 / buy_price, 2) if buy_price > 0 else 0.0
    return {
        "ExitPrice": round(last_close, 2),
        "ExitDate": last_date.strftime("%Y-%m-%d"),
        "HoldDays": len(hold_dates),
        "ExitReason": "end_of_data",
        "TradeReturnPct": ret,
        "SLHit": False, "T1Hit": t1_reached, "T2Hit": False,
        "Won": ret > 0,
        "PeakPrice": round(peak_price, 2),
        "TrailStop": round(trail_stop, 2),
    }


# ---------------------------------------------------------------------------
# Helpers (same as v1)
# ---------------------------------------------------------------------------

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


def _build_runner(min_confidence: float = 40.0, min_volume_ratio: float = 0.5) -> ActionableReportRunner:
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
        min_confidence=min_confidence,
        budget_amount=None,
        risk_percent=1.0,
        min_trade_value=500000.0,
        min_volume_ratio=min_volume_ratio,
        max_workers=1,
        use_live_kite=False,
        strong_buy_only=False,
    )


# ---------------------------------------------------------------------------
# Main backtest engine
# ---------------------------------------------------------------------------

def run_forward_backtest_v2(
    sample_days: int,
    universe_limit: int,
    start: str,
    end: str,
    min_confidence: float = 40.0,
    min_volume_ratio: float = 0.5,
    top_n: int = 3,
    max_hold_days: int = 5,
    trail_pct: float = 1.5,
    use_regime_filter: bool = True,
) -> Dict[str, Path]:
    symbols = _load_universe_symbols(limit=universe_limit)

    # Download Nifty 50 for regime detection
    print(f"Downloading Nifty 50 index data...")
    nifty_raw = yf.download("^NSEI", start=start, end=end, auto_adjust=False, progress=False)
    nifty_hist = pd.DataFrame()
    if nifty_raw is not None and not nifty_raw.empty:
        nifty_hist = nifty_raw.copy()
        nifty_hist.columns = [str(c).strip().lower() if isinstance(c, str) else str(c[0]).strip().lower() for c in nifty_hist.columns]
        nifty_hist.index = pd.to_datetime(nifty_hist.index)
        nifty_hist = nifty_hist.dropna()
    print(f"  Nifty 50: {len(nifty_hist)} trading days loaded")

    print(f"Downloading prices for {len(symbols)} symbols from {start} to {end}...")
    print(f"Filters: min_confidence={min_confidence}, min_volume_ratio={min_volume_ratio}")
    print(f"Strategy: top_n={top_n}, max_hold_days={max_hold_days}, trail={trail_pct}%, regime_filter={use_regime_filter}")
    raw_df = _download_daily_prices(symbols, start=start, end=end)

    print("Building per-symbol history...")
    symbol_histories: Dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        hist = _extract_symbol_history(raw_df, symbol)
        if len(hist) >= 35:
            symbol_histories[symbol] = hist
    print(f"  {len(symbol_histories)} symbols have enough history")

    all_dates = sorted(set().union(*(set(h.index) for h in symbol_histories.values())))
    eligible_dates = all_dates[35:-(max_hold_days + 1)]
    if not eligible_dates:
        raise RuntimeError("Not enough trading days in the window.")

    if sample_days < len(eligible_dates):
        step = max(1, len(eligible_dates) // sample_days)
        sampled_dates = eligible_dates[::step][:sample_days]
    else:
        sampled_dates = eligible_dates
    print(f"  Testing {len(sampled_dates)} signal days out of {len(eligible_dates)} eligible")

    runner = _build_runner(min_confidence=min_confidence, min_volume_ratio=min_volume_ratio)
    trade_rows: List[Dict] = []
    daily_summaries: List[Dict] = []
    regime_stats = {"BULL": 0, "NEUTRAL": 0, "BEAR": 0, "BEAR_SKIPPED": 0}

    for day_idx, signal_date in enumerate(sampled_dates):
        t0 = time.time()
        signal_date_str = signal_date.strftime("%Y-%m-%d")

        # --- REGIME CHECK ---
        regime = "NEUTRAL"
        regime_details = {}
        if use_regime_filter and len(nifty_hist) > 0:
            regime, regime_details = _detect_market_regime(nifty_hist, signal_date)
        regime_stats[regime] = regime_stats.get(regime, 0) + 1

        if regime == "BEAR" and use_regime_filter:
            regime_stats["BEAR_SKIPPED"] = regime_stats.get("BEAR_SKIPPED", 0) + 1
            elapsed = round(time.time() - t0, 1)
            daily_summaries.append({
                "SignalDate": signal_date_str,
                "TradeDate": "",
                "BuyNowCount": 0,
                "Ranked": 0,
                "TradesExecuted": 0,
                "Wins": 0, "Losses": 0,
                "WinRate": 0.0,
                "AvgReturnPct": 0.0,
                "TotalReturnPct": 0.0,
                "Regime": regime,
            })
            print(f"  [{day_idx+1}/{len(sampled_dates)}] {signal_date_str}: BEAR regime — SKIPPED "
                  f"(EMA slope={regime_details.get('slope_5d_pct', '?')}%, "
                  f"5d_ret={regime_details.get('ret_5d_pct', '?')}%)  ({elapsed}s)")
            continue

        # Find next trading day
        later_dates = [d for d in all_dates if d > signal_date]
        if not later_dates:
            continue
        next_day = later_dates[0]
        next_day_str = next_day.strftime("%Y-%m-%d")

        # --- COLLECT ALL BUY NOW SIGNALS ---
        all_buy_signals: List[Dict] = []
        for symbol, hist in symbol_histories.items():
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

            all_buy_signals.append(analyzed)

        buy_now_count = len(all_buy_signals)

        # --- RANK & SELECT TOP N ---
        selected = _rank_signals(all_buy_signals, top_n=top_n)
        day_trades: List[Dict] = []

        for analyzed in selected:
            symbol = str(analyzed["Symbol"])
            entry = float(analyzed.get("Entry", 0.0) or 0.0)
            stop_loss = float(analyzed.get("StopLoss", 0.0) or 0.0)
            target1 = float(analyzed.get("Target1", 0.0) or 0.0)
            target2 = float(analyzed.get("Target2", 0.0) or 0.0)
            confidence = float(analyzed.get("ConfidencePct", 0.0) or 0.0)
            volume_ratio = float(analyzed.get("VolumeRatio", 0.0) or 0.0)
            momentum_score = float(analyzed.get("MomentumScore", 0.0) or 0.0)
            avg_trade_val = float(analyzed.get("AvgTradeValue20", 0.0) or 0.0)
            action_raw = str(analyzed.get("Action", ""))
            now_action = str(analyzed.get("NowAction", ""))
            reason = str(analyzed.get("Reason", ""))
            rank_score = float(analyzed.get("_rank_score", 0.0))

            hist = symbol_histories.get(symbol)
            if hist is None or next_day not in hist.index:
                continue

            # --- ENTRY LOGIC (same as v1) ---
            next_row = hist.loc[next_day]
            next_open = float(next_row["open"])
            next_high = float(next_row["high"])

            if entry > 0 and next_high >= entry:
                buy_price = entry
            else:
                buy_price = next_open

            # --- MULTI-DAY HOLDING ---
            trade_result = _simulate_multiday_trade(
                hist=hist,
                buy_date=next_day,
                buy_price=buy_price,
                stop_loss=stop_loss,
                target1=target1,
                target2=target2,
                max_hold_days=max_hold_days,
                trail_pct=trail_pct,
            )

            trade = {
                "SignalDate": signal_date_str,
                "TradeDate": next_day_str,
                "Symbol": symbol,
                "Action": action_raw,
                "NowAction": now_action,
                "ConfidencePct": confidence,
                "VolumeRatio": round(volume_ratio, 2),
                "MomentumScore": round(momentum_score, 1),
                "AvgTradeValue20": round(avg_trade_val, 0),
                "RankScore": rank_score,
                "Regime": regime,
                "Entry": entry,
                "StopLoss": stop_loss,
                "Target1": target1,
                "Target2": target2,
                "BuyPrice": round(buy_price, 2),
                "ExitPrice": trade_result["ExitPrice"],
                "ExitDate": trade_result["ExitDate"],
                "HoldDays": trade_result["HoldDays"],
                "ExitReason": trade_result["ExitReason"],
                "TradeReturnPct": trade_result["TradeReturnPct"],
                "SLHit": trade_result["SLHit"],
                "T1Hit": trade_result["T1Hit"],
                "T2Hit": trade_result["T2Hit"],
                "Won": trade_result["Won"],
                "PeakPrice": trade_result["PeakPrice"],
                "TrailStop": trade_result["TrailStop"],
                "Reason": reason,
            }
            trade_rows.append(trade)
            day_trades.append(trade)

        elapsed = round(time.time() - t0, 1)
        wins = sum(1 for t in day_trades if t["Won"])
        avg_ret = round(np.mean([t["TradeReturnPct"] for t in day_trades]), 2) if day_trades else 0.0
        daily_summaries.append({
            "SignalDate": signal_date_str,
            "TradeDate": next_day_str,
            "BuyNowCount": buy_now_count,
            "Ranked": len(selected),
            "TradesExecuted": len(day_trades),
            "Wins": wins,
            "Losses": len(day_trades) - wins,
            "WinRate": round(100 * wins / len(day_trades), 1) if day_trades else 0.0,
            "AvgReturnPct": avg_ret,
            "TotalReturnPct": round(sum(t["TradeReturnPct"] for t in day_trades), 2) if day_trades else 0.0,
            "Regime": regime,
        })
        regime_label = f" [{regime}]" if regime != "NEUTRAL" else ""
        print(f"  [{day_idx+1}/{len(sampled_dates)}] {signal_date_str} → {next_day_str}{regime_label}: "
              f"{buy_now_count} signals, top {len(day_trades)} traded, "
              f"{wins}W/{len(day_trades)-wins}L, avg {avg_ret:+.2f}%  ({elapsed}s)")

    # --- RESULTS ---
    trades_df = pd.DataFrame(trade_rows)
    summary_df = pd.DataFrame(daily_summaries)

    print("\n" + "=" * 70)
    print("FORWARD BACKTEST v2 RESULTS")
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
    avg_hold = round(trades_df["HoldDays"].mean(), 1)
    sl_hit_count = int(trades_df["SLHit"].sum())
    t1_hit_count = int(trades_df["T1Hit"].sum())
    t2_hit_count = int(trades_df["T2Hit"].sum())

    active_days = int((summary_df["TradesExecuted"] > 0).sum())
    skipped_days = int((summary_df["Regime"] == "BEAR").sum())

    print(f"  Period: {sampled_dates[0].strftime('%Y-%m-%d')} to {sampled_dates[-1].strftime('%Y-%m-%d')}")
    print(f"  Days sampled: {len(sampled_dates)} | Active: {active_days} | Bear-skipped: {skipped_days}")
    print(f"  Regime distribution: BULL={regime_stats.get('BULL',0)}, NEUTRAL={regime_stats.get('NEUTRAL',0)}, BEAR={regime_stats.get('BEAR',0)}")
    print(f"  Signals per active day: {trades_df.groupby('SignalDate').size().mean():.1f} (top-{top_n} selected)")
    print(f"  Total trades: {total_trades}")
    print(f"  Wins: {total_wins} | Losses: {total_losses}")
    print(f"  Win Rate: {win_rate}%")
    print(f"  Avg Return/trade: {avg_return:+.2f}%")
    print(f"  Median Return/trade: {median_return:+.2f}%")
    print(f"  Avg Winner: {avg_win:+.2f}% | Avg Loser: {avg_loss:+.2f}%")
    print(f"  Avg Hold Days: {avg_hold}")
    print(f"  Stop Loss hit: {sl_hit_count}/{total_trades} ({100*sl_hit_count/total_trades:.1f}%)")
    print(f"  Target 1 hit: {t1_hit_count}/{total_trades} ({100*t1_hit_count/total_trades:.1f}%)")
    print(f"  Target 2 hit: {t2_hit_count}/{total_trades} ({100*t2_hit_count/total_trades:.1f}%)")

    # Exit reason breakdown
    print(f"\n  --- Exit Reasons ---")
    for reason, count in trades_df["ExitReason"].value_counts().items():
        subset = trades_df[trades_df["ExitReason"] == reason]
        wr = 100 * subset["Won"].mean()
        ar = subset["TradeReturnPct"].mean()
        print(f"    {reason:20s}: {count:4d} trades ({100*count/total_trades:.1f}%), WR {wr:.1f}%, avg {ar:+.2f}%")

    # Portfolio simulation
    print(f"\n  --- Simulated Portfolio (start Rs 1,00,000) ---")
    portfolio = 100000.0
    for _, day in summary_df.iterrows():
        if day["TradesExecuted"] > 0:
            per_trade_pct = day["AvgReturnPct"] / 100.0
            portfolio *= (1 + per_trade_pct)
    total_return = round((portfolio - 100000) * 100 / 100000, 1)
    print(f"  Final value: Rs {portfolio:,.0f}")
    print(f"  Total return: {total_return:+.1f}%")

    # Correct monthly return calculation
    first_date = pd.to_datetime(sampled_dates[0])
    last_date = pd.to_datetime(sampled_dates[-1])
    actual_months = max(1, (last_date - first_date).days / 30.44)
    monthly_return = round(((portfolio / 100000) ** (1 / actual_months) - 1) * 100, 2)
    annual_return = round(((portfolio / 100000) ** (12 / actual_months) - 1) * 100, 1)
    print(f"  Period: {actual_months:.1f} months")
    print(f"  Monthly compound return: {monthly_return:+.2f}%")
    print(f"  Annualized return: {annual_return:+.1f}%")

    # Monthly breakdown
    if len(summary_df) > 1:
        summary_df["Month"] = pd.to_datetime(summary_df["SignalDate"]).dt.strftime("%Y-%m")
        monthly = summary_df.groupby("Month").agg(
            Days=("SignalDate", "count"),
            Active=("TradesExecuted", lambda x: (x > 0).sum()),
            Skipped=("Regime", lambda x: (x == "BEAR").sum()),
            TotalTrades=("TradesExecuted", "sum"),
            TotalWins=("Wins", "sum"),
            TotalLosses=("Losses", "sum"),
            AvgDailyReturn=("AvgReturnPct", "mean"),
        ).reset_index()
        monthly["WinRate"] = (100 * monthly["TotalWins"] / monthly["TotalTrades"].clip(lower=1)).round(1)
        monthly["AvgDailyReturn"] = monthly["AvgDailyReturn"].round(2)
        print(f"\n  --- Monthly Breakdown ---")
        print(monthly.to_string(index=False))

    # Regime breakdown
    print(f"\n  --- Performance by Regime ---")
    for reg in ["BULL", "NEUTRAL"]:
        subset = trades_df[trades_df["Regime"] == reg]
        if len(subset) > 0:
            wr = 100 * subset["Won"].mean()
            ar = subset["TradeReturnPct"].mean()
            print(f"    {reg:10s}: {len(subset):4d} trades, WR {wr:.1f}%, avg {ar:+.2f}%")

    # Save
    output_dir = Path(Archiver.get_user_data_dir())
    date_range = f"{sampled_dates[0].strftime('%Y-%m-%d')}_{sampled_dates[-1].strftime('%Y-%m-%d')}"
    tag = f"v2_top{top_n}_hold{max_hold_days}"
    trades_path = output_dir / f"forward_bt_{tag}_trades_{date_range}.csv"
    summary_path = output_dir / f"forward_bt_{tag}_summary_{date_range}.csv"
    trades_df.to_csv(trades_path, index=False)
    summary_df.to_csv(summary_path, index=False)
    print(f"\n  Saved trades to: {trades_path}")
    print(f"  Saved daily summary to: {summary_path}")
    return {"trades_path": trades_path, "summary_path": summary_path}


def main() -> None:
    parser = argparse.ArgumentParser(description="Forward backtest v2 with regime filter, ranking & multi-day holding.")
    parser.add_argument("--days", type=int, default=60)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--start", type=str, default="2025-04-01")
    parser.add_argument("--end", type=str, default="2026-04-06")
    parser.add_argument("--min-conf", type=float, default=40.0)
    parser.add_argument("--min-vol", type=float, default=0.5)
    parser.add_argument("--top-n", type=int, default=3, help="Max signals per day (default: 3)")
    parser.add_argument("--hold-days", type=int, default=5, help="Max hold days (default: 5)")
    parser.add_argument("--trail-pct", type=float, default=1.5, help="Trailing stop %% (default: 1.5)")
    parser.add_argument("--no-regime", action="store_true", help="Disable market regime filter")
    args = parser.parse_args()

    run_forward_backtest_v2(
        sample_days=max(1, int(args.days)),
        universe_limit=max(0, int(args.limit)),
        start=str(args.start),
        end=str(args.end),
        min_confidence=float(args.min_conf),
        min_volume_ratio=float(args.min_vol),
        top_n=max(1, int(args.top_n)),
        max_hold_days=max(1, int(args.hold_days)),
        trail_pct=float(args.trail_pct),
        use_regime_filter=not args.no_regime,
    )


if __name__ == "__main__":
    main()
