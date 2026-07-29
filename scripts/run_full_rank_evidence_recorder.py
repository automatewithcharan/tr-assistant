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
from pkscreener.classes.KiteTickerStream import load_latest_live_ticks  # noqa: E402
from pkscreener.classes.LiveFeatureRecorder import LiveFeatureRecorder  # noqa: E402


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
        payload = {
            "status": "recording_standalone_full_rank_evidence",
            "owner": "standalone",
            "date": now.strftime("%Y-%m-%d"),
            "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            "universe": self.universe,
            "tick_symbols": int(len(ticks)),
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
                payload = worker.cycle(
                    ticks,
                    now=now,
                    alerted_symbols=_alerted_symbols(output_dir, now),
                )
        if args.run_seconds > 0 and time.monotonic() - started >= args.run_seconds:
            break
        time.sleep(poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
