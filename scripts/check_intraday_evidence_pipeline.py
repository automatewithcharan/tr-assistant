#!/usr/bin/env python3
"""Operational readiness check for the paper-only intraday evidence pipeline."""

from __future__ import annotations

import argparse
import ast
import csv
import json
import os
import shutil
import subprocess
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
TRUTHY = {"1", "true", "yes", "y", "on"}
PREDECLARED_CAPACITY_POLICY_NAMES = {
    "opening_ranked_t0_8_s0_5",
    "early_rank_acceleration_t2_0_s1_0",
    "acceleration_t1_5_s1_0",
    "acceleration_t1_5_s1_5",
    "sector_rotation_t1_5_s1_0",
    "sector_rotation_t1_5_s1_5",
    "rapid_climber_t1_0_s1_0",
}


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def parse_ist(value: Any, naive_zone=IST) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=naive_zone).astimezone(IST) if parsed.tzinfo is None else parsed.astimezone(IST)
    except ValueError:
        return None


def is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def check(name: str, passed: bool, detail: str, critical: bool = True) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "critical": critical, "detail": detail}


def dict_assignment_size(path: Path, variable_name: str) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(target, ast.Name) and target.id == variable_name for target in targets):
            continue
        return len(node.value.keys) if isinstance(node.value, ast.Dict) else 0
    return 0


