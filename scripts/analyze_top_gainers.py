"""
Multi-day top-gainer audit for Trade Assistant.

This script answers three practical questions:
1. Which symbols were the biggest close-to-close winners on each selected day?
2. What did the current daily PKScreener logic think about those names before the move?
3. What repeated traits show up across recent winners?
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

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
    symbols = [symbol for symbol in symbols if symbol]
    if limit > 0:
        symbols = symbols[:limit]
    return symbols


def _download_daily_prices(symbols: List[str], start: str, end: str) -> pd.DataFrame:
    tickers = " ".join(f"{symbol}.NS" for symbol in symbols)
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
    if raw_df.empty:
        return pd.DataFrame()
    if not isinstance(raw_df.columns, pd.MultiIndex):
        return pd.DataFrame()
    if ticker not in raw_df.columns.get_level_values(0):
        return pd.DataFrame()
    df = raw_df[ticker].copy().dropna(how="all")
    if df.empty:
        return pd.DataFrame()
    rename_map = {column: str(column).strip().lower() for column in df.columns}
    df = df.rename(columns=rename_map)
    keep = [column for column in ["open", "high", "low", "close", "volume"] if column in df.columns]
    df = df[keep].copy()
    df.index = pd.to_datetime(df.index)
    return df.dropna()


def _build_daily_gainers(raw_df: pd.DataFrame, symbols: List[str]) -> pd.DataFrame:
    rows: List[Dict] = []
    for symbol in symbols:
        history_df = _extract_symbol_history(raw_df, symbol)
        if len(history_df) < 2:
            continue
        sorted_df = history_df.sort_index()
        dates = list(sorted_df.index)
        for index in range(1, len(dates)):
            previous_row = sorted_df.iloc[index - 1]
            current_row = sorted_df.iloc[index]
            previous_close = float(previous_row["close"])
            current_open = float(current_row["open"])
            current_close = float(current_row["close"])
            current_high = float(current_row["high"])
            current_low = float(current_row["low"])
            if previous_close <= 0 or current_open <= 0:
                continue
            rows.append(
                {
                    "Date": dates[index].strftime("%Y-%m-%d"),
                    "Symbol": symbol,
                    "PrevClose": round(previous_close, 2),
                    "Open": round(current_open, 2),
                    "High": round(current_high, 2),
                    "Low": round(current_low, 2),
                    "Close": round(current_close, 2),
                    "Volume": float(current_row["volume"]),
                    "CloseToClosePct": round(((current_close - previous_close) * 100.0) / previous_close, 2),
                    "OpenToClosePct": round(((current_close - current_open) * 100.0) / current_open, 2),
                    "RangePct": round(((current_high - current_low) * 100.0) / previous_close, 2),
                }
            )
    return pd.DataFrame(rows)


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


def _analyze_pre_move_signal(
    runner: ActionableReportRunner,
    full_history_df: pd.DataFrame,
    target_date: str,
    symbol: str,
) -> Dict:
    target_ts = pd.Timestamp(target_date)
    prior_df = full_history_df[full_history_df.index < target_ts].copy()
    if len(prior_df) < 60:
        return {
            "SignalDate": "",
            "Action": "",
            "NowAction": "",
            "ConfidencePct": 0.0,
            "Entry": 0.0,
            "StopLoss": 0.0,
            "Target1": 0.0,
            "Target2": 0.0,
            "VolumeRatio": 0.0,
            "AvgTradeValue20": 0.0,
            "Reason": "Not enough history",
        }
    analyzed = runner.analyze_prepared_data(
        symbol=symbol,
        data=prior_df,
        run_at_text=(target_ts - pd.Timedelta(days=1)).strftime("%Y-%m-%d 15:30:00"),
        live_tick_used=False,
    )
    if analyzed is None:
        return {
            "SignalDate": "",
            "Action": "",
            "NowAction": "",
            "ConfidencePct": 0.0,
            "Entry": 0.0,
            "StopLoss": 0.0,
            "Target1": 0.0,
            "Target2": 0.0,
            "VolumeRatio": 0.0,
            "AvgTradeValue20": 0.0,
            "Reason": "No signal",
        }
    return {
        "SignalDate": str(analyzed.get("RunAt", "")),
        "Action": str(analyzed.get("Action", "")),
        "NowAction": str(analyzed.get("NowAction", "")),
        "ConfidencePct": float(analyzed.get("ConfidencePct", 0.0) or 0.0),
        "Entry": float(analyzed.get("Entry", 0.0) or 0.0),
        "StopLoss": float(analyzed.get("StopLoss", 0.0) or 0.0),
        "Target1": float(analyzed.get("Target1", 0.0) or 0.0),
        "Target2": float(analyzed.get("Target2", 0.0) or 0.0),
        "VolumeRatio": float(analyzed.get("VolumeRatio", 0.0) or 0.0),
        "AvgTradeValue20": float(analyzed.get("AvgTradeValue20", 0.0) or 0.0),
        "Reason": str(analyzed.get("Reason", "")),
    }


def run_audit(days: int, top_n: int, universe_limit: int, start: str, end: str) -> Dict[str, Path]:
    symbols = _load_universe_symbols(limit=universe_limit)
    raw_df = _download_daily_prices(symbols, start=start, end=end)
    gainers_df = _build_daily_gainers(raw_df, symbols)
    if gainers_df.empty:
        raise RuntimeError("No daily price data was available for the selected window.")

    available_dates = sorted(gainers_df["Date"].astype(str).unique().tolist())
    selected_dates = available_dates[-days:]
    filtered_gainers_df = gainers_df[gainers_df["Date"].isin(selected_dates)].copy()
    filtered_gainers_df.sort_values(["Date", "CloseToClosePct"], ascending=[False, False], inplace=True)
    filtered_gainers_df["DayRank"] = (
        filtered_gainers_df.groupby("Date")["CloseToClosePct"]
        .rank(method="first", ascending=False)
        .astype(int)
    )

    top_daily_df = filtered_gainers_df[filtered_gainers_df["DayRank"] <= top_n].copy()
    runner = _build_runner()

    audit_rows: List[Dict] = []
    for _, row in top_daily_df.iterrows():
        symbol = str(row["Symbol"]).strip().upper()
        symbol_history_df = _extract_symbol_history(raw_df, symbol)
        signal_info = _analyze_pre_move_signal(
            runner=runner,
            full_history_df=symbol_history_df,
            target_date=str(row["Date"]),
            symbol=symbol,
        )
        audit_rows.append(
            {
                **row.to_dict(),
                **signal_info,
                "WouldTriggerEntry": bool(signal_info.get("Entry", 0.0) > 0 and float(row["High"]) >= float(signal_info.get("Entry", 0.0))),
                "WouldCloseAboveEntry": bool(signal_info.get("Entry", 0.0) > 0 and float(row["Close"]) > float(signal_info.get("Entry", 0.0))),
            }
        )

    audit_df = pd.DataFrame(audit_rows)
    summary_rows: List[Dict] = []
    for date_text, date_df in audit_df.groupby("Date"):
        summary_rows.append(
            {
                "Date": date_text,
                "TopNamesChecked": int(len(date_df)),
                "BuySignals": int(date_df["NowAction"].astype(str).isin(["Strong Buy Now", "Buy Now"]).sum()),
                "TriggerReached": int(date_df["WouldTriggerEntry"].fillna(False).astype(bool).sum()),
                "ClosedAboveEntry": int(date_df["WouldCloseAboveEntry"].fillna(False).astype(bool).sum()),
                "MedianWinnerPct": round(float(date_df["CloseToClosePct"].median()), 2),
                "MedianWinnerClose": round(float(date_df["Close"].median()), 2),
            }
        )
    summary_df = pd.DataFrame(summary_rows).sort_values("Date", ascending=False)

    output_dir = Path(Archiver.get_user_data_dir())
    audit_path = output_dir / f"top_gainer_audit_{selected_dates[0]}_{selected_dates[-1]}.csv"
    summary_path = output_dir / f"top_gainer_audit_summary_{selected_dates[0]}_{selected_dates[-1]}.csv"
    audit_df.to_csv(audit_path, index=False)
    summary_df.to_csv(summary_path, index=False)

    print("Summary")
    print(summary_df.to_string(index=False))
    print("\nRecent rows")
    print(
        audit_df[
            [
                "Date",
                "DayRank",
                "Symbol",
                "CloseToClosePct",
                "NowAction",
                "ConfidencePct",
                "WouldTriggerEntry",
                "WouldCloseAboveEntry",
            ]
        ].sort_values(["Date", "DayRank"], ascending=[False, True]).head(40).to_string(index=False)
    )
    print(f"\nSaved detailed audit to: {audit_path}")
    print(f"Saved summary audit to: {summary_path}")
    return {"audit_path": audit_path, "summary_path": summary_path}


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit recent top gainers against current PKScreener daily signal logic.")
    parser.add_argument("--days", type=int, default=5, help="How many recent trading days to audit")
    parser.add_argument("--top", type=int, default=20, help="How many top gainers per day to inspect")
    parser.add_argument("--limit", type=int, default=0, help="Optional universe symbol limit for faster testing")
    parser.add_argument("--start", type=str, default="2025-11-01", help="Download window start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, default="2026-04-06", help="Download window end date (YYYY-MM-DD)")
    args = parser.parse_args()

    run_audit(
        days=max(1, int(args.days)),
        top_n=max(1, int(args.top)),
        universe_limit=max(0, int(args.limit)),
        start=str(args.start),
        end=str(args.end),
    )


if __name__ == "__main__":
    main()
