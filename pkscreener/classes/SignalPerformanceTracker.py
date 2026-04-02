"""
Signal outcome ledger and scorecard for PKScreener live alerts.

This module turns fresh Buy Now / Sell / Exit alerts into a measurable ledger,
then updates outcome fields using official Kite historical candles so we can
see what actually worked after 15 minutes, 1 hour, end of day, and next day.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Dict, Optional

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger

from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider
from pkscreener.classes.KiteTickerStream import load_latest_live_ticks


class SignalPerformanceTracker:
    """Track and score fresh monitor alerts over time."""

    LEDGER_COLUMNS = [
        "AlertId",
        "AlertTime",
        "Universe",
        "Symbol",
        "SignalAction",
        "Direction",
        "EntryPrice",
        "TriggerPrice",
        "StopLoss",
        "Target1",
        "Target2",
        "SuggestedQty",
        "SignalStrengthPct",
        "Checks",
        "Why",
        "RegisteredAt",
        "CurrentPrice",
        "CurrentReturnPct",
        "Return15mPct",
        "Return60mPct",
        "ReturnEODPct",
        "ReturnNextDayPct",
        "StopHit",
        "Target1Hit",
        "Target2Hit",
        "OutcomeStatus",
        "LastEvaluatedAt",
    ]

    SCORECARD_COLUMNS = [
        "Segment",
        "TotalAlerts",
        "BuyAlerts",
        "SellAlerts",
        "OpenAlerts",
        "EvaluatedAlerts",
        "Samples15m",
        "WinRate15mPct",
        "AvgReturn15mPct",
        "Samples60m",
        "WinRate60mPct",
        "AvgReturn60mPct",
        "SamplesEOD",
        "WinRateEODPct",
        "AvgReturnEODPct",
        "SamplesNextDay",
        "WinRateNextDayPct",
        "AvgReturnNextDayPct",
        "AvgCurrentReturnPct",
        "StopHitRatePct",
        "Target1HitRatePct",
        "Target2HitRatePct",
        "LastUpdatedAt",
    ]

    def __init__(self, config_manager, universe: str = "nifty50", use_live_kite: bool = False) -> None:
        self.config_manager = config_manager
        self.universe = str(universe or "nifty50").strip().lower()
        self.use_live_kite = bool(use_live_kite)
        self.output_dir = Archiver.get_user_data_dir()
        self.run_at = PKDateUtilities.currentDateTime()
        self.run_at_str = self.run_at.strftime("%Y-%m-%d %H:%M:%S")
        self.history_provider = KiteHistoricalDataProvider()
        self.live_ticks = load_latest_live_ticks(self.universe) if self.use_live_kite else {}

    def register_and_update(self, alerts_df: Optional[pd.DataFrame] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Register fresh alerts, update outcomes, and save the scorecard."""
        ledger_df = self._load_ledger()
        if alerts_df is not None and not alerts_df.empty:
            ledger_df = self._register_alerts(ledger_df, alerts_df)
        ledger_df = self._update_outcomes(ledger_df)
        scorecard_df = self._build_scorecard(ledger_df)
        self._save_outputs(ledger_df, scorecard_df)
        return ledger_df, scorecard_df

    def _load_ledger(self) -> pd.DataFrame:
        """Load the existing ledger or return an empty one."""
        path = self._ledger_latest_path()
        if not os.path.exists(path):
            return pd.DataFrame(columns=self.LEDGER_COLUMNS)
        try:
            df = pd.read_csv(path)
        except Exception as exc:
            default_logger().debug(f"Could not load signal performance ledger: {exc}", exc_info=True)
            return pd.DataFrame(columns=self.LEDGER_COLUMNS)

        for column in self.LEDGER_COLUMNS:
            if column not in df.columns:
                df[column] = pd.NA
        return df[self.LEDGER_COLUMNS].copy()

    def _register_alerts(self, ledger_df: pd.DataFrame, alerts_df: pd.DataFrame) -> pd.DataFrame:
        """Append fresh alerts that are not already present in the ledger."""
        if alerts_df is None or alerts_df.empty:
            return ledger_df

        existing_ids = set(
            ledger_df["AlertId"].astype(str).tolist()
        ) if ledger_df is not None and not ledger_df.empty else set()

        new_rows = []
        for _, row in alerts_df.iterrows():
            action = str(row.get("NowAction", "")).strip()
            if action not in ["Strong Buy Now", "Buy Now", "Sell / Exit", "Sell Now"]:
                continue

            symbol = str(row.get("Symbol", "")).strip().upper()
            alert_time = str(row.get("When", "")).strip() or self.run_at_str
            alert_id = f"{self.universe}|{alert_time}|{symbol}|{action}"
            if alert_id in existing_ids:
                continue

            direction = self._direction_for_action(action)
            entry_price = self._to_float(row.get("PriceNow"))
            if entry_price <= 0:
                continue

            new_rows.append(
                {
                    "AlertId": alert_id,
                    "AlertTime": alert_time,
                    "Universe": self.universe,
                    "Symbol": symbol,
                    "SignalAction": action,
                    "Direction": direction,
                    "EntryPrice": entry_price,
                    "TriggerPrice": self._to_float(row.get("BuyAbove")),
                    "StopLoss": self._to_float(row.get("SellIfBelow")),
                    "Target1": self._to_float(row.get("TakeProfit1")),
                    "Target2": self._to_float(row.get("TakeProfit2")),
                    "SuggestedQty": int(self._to_float(row.get("SuggestedQty"), 0.0)),
                    "SignalStrengthPct": self._to_float(row.get("SignalStrengthPct")),
                    "Checks": str(row.get("Checks", "")).strip(),
                    "Why": str(row.get("Why", "")).strip(),
                    "RegisteredAt": self.run_at_str,
                    "CurrentPrice": pd.NA,
                    "CurrentReturnPct": pd.NA,
                    "Return15mPct": pd.NA,
                    "Return60mPct": pd.NA,
                    "ReturnEODPct": pd.NA,
                    "ReturnNextDayPct": pd.NA,
                    "StopHit": False,
                    "Target1Hit": False,
                    "Target2Hit": False,
                    "OutcomeStatus": "OPEN",
                    "LastEvaluatedAt": pd.NA,
                }
            )

        if not new_rows:
            return ledger_df

        new_df = pd.DataFrame(new_rows, columns=self.LEDGER_COLUMNS)
        if ledger_df is None or ledger_df.empty:
            return new_df
        return pd.concat([ledger_df, new_df], ignore_index=True)

    def _update_outcomes(self, ledger_df: pd.DataFrame) -> pd.DataFrame:
        """Update returns and outcome flags using official historical candles."""
        if ledger_df is None or ledger_df.empty:
            return pd.DataFrame(columns=self.LEDGER_COLUMNS)

        ledger_df = ledger_df.copy()
        for column in ["StopHit", "Target1Hit", "Target2Hit"]:
            ledger_df[column] = ledger_df[column].fillna(False).astype(bool)

        symbols = sorted(
            {
                str(symbol).strip().upper()
                for symbol in ledger_df["Symbol"].tolist()
                if str(symbol).strip()
            }
        )
        history_map: Dict[str, Optional[pd.DataFrame]] = {}

        for symbol in symbols:
            symbol_rows = ledger_df[ledger_df["Symbol"].astype(str).str.upper() == symbol].copy()
            if symbol_rows.empty:
                continue
            alert_times = pd.to_datetime(symbol_rows["AlertTime"], errors="coerce").dropna()
            if alert_times.empty:
                history_map[symbol] = None
                continue
            from_date = alert_times.min().to_pydatetime() - timedelta(minutes=5)
            to_date = datetime.now()
            history_map[symbol] = self.history_provider.get_history_range(
                symbol,
                interval="5minute",
                from_date=from_date,
                to_date=to_date,
            )

        for index in ledger_df.index:
            try:
                ledger_df.loc[index] = self._update_one_row(ledger_df.loc[index], history_map)
            except Exception as exc:
                default_logger().debug(f"Failed updating signal outcome row {index}: {exc}", exc_info=True)

        return ledger_df[self.LEDGER_COLUMNS].copy()

    def _update_one_row(self, row: pd.Series, history_map: Dict[str, Optional[pd.DataFrame]]) -> pd.Series:
        """Update a single ledger row from historical candles."""
        symbol = str(row.get("Symbol", "")).strip().upper()
        direction = int(self._to_float(row.get("Direction"), 0.0))
        entry_price = self._to_float(row.get("EntryPrice"))
        if not symbol or direction == 0 or entry_price <= 0:
            return row

        alert_time = pd.to_datetime(row.get("AlertTime"), errors="coerce")
        if pd.isna(alert_time):
            return row

        history_df = history_map.get(symbol)
        observed_df = history_df[history_df.index >= alert_time] if history_df is not None and not history_df.empty else pd.DataFrame()

        current_price = self._current_price(symbol, observed_df)
        if current_price is not None:
            row["CurrentPrice"] = current_price
            row["CurrentReturnPct"] = self._directional_return(entry_price, current_price, direction)

        row["Return15mPct"] = self._horizon_return(observed_df, alert_time + timedelta(minutes=15), entry_price, direction, row.get("Return15mPct"))
        row["Return60mPct"] = self._horizon_return(observed_df, alert_time + timedelta(minutes=60), entry_price, direction, row.get("Return60mPct"))
        row["ReturnEODPct"] = self._eod_return(observed_df, alert_time, entry_price, direction, row.get("ReturnEODPct"))
        row["ReturnNextDayPct"] = self._next_day_return(observed_df, alert_time, entry_price, direction, row.get("ReturnNextDayPct"))

        stop_hit, target1_hit, target2_hit = self._evaluate_hits(
            observed_df,
            direction,
            stop_loss=self._to_float(row.get("StopLoss")),
            target1=self._to_float(row.get("Target1")),
            target2=self._to_float(row.get("Target2")),
        )
        row["StopHit"] = bool(stop_hit)
        row["Target1Hit"] = bool(target1_hit)
        row["Target2Hit"] = bool(target2_hit)
        row["OutcomeStatus"] = self._derive_status(
            stop_hit=stop_hit,
            target1_hit=target1_hit,
            target2_hit=target2_hit,
            next_day_return=row.get("ReturnNextDayPct"),
        )
        row["LastEvaluatedAt"] = self.run_at_str
        return row

    def _current_price(self, symbol: str, observed_df: pd.DataFrame) -> Optional[float]:
        """Resolve the latest available price, preferring live tick snapshots when enabled."""
        if self.use_live_kite:
            tick = self.live_ticks.get(symbol)
            if tick:
                last_price = self._to_float(tick.get("last_price"))
                if last_price > 0:
                    return last_price
        if observed_df is not None and not observed_df.empty:
            return self._to_float(observed_df["close"].iloc[-1], None)
        return None

    def _horizon_return(
        self,
        observed_df: pd.DataFrame,
        target_time: datetime,
        entry_price: float,
        direction: int,
        existing_value,
    ):
        """Return directional performance for a minute-based horizon if enough data exists."""
        if observed_df is None or observed_df.empty:
            return existing_value
        future_df = observed_df[observed_df.index >= target_time]
        if future_df.empty:
            return existing_value
        price = self._to_float(future_df["close"].iloc[0], None)
        if price is None or price <= 0:
            return existing_value
        return self._directional_return(entry_price, price, direction)

    def _eod_return(self, observed_df: pd.DataFrame, alert_time: datetime, entry_price: float, direction: int, existing_value):
        """Return directional end-of-day performance for the alert day."""
        if observed_df is None or observed_df.empty:
            return existing_value
        day_df = observed_df[(observed_df.index.date == alert_time.date()) & (observed_df.index >= alert_time)]
        if day_df.empty:
            return existing_value
        price = self._to_float(day_df["close"].iloc[-1], None)
        if price is None or price <= 0:
            return existing_value
        return self._directional_return(entry_price, price, direction)

    def _next_day_return(self, observed_df: pd.DataFrame, alert_time: datetime, entry_price: float, direction: int, existing_value):
        """Return directional performance at the close of the next trading day."""
        if observed_df is None or observed_df.empty:
            return existing_value
        future_dates = sorted({ts.date() for ts in observed_df.index if ts.date() > alert_time.date()})
        if not future_dates:
            return existing_value
        next_day = future_dates[0]
        day_df = observed_df[observed_df.index.date == next_day]
        if day_df.empty:
            return existing_value
        price = self._to_float(day_df["close"].iloc[-1], None)
        if price is None or price <= 0:
            return existing_value
        return self._directional_return(entry_price, price, direction)

    def _evaluate_hits(self, observed_df: pd.DataFrame, direction: int, stop_loss: float, target1: float, target2: float):
        """Check whether stop or target levels were touched after the alert."""
        if observed_df is None or observed_df.empty:
            return False, False, False
        high_series = observed_df["high"] if "high" in observed_df.columns else observed_df["close"]
        low_series = observed_df["low"] if "low" in observed_df.columns else observed_df["close"]

        if direction > 0:
            stop_hit = bool(stop_loss > 0 and (low_series <= stop_loss).any())
            target1_hit = bool(target1 > 0 and (high_series >= target1).any())
            target2_hit = bool(target2 > 0 and (high_series >= target2).any())
        else:
            stop_hit = bool(stop_loss > 0 and (high_series >= stop_loss).any())
            target1_hit = bool(target1 > 0 and (low_series <= target1).any())
            target2_hit = bool(target2 > 0 and (low_series <= target2).any())
        return stop_hit, target1_hit, target2_hit

    def _derive_status(self, stop_hit: bool, target1_hit: bool, target2_hit: bool, next_day_return) -> str:
        """Classify the current outcome state conservatively."""
        if stop_hit and (target1_hit or target2_hit):
            return "AMBIGUOUS"
        if target2_hit:
            return "TARGET2"
        if target1_hit:
            return "TARGET1"
        if stop_hit:
            return "STOP"
        if not pd.isna(next_day_return):
            return "EVALUATED"
        return "OPEN"

    def _build_scorecard(self, ledger_df: pd.DataFrame) -> pd.DataFrame:
        """Summarize the ledger into practical win-rate and return metrics."""
        if ledger_df is None or ledger_df.empty:
            return pd.DataFrame(columns=self.SCORECARD_COLUMNS)

        rows = []
        all_df = ledger_df.copy()
        segments = [
            ("ALL", all_df),
            ("Strong Buy Now", all_df[all_df["SignalAction"].astype(str) == "Strong Buy Now"].copy()),
            ("Buy Now", all_df[all_df["Direction"] == 1].copy()),
            ("Sell / Exit", all_df[all_df["Direction"] == -1].copy()),
        ]

        for segment_name, segment_df in segments:
            if segment_df is None or segment_df.empty:
                continue
            rows.append(
                {
                    "Segment": segment_name,
                    "TotalAlerts": int(len(segment_df)),
                    "BuyAlerts": int((segment_df["Direction"] == 1).sum()),
                    "SellAlerts": int((segment_df["Direction"] == -1).sum()),
                    "OpenAlerts": int(segment_df["OutcomeStatus"].astype(str).str.upper().eq("OPEN").sum()),
                    "EvaluatedAlerts": int(segment_df["ReturnNextDayPct"].notna().sum()),
                    "Samples15m": int(segment_df["Return15mPct"].notna().sum()),
                    "WinRate15mPct": self._win_rate(segment_df["Return15mPct"]),
                    "AvgReturn15mPct": self._avg(segment_df["Return15mPct"]),
                    "Samples60m": int(segment_df["Return60mPct"].notna().sum()),
                    "WinRate60mPct": self._win_rate(segment_df["Return60mPct"]),
                    "AvgReturn60mPct": self._avg(segment_df["Return60mPct"]),
                    "SamplesEOD": int(segment_df["ReturnEODPct"].notna().sum()),
                    "WinRateEODPct": self._win_rate(segment_df["ReturnEODPct"]),
                    "AvgReturnEODPct": self._avg(segment_df["ReturnEODPct"]),
                    "SamplesNextDay": int(segment_df["ReturnNextDayPct"].notna().sum()),
                    "WinRateNextDayPct": self._win_rate(segment_df["ReturnNextDayPct"]),
                    "AvgReturnNextDayPct": self._avg(segment_df["ReturnNextDayPct"]),
                    "AvgCurrentReturnPct": self._avg(segment_df["CurrentReturnPct"]),
                    "StopHitRatePct": self._true_rate(segment_df["StopHit"]),
                    "Target1HitRatePct": self._true_rate(segment_df["Target1Hit"]),
                    "Target2HitRatePct": self._true_rate(segment_df["Target2Hit"]),
                    "LastUpdatedAt": self.run_at_str,
                }
            )

        scorecard_df = pd.DataFrame(rows, columns=self.SCORECARD_COLUMNS)
        return scorecard_df

    def _save_outputs(self, ledger_df: pd.DataFrame, scorecard_df: pd.DataFrame) -> None:
        """Persist the ledger and scorecard snapshots."""
        ledger_df.to_csv(self._ledger_latest_path(), index=False)
        scorecard_df.to_csv(self._scorecard_latest_path(), index=False)

        timestamp = self.run_at.strftime("%Y%m%d_%H%M%S")
        ledger_df.to_csv(
            os.path.join(self.output_dir, f"signalperf_{self.universe}_ledger_{timestamp}.csv"),
            index=False,
        )
        scorecard_df.to_csv(
            os.path.join(self.output_dir, f"signalperf_{self.universe}_scorecard_{timestamp}.csv"),
            index=False,
        )

    def _ledger_latest_path(self) -> str:
        return os.path.join(self.output_dir, f"signalperf_{self.universe}_ledger_latest.csv")

    def _scorecard_latest_path(self) -> str:
        return os.path.join(self.output_dir, f"signalperf_{self.universe}_scorecard_latest.csv")

    def _direction_for_action(self, action: str) -> int:
        normalized = str(action or "").strip().upper()
        if "BUY" in normalized:
            return 1
        if "SELL" in normalized:
            return -1
        return 0

    def _directional_return(self, entry_price: float, price: float, direction: int) -> Optional[float]:
        if entry_price <= 0 or price <= 0 or direction == 0:
            return None
        return round((((price - entry_price) * direction) / entry_price) * 100, 2)

    def _to_float(self, value, fallback: Optional[float] = 0.0) -> Optional[float]:
        try:
            if value in [None, "", "None"]:
                return fallback
            numeric = float(value)
            if numeric == numeric:
                return numeric
            return fallback
        except Exception:
            return fallback

    def _avg(self, series: pd.Series) -> float:
        clean = pd.to_numeric(series, errors="coerce").dropna()
        if clean.empty:
            return 0.0
        return round(float(clean.mean()), 2)

    def _win_rate(self, series: pd.Series) -> float:
        clean = pd.to_numeric(series, errors="coerce").dropna()
        if clean.empty:
            return 0.0
        return round(float((clean > 0).mean() * 100), 2)

    def _true_rate(self, series: pd.Series) -> float:
        clean = series.fillna(False).astype(bool)
        if clean.empty:
            return 0.0
        return round(float(clean.mean() * 100), 2)