def dict_assignment_keys(path: Path, variable_name: str) -> set[str]:
    """Return literal string keys from a top-level dictionary assignment."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == variable_name
            for target in targets
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            return set()
        keys = {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        return keys if len(keys) == len(node.value.keys) else set()
    return set()


def sequence_assignment_size(path: Path, variable_name: str) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(
            isinstance(target, ast.Name) and target.id == variable_name
            for target in targets
        ):
            continue
        if isinstance(node.value, (ast.Tuple, ast.List, ast.Set)):
            return len(node.value.elts)
        return 0
    return 0


def live_rank_session_coverage(path: Path, session_date: str) -> dict[str, Any]:
    """Summarize cumulative recorder continuity without trusting latest status."""
    observations: list[tuple[datetime, int | None]] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                parsed = parse_ist(row.get("When"))
                if parsed is not None and parsed.strftime("%Y-%m-%d") == session_date:
                    try:
                        generation = int(float(str(row.get("RecorderGeneration", ""))))
                    except (TypeError, ValueError):
                        generation = None
                    observations.append((parsed, generation))
    except (OSError, csv.Error):
        pass
    generations = [generation for _, generation in observations if generation is not None]
    selected_generation = max(generations) if generations else None
    timestamps = {
        timestamp
        for timestamp, generation in observations
        if selected_generation is None or generation == selected_generation
    }
    ordered = sorted(timestamps)
    if not ordered:
        return {
            "recorder_generation": selected_generation,
            "snapshots": 0,
            "first_snapshot": None,
            "last_snapshot": None,
            "max_gap_seconds": float("inf"),
            "opening_covered": False,
            "continuity_ok": False,
        }
    gaps = [
        (current - previous).total_seconds()
        for previous, current in zip(ordered, ordered[1:])
    ]
    first = ordered[0]
    return {
        "recorder_generation": selected_generation,
        "snapshots": len(ordered),
        "first_snapshot": first,
        "last_snapshot": ordered[-1],
        "max_gap_seconds": max(gaps, default=0.0),
        "opening_covered": first.hour * 60 + first.minute <= 9 * 60 + 20,
        "continuity_ok": max(gaps, default=0.0) <= 45.0,
    }


def _service_active(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "--quiet", name], check=False, capture_output=True
    )
    return result.returncode == 0


def _timer_enabled(name: str) -> bool:
    result = subprocess.run(
        ["systemctl", "is-enabled", "--quiet", name], check=False, capture_output=True
    )
    return result.returncode == 0


def assess(
    root: Path,
    now: datetime,
    *,
    cron_text: str,
    service_active: bool,
    start_timer_enabled: bool,
    prewarm_timer_enabled: bool,
    disk_free_gb: float,
    disk_used_pct: float,
) -> dict[str, Any]:
    data = root / "results" / "Data"
    env = load_env(root / ".env.dev")
    checks: list[dict[str, Any]] = []
    weekday = now.weekday() < 5
    live_market = weekday and time(9, 15) <= now.time() <= time(15, 30)
    service_required = weekday and time(8, 45) <= now.time() <= time(15, 30)
    eod_due = weekday and now.time() >= time(16, 40)

    unsafe = {
        key: env.get(key, "0")
        for key in (
            "KITE_AUTO_TRADE",
            "ENABLE_GRADE_B_AUTOTRADE",
            "SIGNALMONITOR_AUTOTRADE_ENABLED",
            "SIGNALMONITOR_AUTOTRADE_REAL_ORDERS",
            "KITE_AUTO_BUY",
            "KITE_AUTO_SELL",
        )
        if is_truthy(env.get(key, "0"))
    }
    checks.append(check("real_orders_disabled", not unsafe, "all auto-order flags are off" if not unsafe else f"unsafe flags: {unsafe}"))
    service_ok = service_active or not service_required
    checks.append(check("trade_service_active", service_ok, "trade-assistant.service active" if service_active else "service inactive outside required collection window"))
    checks.append(check("start_timer_enabled", start_timer_enabled, "08:45 IST weekday start enabled" if start_timer_enabled else "trade-assistant.timer not enabled"))
    checks.append(check("prewarm_timer_enabled", prewarm_timer_enabled, "08:30 IST weekday prewarm enabled" if prewarm_timer_enabled else "prewarm timer not enabled"))
    checks.append(check("disk_headroom", disk_free_gb >= 10.0 and disk_used_pct < 85.0, f"{disk_free_gb:.1f} GB free, {disk_used_pct:.1f}% used"))

    required_files = [
        "pkscreener/classes/FullUniverseRankPaperObserver.py",
        "pkscreener/classes/FullUniverseRankContractMatrixObserver.py",
        "pkscreener/classes/FullUniverseRankEntryTimingObserver.py",
        "pkscreener/classes/KiteMarginEvidenceRecorder.py",
        "pkscreener/classes/MarginQualifiedLeverageObserver.py",
        "pkscreener/classes/FrozenRankDisplacementPaperTrader.py",
        "scripts/assess_exact_full_rank_paper.py",
        "scripts/assess_full_rank_contract_matrix.py",
        "scripts/assess_depth_executable_rank_strategy.py",
        "scripts/assess_predeclared_capacity_challenger.py",
        "scripts/assess_frozen_rank_migration_depth_forward.py",
        "scripts/assess_frozen_stop_avoidance_depth_forward.py",
        "scripts/assess_frozen_runner_qualified_stop_depth_forward.py",
        "scripts/assess_frozen_balanced_stop_portfolio_forward.py",
        "scripts/assess_frozen_balanced_margin_forward.py",
        "scripts/assess_frozen_rank_displacement_forward.py",
        "scripts/assess_historical_rank_displacement.py",
        "scripts/assess_fixed_rank_first_forward.py",
        "scripts/assess_full_rank_entry_timing.py",
        "scripts/assess_full_rank_feature_gates.py",
        "scripts/assess_full_rank_capital_allocation.py",
        "scripts/assess_full_rank_integrated_policies.py",
        "scripts/assess_full_rank_opportunity_ceiling.py",
        "scripts/assess_frozen_ranker_prediction_matrix.py",
        "scripts/assess_prior_session_ranker_paper.py",
        "scripts/build_intraday_strategy_evidence_scoreboard.py",
        "scripts/project_intraday_capital_growth.py",
        "scripts/run_full_rank_evidence_recorder.py",
        "scripts/live_evidence_eod.sh",
        "scripts/check_intraday_eod_evidence.py",
    ]
    missing = [value for value in required_files if not (root / value).is_file()]
    checks.append(check("required_code_present", not missing, "all evidence components present" if not missing else "missing: " + ", ".join(missing)))

    predeclared_path = root / "scripts/assess_predeclared_capacity_challenger.py"
    predeclared_names = dict_assignment_keys(predeclared_path, "POLICIES")
    predeclared_ok = predeclared_names == PREDECLARED_CAPACITY_POLICY_NAMES
    checks.append(
        check(
            "predeclared_capacity_registry",
            predeclared_ok,
            (
                "seven fixed paper policies declared"
                if predeclared_ok
                else f"expected {len(PREDECLARED_CAPACITY_POLICY_NAMES)} fixed policies, found {len(predeclared_names)}"
            ),
        )
    )

    eod_script = (root / "scripts/live_evidence_eod.sh").read_text(errors="ignore") if (root / "scripts/live_evidence_eod.sh").exists() else ""
    required_eod_calls = [
        "run_exact_full_rank_strategy",
        "run_full_rank_contract_matrix",
        "run_depth_executable_rank_strategy",
        "run_predeclared_capacity_challenger",
        "run_frozen_rank_migration_depth_forward",
        "run_frozen_stop_avoidance_depth_forward",
        "run_frozen_runner_qualified_stop_depth_forward",
        "run_frozen_balanced_stop_portfolio_forward",
        "run_frozen_balanced_margin_forward",
        "run_frozen_rank_displacement_forward",
        "run_historical_rank_displacement_audit",
        "run_fixed_rank_first_forward",
        "run_full_rank_entry_timing",
        "run_full_rank_feature_gates",
        "run_full_rank_capital_allocation",
        "run_full_rank_integrated_policies",
        "run_full_rank_opportunity_ceiling",
        "run_frozen_ranker_prediction_matrix",
        "run_prior_session_ranker_paper",
        "run_strategy_evidence_scoreboard",
        "run_intraday_capital_growth",
        "run_eod_integrity",
    ]
    absent_calls = [value for value in required_eod_calls if eod_script.count(value) < 2]
    checks.append(check("eod_judges_wired", not absent_calls, "all exact judges run after close" if not absent_calls else "missing calls: " + ", ".join(absent_calls)))
    cron_ok = "55 10 * * 1-5" in cron_text and "scripts/live_evidence_eod.sh" in cron_text
    checks.append(check("eod_schedule_installed", cron_ok, "16:25 IST weekday EOD judge installed" if cron_ok else "EOD evidence cron missing or stale"))

    ticker = load_json(data / "kiteticker_all_status_latest.json")
    ticker_at = parse_ist(
        ticker.get("last_tick_at") or ticker.get("last_save_at"),
        naive_zone=timezone.utc,
    )
    ticker_age = (now - ticker_at).total_seconds() if ticker_at else float("inf")
    ticker_ok = bool(ticker.get("connected")) and ticker_age <= 180 and int(ticker.get("snapshot_symbols") or 0) >= 2500
    checks.append(check("live_ticks_fresh", ticker_ok or not live_market, f"connected={bool(ticker.get('connected'))}, symbols={int(ticker.get('snapshot_symbols') or 0)}, age={ticker_age:.0f}s" + ("" if live_market else "; freshness not required outside live market")))
    ticker_started = parse_ist(ticker.get("started_at"), naive_zone=timezone.utc)
    ticker_uptime = (now - ticker_started).total_seconds() if ticker_started else 0.0
    ticker_continuous = ticker_uptime >= 300.0
    checks.append(
        check(
            "ticker_stream_continuity",
            ticker_continuous or not live_market,
            f"current stream uptime={ticker_uptime / 60.0:.1f}m"
            + ("" if live_market else "; continuity not required outside live market"),
        )
    )

    standalone_evidence = is_truthy(
        env.get("SIGNALMONITOR_STANDALONE_EVIDENCE", "1")
    )
    frozen_ranker_expected = is_truthy(
        env.get(
            "SIGNALMONITOR_ENABLE_FROZEN_MICROSTRUCTURE_PAPER", "1"
        )
    )
    evidence_runtime = load_json(
        data / "full_rank_evidence_runtime" / "status.json"
    )
    evidence_updated = parse_ist(evidence_runtime.get("updated_at"))
    evidence_age = (
        (now - evidence_updated).total_seconds()
        if evidence_updated
        else float("inf")
    )
    evidence_safe = (
        bool(evidence_runtime.get("paper_only"))
        and not bool(evidence_runtime.get("alerts_allowed"))
        and not bool(evidence_runtime.get("real_orders_allowed"))
    )
    evidence_worker_ok = (
        evidence_runtime.get("status")
        == "recording_standalone_full_rank_evidence"
        and evidence_runtime.get("owner") == "standalone"
        and str(evidence_runtime.get("date")) == now.strftime("%Y-%m-%d")
        and evidence_age <= 30.0
        and int(evidence_runtime.get("tick_symbols") or 0) >= 2500
        and bool(evidence_runtime.get("depth_contract_valid"))
        and float(
            evidence_runtime.get("depth_contract_coverage_pct") or 0.0
        ) >= 80.0
        and float(evidence_runtime.get("feature_interval_seconds") or 0) <= 20.0
        and 0.0 < float(evidence_runtime.get("poll_interval_seconds") or 0) <= 5.0
        and (
            not frozen_ranker_expected
            or bool(evidence_runtime.get("frozen_ranker_enabled"))
        )
        and evidence_safe
    )
    owner_ok = not standalone_evidence or evidence_worker_ok or not live_market
    owner_detail = (
        (
            f"standalone status={evidence_runtime.get('status') or 'missing'}, "
            f"owner={evidence_runtime.get('owner') or 'missing'}, "
            f"ticks={int(evidence_runtime.get('tick_symbols') or 0)}, "
            f"depth_contract={bool(evidence_runtime.get('depth_contract_valid'))}, "
            f"depth_coverage={float(evidence_runtime.get('depth_contract_coverage_pct') or 0):.1f}%, "
            f"age={evidence_age:.0f}s, "
            f"feature_interval={float(evidence_runtime.get('feature_interval_seconds') or 0):.1f}s, "
            f"poll={float(evidence_runtime.get('poll_interval_seconds') or 0):.1f}s, "
            f"frozen_ranker={bool(evidence_runtime.get('frozen_ranker_enabled'))}, "
            f"paper_only={evidence_safe}"
        )
        if standalone_evidence
        else "inline SignalMonitor evidence owner configured"
    )
    if standalone_evidence and not live_market:
        owner_detail += "; freshness not required outside live market"
    checks.append(
        check(
            "evidence_worker_owner",
            owner_ok,
            owner_detail,
        )
    )

    recorder = load_json(data / "live_feature_recorder/full_universe_rank_status.json")
    recorder_at = parse_ist(recorder.get("recorded_at"))
    recorder_age = (now - recorder_at).total_seconds() if recorder_at else float("inf")
    recorder_safe = not bool(recorder.get("alerts_allowed")) and not bool(recorder.get("real_orders_allowed"))
    recorder_ok = (
        recorder.get("status") == "recording_full_universe_rank"
        and str(recorder.get("date")) == now.strftime("%Y-%m-%d")
        and recorder_age <= 180
        and int(recorder.get("universe_size") or 0) >= 2500
        and int(recorder.get("rows_this_snapshot") or 0) >= 75
        and float(recorder.get("interval_seconds") or 0) <= 20
        and int(recorder.get("recorder_generation") or 0) == 6
        and int(recorder.get("leader_pool_size") or 0) >= 200
        and recorder_safe
    )
    checks.append(check("rank_recorder_fresh", recorder_ok or not live_market, f"universe={int(recorder.get('universe_size') or 0)}, rows={int(recorder.get('rows_this_snapshot') or 0)}, generation={int(recorder.get('recorder_generation') or 0)}, age={recorder_age:.0f}s, paper_only={recorder_safe}" + ("" if live_market else "; freshness not required outside live market")))

    leader_candidates = int(
        recorder.get("leader_pool_candidates_this_snapshot") or 0
    )
    leader_recorded = int(
        recorder.get("leader_pool_recorded_this_snapshot") or 0
    )
    leader_recall_ok = (
        leader_recorded == leader_candidates
        and float(recorder.get("leader_pool_recall_pct") or 0.0) == 100.0
    )
    checks.append(
        check(
            "rank_leader_pool_recall",
            leader_recall_ok or not live_market,
            (
                f"recorded={leader_recorded}/{leader_candidates}, "
                f"recall={float(recorder.get('leader_pool_recall_pct') or 0.0):.1f}%"
                + ("" if live_market else "; recall not required outside live market")
            ),
        )
    )

    rapid_candidates = int(recorder.get("rapid_candidates_this_snapshot") or 0)
    rapid_recorded = int(recorder.get("rapid_recorded_this_snapshot") or 0)
    rapid_recall_ok = (
        rapid_recorded == rapid_candidates
        and float(recorder.get("rapid_recall_pct") or 0.0) == 100.0
    )
    checks.append(
        check(
            "rank_rapid_climber_recall",
            rapid_recall_ok or not live_market,
            (
                f"recorded={rapid_recorded}/{rapid_candidates}, "
                f"recall={float(recorder.get('rapid_recall_pct') or 0.0):.1f}%"
                + ("" if live_market else "; recall not required outside live market")
            ),
        )
    )

    live_coverage = live_rank_session_coverage(
        data
        / "live_feature_recorder"
        / f"live_features_{now.strftime('%Y-%m-%d')}.csv",
        now.strftime("%Y-%m-%d"),
    )
    progress_start = now.replace(hour=9, minute=20, second=0, microsecond=0)
    progress_end = min(
        now,
        now.replace(hour=15, minute=10, second=0, microsecond=0),
    )
    progress_seconds = max(
        0.0, (progress_end - progress_start).total_seconds()
    )
    required_snapshots_so_far = min(
        1000,
        max(2, int(progress_seconds * 1000 / (350 * 60))),
    )
    last_snapshot_age = (
        (now - live_coverage["last_snapshot"]).total_seconds()
        if live_coverage["last_snapshot"] is not None
        else float("inf")
    )
    coverage_ok = (
        live_coverage["recorder_generation"] == 6
        and live_coverage["snapshots"] >= required_snapshots_so_far
        and live_coverage["opening_covered"]
        and live_coverage["continuity_ok"]
        and last_snapshot_age <= 45.0
    )
    first_snapshot = live_coverage["first_snapshot"]
    last_snapshot = live_coverage["last_snapshot"]
    checks.append(
        check(
            "rank_session_continuity_so_far",
            coverage_ok or not live_market,
            (
                f"snapshots={live_coverage['snapshots']}, "
                f"generation={live_coverage['recorder_generation'] or 'legacy'}, "
                f"first={first_snapshot.strftime('%H:%M:%S') if first_snapshot else 'missing'}, "
                f"last={last_snapshot.strftime('%H:%M:%S') if last_snapshot else 'missing'}, "
                f"max_gap={live_coverage['max_gap_seconds']:.0f}s, "
                f"required_so_far={required_snapshots_so_far}, "
                f"last_age={last_snapshot_age:.0f}s"
                + ("" if live_market else "; continuity not required outside live market")
            ),
        )
    )

    observers = {
        "exact_ask_bid_observer": ("full_universe_rank_paper/status.json", "recording_exact_ask_to_bid_paper_outcomes", None, None),
        "exit_contract_matrix": ("full_universe_rank_contract_matrix/status.json", "recording_exact_exit_contract_matrix", "contracts", 9),
        "entry_timing_matrix": ("full_universe_rank_entry_timing/status.json", "recording_exact_entry_timing_matrix", "variants", 6),
        "margin_leverage_contract_matrix": ("frozen_margin_leverage_contract_matrix/status.json", "recording_exact_exit_contract_matrix", "contracts", 9),
    }
    for name, (relative, expected_status, count_key, expected_count) in observers.items():
        status = load_json(data / relative)
        updated = parse_ist(status.get("updated_at"))
        age = (now - updated).total_seconds() if updated else float("inf")
        safe = bool(status.get("paper_only")) and not bool(status.get("alerts_allowed")) and not bool(status.get("real_orders_allowed"))
        count_ok = count_key is None or int(status.get(count_key) or 0) == expected_count
        schema_ok = int(status.get("execution_schema_version") or 0) >= 2
        passed = status.get("status") == expected_status and str(status.get("date")) == now.strftime("%Y-%m-%d") and age <= 180 and safe and count_ok and schema_ok
        count_detail = f", {count_key}={status.get(count_key)}" if count_key else ""
        checks.append(check(name, passed or not live_market, f"age={age:.0f}s, paper_only={safe}, execution_schema={status.get('execution_schema_version', 0)}{count_detail}" + ("" if live_market else "; freshness not required outside live market")))

    displacement = load_json(
        data / "frozen_stop_displacement_paper/status.json"
    )
    displacement_updated = parse_ist(
        displacement.get("last_heartbeat_at")
    )
    displacement_age = (
        (now - displacement_updated).total_seconds()
        if displacement_updated
        else float("inf")
    )
    displacement_safe = (
        bool(displacement.get("paper_only"))
        and not bool(displacement.get("alerts_allowed"))
        and not bool(displacement.get("real_orders_allowed"))
    )
    displacement_ok = bool(
        displacement.get("status")
        == "collecting_frozen_rank_displacement_paper"
        and str(displacement.get("date")) == now.strftime("%Y-%m-%d")
        and displacement_age <= 180
        and displacement_safe
        and int(displacement.get("execution_schema_version") or 0) >= 2
        and bool(displacement.get("requires_five_level_fillability"))
        and str(displacement.get("policy_version") or "")
        and str(displacement.get("policy_sha256") or "")
    )
    checks.append(
        check(
            "rank_displacement_paper",
            displacement_ok or not live_market,
            (
                f"age={displacement_age:.0f}s, paper_only={displacement_safe}, "
                f"policy={displacement.get('policy_version', 'missing')}, "
                f"execution_schema={displacement.get('execution_schema_version', 0)}"
                + (
                    ""
                    if live_market
                    else "; freshness not required outside live market"
                )
            ),
        )
    )

    matrix_counts = {
        "contracts": dict_assignment_size(root / "pkscreener/classes/FullUniverseRankContractMatrixObserver.py", "CONTRACTS"),
        "variants": dict_assignment_size(root / "pkscreener/classes/FullUniverseRankEntryTimingObserver.py", "ENTRY_VARIANTS"),
        "gates": dict_assignment_size(root / "scripts/assess_full_rank_feature_gates.py", "FEATURE_GATES"),
        "selectors": dict_assignment_size(root / "scripts/assess_exact_full_rank_paper.py", "SELECTORS"),
        "slot_counts": 4 if "SLOT_COUNTS = (1, 2, 3, 5)" in (root / "scripts/assess_full_rank_capital_allocation.py").read_text(errors="ignore") else 0,
        "integrated_policies": dict_assignment_size(root / "scripts/assess_full_rank_integrated_policies.py", "INTEGRATED_POLICIES"),
        "score_thresholds": sequence_assignment_size(
            root / "scripts/assess_frozen_ranker_prediction_matrix.py",
            "SCORE_THRESHOLDS",
        ),
        "depth_execution_combinations": (
            144
            if "PREDECLARED_COMBINATIONS = 144"
            in (
                root / "scripts/assess_depth_executable_rank_strategy.py"
            ).read_text(errors="ignore")
            else 0
        ),
    }
    matrix_ok = matrix_counts == {"contracts": 9, "variants": 6, "gates": 12, "selectors": 4, "slot_counts": 4, "integrated_policies": 10, "score_thresholds": 4, "depth_execution_combinations": 144}
    checks.append(check("predeclared_search_space", matrix_ok, "9 exit contracts, 6 timing variants, 12 feature gates, 4 selectors, 4 capital slot counts, 10 integrated policies, 4 frozen score thresholds, and 144 depth-execution combinations declared" if matrix_ok else "one or more predeclared matrices are incomplete"))

    eod = load_json(data / "intraday_evidence_pipeline" / "eod_latest.json")
    eod_status = str(eod.get("status") or "")
    eod_ok = (
        str(eod.get("date") or "") == now.strftime("%Y-%m-%d")
        and eod_status.startswith("complete_")
    )
    checks.append(
        check(
            "today_eod_evidence",
            eod_ok or not eod_due,
            (
                f"today EOD status={eod_status or 'missing'}"
                if eod_due
                else "EOD evidence not due until 16:40 IST"
            ),
        )
    )

    failures = [item for item in checks if item["critical"] and not item["passed"]]
    return {
        "status": "ready_for_next_full_session" if not failures else "operational_issues_detected",
        "checked_at": now.isoformat(timespec="seconds"),
        "market_phase": "live" if live_market else ("post_close" if now.time() > time(15, 30) else "pre_open"),
        "strategy_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "checks_passed": sum(1 for item in checks if item["passed"]),
        "checks_total": len(checks),
        "issues": [item["detail"] for item in failures],
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-issues", action="store_true")
    args = parser.parse_args()
    root = args.root.resolve()
    now = datetime.now(IST)
    cron = subprocess.run(["crontab", "-l"], text=True, capture_output=True, check=False).stdout
    usage = shutil.disk_usage(root)
    disk_free_gb = usage.free / (1024 ** 3)
    disk_used_pct = (usage.used / usage.total * 100.0) if usage.total else 100.0
    report = assess(
        root,
        now,
        cron_text=cron,
        service_active=_service_active("trade-assistant.service"),
        start_timer_enabled=_timer_enabled("trade-assistant.timer"),
        prewarm_timer_enabled=_timer_enabled("trade-assistant-prewarm.timer"),
        disk_free_gb=disk_free_gb,
        disk_used_pct=disk_used_pct,
    )
    output = args.output or root / "results/Data/intraday_evidence_pipeline/status.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"{report['status']}: {report['checks_passed']}/{report['checks_total']} checks passed")
    for item in report["checks"]:
        print(f"{'OK' if item['passed'] else 'FAIL'} {item['name']}: {item['detail']}")
    return 0 if report["status"] == "ready_for_next_full_session" or args.allow_issues else 1


if __name__ == "__main__":
    raise SystemExit(main())
