"""
Recent-history validation for the current live signal logic.

This module replays the same actionable signal rules over recent daily candles
so we can estimate which setup types have been working before a long live track
record exists for the current user.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls
from PKDevTools.classes.PKDateUtilities import PKDateUtilities
from PKDevTools.classes.log import default_logger

from pkscreener.classes.ActionableReport import ActionableReportRunner
from pkscreener.classes.KiteHistoricalData import KiteHistoricalDataProvider


class HistoricalSignalValidator:
    """Validate current signal rules against recent daily history."""

    LEDGER_COLUMNS = [
        "SignalId",
        "SignalDate",
        "Universe",
        "Symbol",
        "Action",
        "NowAction",
        "Direction",
        "Signal",
        "ConfidencePct",
        "CloseAtSignal",
        "Entry",
        "StopLoss",
        "Target1",
        "Target2",
        "RiskPct",
        "VolumeRatio",
        "AvgTradeValue20",
        "QualityChecks",
        "Reason",
        "Triggered1D",
        "Triggered3D",
        "Triggered5D",
        "Return1DPct",
        "Return3DPct",
        "Return5DPct",
        "BestReturn3DPct",
        "WorstReturn3DPct",
        "BestReturn5DPct",
        "WorstReturn5DPct",
        "ValidatedAt",
    ]

    SCORECARD_COLUMNS = [
        "Segment",
        "TotalSignals",
        "BuySetups",
        "SellSetups",
        "TriggerRate1DPct",
        "TriggerRate3DPct",
        "TriggerRate5DPct",
        "Samples1D",
        "WinRate1DPct",
        "AvgReturn1DPct",
        "Samples3D",
        "WinRate3DPct",
        "AvgReturn3DPct",
        "Samples5D",
        "WinRate5DPct",
        "AvgReturn5DPct",
        "AvgBestReturn5DPct",
        "AvgWorstReturn5DPct",
        "AvgConfidencePct",
        "AvgVolumeRatio",
        "LastUpdatedAt",
    ]

    def __init__(
        self,
        config_manager,
        universe: str = "nifty50",
        lookback_days: int = 60,
        symbol_limit: int = 0,
        min_confidence: float = 15.0,
        min_trade_value: float = 10000000.0,
        min_volume_ratio: float = 1.0,
        max_workers: int = 2,
    ) -> None:
        self.config_manager = config_manager
        self.universe = str(universe or "nifty50").strip().lower()
        self.lookback_days = max(10, int(lookback_days or 60))
        self.symbol_limit = max(0, int(symbol_limit or 0))
        self.min_confidence = float(min_confidence)
        self.min_trade_value = float(min_trade_value)
        self.min_volume_ratio = float(min_volume_ratio)
        self.max_workers = max(1, int(max_workers or 1))
        self.output_dir = Archiver.get_user_data_dir()
        self.validated_at = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d %H:%M:%S")
        self.cutoff_date = pd.Timestamp(datetime.now() - timedelta(days=self.lookback_days)).tz_localize(None)
        self.history_provider = KiteHistoricalDataProvider()
        self.report_runner = ActionableReportRunner(
            config_manager=config_manager,
            universe=self.universe,
            limit=(self.symbol_limit if self.symbol_limit > 0 else 5000),
            min_confidence=self.min_confidence,
            budget_amount=None,
            risk_percent=1.0,
            min_trade_value=self.min_trade_value,
            min_volume_ratio=self.min_volume_ratio,
            max_workers=1,
            use_live_kite=False,
        )

    def run(self) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """Validate recent history and save a ledger plus scorecard."""
        symbols = self.report_runner.get_universe_symbols()
        if self.symbol_limit > 0:
            symbols = symbols[: self.symbol_limit]

        if not symbols:
            return pd.DataFrame(columns=self.LEDGER_COLUMNS), pd.DataFrame(columns=self.SCORECARD_COLUMNS)

        OutputControls().printOutput(
            f"  [+] Validating recent signal history for {len(symbols)} symbols from {self.universe}..."
        )

        rows = self._collect_rows(symbols)
        ledger_df = pd.DataFrame(rows, columns=self.LEDGER_COLUMNS) if rows else pd.DataFrame(columns=self.LEDGER_COLUMNS)
        if not ledger_df.empty:
            ledger_df.sort_values(["SignalDate", "Symbol"], ascending=[False, True], inplace=True)

        scorecard_df = self._build_scorecard(ledger_df)
        self._save_outputs(ledger_df, scorecard_df)
        return ledger_df, scorecard_df

    def _collect_rows(self, symbols: List[str]) -> List[Dict]:
        """Collect validation rows across the selected symbol list."""
        if self.max_workers <= 1 or len(symbols) <= 1:
            rows: List[Dict] = []
            for index, symbol in enumerate(symbols, start=1):
                rows.extend(self._validate_symbol(symbol))
                if index % 10 == 0 or index == len(symbols):
                    OutputControls().printOutput(
                        f"  [+] Validated {index}/{len(symbols)} symbols...", end="\r"
                    )
            OutputControls().printOutput("")
            return rows

        rows = []
        completed = 0
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_map = {
                executor.submit(self._validate_symbol, symbol): symbol
                for symbol in symbols
            }
            for future in as_completed(future_map):
                completed += 1
                try:
                    rows.extend(future.result())
                except Exception as exc:
                    symbol = future_map.get(future, "UNKNOWN")
                    default_logger().debug(
                        f"Historical validation failed for {symbol}: {exc}",
                        exc_info=True,
                    )
                if completed % 10 == 0 or completed == len(symbols):
                    OutputControls().printOutput(
                        f"  [+] Validated {completed}/{len(symbols)} symbols...", end="\r"
                    )
        OutputControls().printOutput("")
        return rows

    def _validate_symbol(self, symbol: str) -> List[Dict]:
        """Replay the current signal logic over recent daily candles for one symbol."""
        history_count = max(120, self.lookback_days + 90)
        history_df = self.history_provider.get_stock_data(symbol, interval="day", count=history_count)
        history_df = self.report_runner._normalize_if_ohlcv(history_df)
        if history_df is None or history_df.empty or len(history_df) < 70:
            return []

        rows: List[Dict] = []
        for current_index in range(59, len(history_df)):
            signal_time = pd.Timestamp(history_df.index[current_index])
            signal_time_naive = signal_time.tz_localize(None) if signal_time.tzinfo is not None else signal_time
            if signal_time_naive < self.cutoff_date:
                continue

            upto_df = history_df.iloc[: current_index + 1].copy()
            analyzed = self.report_runner.analyze_prepared_data(
                symbol=symbol,
                data=upto_df,
                run_at_text=signal_time_naive.strftime("%Y-%m-%d %H:%M:%S"),
                live_tick_used=False,
            )
            if analyzed is None:
                continue

            action = str(analyzed.get("Action", "")).strip().upper()
            direction = self._direction_for_action(action)
            if direction == 0:
                continue

            future_df = history_df.iloc[current_index + 1 : current_index + 6].copy()
            rows.append(self._build_validation_row(analyzed, future_df))
        return rows

    def _build_validation_row(self, analyzed: Dict, future_df: pd.DataFrame) -> Dict:
        """Build one historical-validation row from an analyzed setup."""
        action = str(analyzed.get("Action", "")).strip().upper()
        direction = self._direction_for_action(action)
        entry = self._to_float(analyzed.get("Entry"))

        triggered_1d, return_1d = self._horizon_result(future_df, direction, entry, 1)
        triggered_3d, return_3d = self._horizon_result(future_df, direction, entry, 3)
        triggered_5d, return_5d = self._horizon_result(future_df, direction, entry, 5)
        best_3d, worst_3d = self._excursions(future_df, direction, entry, 3, triggered_3d)
        best_5d, worst_5d = self._excursions(future_df, direction, entry, 5, triggered_5d)

        signal_date = str(analyzed.get("RunAt", "")).strip()
        now_action = str(analyzed.get("NowAction", "")).strip()
        return {
            "SignalId": f"{self.universe}|{signal_date}|{analyzed.get('Symbol')}|{action}|{now_action}",
            "SignalDate": signal_date,
            "Universe": self.universe,
            "Symbol": str(analyzed.get("Symbol", "")).strip().upper(),
            "Action": action,
            "NowAction": now_action,
            "Direction": direction,
            "Signal": str(analyzed.get("Signal", "")).strip(),
            "ConfidencePct": self._to_float(analyzed.get("ConfidencePct")),
            "CloseAtSignal": self._to_float(analyzed.get("LTP")),
            "Entry": entry,
            "StopLoss": self._to_float(analyzed.get("StopLoss")),
            "Target1": self._to_float(analyzed.get("Target1")),
            "Target2": self._to_float(analyzed.get("Target2")),
            "RiskPct": self._to_float(analyzed.get("RiskPct")),
            "VolumeRatio": self._to_float(analyzed.get("VolumeRatio")),
            "AvgTradeValue20": self._to_float(analyzed.get("AvgTradeValue20")),
            "QualityChecks": str(analyzed.get("QualityChecks", "")).strip(),
            "Reason": str(analyzed.get("Reason", "")).strip(),
            "Triggered1D": triggered_1d,
            "Triggered3D": triggered_3d,
            "Triggered5D": triggered_5d,
            "Return1DPct": return_1d,
            "Return3DPct": return_3d,
            "Return5DPct": return_5d,
            "BestReturn3DPct": best_3d,
            "WorstReturn3DPct": worst_3d,
            "BestReturn5DPct": best_5d,
            "WorstReturn5DPct": worst_5d,
            "ValidatedAt": self.validated_at,
        }

    def _horizon_result(
        self,
        future_df: pd.DataFrame,
        direction: int,
        entry: float,
        horizon_days: int,
    ) -> Tuple[bool, Optional[float]]:
        """Return trigger status and directional close return for a forward horizon."""
        if future_df is None or future_df.empty or direction == 0 or entry <= 0:
            return False, None

        window = future_df.iloc[:horizon_days].copy()
        if window.empty:
            return False, None

        triggered = self._entry_reached(window, direction, entry)
        if not triggered:
            return False, None

        exit_price = self._to_float(window["close"].iloc[-1])
        if exit_price <= 0:
            return True, None
        return True, self._directional_return(entry, exit_price, direction)

    def _excursions(
        self,
        future_df: pd.DataFrame,
        direction: int,
        entry: float,
        horizon_days: int,
        triggered: bool,
    ) -> Tuple[Optional[float], Optional[float]]:
        """Estimate best and worst directional excursion over a forward window."""
        if not triggered or future_df is None or future_df.empty or direction == 0 or entry <= 0:
            return None, None

        window = future_df.iloc[:horizon_days].copy()
        if window.empty:
            return None, None

        if direction > 0:
            best_price = self._to_float(window["high"].max())
            worst_price = self._to_float(window["low"].min())
        else:
            best_price = self._to_float(window["low"].min())
            worst_price = self._to_float(window["high"].max())

        return (
            self._directional_return(entry, best_price, direction) if best_price > 0 else None,
            self._directional_return(entry, worst_price, direction) if worst_price > 0 else None,
        )

    def _entry_reached(self, future_df: pd.DataFrame, direction: int, entry: float) -> bool:
        """Check whether the planned entry would have triggered in the forward window."""
        if future_df is None or future_df.empty or entry <= 0 or direction == 0:
            return False
        if direction > 0:
            return self._to_float(future_df["high"].max()) >= entry
        return self._to_float(future_df["low"].min()) <= entry

    def _build_scorecard(self, ledger_df: pd.DataFrame) -> pd.DataFrame:
        """Aggregate the ledger into a compact scorecard."""
        if ledger_df is None or ledger_df.empty:
            return pd.DataFrame(columns=self.SCORECARD_COLUMNS)

        ledger_df = ledger_df.copy()
        segments = {
            "ALL": ledger_df,
            "BUY SETUPS": ledger_df[ledger_df["Direction"] > 0],
            "SELL SETUPS": ledger_df[ledger_df["Direction"] < 0],
            "STRONG BUY NOW": ledger_df[ledger_df["NowAction"].astype(str) == "Strong Buy Now"],
            "BUY NOW": ledger_df[ledger_df["NowAction"].astype(str) == "Buy Now"],
            "SELL NOW": ledger_df[ledger_df["NowAction"].astype(str).isin(["Sell Now", "Sell / Exit"])],
        }
        for action_name in sorted(
            {
                str(action).strip()
                for action in ledger_df["Action"].tolist()
                if str(action).strip()
            }
        ):
            segments[action_name] = ledger_df[ledger_df["Action"].astype(str) == action_name]

        rows = [self._scorecard_row(segment_name, segment_df) for segment_name, segment_df in segments.items()]
        scorecard_df = pd.DataFrame(rows, columns=self.SCORECARD_COLUMNS)
        scorecard_df.sort_values(["Segment"], inplace=True)
        return scorecard_df

    def _scorecard_row(self, segment_name: str, segment_df: pd.DataFrame) -> Dict:
        """Build a single scorecard row for a segment."""
        if segment_df is None:
            segment_df = pd.DataFrame(columns=self.LEDGER_COLUMNS)

        total_signals = int(len(segment_df))
        return {
            "Segment": segment_name,
            "TotalSignals": total_signals,
            "BuySetups": int((segment_df["Direction"] > 0).sum()) if total_signals else 0,
            "SellSetups": int((segment_df["Direction"] < 0).sum()) if total_signals else 0,
            "TriggerRate1DPct": self._percentage(segment_df["Triggered1D"].sum(), total_signals),
            "TriggerRate3DPct": self._percentage(segment_df["Triggered3D"].sum(), total_signals),
            "TriggerRate5DPct": self._percentage(segment_df["Triggered5D"].sum(), total_signals),
            "Samples1D": self._sample_count(segment_df["Return1DPct"]),
            "WinRate1DPct": self._win_rate(segment_df["Return1DPct"]),
            "AvgReturn1DPct": self._avg(segment_df["Return1DPct"]),
            "Samples3D": self._sample_count(segment_df["Return3DPct"]),
            "WinRate3DPct": self._win_rate(segment_df["Return3DPct"]),
            "AvgReturn3DPct": self._avg(segment_df["Return3DPct"]),
            "Samples5D": self._sample_count(segment_df["Return5DPct"]),
            "WinRate5DPct": self._win_rate(segment_df["Return5DPct"]),
            "AvgReturn5DPct": self._avg(segment_df["Return5DPct"]),
            "AvgBestReturn5DPct": self._avg(segment_df["BestReturn5DPct"]),
            "AvgWorstReturn5DPct": self._avg(segment_df["WorstReturn5DPct"]),
            "AvgConfidencePct": self._avg(segment_df["ConfidencePct"]),
            "AvgVolumeRatio": self._avg(segment_df["VolumeRatio"]),
            "LastUpdatedAt": self.validated_at,
        }

    def _save_outputs(self, ledger_df: pd.DataFrame, scorecard_df: pd.DataFrame) -> None:
        """Save latest validator outputs and one daily snapshot after 3:20 PM IST."""
        import glob as _glob
        ledger_latest = os.path.join(self.output_dir, f"signalvalidate_{self.universe}_ledger_latest.csv")
        scorecard_latest = os.path.join(self.output_dir, f"signalvalidate_{self.universe}_scorecard_latest.csv")
        ledger_df.to_csv(ledger_latest, index=False)
        scorecard_df.to_csv(scorecard_latest, index=False)

        try:
            import pytz as _pytz
            _ist = _pytz.timezone("Asia/Kolkata")
            _now = datetime.now(_ist)
        except Exception:
            _now = datetime.now()
        if not (15 * 60 + 20 <= _now.hour * 60 + _now.minute <= 15 * 60 + 35):
            return
        today_stamp = _now.strftime("%Y%m%d")
        sentinel = os.path.join(self.output_dir, f"signalvalidate_{self.universe}_ledger_{today_stamp}.csv")
        if os.path.exists(sentinel):
            return
        ledger_df.to_csv(sentinel, index=False)
        scorecard_df.to_csv(
            os.path.join(self.output_dir, f"signalvalidate_{self.universe}_scorecard_{today_stamp}.csv"),
            index=False,
        )
        # Purge daily snapshots older than 30 days
        cutoff = datetime.now() - timedelta(days=30)
        for pattern in [f"signalvalidate_{self.universe}_ledger_????????.csv",
                        f"signalvalidate_{self.universe}_scorecard_????????.csv"]:
            for path in _glob.glob(os.path.join(self.output_dir, pattern)):
                parts = os.path.basename(path).replace(".csv", "").rsplit("_", 1)
                if len(parts) == 2 and len(parts[1]) == 8 and parts[1].isdigit():
                    try:
                        if datetime.strptime(parts[1], "%Y%m%d") < cutoff:
                            os.remove(path)
                    except Exception:
                        pass

    def _direction_for_action(self, action: str) -> int:
        """Map action text to long/short direction."""
        normalized = str(action or "").strip().upper()
        if normalized in ["BUY", "WATCH BUY"]:
            return 1
        if normalized in ["SELL/EXIT", "WATCH SELL"]:
            return -1
        return 0

    def _directional_return(self, entry_price: float, exit_price: float, direction: int) -> Optional[float]:
        """Return a positive percentage when the signal moved in its intended direction."""
        if entry_price <= 0 or exit_price <= 0 or direction == 0:
            return None
        raw_return = ((exit_price - entry_price) * 100.0) / entry_price
        return round(raw_return * direction, 2)

    def _sample_count(self, series: pd.Series) -> int:
        """Count valid numeric samples in a pandas series."""
        if series is None:
            return 0
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        return int(len(numeric))

    def _win_rate(self, series: pd.Series) -> float:
        """Compute win rate percentage for a return series."""
        if series is None:
            return 0.0
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if numeric.empty:
            return 0.0
        return round(float((numeric > 0).mean() * 100.0), 2)

    def _avg(self, series: pd.Series) -> float:
        """Compute a rounded mean with NaN safety."""
        if series is None:
            return 0.0
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if numeric.empty:
            return 0.0
        return round(float(numeric.mean()), 2)

    def _percentage(self, numerator, denominator) -> float:
        """Compute a rounded percentage."""
        try:
            numerator_value = float(numerator or 0.0)
            denominator_value = float(denominator or 0.0)
            if denominator_value <= 0:
                return 0.0
            return round((numerator_value * 100.0) / denominator_value, 2)
        except Exception:
            return 0.0

    def _to_float(self, value) -> float:
        """Best-effort numeric conversion."""
        try:
            if value in [None, "", "None"]:
                return 0.0
            return float(value)
        except Exception:
            return 0.0
