#!/usr/bin/env python3
"""Run full-rank paper evidence independently from the signal scanner."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PKDevTools.classes import Archiver  # noqa: E402
from PKDevTools.classes.PKDateUtilities import PKDateUtilities  # noqa: E402
from pkscreener.classes.FullUniverseRankContractMatrixObserver import (  # noqa: E402
    FullUniverseRankContractMatrixObserver,
)
from pkscreener.classes.FullUniverseRankEntryTimingObserver import (  # noqa: E402
    FullUniverseRankEntryTimingObserver,
)
from pkscreener.classes.FullUniverseRankPaperObserver import (  # noqa: E402
    FullUniverseRankPaperObserver,
)
from pkscreener.classes.FrozenMicrostructurePaperTrader import (  # noqa: E402
    FrozenMicrostructurePaperTrader,
    RANK_MIGRATION_MODEL_FEATURES,
)
from pkscreener.classes.FrozenRankDisplacementPaperTrader import (  # noqa: E402
    FrozenRankDisplacementPaperTrader,
)
from pkscreener.classes.KiteTickerStream import load_latest_live_ticks  # noqa: E402
from pkscreener.classes.KiteMarginEvidenceRecorder import (  # noqa: E402
    KiteMarginEvidenceRecorder,
)
from pkscreener.classes.LiveFeatureRecorder import LiveFeatureRecorder  # noqa: E402
from pkscreener.classes.MarginQualifiedLeverageObserver import (  # noqa: E402
    MarginQualifiedLeverageObserver,
)


def _truthy(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default) or default).strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
        "on",
    }


def _market_open(now: datetime) -> bool:
    minute = now.hour * 60 + now.minute
    return now.weekday() < 5 and 9 * 60 + 15 <= minute <= 15 * 60 + 30


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _alerted_symbols(output_dir: Path, now: datetime) -> set[str]:
    day = now.strftime("%Y-%m-%d")
    symbols: set[str] = set()
    for path in output_dir.glob(f"signalmonitor_*_{day}.txt"):
        try:
            symbols.update(
                line.strip().upper()
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
        except OSError:
            continue
    return symbols


def _depth_contract_health(
    ticks: dict[str, dict[str, Any]],
    *,
    minimum_coverage_pct: float = 80.0,
) -> dict[str, Any]:
    """Verify the shared snapshot preserved the full-mode depth container."""
    total = int(len(ticks or {}))
    structural = 0
    two_sided = 0
    for tick in (ticks or {}).values():
        if not isinstance(tick, dict):
            continue
        depth = tick.get("depth")
        if not isinstance(depth, dict):
            continue
        buy = depth.get("buy")
        sell = depth.get("sell")
        if not isinstance(buy, list) or not isinstance(sell, list):
            continue
        structural += 1
        if buy and sell:
            two_sided += 1
    coverage = structural * 100.0 / total if total > 0 else 0.0
    return {
        "tick_symbols": total,
        "depth_contract_symbols": structural,
        "two_sided_depth_symbols": two_sided,
        "depth_contract_coverage_pct": round(coverage, 4),
        "minimum_depth_contract_coverage_pct": float(
            minimum_coverage_pct
        ),
        "depth_contract_valid": bool(
            total > 0 and coverage >= float(minimum_coverage_pct)
        ),
    }


class StandaloneFullRankEvidenceWorker:
    """Advance paper-only recorders from the shared local tick snapshot."""

    def __init__(
        self,
        *,
        output_dir: Path,
        universe: str = "all",
        poll_seconds: float = 5.0,
        recorder: LiveFeatureRecorder | None = None,
        paper: FullUniverseRankPaperObserver | None = None,
        contracts: FullUniverseRankContractMatrixObserver | None = None,
        timing: FullUniverseRankEntryTimingObserver | None = None,
        frozen_ranker: FrozenMicrostructurePaperTrader | None = None,
        frozen_contracts: FullUniverseRankContractMatrixObserver | None = None,
        rank_migration_ranker: FrozenMicrostructurePaperTrader | None = None,
        rank_migration_contracts: FullUniverseRankContractMatrixObserver | None = None,
        stop_avoidance_ranker: FrozenMicrostructurePaperTrader | None = None,
        stop_avoidance_contracts: FullUniverseRankContractMatrixObserver | None = None,
        runner_qualified_ranker: FrozenMicrostructurePaperTrader | None = None,
        runner_qualified_contracts: FullUniverseRankContractMatrixObserver | None = None,
        rank_displacement_paper: FrozenRankDisplacementPaperTrader | None = None,
        margin_evidence_recorder: KiteMarginEvidenceRecorder | None = None,
        margin_leverage_observer: MarginQualifiedLeverageObserver | None = None,
        margin_leverage_contracts: FullUniverseRankContractMatrixObserver | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.universe = str(universe or "all").strip().lower()
        self.poll_seconds = max(1.0, float(poll_seconds or 5.0))
        interval = float(
            os.environ.get("SIGNALMONITOR_LIVE_FEATURE_INTERVAL_SECONDS", "15")
            or 15
        )
        stress = float(
            os.environ.get("SIGNALMONITOR_FULL_RANK_PAPER_STRESS_PCT", "0.10")
            or 0.10
        )
        capital = float(
            os.environ.get("SIGNALMONITOR_FULL_RANK_PAPER_CAPITAL", "500000")
            or 500000
        )
        target = float(
            os.environ.get("SIGNALMONITOR_FULL_RANK_PAPER_TARGET_PCT", "1.5")
            or 1.5
        )
        stop = float(
            os.environ.get("SIGNALMONITOR_FULL_RANK_PAPER_STOP_PCT", "1.0")
            or 1.0
        )
        horizon = float(
            os.environ.get(
                "SIGNALMONITOR_FULL_RANK_PAPER_HORIZON_MINUTES", "60"
            )
            or 60
        )
        self.recorder = recorder or LiveFeatureRecorder(
            output_dir=str(self.output_dir),
            interval_seconds=interval,
            top_gainer_pct=float(
                os.environ.get(
                    "SIGNALMONITOR_LIVE_FEATURE_TOP_GAINER_PCT", "5.0"
                )
                or 5.0
            ),
            stability_window=int(
                float(
                    os.environ.get(
                        "SIGNALMONITOR_LIVE_FEATURE_STABILITY_WINDOW", "3"
                    )
                    or 3
                )
            ),
            min_stable_bs_ratio=float(
                os.environ.get(
                    "SIGNALMONITOR_LIVE_FEATURE_MIN_STABLE_BS", "2.0"
                )
                or 2.0
            ),
            spread_ok_bps=float(
                os.environ.get(
                    "SIGNALMONITOR_LIVE_FEATURE_SPREAD_OK_BPS", "35"
                )
                or 35
            ),
            max_rows_per_cycle=int(
                float(
                    os.environ.get(
                        "SIGNALMONITOR_LIVE_FEATURE_MAX_ROWS_PER_CYCLE", "200"
                    )
                    or 200
                )
            ),
            leader_pool_size=int(
                float(
                    os.environ.get(
                        "SIGNALMONITOR_LIVE_FEATURE_LEADER_POOL_SIZE", "200"
                    )
                    or 200
                )
            ),
            rapid_rank_improvement=int(
                float(
                    os.environ.get(
                        "SIGNALMONITOR_LIVE_FEATURE_RAPID_RANK_IMPROVEMENT",
                        "25",
                    )
                    or 25
                )
            ),
            rapid_change_delta_pct=float(
                os.environ.get(
                    "SIGNALMONITOR_LIVE_FEATURE_RAPID_CHANGE_DELTA_PCT", "0.15"
                )
                or 0.15
            ),
        )
        self.paper = paper or FullUniverseRankPaperObserver(
            output_dir=str(self.output_dir),
            capital=capital,
            target_pct=target,
            stop_pct=stop,
            horizon_minutes=horizon,
            stress_pct=stress,
        )
        self.contracts = contracts or FullUniverseRankContractMatrixObserver(
            output_dir=str(self.output_dir),
            stress_pct=stress,
        )
        self.timing = timing or FullUniverseRankEntryTimingObserver(
            output_dir=str(self.output_dir),
            capital=capital,
            target_pct=target,
            stop_pct=stop,
            horizon_minutes=horizon,
            stress_pct=stress,
        )
        self.frozen_ranker = frozen_ranker
        if (
            self.frozen_ranker is None
            and _truthy(
                "SIGNALMONITOR_ENABLE_FROZEN_MICROSTRUCTURE_PAPER", "0"
            )
        ):
            model_path = Path(
                str(
                    os.environ.get(
                        "SIGNALMONITOR_FROZEN_MICROSTRUCTURE_MODEL",
                        self.output_dir
                        / "live_leader_continuation"
                        / "frozen_ranker_live_model.joblib",
                    )
                    or ""
                )
            )
            if model_path.is_file():
                self.frozen_ranker = FrozenMicrostructurePaperTrader(
                    output_dir=str(self.output_dir),
                    model_path=str(model_path),
                    capital=float(
                        os.environ.get(
                            "SIGNALMONITOR_FROZEN_PAPER_CAPITAL", "500000"
                        )
                        or 500000
                    ),
                    threshold=float(
                        os.environ.get(
                            "SIGNALMONITOR_FROZEN_PAPER_THRESHOLD", "0.20"
                        )
                        or 0.20
                    ),
                    max_trades_per_day=int(
                        float(
                            os.environ.get(
                                "SIGNALMONITOR_FROZEN_PAPER_MAX_TRADES", "10"
                            )
                            or 10
                        )
                    ),
                )
        self.frozen_contracts = frozen_contracts
        if self.frozen_contracts is None and self.frozen_ranker is not None:
            self.frozen_contracts = FullUniverseRankContractMatrixObserver(
                output_dir=str(self.output_dir),
                stress_pct=stress,
                root_name="frozen_ranker_prediction_contract_matrix",
                metadata_columns=[
                    "ExpectedReturnPct",
                    "MoveBand",
                    "ModelTrainedThrough",
                    "ModelSourceSha256",
                ],
            )
        self.rank_migration_ranker = rank_migration_ranker
        if (
            self.rank_migration_ranker is None
            and _truthy("SIGNALMONITOR_ENABLE_RANK_MIGRATION_PAPER", "1")
        ):
            rank_model_path = Path(
                str(
                    os.environ.get(
                        "SIGNALMONITOR_RANK_MIGRATION_MODEL",
                        self.output_dir
                        / "live_rank_migration"
                        / "frozen_rank_migration_model.joblib",
                    )
                    or ""
                )
            )
            if rank_model_path.is_file():
                self.rank_migration_ranker = FrozenMicrostructurePaperTrader(
                    output_dir=str(self.output_dir),
                    model_path=str(rank_model_path),
                    capital=capital,
                    threshold=float(
                        os.environ.get(
                            "SIGNALMONITOR_RANK_MIGRATION_THRESHOLD", "0.20"
                        )
                        or 0.20
                    ),
                    max_trades_per_day=10,
                    root_name="frozen_rank_migration_paper",
                    model_features=RANK_MIGRATION_MODEL_FEATURES,
                )
        self.rank_migration_contracts = rank_migration_contracts
        if (
            self.rank_migration_contracts is None
            and self.rank_migration_ranker is not None
        ):
            self.rank_migration_contracts = (
                FullUniverseRankContractMatrixObserver(
                    output_dir=str(self.output_dir),
                    stress_pct=stress,
                    root_name="frozen_rank_migration_contract_matrix",
                    metadata_columns=[
                        "ExpectedReturnPct",
                        "MoveBand",
                        "ModelTrainedThrough",
                        "ModelSourceSha256",
                    ],
                )
            )
        self.stop_avoidance_ranker = stop_avoidance_ranker
        if (
            self.stop_avoidance_ranker is None
            and _truthy("SIGNALMONITOR_ENABLE_STOP_AVOIDANCE_PAPER", "1")
        ):
            stop_avoidance_model = Path(
                str(
                    os.environ.get(
                        "SIGNALMONITOR_STOP_AVOIDANCE_MODEL",
                        self.output_dir
                        / "live_stop_avoidance"
                        / "frozen_stop_avoidance_model.joblib",
                    )
                    or ""
                )
            )
            if stop_avoidance_model.is_file():
                self.stop_avoidance_ranker = FrozenMicrostructurePaperTrader(
                    output_dir=str(self.output_dir),
                    model_path=str(stop_avoidance_model),
                    capital=capital,
                    threshold=0.78,
                    max_trades_per_day=5,
                    root_name="frozen_stop_avoidance_paper",
                    model_features=RANK_MIGRATION_MODEL_FEATURES,
                    model_artifact_type=(
                        "prior_session_rank_migration_stop_avoidance"
                    ),
                    source_name=(
                        "prior_session_rank_migration_stop_avoidance"
                    ),
                )
        self.stop_avoidance_contracts = stop_avoidance_contracts
        if (
            self.stop_avoidance_contracts is None
            and self.stop_avoidance_ranker is not None
        ):
            self.stop_avoidance_contracts = (
                FullUniverseRankContractMatrixObserver(
                    output_dir=str(self.output_dir),
                    stress_pct=stress,
                    root_name="frozen_stop_avoidance_contract_matrix",
                    metadata_columns=[
                        "ExpectedReturnPct",
                        "MoveBand",
                        "ModelTrainedThrough",
                        "ModelSourceSha256",
                    ],
                )
            )
        self.runner_qualified_ranker = runner_qualified_ranker
        if (
            self.runner_qualified_ranker is None
            and _truthy(
                "SIGNALMONITOR_ENABLE_RUNNER_QUALIFIED_STOP_PAPER", "1"
            )
        ):
            runner_qualified_model = Path(
                str(
                    os.environ.get(
                        "SIGNALMONITOR_RUNNER_QUALIFIED_STOP_MODEL",
                        self.output_dir
                        / "live_stop_avoidance"
                        / "frozen_runner_qualified_stop_model.joblib",
                    )
                    or ""
                )
            )
            if runner_qualified_model.is_file():
                self.runner_qualified_ranker = (
                    FrozenMicrostructurePaperTrader(
                        output_dir=str(self.output_dir),
                        model_path=str(runner_qualified_model),
                        capital=capital,
                        threshold=0.0,
                        max_trades_per_day=10,
                        root_name="frozen_runner_qualified_stop_paper",
                        model_features=RANK_MIGRATION_MODEL_FEATURES,
                        model_artifact_type=(
                            "prior_session_runner_qualified_stop_avoidance"
                        ),
                        source_name=(
                            "prior_session_runner_qualified_stop_avoidance"
                        ),
                    )
                )
        self.runner_qualified_contracts = runner_qualified_contracts
        if (
            self.runner_qualified_contracts is None
            and self.runner_qualified_ranker is not None
        ):
            self.runner_qualified_contracts = (
                FullUniverseRankContractMatrixObserver(
                    output_dir=str(self.output_dir),
                    stress_pct=stress,
                    root_name=(
                        "frozen_runner_qualified_stop_contract_matrix"
                    ),
                    metadata_columns=[
                        "ExpectedReturnPct",
                        "MoveBand",
                        "ModelTrainedThrough",
                        "ModelSourceSha256",
                    ],
                )
            )
        self.rank_displacement_paper = rank_displacement_paper
        if (
            self.rank_displacement_paper is None
            and self.stop_avoidance_ranker is not None
            and _truthy("SIGNALMONITOR_ENABLE_RANK_DISPLACEMENT_PAPER", "1")
        ):
            self.rank_displacement_paper = (
                FrozenRankDisplacementPaperTrader(
                    output_dir=self.output_dir,
                )
            )
        self.margin_evidence_recorder = margin_evidence_recorder
        if (
            self.margin_evidence_recorder is None
            and _truthy("SIGNALMONITOR_ENABLE_MARGIN_EVIDENCE", "1")
        ):
            self.margin_evidence_recorder = KiteMarginEvidenceRecorder(
                output_dir=self.output_dir,
                min_interval_seconds=float(
                    os.environ.get(
                        "SIGNALMONITOR_MARGIN_EVIDENCE_INTERVAL_SECONDS",
                        "60",
                    )
                    or 60
                ),
                max_batch_size=int(
                    float(
                        os.environ.get(
                            "SIGNALMONITOR_MARGIN_EVIDENCE_BATCH_SIZE",
                            "50",
                        )
                        or 50
                    )
                ),
                max_attempts_per_symbol=int(
                    float(
                        os.environ.get(
                            "SIGNALMONITOR_MARGIN_EVIDENCE_MAX_ATTEMPTS",
                            "3",
                        )
                        or 3
                    )
                ),
            )
        self.margin_leverage_observer = margin_leverage_observer
        if (
            self.margin_leverage_observer is None
            and self.margin_evidence_recorder is not None
        ):
            self.margin_leverage_observer = (
                MarginQualifiedLeverageObserver(
                    output_dir=self.output_dir,
                    lane_capital=250_000.0,
                    multipliers=(1.25, 1.50),
                )
            )
        self.margin_leverage_contracts = margin_leverage_contracts
        if (
            self.margin_leverage_contracts is None
            and self.margin_leverage_observer is not None
        ):
            self.margin_leverage_contracts = (
                FullUniverseRankContractMatrixObserver(
                    output_dir=str(self.output_dir),
                    stress_pct=stress,
                    root_name="frozen_margin_leverage_contract_matrix",
                    metadata_columns=[
                        "OriginalEventId",
                        "ExpectedReturnPct",
                        "MoveBand",
                        "ModelTrainedThrough",
                        "ModelSourceSha256",
                        "MarginQuotedAt",
                        "MarginPct",
                        "RequiredMarginEstimate",
                        "NotionalMultiplier",
                        "BaseLaneCapital",
                    ],
                )
            )
        self.status_path = (
            self.output_dir / "full_rank_evidence_runtime" / "status.json"
        )

    def cycle(
        self,
        ticks: dict[str, dict[str, Any]],
        *,
        now: datetime,
        alerted_symbols: set[str] | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        depth_health = _depth_contract_health(ticks)
        rows = self.recorder.record(
            ticks,
            alerted_symbols=alerted_symbols or set(),
            now=now,
        )
        if rows is None:
            rows = pd.DataFrame()
        paper_events = self.paper.process(rows, ticks, now=now)
        contract_events = self.contracts.process(paper_events, ticks, now=now)
        timing_events = self.timing.process(paper_events, ticks, now=now)
        frozen_events = []
        frozen_contract_events = []
        frozen_prediction_entries = []
        if self.frozen_ranker is not None:
            frozen_events = self.frozen_ranker.process(rows, ticks, now=now)
            frozen_prediction_entries = (
                self.frozen_ranker.contract_entry_events(ticks)
            )
        if self.frozen_contracts is not None:
            frozen_contract_events = self.frozen_contracts.process(
                frozen_prediction_entries,
                ticks,
                now=now,
            )
        rank_migration_events = []
        rank_migration_entries = []
        rank_migration_contract_events = []
        if self.rank_migration_ranker is not None:
            rank_migration_events = self.rank_migration_ranker.process(
                rows,
                ticks,
                now=now,
            )
            rank_migration_entries = (
                self.rank_migration_ranker.contract_entry_events(ticks)
            )
        if self.rank_migration_contracts is not None:
            rank_migration_contract_events = (
                self.rank_migration_contracts.process(
                    rank_migration_entries,
                    ticks,
                    now=now,
                )
            )
        stop_avoidance_events = []
        stop_avoidance_entries = []
        stop_avoidance_contract_events = []
        if self.stop_avoidance_ranker is not None:
            stop_avoidance_events = self.stop_avoidance_ranker.process(
                rows,
                ticks,
                now=now,
            )
            stop_avoidance_entries = (
                self.stop_avoidance_ranker.contract_entry_events(ticks)
            )
        if self.stop_avoidance_contracts is not None:
            stop_avoidance_contract_events = (
                self.stop_avoidance_contracts.process(
                    stop_avoidance_entries,
                    ticks,
                    now=now,
                )
            )
        runner_qualified_events = []
        runner_qualified_entries = []
        runner_qualified_contract_events = []
        if self.runner_qualified_ranker is not None:
            runner_qualified_events = self.runner_qualified_ranker.process(
                rows,
                ticks,
                now=now,
            )
            runner_qualified_entries = (
                self.runner_qualified_ranker.contract_entry_events(ticks)
            )
        if self.runner_qualified_contracts is not None:
            runner_qualified_contract_events = (
                self.runner_qualified_contracts.process(
                    runner_qualified_entries,
                    ticks,
                    now=now,
                )
            )
        rank_displacement_events = []
        if self.rank_displacement_paper is not None:
            rank_displacement_events = self.rank_displacement_paper.process(
                stop_avoidance_entries,
                ticks,
                now=now,
            )
        margin_evidence_rows = []
        if self.margin_evidence_recorder is not None:
            margin_evidence_rows = self.margin_evidence_recorder.process(
                stop_avoidance_entries + runner_qualified_entries,
                now=now,
            )
        margin_leverage_entries = []
        if self.margin_leverage_observer is not None:
            margin_leverage_entries = self.margin_leverage_observer.process(
                stop_avoidance_entries + runner_qualified_entries,
                margin_evidence_rows,
                ticks,
                now=now,
            )
        margin_leverage_contract_events = []
        if self.margin_leverage_contracts is not None:
            margin_leverage_contract_events = (
                self.margin_leverage_contracts.process(
                    margin_leverage_entries,
                    ticks,
                    now=now,
                )
            )
        payload = {
            "status": "recording_standalone_full_rank_evidence",
            "owner": "standalone",
            "date": now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "universe": self.universe,
            "tick_symbols": int(len(ticks)),
            **depth_health,
            "feature_rows": int(len(rows)),
            "paper_events": int(len(paper_events)),
            "contract_events": int(len(contract_events)),
            "timing_events": int(len(timing_events)),
            "frozen_ranker_enabled": self.frozen_ranker is not None,
            "frozen_ranker_events": int(len(frozen_events)),
            "frozen_prediction_entries": int(
                len(frozen_prediction_entries)
            ),
            "frozen_contract_events": int(len(frozen_contract_events)),
            "rank_migration_enabled": self.rank_migration_ranker is not None,
            "rank_migration_events": int(len(rank_migration_events)),
            "rank_migration_prediction_entries": int(
                len(rank_migration_entries)
            ),
            "rank_migration_contract_events": int(
                len(rank_migration_contract_events)
            ),
            "stop_avoidance_enabled": (
                self.stop_avoidance_ranker is not None
            ),
            "stop_avoidance_events": int(len(stop_avoidance_events)),
            "stop_avoidance_prediction_entries": int(
                len(stop_avoidance_entries)
            ),
            "stop_avoidance_contract_events": int(
                len(stop_avoidance_contract_events)
            ),
            "runner_qualified_stop_enabled": (
                self.runner_qualified_ranker is not None
            ),
            "runner_qualified_stop_events": int(
                len(runner_qualified_events)
            ),
            "runner_qualified_stop_prediction_entries": int(
                len(runner_qualified_entries)
            ),
            "runner_qualified_stop_contract_events": int(
                len(runner_qualified_contract_events)
            ),
            "rank_displacement_enabled": (
                self.rank_displacement_paper is not None
            ),
            "rank_displacement_events": int(
                len(rank_displacement_events)
            ),
            "margin_evidence_enabled": (
                self.margin_evidence_recorder is not None
            ),
            "margin_evidence_quotes_this_cycle": int(
                len(margin_evidence_rows)
            ),
            "margin_leverage_enabled": (
                self.margin_leverage_observer is not None
            ),
            "margin_leverage_entries_this_cycle": int(
                len(margin_leverage_entries)
            ),
            "margin_leverage_contract_events": int(
                len(margin_leverage_contract_events)
            ),
            "cycle_ms": round((time.monotonic() - started) * 1000.0, 2),
            "feature_interval_seconds": float(self.recorder.interval_seconds),
            "poll_interval_seconds": self.poll_seconds,
            "paper_only": True,
            "alerts_allowed": False,
            "real_orders_allowed": False,
        }
        _atomic_json(self.status_path, payload)
        return payload

    def stale_tick_status(self, *, now: datetime, age_seconds: float) -> None:
        """Publish a health state without advancing any evidence observer."""
        _atomic_json(
            self.status_path,
            {
                "status": "waiting_for_fresh_tick_snapshot",
                "owner": "standalone",
                "date": now.strftime("%Y-%m-%d"),
                "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "universe": self.universe,
                "tick_snapshot_age_seconds": (
                    round(age_seconds, 2)
                    if math.isfinite(age_seconds)
                    else None
                ),
                "feature_interval_seconds": float(self.recorder.interval_seconds),
                "poll_interval_seconds": self.poll_seconds,
                "paper_only": True,
                "alerts_allowed": False,
                "real_orders_allowed": False,
            },
        )

    def missing_depth_status(
        self,
        *,
        now: datetime,
        health: dict[str, Any],
    ) -> None:
        """Publish a fail-closed state without advancing evidence observers."""
        _atomic_json(
            self.status_path,
            {
                "status": "waiting_for_five_level_depth_snapshot",
                "owner": "standalone",
                "date": now.strftime("%Y-%m-%d"),
                "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
                "universe": self.universe,
                **health,
                "feature_interval_seconds": float(self.recorder.interval_seconds),
                "poll_interval_seconds": self.poll_seconds,
                "paper_only": True,
                "alerts_allowed": False,
                "real_orders_allowed": False,
            },
        )


def _tick_snapshot_age_seconds(
    output_dir: Path,
    universe: str,
    *,
    now_ts: float | None = None,
) -> float:
    path = output_dir / f"kiteticker_{universe}_ticks_latest.json"
    try:
        reference = time.time() if now_ts is None else float(now_ts)
        return max(0.0, reference - path.stat().st_mtime)
    except OSError:
        return float("inf")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--universe", default="all")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--run-seconds", type=float, default=0.0)
    args = parser.parse_args()
    poll_seconds = max(1.0, float(args.poll_seconds or 5.0))
    output_dir = Path(Archiver.get_user_data_dir())
    worker = StandaloneFullRankEvidenceWorker(
        output_dir=output_dir,
        universe=args.universe,
        poll_seconds=poll_seconds,
    )
    lock_path = output_dir / ".full_rank_evidence_runtime.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        import fcntl

        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (ImportError, BlockingIOError):
        print("Standalone full-rank evidence worker is already running.")
        return 0

    started = time.monotonic()
    while True:
        now = PKDateUtilities.currentDateTime()
        if _market_open(now):
            snapshot_age = _tick_snapshot_age_seconds(
                output_dir,
                args.universe,
            )
            if snapshot_age <= max(15.0, poll_seconds * 3.0):
                ticks = load_latest_live_ticks(args.universe)
            else:
                ticks = {}
                worker.stale_tick_status(now=now, age_seconds=snapshot_age)
            if ticks:
                depth_health = _depth_contract_health(ticks)
                if depth_health["depth_contract_valid"]:
                    payload = worker.cycle(
                        ticks,
                        now=now,
                        alerted_symbols=_alerted_symbols(output_dir, now),
                    )
                else:
                    worker.missing_depth_status(
                        now=now,
                        health=depth_health,
                    )
        if args.run_seconds > 0 and time.monotonic() - started >= args.run_seconds:
            break
        time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
