"""
Backtest evaluation module for measuring screener signal quality.

Provides systematic measurement of:
- Whether top-ranked stocks actually move after signal
- Time-to-signal vs move start
- Missed-winner analysis
- Scan latency impact
- False positive rate
- Performance by exchange, setup type, and score bucket

Works with the RankingEngine output format to evaluate signal effectiveness.
"""

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger("pkscreener")


@dataclass
class SignalOutcome:
    """Outcome record for a single screener signal."""
    symbol: str
    exchange: str
    signal_date: str
    setup_type: str
    composite_rank: float
    entry_price: float
    stop_level: float
    return_1d: Optional[float] = None
    return_3d: Optional[float] = None
    return_5d: Optional[float] = None
    max_favorable_excursion_5d: Optional[float] = None
    max_adverse_excursion_5d: Optional[float] = None
    triggered: bool = False
    score_bucket: str = ""


@dataclass
class EvaluationReport:
    """Aggregated evaluation report across all signals."""
    total_signals: int = 0
    trigger_rate: float = 0.0
    win_rate_1d: float = 0.0
    win_rate_3d: float = 0.0
    win_rate_5d: float = 0.0
    avg_return_1d: float = 0.0
    avg_return_3d: float = 0.0
    avg_return_5d: float = 0.0
    avg_mfe_5d: float = 0.0
    avg_mae_5d: float = 0.0
    false_positive_rate: float = 0.0
    by_exchange: Dict[str, Dict] = field(default_factory=dict)
    by_setup_type: Dict[str, Dict] = field(default_factory=dict)
    by_score_bucket: Dict[str, Dict] = field(default_factory=dict)


