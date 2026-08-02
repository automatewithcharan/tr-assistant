#!/usr/bin/env python3
"""Untouched five-level judge for the frozen stop-avoidance selector."""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.assess_frozen_rank_migration_depth_forward as judge  # noqa: E402


judge.LANE_SOURCE = "prior_session_rank_migration_stop_avoidance"
judge.EVIDENCE_NAME = "stop_avoidance_five_level_depth_forward"
judge.SCORE_THRESHOLD = 0.78
judge.SELECTION_CUTOFF = "2026-07-29"
judge.FORWARD_START = "2026-07-30"
judge.MODEL_TRAINED_THROUGH = "2026-07-29"
judge.MAX_TRADES_PER_DAY = 5


if __name__ == "__main__":
    raise SystemExit(judge.main())
