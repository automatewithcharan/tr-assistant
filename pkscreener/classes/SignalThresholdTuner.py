"""
Threshold tuning from recent-history validation output.

This module reads the recent-history validation ledger and searches for a
stricter-but-useful combination of confidence, volume ratio, and liquidity
thresholds for live Buy Now alerts.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

import pandas as pd

from PKDevTools.classes import Archiver
from PKDevTools.classes.PKDateUtilities import PKDateUtilities


class SignalThresholdTuner:
    """Recommend live Buy Now thresholds from recent validation history."""

    def __init__(self, universe: str = "nifty50", min_samples: int = 12) -> None:
        self.universe = str(universe or "nifty50").strip().lower()
        self.min_samples = max(3, int(min_samples or 12))
        self.output_dir = Archiver.get_user_data_dir()
        self.run_at = PKDateUtilities.currentDateTime().strftime("%Y-%m-%d %H:%M:%S")

    def tune(self) -> tuple[Optional[Dict], pd.DataFrame]:
        """Search recent validation history and save a recommended profile."""
        ledger_df = self._load_ledger()
        candidates_df = self._build_candidates(ledger_df)
        profile = self._select_profile(candidates_df)
        self._save_outputs(profile, candidates_df)
        return profile, candidates_df

    def _load_ledger(self) -> pd.DataFrame:
        """Load the latest validation ledger for the selected universe."""
        path = self._ledger_path()
        if not os.path.exists(path):
            return pd.DataFrame()
        try:
            return pd.read_csv(path)
        except Exception:
            return pd.DataFrame()

    def _build_candidates(self, ledger_df: pd.DataFrame) -> pd.DataFrame:
        """Build scored threshold candidates from buy-side validation rows."""
        if ledger_df is None or ledger_df.empty:
            return pd.DataFrame()

        df = ledger_df.copy()
        df["Direction"] = pd.to_numeric(df.get("Direction"), errors="coerce").fillna(0)
        df = df[df["Direction"] > 0].copy()
        if df.empty:
            return pd.DataFrame()

        for column in [
            "ConfidencePct",
            "VolumeRatio",
            "AvgTradeValue20",
            "Return1DPct",
            "Return3DPct",
            "Return5DPct",
            "BestReturn5DPct",
            "WorstReturn5DPct",
        ]:
            df[column] = pd.to_numeric(df.get(column), errors="coerce")

        df["Triggered5D"] = df.get("Triggered5D", False).fillna(False).astype(bool)
        df["Triggered3D"] = df.get("Triggered3D", False).fillna(False).astype(bool)
        df["Triggered1D"] = df.get("Triggered1D", False).fillna(False).astype(bool)

        min_samples = min(self.min_samples, max(3, len(df)))
        confidence_values = self._candidate_values(
            df["ConfidencePct"],
            default_values=[10.0, 12.5, 15.0, 18.0, 20.0, 25.0],
        )
        volume_values = self._candidate_values(
            df["VolumeRatio"],
            default_values=[0.8, 1.0, 1.2, 1.5, 2.0],
        )
        liquidity_values = self._candidate_values(
            df["AvgTradeValue20"],
            default_values=[5000000.0, 7500000.0, 10000000.0, 15000000.0, 25000000.0],
        )

        rows: List[Dict] = []
        for confidence in confidence_values:
            for volume_ratio in volume_values:
                for trade_value in liquidity_values:
                    filtered_df = df[
                        (df["ConfidencePct"] >= confidence)
                        & (df["VolumeRatio"] >= volume_ratio)
                        & (df["AvgTradeValue20"] >= trade_value)
                    ].copy()
                    if len(filtered_df) < min_samples:
                        continue
                    rows.append(
                        self._score_candidate(
                            filtered_df,
                            confidence=confidence,
                            volume_ratio=volume_ratio,
                            trade_value=trade_value,
                        )
                    )

        candidates_df = pd.DataFrame(rows)
        if not candidates_df.empty:
            candidates_df.sort_values(
                ["Score", "AvgReturn5DPct", "Samples5D"],
                ascending=[False, False, False],
                inplace=True,
            )
        return candidates_df

    def _score_candidate(
        self,
        filtered_df: pd.DataFrame,
        confidence: float,
        volume_ratio: float,
        trade_value: float,
    ) -> Dict:
        """Compute one candidate threshold row and its heuristic score."""
        samples_1d = self._sample_count(filtered_df["Return1DPct"])
        samples_3d = self._sample_count(filtered_df["Return3DPct"])
        samples_5d = self._sample_count(filtered_df["Return5DPct"])
        avg_1d = self._avg(filtered_df["Return1DPct"])
        avg_3d = self._avg(filtered_df["Return3DPct"])
        avg_5d = self._avg(filtered_df["Return5DPct"])
        win_5d = self._win_rate(filtered_df["Return5DPct"])
        trigger_5d = self._percentage(filtered_df["Triggered5D"].sum(), len(filtered_df))
        avg_best_5d = self._avg(filtered_df["BestReturn5DPct"])
        avg_worst_5d = self._avg(filtered_df["WorstReturn5DPct"])

        downside_penalty = abs(min(avg_worst_5d, 0.0))
        score = round(
            (avg_5d * 4.0)
            + (avg_3d * 2.0)
            + (avg_1d * 1.0)
            + ((win_5d / 100.0) * 2.0)
            + ((trigger_5d / 100.0) * 1.0)
            + (avg_best_5d * 0.5)
            - (downside_penalty * 1.5),
            4,
        )

        return {
            "MinConfidence": round(float(confidence), 2),
            "MinVolumeRatio": round(float(volume_ratio), 2),
            "MinTradeValue": round(float(trade_value), 2),
            "SignalsKept": int(len(filtered_df)),
            "Samples1D": samples_1d,
            "AvgReturn1DPct": avg_1d,
            "Samples3D": samples_3d,
            "AvgReturn3DPct": avg_3d,
            "Samples5D": samples_5d,
            "AvgReturn5DPct": avg_5d,
            "WinRate5DPct": win_5d,
            "TriggerRate5DPct": trigger_5d,
            "AvgBestReturn5DPct": avg_best_5d,
            "AvgWorstReturn5DPct": avg_worst_5d,
            "Score": score,
            "GeneratedAt": self.run_at,
        }

    def _select_profile(self, candidates_df: pd.DataFrame) -> Optional[Dict]:
        """Choose the best candidate threshold profile."""
        if candidates_df is None or candidates_df.empty:
            return None

        positive_df = candidates_df[
            (candidates_df["AvgReturn5DPct"] > 0)
            & (candidates_df["AvgReturn3DPct"] >= 0)
        ].copy()
        selected_df = positive_df if not positive_df.empty else candidates_df
        top_score = float(selected_df["Score"].max())
        shortlist_df = selected_df[selected_df["Score"] >= (top_score - 2.0)].copy()
        shortlist_df.sort_values(
            [
                "Samples5D",
                "SignalsKept",
                "MinTradeValue",
                "MinVolumeRatio",
                "MinConfidence",
                "AvgReturn5DPct",
                "Score",
            ],
            ascending=[False, False, True, True, True, False, False],
            inplace=True,
        )
        best = shortlist_df.iloc[0].to_dict()
        return {
            "universe": self.universe,
            "min_confidence": float(best["MinConfidence"]),
            "min_volume_ratio": float(best["MinVolumeRatio"]),
            "min_trade_value": float(best["MinTradeValue"]),
            "signals_kept": int(best["SignalsKept"]),
            "samples_5d": int(best["Samples5D"]),
            "avg_return_5d_pct": float(best["AvgReturn5DPct"]),
            "avg_return_3d_pct": float(best["AvgReturn3DPct"]),
            "win_rate_5d_pct": float(best["WinRate5DPct"]),
            "trigger_rate_5d_pct": float(best["TriggerRate5DPct"]),
            "avg_best_return_5d_pct": float(best["AvgBestReturn5DPct"]),
            "avg_worst_return_5d_pct": float(best["AvgWorstReturn5DPct"]),
            "score": float(best["Score"]),
            "generated_at": self.run_at,
            "notes": "Auto-tuned from recent validation history. Use as a stricter live starting point, not a guarantee.",
        }

    def _save_outputs(self, profile: Optional[Dict], candidates_df: pd.DataFrame) -> None:
        """Save the top candidate table and the latest selected profile."""
        timestamp = PKDateUtilities.currentDateTime().strftime("%Y%m%d_%H%M%S")
        top_df = candidates_df.head(25).copy() if candidates_df is not None and not candidates_df.empty else pd.DataFrame()

        top_latest = os.path.join(self.output_dir, f"signaltune_{self.universe}_latest.csv")
        top_dated = os.path.join(self.output_dir, f"signaltune_{self.universe}_{timestamp}.csv")
        top_df.to_csv(top_latest, index=False)
        top_df.to_csv(top_dated, index=False)

        if profile is None:
            return

        json_latest = os.path.join(self.output_dir, f"signalprofile_{self.universe}_latest.json")
        json_dated = os.path.join(self.output_dir, f"signalprofile_{self.universe}_{timestamp}.json")
        with open(json_latest, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, indent=2)
        with open(json_dated, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, indent=2)

    def _ledger_path(self) -> str:
        """Return the latest validation ledger path."""
        return os.path.join(self.output_dir, f"signalvalidate_{self.universe}_ledger_latest.csv")

    @classmethod
    def load_latest_profile(cls, universe: str = "nifty50") -> Optional[Dict]:
        """Load a saved threshold profile if one exists."""
        output_dir = Archiver.get_user_data_dir()
        path = os.path.join(output_dir, f"signalprofile_{str(universe or 'nifty50').strip().lower()}_latest.json")
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return None

    def _candidate_values(self, series: pd.Series, default_values: List[float]) -> List[float]:
        """Build a compact threshold candidate list from defaults and quantiles."""
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        values = {round(float(value), 2) for value in default_values if value == value}
        if not numeric.empty:
            for quantile in [0.2, 0.35, 0.5, 0.65, 0.8]:
                try:
                    values.add(round(float(numeric.quantile(quantile)), 2))
                except Exception:
                    pass
            values.add(round(float(numeric.min()), 2))
            values.add(round(float(numeric.median()), 2))
        return sorted(value for value in values if value >= 0)

    def _sample_count(self, series: pd.Series) -> int:
        """Count valid numeric samples."""
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        return int(len(numeric))

    def _avg(self, series: pd.Series) -> float:
        """Average valid numeric values."""
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if numeric.empty:
            return 0.0
        return round(float(numeric.mean()), 2)

    def _win_rate(self, series: pd.Series) -> float:
        """Win rate for positive returns."""
        numeric = pd.to_numeric(series, errors="coerce").dropna()
        if numeric.empty:
            return 0.0
        return round(float((numeric > 0).mean() * 100.0), 2)

    def _percentage(self, numerator, denominator) -> float:
        """Rounded percentage helper."""
        try:
            if float(denominator or 0.0) <= 0:
                return 0.0
            return round((float(numerator or 0.0) * 100.0) / float(denominator), 2)
        except Exception:
            return 0.0