class BacktestEvaluator:
    """
    Evaluate screener signal quality using historical forward returns.

    Takes ranked output from the RankingEngine and measures:
    1. Signal accuracy (did it move in the expected direction?)
    2. Timeliness (did the move happen in 1d, 3d, 5d?)
    3. Quality (what was the max favorable/adverse excursion?)
    4. Segmentation (performance by exchange, setup type, score bucket)
    """

    SCORE_BUCKETS = [
        (80, 100, "A (80-100)"),
        (60, 80, "B (60-80)"),
        (40, 60, "C (40-60)"),
        (20, 40, "D (20-40)"),
        (0, 20, "F (0-20)"),
    ]

    def __init__(self, forward_days: int = 5):
        self.forward_days = max(1, forward_days)
        self.outcomes: List[SignalOutcome] = []

    def evaluate_signal(
        self,
        symbol: str,
        exchange: str,
        signal_date: str,
        setup_type: str,
        composite_rank: float,
        entry_price: float,
        stop_level: float,
        forward_data: pd.DataFrame,
    ) -> Optional[SignalOutcome]:
        """
        Evaluate a single signal against forward price data.

        Args:
            symbol: Stock symbol
            exchange: 'NSE' or 'BSE'
            signal_date: Date of the signal
            setup_type: e.g. 'breakout', 'momentum', 'volatility_expansion'
            composite_rank: CompositeRank score (0-100)
            entry_price: Signal entry price
            stop_level: Stop-loss level
            forward_data: OHLCV DataFrame for the forward period (1-5 days)
        """
        if forward_data is None or forward_data.empty or entry_price <= 0:
            return None

        close_col = "close" if "close" in forward_data.columns else "Close"
        high_col = "high" if "high" in forward_data.columns else "High"
        low_col = "low" if "low" in forward_data.columns else "Low"

        if close_col not in forward_data.columns:
            return None

        closes = forward_data[close_col].to_numpy(dtype=float)
        highs = forward_data[high_col].to_numpy(dtype=float) if high_col in forward_data.columns else closes
        lows = forward_data[low_col].to_numpy(dtype=float) if low_col in forward_data.columns else closes

        # Forward returns
        def _return_at(idx):
            if idx < len(closes) and closes[idx] > 0:
                return round((closes[idx] - entry_price) / entry_price * 100.0, 2)
            return None

        return_1d = _return_at(0)
        return_3d = _return_at(min(2, len(closes) - 1))
        return_5d = _return_at(min(4, len(closes) - 1))

        # Max favorable and adverse excursion
        window = min(5, len(highs))
        mfe = (
            round((float(np.max(highs[:window])) - entry_price) / entry_price * 100.0, 2)
            if window > 0 and entry_price > 0 else None
        )
        mae = (
            round((float(np.min(lows[:window])) - entry_price) / entry_price * 100.0, 2)
            if window > 0 and entry_price > 0 else None
        )

        # Triggered = entry price was reached (for buy signals: high >= entry)
        triggered = bool(np.any(highs[:window] >= entry_price)) if window > 0 else False

        # Score bucket
        score_bucket = self._get_score_bucket(composite_rank)

        outcome = SignalOutcome(
            symbol=symbol,
            exchange=exchange,
            signal_date=signal_date,
            setup_type=setup_type,
            composite_rank=composite_rank,
            entry_price=entry_price,
            stop_level=stop_level,
            return_1d=return_1d,
            return_3d=return_3d,
            return_5d=return_5d,
            max_favorable_excursion_5d=mfe,
            max_adverse_excursion_5d=mae,
            triggered=triggered,
            score_bucket=score_bucket,
        )
        self.outcomes.append(outcome)
        return outcome

    def generate_report(self) -> EvaluationReport:
        """Generate an aggregated evaluation report from all recorded outcomes."""
        if not self.outcomes:
            return EvaluationReport()

        total = len(self.outcomes)
        triggered = [o for o in self.outcomes if o.triggered]
        trigger_rate = len(triggered) / total * 100.0 if total > 0 else 0.0

        report = EvaluationReport(
            total_signals=total,
            trigger_rate=round(trigger_rate, 1),
            win_rate_1d=self._win_rate([o.return_1d for o in triggered]),
            win_rate_3d=self._win_rate([o.return_3d for o in triggered]),
            win_rate_5d=self._win_rate([o.return_5d for o in triggered]),
            avg_return_1d=self._avg([o.return_1d for o in triggered]),
            avg_return_3d=self._avg([o.return_3d for o in triggered]),
            avg_return_5d=self._avg([o.return_5d for o in triggered]),
            avg_mfe_5d=self._avg([o.max_favorable_excursion_5d for o in triggered]),
            avg_mae_5d=self._avg([o.max_adverse_excursion_5d for o in triggered]),
            false_positive_rate=self._false_positive_rate(triggered),
        )

        # Segment by exchange
        report.by_exchange = self._segment_by("exchange")

        # Segment by setup type
        report.by_setup_type = self._segment_by("setup_type")

        # Segment by score bucket
        report.by_score_bucket = self._segment_by("score_bucket")

        return report

    def to_dataframe(self) -> pd.DataFrame:
        """Convert all outcomes to a pandas DataFrame for analysis."""
        if not self.outcomes:
            return pd.DataFrame()

        records = []
        for o in self.outcomes:
            records.append({
                "Symbol": o.symbol,
                "Exchange": o.exchange,
                "SignalDate": o.signal_date,
                "SetupType": o.setup_type,
                "CompositeRank": o.composite_rank,
                "ScoreBucket": o.score_bucket,
                "EntryPrice": o.entry_price,
                "StopLevel": o.stop_level,
                "Triggered": o.triggered,
                "Return1D": o.return_1d,
                "Return3D": o.return_3d,
                "Return5D": o.return_5d,
                "MFE5D": o.max_favorable_excursion_5d,
                "MAE5D": o.max_adverse_excursion_5d,
            })
        return pd.DataFrame(records)

    def summary_text(self) -> str:
        """Return a human-readable summary of the evaluation."""
        report = self.generate_report()
        lines = [
            f"=== Backtest Evaluation Summary ===",
            f"Total signals: {report.total_signals}",
            f"Trigger rate: {report.trigger_rate:.1f}%",
            f"Win rate (1d/3d/5d): {report.win_rate_1d:.1f}% / {report.win_rate_3d:.1f}% / {report.win_rate_5d:.1f}%",
            f"Avg return (1d/3d/5d): {report.avg_return_1d:.2f}% / {report.avg_return_3d:.2f}% / {report.avg_return_5d:.2f}%",
            f"Avg MFE/MAE (5d): +{report.avg_mfe_5d:.2f}% / {report.avg_mae_5d:.2f}%",
            f"False positive rate: {report.false_positive_rate:.1f}%",
            "",
            "--- By Exchange ---",
        ]
        for name, stats in sorted(report.by_exchange.items()):
            lines.append(f"  {name}: {stats['count']} signals, WR5d={stats['win_rate_5d']:.1f}%, AvgRet5d={stats['avg_return_5d']:.2f}%")

        lines.append("")
        lines.append("--- By Setup Type ---")
        for name, stats in sorted(report.by_setup_type.items()):
            lines.append(f"  {name}: {stats['count']} signals, WR5d={stats['win_rate_5d']:.1f}%, AvgRet5d={stats['avg_return_5d']:.2f}%")

        lines.append("")
        lines.append("--- By Score Bucket ---")
        for name, stats in sorted(report.by_score_bucket.items()):
            lines.append(f"  {name}: {stats['count']} signals, WR5d={stats['win_rate_5d']:.1f}%, AvgRet5d={stats['avg_return_5d']:.2f}%")

        return "\n".join(lines)

    # ---- Internal helpers ----

    def _get_score_bucket(self, rank: float) -> str:
        for lo, hi, label in self.SCORE_BUCKETS:
            if lo <= rank <= hi:
                return label
        return "F (0-20)"

    def _win_rate(self, returns: List[Optional[float]]) -> float:
        valid = [r for r in returns if r is not None]
        if not valid:
            return 0.0
        return round(sum(1 for r in valid if r > 0) / len(valid) * 100.0, 1)

    def _avg(self, values: List[Optional[float]]) -> float:
        valid = [v for v in values if v is not None]
        if not valid:
            return 0.0
        return round(float(np.mean(valid)), 2)

    def _false_positive_rate(self, triggered: List[SignalOutcome]) -> float:
        if not triggered:
            return 0.0
        losers = sum(1 for o in triggered if o.return_5d is not None and o.return_5d < 0)
        return round(losers / len(triggered) * 100.0, 1)

    def _segment_by(self, field_name: str) -> Dict[str, Dict]:
        groups: Dict[str, List[SignalOutcome]] = defaultdict(list)
        for o in self.outcomes:
            key = getattr(o, field_name, "unknown")
            groups[key].append(o)

        result = {}
        for name, outcomes in groups.items():
            triggered = [o for o in outcomes if o.triggered]
            result[name] = {
                "count": len(outcomes),
                "triggered": len(triggered),
                "trigger_rate": round(len(triggered) / len(outcomes) * 100.0, 1) if outcomes else 0.0,
                "win_rate_1d": self._win_rate([o.return_1d for o in triggered]),
                "win_rate_3d": self._win_rate([o.return_3d for o in triggered]),
                "win_rate_5d": self._win_rate([o.return_5d for o in triggered]),
                "avg_return_1d": self._avg([o.return_1d for o in triggered]),
                "avg_return_3d": self._avg([o.return_3d for o in triggered]),
                "avg_return_5d": self._avg([o.return_5d for o in triggered]),
                "avg_composite_rank": self._avg([o.composite_rank for o in outcomes]),
            }
        return result

    def clear(self) -> None:
        """Reset all recorded outcomes."""
        self.outcomes.clear()
