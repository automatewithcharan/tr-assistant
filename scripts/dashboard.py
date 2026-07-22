#!/usr/bin/env python3
"""Local status dashboard for tr-assistant.

Reads the REAL journals + REAL Kite positions directly — never the Discord
self-reports. One honest place to answer "is it working and where do I stand".

Run it:
    .venv/bin/python3 scripts/dashboard.py
    # then open http://localhost:8501 in a browser ON this machine

View it from your laptop securely (no public exposure, no firewall change):
    ssh -L 8501:localhost:8501 <user>@<vm-ip>
    # then open http://localhost:8501 on your laptop

By default it binds to 127.0.0.1 (local only). To expose on the VM's network
interface instead (understand the risk — this page shows your real positions):
    DASH_HOST=0.0.0.0 .venv/bin/python3 scripts/dashboard.py
"""
import csv
import json
import os
import statistics
import sys
import time
from datetime import datetime
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "results" / "Data"
IST = ZoneInfo("Asia/Kolkata")

HOST = os.environ.get("DASH_HOST", "127.0.0.1")
PORT = int(os.environ.get("DASH_PORT", "8501"))

# Lanes intentionally paused (cron disabled). Their journal is frozen history,
# not live measurement — shown greyed so a stale number isn't mistaken for a live one.
PAUSED_LANES = {"Intraday (Tier C)"}

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import live_alert_watch
except Exception:
    live_alert_watch = None

_WATCH_CACHE = {"ts": 0.0, "data": None}


def get_watch():
    """Live-alert watch, recomputed at most every 10 min (reads many files)."""
    if live_alert_watch is None:
        return None
    if _WATCH_CACHE["data"] is None or time.time() - _WATCH_CACHE["ts"] > 600:
        try:
            _WATCH_CACHE["data"] = live_alert_watch.compute_watch()
            _WATCH_CACHE["ts"] = time.time()
        except Exception:
            pass
    return _WATCH_CACHE["data"]


# Stage-1 gate for blind-buy: 30 closed trades accumulated FORWARD (i.e. closed
# on/after the freeze baseline). The 23 backfill-seed trades all exited by
# 2026-05-14, so this date cleanly separates seed from real forward evidence.
BLIND_BUY_JOURNAL = DATA / "blind_buy_alert_paper" / "journal.json"
FORWARD_START = "2026-05-28"
STAGE1_GATE = 30


def blind_buy_gate():
    j = load_json(BLIND_BUY_JOURNAL, {})
    trades = j.get("trades", [])
    closed = [t for t in trades if t.get("status") == "closed"]
    fwd = [t for t in closed if str(t.get("exit_date") or "") >= FORWARD_START]
    seed = [t for t in closed if str(t.get("exit_date") or "") < FORWARD_START]
    active = [t for t in trades if t.get("status") == "active"]
    fwd_rets = [t["net_return_pct"] for t in fwd if "net_return_pct" in t]
    seed_rets = [t["net_return_pct"] for t in seed if "net_return_pct" in t]
    return {
        "fwd": len(fwd),
        "gate": STAGE1_GATE,
        "active": len(active),
        "fwd_avg": (statistics.mean(fwd_rets) if fwd_rets else None),
        "fwd_wr": (sum(1 for r in fwd_rets if r > 0) / len(fwd_rets) * 100 if fwd_rets else None),
        "seed": len(seed),
        "seed_avg": (statistics.mean(seed_rets) if seed_rets else None),
    }


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return default


def load_csv_rows(path):
    try:
        with Path(path).open(newline="", encoding="utf-8") as handle:
            return list(csv.DictReader(handle))
    except Exception:
        return []


def truthy(value):
    return str(value or "").strip().lower() in {"1", "1.0", "true", "yes", "y", "on"}


def ranked_confirmation_stats():
    root = DATA / "ranked_confirmation_paper"
    rows = load_csv_rows(root / "trades_latest.csv")
    closed = [row for row in rows if truthy(row.get("closed"))]
    open_rows = [row for row in rows if not truthy(row.get("closed"))]
    pnls = [float(row.get("net_pnl") or 0.0) for row in closed]
    wins = sum(1 for value in pnls if value > 0)
    losses = sum(1 for value in pnls if value < 0)
    streak = 0
    for value in reversed(pnls):
        if value < 0:
            streak += 1
        else:
            break
    candidates = load_csv_rows(root / "candidates_latest.csv")
    accumulated = []
    for path in sorted(root.glob("trades_20*.csv")):
        accumulated.extend(load_csv_rows(path))
    if not accumulated:
        accumulated = list(rows)
    unique_accumulated = {}
    for row in accumulated:
        key = (
            str(row.get("date", "")),
            str(row.get("symbol", "")),
            str(row.get("entry_time", "")),
        )
        unique_accumulated[key] = row
    accumulated_closed = [
        row for row in unique_accumulated.values() if truthy(row.get("closed"))
    ]
    accumulated_pnls = [float(row.get("net_pnl") or 0.0) for row in accumulated_closed]
    accumulated_wins = sum(1 for value in accumulated_pnls if value > 0)
    return {
        "date": str(rows[0].get("date", "")) if rows else "",
        "closed": len(closed),
        "open": len(open_rows),
        "candidates": len(candidates),
        "wins": wins,
        "losses": losses,
        "win_rate": wins / len(closed) * 100.0 if closed else 0.0,
        "net_pnl": sum(pnls),
        "targets": sum(1 for row in closed if row.get("exit_reason") == "target_hit"),
        "stops": sum(1 for row in closed if row.get("exit_reason") == "stop_hit"),
        "consecutive_losses": streak,
        "entry_paused": streak >= 2,
        "trades": rows,
        "forward_days": len({str(row.get("date", "")) for row in accumulated_closed}),
        "forward_closed": len(accumulated_closed),
        "forward_wins": accumulated_wins,
        "forward_win_rate": (
            accumulated_wins / len(accumulated_closed) * 100.0
            if accumulated_closed else 0.0
        ),
        "forward_net_pnl": sum(accumulated_pnls),
    }


def evidence_readiness_stats():
    rows = load_csv_rows(DATA / "live_feature_recorder" / "evidence_readiness.csv")
    eligible = [row for row in rows if truthy(row.get("StrategyEligibleBucket"))]

    def number(row, field):
        try:
            return float(row.get(field) or 0.0)
        except Exception:
            return 0.0

    best = max(eligible, key=lambda row: number(row, "StressAvgDailyReturnPct"), default={})
    status_paths = sorted((DATA / "live_feature_recorder" / "eod").glob("*/status.json"))
    if not status_paths:
        status_paths = sorted((DATA / "live_feature_recorder" / "bootstrap").glob("*/status.json"))
    status = load_json(status_paths[-1], {}) if status_paths else {}
    return {
        "rows": len(rows),
        "sessions": max((int(number(row, "Sessions")) for row in rows), default=0),
        "shadow_candidates": sum(1 for row in rows if truthy(row.get("ShadowCandidate"))),
        "goal_passes": sum(1 for row in rows if truthy(row.get("GoalEvidencePassed"))),
        "best_bucket": str(best.get("FeatureBucket", "")),
        "best_contract": str(best.get("Contract", "")),
        "best_coverage": number(best, "CoveragePct"),
        "best_stress_return": number(best, "StressAvgDailyReturnPct"),
        "best_stress_pnl": number(best, "ProjectedStressDailyPnl"),
        "best_lower_bound": number(best, "StressReturnLower95Pct"),
        "coverage": float(
            status.get("promotion_coverage_pct")
            if status.get("promotion_coverage_pct") is not None
            else status.get("combined_coverage_pct") or 0.0
        ),
        "coverage_passed": bool(status.get("coverage_gate_passed", False)),
        "evidence_accepted": False,
        "status_date": str(status.get("date") or status.get("end_date") or ""),
    }


def ranked_replay_readiness_stats():
    rows = load_csv_rows(DATA / "ranked_confirmation_replay" / "readiness.csv")

    def number(row, field):
        try:
            return float(row.get(field) or 0.0)
        except Exception:
            return 0.0

    best = max(
        rows,
        key=lambda row: (
            truthy(row.get("ShadowCandidate")),
            number(row, "StressLower95DailyPnl"),
            number(row, "StressAvgDailyPnl"),
        ),
        default={},
    )
    return {
        "configs": len(rows),
        "complete_days": max((int(number(row, "CompleteDays")) for row in rows), default=0),
        "closed_trades": int(number(best, "ClosedTrades")),
        "wins": int(number(best, "Wins")),
        "best_config": str(best.get("ConfigName", "")),
        "stress_avg_daily_pnl": number(best, "StressAvgDailyPnl"),
        "stress_lower95_daily_pnl": number(best, "StressLower95DailyPnl"),
        "profit_factor": number(best, "ProfitFactor"),
        "shadow_candidates": sum(1 for row in rows if truthy(row.get("ShadowCandidate"))),
        "goal_passes": sum(1 for row in rows if truthy(row.get("GoalEvidencePassed"))),
        "paper_promotion_allowed": any(truthy(row.get("PaperPromotionAllowed")) for row in rows),
        "real_orders_allowed": False,
    }


def event_shadow_readiness_stats():
    summary = load_json(
        DATA / "intraday_event_shadow_rebuilt" / "model_v1" / "summary.json",
        {},
    )
    baseline = summary.get("frozen_heuristic_baseline", {}) or {}
    return {
        "sessions": int(summary.get("sessions") or 0),
        "events": int(summary.get("events") or 0),
        "trades": int(baseline.get("trades") or 0),
        "stress_avg_return": float(baseline.get("stress_avg_daily_return_pct") or 0.0),
        "stress_lower95": float(baseline.get("stress_lower95_daily_return_pct") or 0.0),
        "stress_avg_pnl": float(baseline.get("stress_avg_daily_pnl") or 0.0),
        "positive_sessions": float(baseline.get("positive_session_pct") or 0.0),
        "goal_hit_pct": float(baseline.get("daily_goal_hit_pct") or 0.0),
        "sample_gate_passed": bool(baseline.get("sample_gate_passed", False)),
        "performance_gate_passed": bool(baseline.get("performance_gate_passed", False)),
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
        "status": str(summary.get("status") or "no evidence"),
    }


def historical_transfer_stats():
    summary = load_json(
        DATA / "intraday_event_shadow_rebuilt" / "historical_transfer" / "holdout_summary.json",
        {},
    )
    return {
        "sessions": int(summary.get("sessions") or 0),
        "candidates": int(summary.get("scored_live_candidates") or 0),
        "trades": int(summary.get("traded_sessions") or 0),
        "avg_return": float(summary.get("avg_stressed_return_pct") or 0.0),
        "avg_pnl": float(summary.get("avg_stressed_pnl") or 0.0),
        "total_pnl": float(summary.get("total_stressed_pnl") or 0.0),
        "lower95": float(summary.get("lower_95_pct") or 0.0),
        "positive_sessions": float(summary.get("positive_session_pct") or 0.0),
        "goal_hit_pct": float(summary.get("goal_hit_pct") or 0.0),
        "paper_promotion_allowed": bool(summary.get("paper_promotion_allowed", False)),
        "real_orders_allowed": False,
        "historical_end": str((summary.get("temporal_boundary") or {}).get("historical_end") or "n/a"),
        "live_end": str((summary.get("temporal_boundary") or {}).get("live_end") or "n/a"),
    }


def event_exit_contract_stats():
    status = load_json(
        DATA / "intraday_event_shadow_rebuilt" / "exit_contracts" / "status.json",
        {},
    )
    rows = load_csv_rows(
        DATA / "intraday_event_shadow_rebuilt" / "exit_contracts" / "readiness.csv"
    )

    def number(row, field):
        try:
            return float(row.get(field) or 0.0)
        except Exception:
            return 0.0

    exploratory_best = max(rows, key=lambda row: number(row, "exploratory_avg_daily_pnl"), default={})
    forward_best = max(
        rows,
        key=lambda row: (
            truthy(row.get("paper_candidate")),
            number(row, "forward_lower_95_pct"),
            number(row, "forward_avg_daily_pnl"),
        ),
        default={},
    )
    return {
        "configs": int(status.get("config_count") or len(rows)),
        "sessions": int(status.get("sessions") or 0),
        "forward_sessions": int(status.get("forward_sessions") or 0),
        "paper_candidates": int(status.get("paper_candidates") or 0),
        "exploratory_best": str(exploratory_best.get("config") or "none"),
        "exploratory_avg_pnl": number(exploratory_best, "exploratory_avg_daily_pnl"),
        "exploratory_lower95": number(exploratory_best, "exploratory_lower_95_pct"),
        "forward_best": str(forward_best.get("config") or "none"),
        "forward_avg_pnl": number(forward_best, "forward_avg_daily_pnl"),
        "forward_lower95": number(forward_best, "forward_lower_95_pct"),
        "paper_promotion_allowed": any(truthy(row.get("paper_candidate")) for row in rows),
        "real_orders_allowed": False,
    }


def frozen_microstructure_stats():
    status = load_json(
        DATA / "live_feature_recorder" / "frozen_microstructure_selector_status.json",
        {},
    )
    selection = status.get("selection", {}) or {}
    forward = status.get("forward", {}) or {}
    stress = status.get("forward_stress", {}) or {}
    return {
        "selector": str(status.get("selector") or "none"),
        "contract": str(status.get("contract") or "none"),
        "forward_start": str(status.get("forward_start") or "n/a"),
        "forward_sessions": int(forward.get("sessions") or 0),
        "minimum_sessions": int(status.get("minimum_forward_sessions") or 20),
        "candidates": int(forward.get("candidates") or 0),
        "selection_avg_pnl": float(selection.get("avg_daily_pnl") or 0.0),
        "stress_avg_pnl": float(stress.get("avg_daily_pnl") or 0.0),
        "stress_lower95": float(stress.get("lower_95_pct") or 0.0),
        "selection_rejected": bool(status.get("selection_rejected", False)),
        "accepted": bool(status.get("accepted", False)),
        "paper_promotion_allowed": bool(status.get("paper_promotion_allowed", False)),
        "real_orders_allowed": False,
    }


def ignition_registry_stats():
    status = load_json(DATA / "live_microstructure_ignition" / "status.json", {})
    frozen = status.get("frozen_configs", []) or []
    return {
        "selection_end": str(status.get("selection_end") or "n/a"),
        "forward_start": str(status.get("forward_start") or "n/a"),
        "selection_sessions": int(status.get("selection_sessions") or 0),
        "forward_sessions": int(status.get("forward_sessions") or 0),
        "frozen_configs": len(frozen),
        "selection_rejected": bool(status.get("selection_rejected", False)),
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }


def leader_discovery_stats():
    recall = load_json(DATA / "live_top_gainer_recorder_recall" / "status.json", {})
    recall_rows = recall.get("summary", []) if isinstance(recall, dict) else []
    ten_pct = next(
        (
            row
            for row in recall_rows
            if float(row.get("RunnerThresholdPct") or 0.0) == 10.0
        ),
        {},
    )
    continuation = load_json(DATA / "live_leader_continuation" / "status.json", {})
    ranker = load_json(
        DATA / "live_leader_continuation" / "frozen_ranker_status.json", {}
    )
    live_paper = load_json(
        DATA / "frozen_microstructure_paper" / "status.json", {}
    )
    reclaim = load_json(
        DATA / "leader_pullback_reclaim_audit" / "status.json", {}
    )
    reclaim_stress = reclaim.get("goal_stress", {}) or {}
    rank_migration = load_json(
        DATA / "live_rank_migration" / "status.json", {}
    )
    rank_migration_diagnostic = rank_migration.get("diagnostic_stress", {}) or {}
    rank_migration_forward = rank_migration.get("forward_stress", {}) or {}
    full_rank_recorder = load_json(
        DATA / "live_feature_recorder" / "full_universe_rank_status.json", {}
    )
    full_rank_strategy = load_json(
        DATA / "full_universe_rank_strategy" / "status.json", {}
    )
    full_rank_paper = load_json(
        DATA / "full_universe_rank_paper" / "status.json", {}
    )
    full_rank_exact = load_json(
        DATA / "full_universe_rank_exact_strategy" / "status.json", {}
    )
    full_rank_exact_forward = full_rank_exact.get("forward", {}) or {}
    full_rank_forward = full_rank_strategy.get("forward_stress", {}) or {}
    ranker_model = ranker.get("live_model", {}) or {}
    prediction_paths = sorted(
        (DATA / "frozen_microstructure_paper").glob("predictions_20*.csv")
    ) if (DATA / "frozen_microstructure_paper").exists() else []
    live_predictions = load_csv_rows(prediction_paths[-1]) if prediction_paths else []
    ranker_stress = ranker.get("forward_stress", {}) or {}
    opportunity = continuation.get("clairvoyant_opportunity_bound", {}) or {}
    rows = load_csv_rows(DATA / "live_leader_continuation" / "summary.csv")
    snapshot_rows = load_csv_rows(
        DATA / "live_leader_continuation" / "snapshot_summary.csv"
    )

    def number(row, field):
        try:
            return float(row.get(field) or 0.0)
        except Exception:
            return 0.0

    def extension_row(phase):
        return next(
            (
                row
                for row in rows
                if row.get("EvidencePhase") == phase
                and row.get("Cohort") == "extension_acceleration"
                and row.get("Contract") == "t1.0_s1.0"
            ),
            {},
        )

    retrospective = extension_row("retrospective")
    forward = extension_row("frozen_forward")
    snapshot_retrospective = next(
        (
            row
            for row in snapshot_rows
            if row.get("EvidencePhase") == "retrospective"
            and row.get("Cohort") == "extension_acceleration"
            and row.get("Contract") == "t1.0_s1.0"
        ),
        {},
    )
    return {
        "recall_sessions": int(recall.get("sessions") or 0),
        "runner_events_10": int(number(ten_pct, "RunnerEvents")),
        "seen_before_10_pct": number(ten_pct, "FeatureSeenBeforeCrossPct"),
        "band_0_5_to_3_recall": number(ten_pct, "Band0_5To3RecallPct"),
        "band_0_5_to_5_recall": number(ten_pct, "Band0_5To5RecallPct"),
        "median_lead_minutes": number(ten_pct, "MedianLeadMinutes"),
        "forward_start": str(continuation.get("frozen_forward_start") or "n/a"),
        "evaluated_states": int(continuation.get("evaluated_candidate_states") or 0),
        "forward_states": int(continuation.get("forward_candidate_states") or 0),
        "retrospective_evaluated": int(number(retrospective, "Evaluated")),
        "retrospective_target_hit_pct": number(retrospective, "TargetHitPct"),
        "retrospective_avg_return_pct": number(retrospective, "AvgNetReturnPct"),
        "retrospective_lower95_pct": number(
            retrospective, "Lower95DailyEqualReturnPct"
        ),
        "snapshot_evaluated": int(number(snapshot_retrospective, "Evaluated")),
        "snapshot_target_hit_pct": number(snapshot_retrospective, "TargetHitPct"),
        "snapshot_avg_return_pct": number(snapshot_retrospective, "AvgNetReturnPct"),
        "snapshot_lower95_pct": number(
            snapshot_retrospective, "Lower95DailyEqualReturnPct"
        ),
        "selection_rejected": bool(
            continuation.get("extension_acceleration_selection_rejected", True)
        ),
        "opportunity_contract": str(opportunity.get("Contract") or "none"),
        "opportunity_avg_pnl": number(opportunity, "AvgPerfectOneSlotPnl"),
        "opportunity_lower95_pnl": number(
            opportunity, "Lower95PerfectOneSlotPnl"
        ),
        "opportunity_goal_hit_pct": number(opportunity, "GoalHitPct"),
        "forward_evaluated": int(number(forward, "Evaluated")),
        "forward_target_hit_pct": number(forward, "TargetHitPct"),
        "forward_avg_return_pct": number(forward, "AvgNetReturnPct"),
        "forward_lower95_pct": number(forward, "Lower95DailyEqualReturnPct"),
        "ranker_status": str(ranker.get("status") or "pending"),
        "ranker_contract": str(ranker.get("contract") or "none"),
        "ranker_forward_start": str(ranker.get("forward_start") or "n/a"),
        "ranker_forward_sessions": int(number(ranker_stress, "sessions")),
        "ranker_forward_trades": int(number(ranker_stress, "trades")),
        "ranker_stress_avg_pnl": number(ranker_stress, "avg_daily_pnl"),
        "ranker_stress_lower95_pct": number(ranker_stress, "lower_95_pct"),
        "ranker_stress_goal_hit_pct": number(ranker_stress, "goal_hit_pct"),
        "ranker_accepted": bool(ranker.get("accepted", False)),
        "live_paper_status": str(
            live_paper.get("status")
            or ("artifact ready" if ranker_model else "awaiting artifact")
        ),
        "live_paper_date": str(
            live_paper.get("date") or ranker_model.get("valid_from") or "n/a"
        ),
        "live_paper_model_through": str(
            live_paper.get("model_trained_through")
            or ranker_model.get("trained_through")
            or "n/a"
        ),
        "live_paper_predictions": len(live_predictions),
        "live_paper_max_score": max(
            (number(row, "expected_return_pct") for row in live_predictions),
            default=0.0,
        ),
        "live_paper_trades": int(live_paper.get("trades") or 0),
        "live_paper_closed": int(live_paper.get("closed_trades") or 0),
        "live_paper_net_pnl": number(live_paper, "net_pnl"),
        "reclaim_status": str(reclaim.get("status") or "pending"),
        "reclaim_sessions": int(number(reclaim_stress, "sessions")),
        "reclaim_trades": int(number(reclaim_stress, "trades")),
        "reclaim_stress_avg_pnl": number(reclaim_stress, "avg_daily_pnl"),
        "reclaim_stress_lower95_pct": number(reclaim_stress, "lower_95_pct"),
        "reclaim_positive_session_pct": number(
            reclaim_stress, "positive_session_pct"
        ),
        "reclaim_goal_hit_pct": number(reclaim_stress, "goal_hit_pct"),
        "reclaim_accepted": bool(reclaim.get("accepted", False)),
        "rank_migration_status": str(
            rank_migration.get("status") or "pending"
        ),
        "rank_migration_diagnostic_sessions": int(
            number(rank_migration_diagnostic, "sessions")
        ),
        "rank_migration_diagnostic_trades": int(
            number(rank_migration_diagnostic, "trades")
        ),
        "rank_migration_diagnostic_avg_pnl": number(
            rank_migration_diagnostic, "avg_daily_pnl"
        ),
        "rank_migration_diagnostic_lower95_pct": number(
            rank_migration_diagnostic, "lower_95_pct"
        ),
        "rank_migration_forward_sessions": int(
            number(rank_migration_forward, "sessions")
        ),
        "rank_migration_forward_trades": int(
            number(rank_migration_forward, "trades")
        ),
        "rank_migration_forward_avg_pnl": number(
            rank_migration_forward, "avg_daily_pnl"
        ),
        "rank_migration_forward_lower95_pct": number(
            rank_migration_forward, "lower_95_pct"
        ),
        "rank_migration_forward_goal_hit_pct": number(
            rank_migration_forward, "goal_hit_pct"
        ),
        "rank_migration_accepted": bool(rank_migration.get("accepted", False)),
        "full_rank_recorder_status": str(
            full_rank_recorder.get("status") or "awaiting runtime restart"
        ),
        "full_rank_recorded_at": str(
            full_rank_recorder.get("recorded_at") or "n/a"
        ),
        "full_rank_first_recorded_at": str(
            full_rank_recorder.get("first_recorded_at") or "n/a"
        ),
        "full_rank_session_eligible": bool(
            full_rank_recorder.get("full_session_eligible", False)
        ),
        "full_rank_universe": int(number(full_rank_recorder, "universe_size")),
        "full_rank_rows": int(number(full_rank_recorder, "rows_this_snapshot")),
        "full_rank_rapid": int(
            number(full_rank_recorder, "rapid_rank_climbers_this_snapshot")
        ),
        "full_rank_strategy_status": str(
            full_rank_strategy.get("status") or "awaiting EOD judge"
        ),
        "full_rank_strategy_eligible_sessions": int(
            number(full_rank_strategy, "eligible_full_sessions")
        ),
        "full_rank_strategy_required_sessions": int(
            number(full_rank_strategy, "selection_sessions_required") or 12
        ),
        "full_rank_strategy_partial_sessions": int(
            number(full_rank_strategy, "partial_sessions")
        ),
        "full_rank_strategy_rows": int(
            number(full_rank_strategy, "executable_rows")
        ),
        "full_rank_strategy_forward_sessions": int(
            number(full_rank_forward, "sessions")
        ),
        "full_rank_strategy_forward_trades": int(
            number(full_rank_forward, "trades")
        ),
        "full_rank_strategy_forward_avg_pnl": number(
            full_rank_forward, "avg_daily_pnl"
        ),
        "full_rank_strategy_accepted": bool(
            full_rank_strategy.get("accepted", False)
        ),
        "full_rank_paper_status": str(
            full_rank_paper.get("status") or "awaiting live observer"
        ),
        "full_rank_paper_updated_at": str(
            full_rank_paper.get("updated_at") or "n/a"
        ),
        "full_rank_paper_entries": int(number(full_rank_paper, "entries")),
        "full_rank_paper_active": int(number(full_rank_paper, "active")),
        "full_rank_paper_closed": int(number(full_rank_paper, "closed")),
        "full_rank_paper_wins": int(number(full_rank_paper, "wins")),
        "full_rank_paper_net_pnl": number(full_rank_paper, "net_pnl"),
        "full_rank_exact_status": str(
            full_rank_exact.get("status") or "awaiting EOD exact-capital judge"
        ),
        "full_rank_exact_sessions": int(
            number(full_rank_exact, "eligible_exact_sessions")
        ),
        "full_rank_exact_required": int(
            number(full_rank_exact, "selection_sessions_required") or 12
        ),
        "full_rank_exact_partial": int(
            number(full_rank_exact, "partial_or_incomplete_sessions")
        ),
        "full_rank_exact_labels": int(number(full_rank_exact, "entry_labels")),
        "full_rank_exact_closed": int(number(full_rank_exact, "closed_labels")),
        "full_rank_exact_selector": str(
            (full_rank_exact.get("frozen_selection", {}) or {}).get("selector")
            or "not frozen"
        ),
        "full_rank_exact_forward_sessions": int(
            number(full_rank_exact_forward, "sessions")
        ),
        "full_rank_exact_forward_trades": int(
            number(full_rank_exact_forward, "trades")
        ),
        "full_rank_exact_forward_avg_pnl": number(
            full_rank_exact_forward, "avg_daily_pnl"
        ),
        "full_rank_exact_accepted": bool(full_rank_exact.get("accepted", False)),
        "evidence_accepted": False,
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }


def broad_backfill_stats():
    summary = load_json(DATA / "broad_universe_5minute_backfill_summary.json", {})
    mapped = int(summary.get("mapped_symbols") or 0)
    present = int(summary.get("exact_range_cache_files") or 0)
    cached = int(summary.get("quality_eligible_cache_files", present) or 0)
    return {
        "universe": int(summary.get("universe_symbols") or 0),
        "mapped": mapped,
        "cached": cached,
        "present": present,
        "rejected": int(summary.get("quality_rejected_cache_files") or 0),
        "remaining": int(summary.get("needs_fetch_symbols") or 0),
        "api_calls": int(summary.get("estimated_api_calls") or 0),
        "coverage_pct": cached / mapped * 100.0 if mapped else 0.0,
        "estimated_total_gb": float(summary.get("estimated_total_range_bytes") or 0.0) / 1024**3,
        "start": str(summary.get("start") or "n/a"),
        "end": str(summary.get("end") or "n/a"),
    }


def broad_rank_checkpoint_stats():
    root = DATA / "broad_rank_acceleration"
    statuses = []
    for path in sorted(root.glob("checkpoint_*/status.json")) if root.exists() else []:
        status = load_json(path, {})
        if not isinstance(status, dict) or not status:
            continue
        try:
            milestone = int(status.get("milestone") or path.parent.name.rsplit("_", 1)[-1])
        except (TypeError, ValueError):
            continue
        statuses.append((milestone, status))
    statuses.sort(key=lambda item: item[0])
    completed = [milestone for milestone, _ in statuses]
    latest = statuses[-1][1] if statuses else {}
    next_milestone = next(
        (milestone for milestone in (1_000, 2_000, 2_900) if milestone not in completed),
        0,
    )
    return {
        "completed": completed,
        "latest_milestone": completed[-1] if completed else 0,
        "next_milestone": next_milestone,
        "diagnostic_only": bool(latest.get("diagnostic_only", True)),
        "coverage_gate_passed": bool(latest.get("broad_coverage_gate_passed", False)),
        "historical_goal_gate_passed": bool(latest.get("historical_goal_gate_passed", False)),
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }


def _classifier_stats(current_name, checkpoint_name, fallback_strategy):
    status_path = DATA / "eventual_runner_current_505" / current_name
    evidence_scope = "current 505-symbol baseline"
    checkpoint_root = DATA / "broad_rank_acceleration"
    checkpoint_statuses = []
    if checkpoint_root.exists():
        for path in checkpoint_root.glob(
            f"checkpoint_*/{checkpoint_name}"
        ):
            try:
                milestone = int(path.parent.name.rsplit("_", 1)[-1])
            except (TypeError, ValueError):
                continue
            checkpoint_statuses.append((milestone, path))
    if checkpoint_statuses:
        milestone, status_path = max(checkpoint_statuses, key=lambda item: item[0])
        evidence_scope = f"broad checkpoint {milestone}"
    status = load_json(status_path, {})
    selected = status.get("selected", {}) if isinstance(status, dict) else {}
    bound_rows = status.get("optimistic_opportunity_bound", []) if isinstance(status, dict) else []
    bound_rows = [row for row in bound_rows if isinstance(row, dict)]
    bound = max(
        bound_rows,
        key=lambda row: float(row.get("stress_final_mean_lower_95_pnl") or 0.0),
        default={},
    )
    return {
        "available": bool(status and selected),
        "evidence_scope": evidence_scope,
        "strategy": str(status.get("strategy") or fallback_strategy),
        "capital_reuse": str(status.get("capital_reuse") or "pending exact replay"),
        "selection_gate_passed": bool(selected.get("selection_gate_passed", False)),
        "final_goal_contract_passed": bool(status.get("final_goal_contract_passed", False)),
        "stress_final_avg_pnl": float(selected.get("stress_final_avg_daily_pnl") or 0.0),
        "stress_final_lower95_pnl": float(selected.get("stress_final_mean_lower_95_pnl") or 0.0),
        "stress_final_goal_hit_pct": float(selected.get("stress_final_goal_hit_pct") or 0.0),
        "threshold": float(selected.get("threshold") or 0.0),
        "slots": int(selected.get("slots") or 0),
        "signal_window": str(selected.get("signal_window") or "pending"),
        "opportunity_bound_available": bool(bound),
        "opportunity_bound_slots": int(bound.get("slots") or 0),
        "opportunity_bound_final_avg_pnl": float(
            bound.get("stress_final_avg_daily_pnl") or 0.0
        ),
        "opportunity_bound_final_lower95_pnl": float(
            bound.get("stress_final_mean_lower_95_pnl") or 0.0
        ),
        "opportunity_bound_goal_hit_pct": float(
            bound.get("stress_final_goal_hit_pct") or 0.0
        ),
        "paper_promotion_allowed": False,
        "real_orders_allowed": False,
    }


def eventual_runner_classifier_stats():
    return _classifier_stats(
        "classifier_status.json",
        "eventual_runner_classifier_status.json",
        "eventual 10% runner",
    )


def direct_target_classifier_stats():
    return _classifier_stats(
        "direct_target_status.json",
        "direct_target_classifier_status.json",
        "target before stop",
    )


# ---------------------------------------------------------------- paper lanes

def lane_stats(path, label, closed_filter, ret_field="net_return_pct"):
    j = load_json(path, {})
    cfg = j.get("config", {})
    trades = j.get("trades", [])
    closed = [t for t in trades if closed_filter(t)]
    active = [t for t in trades if t.get("status") in ("active", "open")]
    pending = [t for t in trades if t.get("status") == "pending"]
    rets = [t.get(ret_field, 0.0) for t in closed if ret_field in t]
    # Account-level booked P&L = per-trade position × return, summed. Only meaningful
    # when positions are ₹-sized and a capital base is known (else show "—").
    cap = cfg.get("total_capital_inr")
    sized = [t for t in closed if t.get("position_inr") and ret_field in t]
    cum_inr = sum(t["position_inr"] * t[ret_field] / 100.0 for t in sized) if sized else None
    on_cap = (cum_inr / cap * 100) if (cum_inr is not None and cap) else None
    if rets:
        wr = sum(1 for r in rets if r > 0) / len(rets) * 100
        avg = statistics.mean(rets)
    else:
        wr = avg = 0.0
    return {
        "label": label,
        "closed": len(closed),
        "active": len(active),
        "pending": len(pending),
        "wr": wr,
        "avg": avg,
        "has_data": bool(rets),
        "cum_inr": cum_inr,
        "cap": cap,
        "on_cap": on_cap,
        "started": cfg.get("started"),
    }


def all_lanes():
    return [
        lane_stats(
            DATA / "blind_buy_alert_paper" / "journal.json",
            "Blind-Buy",
            lambda t: t.get("status") == "closed",
        ),
        lane_stats(
            DATA / "intraday_alert_paper" / "journal.json",
            "Intraday (Tier C)",
            lambda t: not t.get("skipped") and "net_return_pct" in t,
        ),
        lane_stats(
            DATA / "swing_alert_paper" / "journal.json",
            "Swing-Alert",
            lambda t: t.get("status") == "closed",
        ),
        lane_stats(
            DATA / "swing_paper_trades" / "paper_journal.json",
            "Swing-Momentum",
            lambda t: t.get("status") == "closed",
        ),
    ]


# ------------------------------------------------------------- real positions

def real_positions():
    pos = load_json(DATA / "kite_tracked_positions.json", {})
    ticks = load_json(DATA / "kiteticker_all_ticks_latest.json", {})
    rows = []
    for sym, p in pos.items():
        if not isinstance(p, dict) or p.get("status") != "open":
            continue
        qty = p.get("qty", 0) or 0
        buy = p.get("buy_price", 0) or 0
        tick = ticks.get(sym, {})
        cur = tick.get("last_price") or tick.get("close") or 0
        invested = qty * buy
        value = qty * cur
        pnl = value - invested
        pnlpct = (cur / buy - 1) * 100 if buy and cur else 0.0
        stop = p.get("stop_loss", 0) or 0
        rows.append({
            "sym": sym,
            "qty": qty,
            "buy": buy,
            "cur": cur,
            "invested": invested,
            "value": value,
            "pnl": pnl,
            "pnlpct": pnlpct,
            "stop": stop,
            "gtt": p.get("gtt_status", ""),
            "stage": p.get("swing_stage", ""),
            "has_price": bool(cur),
            "blown": bool(cur and stop and cur < stop),
            "unprotected": p.get("gtt_status", "") == "missing",
            "last_trade": tick.get("last_trade_time", ""),
        })
    rows.sort(key=lambda r: r["pnlpct"])  # worst first
    return rows


# -------------------------------------------------------------------- render

def money(x):
    value = float(x or 0.0)
    sign = "-" if value < 0 else ""
    return f"{sign}₹{abs(value):,.0f}"


def render():
    now = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
    positions = real_positions()
    lanes = all_lanes()
    ranked = ranked_confirmation_stats()
    evidence = evidence_readiness_stats()
    ranked_replay = ranked_replay_readiness_stats()
    event_shadow = event_shadow_readiness_stats()
    historical_transfer = historical_transfer_stats()
    exit_contracts = event_exit_contract_stats()
    frozen_micro = frozen_microstructure_stats()
    ignition_registry = ignition_registry_stats()
    leader_discovery = leader_discovery_stats()
    broad_backfill = broad_backfill_stats()
    broad_rank = broad_rank_checkpoint_stats()
    runner_classifier = eventual_runner_classifier_stats()
    direct_classifier = direct_target_classifier_stats()

    tot_inv = sum(r["invested"] for r in positions)
    tot_val = sum(r["value"] for r in positions)
    tot_pnl = tot_val - tot_inv
    tot_pct = (tot_val / tot_inv - 1) * 100 if tot_inv else 0.0

    pos_rows = ""
    for r in positions:
        cls = "loss" if r["pnl"] < 0 else "gain"
        badges = ""
        if r["blown"]:
            badges += '<span class="badge red">STOP BLOWN</span>'
        if r["unprotected"]:
            badges += '<span class="badge orange">UNPROTECTED</span>'
        cur_disp = f"{r['cur']:.2f}" if r["has_price"] else "<i>no live price</i>"
        pos_rows += f"""<tr class="{cls}">
            <td class="sym">{escape(r['sym'])} {badges}</td>
            <td class="num">{r['qty']}</td>
            <td class="num">{r['buy']:.2f}</td>
            <td class="num">{cur_disp}</td>
            <td class="num">{money(r['invested'])}</td>
            <td class="num {cls}">{r['pnlpct']:+.1f}%</td>
            <td class="num {cls}">{money(r['pnl'])}</td>
            <td class="num">{r['stop']:.2f}</td>
        </tr>"""
    if not positions:
        pos_rows = '<tr><td colspan="8" class="muted">No open real positions tracked.</td></tr>'

    lane_rows = ""
    for s in lanes:
        paused = s["label"] in PAUSED_LANES
        badge = ' <span class="badge grey">PAUSED</span>' if paused else ""
        row_cls = ' class="paused"' if paused else ""
        since = (f' <span class="muted" style="font-weight:400">since {s["started"][5:]}</span>'
                 if s.get("started") else "")
        label_cell = f'<td class="sym">{escape(s["label"])}{badge}{since}</td>'
        if s["on_cap"] is not None:
            oc_cls = "muted" if paused else ("gain" if s["on_cap"] >= 0 else "loss")
            oncap_cell = (f'<td class="num {oc_cls}">{s["on_cap"]:+.1f}% '
                          f'<span class="muted">(₹{s["cum_inr"]:+,.0f})</span></td>')
        else:
            oncap_cell = '<td class="num muted">—</td>'
        if s["has_data"]:
            cls = "muted" if paused else ("gain" if s["avg"] >= 0 else "loss")
            lane_rows += f"""<tr{row_cls}>
                {label_cell}
                <td class="num">{s['closed']}</td>
                <td class="num">{s['active']}</td>
                <td class="num">{s['pending']}</td>
                <td class="num">{s['wr']:.0f}%</td>
                <td class="num {cls}">{s['avg']:+.2f}%</td>
                {oncap_cell}
            </tr>"""
        else:
            lane_rows += f"""<tr{row_cls}>
                {label_cell}
                <td class="num">0</td><td class="num">{s['active']}</td>
                <td class="num">{s['pending']}</td>
                <td class="muted" colspan="3">no closed trades yet</td>
            </tr>"""

    # Blind-buy Stage-1 gate progress
    g = blind_buy_gate()
    pct = min(100, g["fwd"] / g["gate"] * 100) if g["gate"] else 0
    if g["fwd"]:
        fwd_line = (f'<span class="big {"gain" if (g["fwd_avg"] or 0) >= 0 else "loss"}">'
                    f'{g["fwd_avg"]:+.2f}% avg</span> · {g["fwd_wr"]:.0f}% win so far')
    else:
        fwd_line = ('<span class="muted">no forward trades closed yet — first batch of '
                    f'{g["active"]} open positions closes ~mid-June</span>')
    seed_avg = f'{g["seed_avg"]:+.2f}%' if g["seed_avg"] is not None else "n/a"
    gate_html = f"""
  <h2>Blind-Buy &rarr; Stage-1 gate progress</h2>
  <div class="summary">
    Forward trades: <span class="big">{g['fwd']} / {g['gate']}</span>
    &nbsp;<span class="bar"><span class="bar-fill" style="width:{pct:.0f}%"></span></span><br>
    <div style="margin-top:8px">{fwd_line}</div>
  </div>
  <div class="updated" style="margin-top:6px">
    Gate = {g['gate']} forward closed trades (closed on/after {FORWARD_START}) sustained &ge;0.3%/day net &rarr; then consider real capital.
    {g['active']} positions in flight. Seed: {g['seed']} backfill trades (avg {seed_avg}) shown as prior context only &mdash; NOT counted toward the gate.
  </div>"""

    # Live-alert watch section (observational; the bot's chase-the-move feed)
    watch_html = ""
    w = get_watch()
    if w and w["agg"]["total_alerts"]:
        a = w["agg"]
        if a["closed"]:
            avg_cls = "gain" if (a["avg_net"] or 0) >= 0 else "loss"
            verdict = (f'<span class="big {avg_cls}">{a["avg_net"]:+.2f}% avg</span> · '
                       f'{a["win_rate"]:.0f}% win · {a["targets"]} hit target / {a["stops"]} stopped '
                       f'· {a["closed"]} scored')
        else:
            verdict = '<span class="muted">no alerts scored yet — all too recent</span>'
        wrows = ""
        for r in w["rows"][:10]:
            if r["status"] == "closed":
                rc = "gain" if r["net_pct"] >= 0 else "loss"
                out = f'<span class="{rc}">{r["net_pct"]:+.2f}%</span> <span class="muted">({escape(str(r["exit_reason"]))})</span>'
            else:
                out = f'<span class="muted">{escape(r["status"])}</span>'
            wrows += (f'<tr><td>{escape(r["date"])}</td>'
                      f'<td class="sym">{escape(r["symbol"])}</td>'
                      f'<td class="num">{out}</td></tr>')
        watch_html = f"""
  <h2>Live BUY alerts — watch only (NOT traded)</h2>
  <div class="summary">{verdict}</div>
  <div class="updated" style="margin-top:6px">
    &#9888; Best-case read: scored from the point-in-time actionable board, so alerts that fired and faded intraday (like ATGL today) are NOT captured &mdash; the real edge is likely worse. {a['pending']} too recent to score. Entry at alert price, {a['hold_days']}-day forward outcome on daily bars, net ~0.25% cost. Shown to judge whether acting on these would pay &mdash; they are not traded.
  </div>
  <table>
    <tr><th>Alert date</th><th>Symbol</th><th class="num">If you'd bought it</th></tr>
    {wrows}
  </table>"""

    ranked_rows = ""
    for row in ranked["trades"]:
        closed = truthy(row.get("closed"))
        pnl = float(row.get("net_pnl") or 0.0)
        pnl_class = "gain" if pnl > 0 else ("loss" if pnl < 0 else "muted")
        status = escape(str(row.get("exit_reason") or ("open" if not closed else "closed")))
        ranked_rows += f"""<tr>
            <td class="sym">{escape(str(row.get('symbol', '')))}</td>
            <td class="num">{float(row.get('entry_price') or 0.0):.2f}</td>
            <td class="num">{float(row.get('exit_price') or 0.0):.2f}</td>
            <td>{status}</td>
            <td class="num {pnl_class}">{money(pnl)}</td>
        </tr>"""
    if not ranked_rows:
        ranked_rows = '<tr><td colspan="5" class="muted">No ranked paper trades yet.</td></tr>'
    pause_badge = (
        '<span class="badge red">ENTRY PAUSED: 2 LOSSES</span>'
        if ranked["entry_paused"]
        else '<span class="badge grey">PAPER ONLY</span>'
    )
    ranked_pnl_class = "gain" if ranked["forward_net_pnl"] >= 0 else "loss"
    ranked_html = f"""
  <h2>Ranked Grade-B confirmation (paper only)</h2>
  <div class="summary">
    <span>Forward days <span class="big">{ranked['forward_days']}</span></span>
    <span>Closed <span class="big">{ranked['forward_closed']}</span></span>
    <span>Win rate <span class="big">{ranked['forward_win_rate']:.0f}%</span></span>
    <span>Net <span class="big {ranked_pnl_class}">{money(ranked['forward_net_pnl'])}</span></span>
    {pause_badge}
  </div>
  <div class="updated" style="margin-top:6px">
    Accumulated forward-live ledger through {escape(ranked['date'] or 'n/a')}.
    Today: {ranked['candidates']} candidates observed; {ranked['closed']} closed,
    {ranked['targets']} targets, {ranked['stops']} stops, {money(ranked['net_pnl'])} net.
    Simulated fills include fees and slippage. No broker orders.
  </div>
  <table>
    <tr><th>Symbol</th><th class="num">Entry</th><th class="num">Exit</th><th>Reason</th><th class="num">Net P&amp;L</th></tr>
    {ranked_rows}
  </table>"""

    replay_class = "gain" if ranked_replay["stress_avg_daily_pnl"] > 0 else "loss"
    replay_verdict = "SHADOW CANDIDATE" if ranked_replay["shadow_candidates"] else "NOT READY"
    ranked_replay_html = f"""
  <h2>Ranked exact-tape readiness</h2>
  <div class="summary">
    <span>Complete days <span class="big">{ranked_replay['complete_days']}</span></span>
    <span>Fixed configs <span class="big">{ranked_replay['configs']}</span></span>
    <span>Shadow candidates <span class="big">{ranked_replay['shadow_candidates']}</span></span>
    <span>&#8377;10k passes <span class="big">{ranked_replay['goal_passes']}</span></span>
    <span class="badge {'grey' if ranked_replay['shadow_candidates'] else 'red'}">{replay_verdict}</span>
  </div>
  <div class="updated" style="margin-top:6px">
    Best fixed configuration: <b>{escape(ranked_replay['best_config'] or 'none')}</b>;
    {ranked_replay['closed_trades']} closed trades, {ranked_replay['wins']} winners,
    stressed average <span class="{replay_class}">{money(ranked_replay['stress_avg_daily_pnl'])}/day</span>,
    lower confidence bound {money(ranked_replay['stress_lower95_daily_pnl'])}/day,
    profit factor {ranked_replay['profit_factor']:.2f}.
    Paper promotion: <b>{'YES' if ranked_replay['paper_promotion_allowed'] else 'NO'}</b>;
    real orders: <b>NO</b>.
  </div>"""

    evidence_class = "gain" if evidence["best_stress_return"] > 0 else "loss"
    coverage_class = "gain" if evidence["coverage_passed"] else "loss"
    evidence_html = f"""
  <h2>Live evidence readiness</h2>
  <div class="summary">
    <span>Sessions <span class="big">{evidence['sessions']}</span></span>
    <span>Coverage <span class="big {coverage_class}">{evidence['coverage']:.1f}%</span></span>
    <span>Shadow candidates <span class="big">{evidence['shadow_candidates']}</span></span>
    <span>&#8377;10k passes <span class="big">{evidence['goal_passes']}</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Latest evidence through {escape(evidence['status_date'] or 'n/a')}.
    Best eligible research row: <b>{escape(evidence['best_bucket'] or 'none')}</b> /
    {escape(evidence['best_contract'] or 'none')} at
    <span class="{evidence_class}">{evidence['best_stress_return']:+.3f}% stressed daily
    ({money(evidence['best_stress_pnl'])})</span>, coverage {evidence['best_coverage']:.1f}%,
    lower bound {evidence['best_lower_bound']:+.3f}%.
    Evidence accepted: <b>NO</b>; paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    event_shadow_class = "gain" if event_shadow["stress_avg_return"] > 0 else "loss"
    event_shadow_verdict = (
        "PAPER GATE PASSED"
        if event_shadow["sample_gate_passed"] and event_shadow["performance_gate_passed"]
        else "COLLECTING EVIDENCE"
    )
    event_shadow_html = f"""
  <h2>Causal event-shadow readiness</h2>
  <div class="summary">
    <span>Sessions <span class="big">{event_shadow['sessions']}</span></span>
    <span>Complete candidates <span class="big">{event_shadow['events']}</span></span>
    <span>Frozen trades <span class="big">{event_shadow['trades']}</span></span>
    <span>Positive sessions <span class="big">{event_shadow['positive_sessions']:.0f}%</span></span>
    <span class="badge {'grey' if event_shadow['sample_gate_passed'] else 'red'}">{event_shadow_verdict}</span>
  </div>
  <div class="updated" style="margin-top:6px">
    Frozen first-signal-batch heuristic after embedded costs plus 0.10% stress:
    <span class="{event_shadow_class}">{event_shadow['stress_avg_return']:+.3f}% / {money(event_shadow['stress_avg_pnl'])} per day</span>;
    lower confidence bound {event_shadow['stress_lower95']:+.3f}%;
    &#8377;10k hit sessions {event_shadow['goal_hit_pct']:.1f}%.
    Status: <b>{escape(event_shadow['status'])}</b>;
    paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    transfer_class = "gain" if historical_transfer["avg_return"] > 0 else "loss"
    transfer_html = f"""
  <h2>Historical ML to live transfer</h2>
  <div class="summary">
    <span>Future sessions <span class="big">{historical_transfer['sessions']}</span></span>
    <span>Scored candidates <span class="big">{historical_transfer['candidates']}</span></span>
    <span>Frozen trades <span class="big">{historical_transfer['trades']}</span></span>
    <span>Positive sessions <span class="big">{historical_transfer['positive_sessions']:.0f}%</span></span>
    <span class="badge red">NOT PROMOTED</span>
  </div>
  <div class="updated" style="margin-top:6px">
    Historical family model frozen through {escape(historical_transfer['historical_end'])}, evaluated through {escape(historical_transfer['live_end'])}.
    Embedded costs plus 0.10% stress:
    <span class="{transfer_class}">{historical_transfer['avg_return']:+.3f}% / {money(historical_transfer['avg_pnl'])} per day</span>;
    total {money(historical_transfer['total_pnl'])}; lower confidence bound {historical_transfer['lower95']:+.3f}%;
    &#8377;10k hit sessions {historical_transfer['goal_hit_pct']:.1f}%.
    Paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    exit_forward_class = "gain" if exit_contracts["forward_avg_pnl"] > 0 else "loss"
    exit_contract_html = f"""
  <h2>Frozen event exit contracts</h2>
  <div class="summary">
    <span>Contracts <span class="big">{exit_contracts['configs']}</span></span>
    <span>Total sessions <span class="big">{exit_contracts['sessions']}</span></span>
    <span>Forward sessions <span class="big">{exit_contracts['forward_sessions']}/20</span></span>
    <span>Paper candidates <span class="big">{exit_contracts['paper_candidates']}</span></span>
    <span class="badge red">FORWARD ONLY</span>
  </div>
  <div class="updated" style="margin-top:6px">
    July 13-16 is exploratory only. Best exploratory mean was
    <b>{escape(exit_contracts['exploratory_best'])}</b> at {money(exit_contracts['exploratory_avg_pnl'])}/day,
    lower bound {exit_contracts['exploratory_lower95']:+.3f}% and cannot be selected.
    Current forward leader: <b>{escape(exit_contracts['forward_best'])}</b>,
    <span class="{exit_forward_class}">{money(exit_contracts['forward_avg_pnl'])}/day</span>,
    lower bound {exit_contracts['forward_lower95']:+.3f}%.
    Paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    micro_class = "gain" if frozen_micro["stress_avg_pnl"] > 0 else "loss"
    micro_badge = "SELECTION REJECTED" if frozen_micro["selection_rejected"] else "FORWARD ONLY"
    micro_html = f"""
  <h2>Frozen live microstructure selector</h2>
  <div class="summary">
    <span>Forward sessions <span class="big">{frozen_micro['forward_sessions']}/{frozen_micro['minimum_sessions']}</span></span>
    <span>Forward candidates <span class="big">{frozen_micro['candidates']}</span></span>
    <span>Selection reference <span class="big">{money(frozen_micro['selection_avg_pnl'])}/day</span></span>
    <span class="badge red">{micro_badge}</span>
  </div>
  <div class="updated" style="margin-top:6px">
    Frozen rule: <b>{escape(frozen_micro['selector'])}</b> / {escape(frozen_micro['contract'])};
    untouched evidence starts {escape(frozen_micro['forward_start'])}.
    Stressed forward result: <span class="{micro_class}">{money(frozen_micro['stress_avg_pnl'])}/day</span>,
    lower confidence bound {frozen_micro['stress_lower95']:+.3f}%.
    Accepted: <b>{'YES' if frozen_micro['accepted'] else 'NO'}</b>;
    paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    ignition_html = f"""
  <h2>Early pre-runner ignition registry</h2>
  <div class="summary">
    <span>Selection sessions <span class="big">{ignition_registry['selection_sessions']}</span></span>
    <span>Forward sessions <span class="big">{ignition_registry['forward_sessions']}</span></span>
    <span>Frozen configurations <span class="big">{ignition_registry['frozen_configs']}</span></span>
    <span class="badge red">{'SELECTION REJECTED' if ignition_registry['selection_rejected'] else 'RESEARCH ONLY'}</span>
  </div>
  <div class="updated" style="margin-top:6px">
    True early window: +0.5% to +3.0%, executable spread, liquid tape, and one earliest ranked candidate.
    Selection ended {escape(ignition_registry['selection_end'])}; untouched period starts {escape(ignition_registry['forward_start'])}.
    Paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    leader_snapshot_class = (
        "gain" if leader_discovery["snapshot_avg_return_pct"] > 0 else "loss"
    )
    leader_ranker_class = (
        "gain" if leader_discovery["ranker_stress_avg_pnl"] > 0 else "loss"
    )
    leader_html = f"""
  <h2>Leader discovery and continuation</h2>
  <div class="summary">
    <span>10% runner events <span class="big">{leader_discovery['runner_events_10']}</span></span>
    <span>Seen before 10% <span class="big">{leader_discovery['seen_before_10_pct']:.1f}%</span></span>
    <span>Median lead <span class="big">{leader_discovery['median_lead_minutes']:.0f} min</span></span>
    <span>Forward states <span class="big">{leader_discovery['forward_states']}</span></span>
    <span class="badge red">{'SELECTION REJECTED' if leader_discovery['selection_rejected'] else 'FORWARD ONLY'}</span>
  </div>
  <div class="updated" style="margin-top:6px">
    Recorder recall was {leader_discovery['band_0_5_to_3_recall']:.1f}% in the +0.5%-3% band and
    {leader_discovery['band_0_5_to_5_recall']:.1f}% through +5%.
    The corrected next-untouched five-minute replay covered {leader_discovery['retrospective_evaluated']} acceleration states
    and rejected the rule at {leader_discovery['retrospective_target_hit_pct']:.1f}% target hits,
    {leader_discovery['retrospective_avg_return_pct']:+.3f}% mean net, and
    {leader_discovery['retrospective_lower95_pct']:+.3f}% lower daily bound.
    The executable ask-to-bid replay independently covered {leader_discovery['snapshot_evaluated']} states:
    {leader_discovery['snapshot_target_hit_pct']:.1f}% target hits,
    <span class="{leader_snapshot_class}">{leader_discovery['snapshot_avg_return_pct']:+.3f}% mean net</span>,
    lower daily bound {leader_discovery['snapshot_lower95_pct']:+.3f}%.
    Recording after {escape(leader_discovery['forward_start'])} remains diagnostic only.
    Evidence accepted: <b>NO</b>; paper promotion: <b>NO</b>; real orders: <b>NO</b>.
    Clairvoyant one-slot ceiling ({escape(leader_discovery['opportunity_contract'])}, future-known outcomes):
    {money(leader_discovery['opportunity_avg_pnl'])}/day, lower 95%
    {money(leader_discovery['opportunity_lower95_pnl'])}/day, goal sessions
    {leader_discovery['opportunity_goal_hit_pct']:.1f}%; this bound is never tradeable.
  </div>"""

    leader_html += f"""
  <div class="summary" style="margin-top:10px">
    <span>Frozen ranker <span class="big">{escape(leader_discovery['ranker_status'])}</span></span>
    <span>Forward sessions <span class="big">{leader_discovery['ranker_forward_sessions']}</span></span>
    <span>Forward trades <span class="big">{leader_discovery['ranker_forward_trades']}</span></span>
    <span>Stressed P&amp;L <span class="big {leader_ranker_class}">{money(leader_discovery['ranker_stress_avg_pnl'])}/day</span></span>
    <span>Goal days <span class="big">{leader_discovery['ranker_stress_goal_hit_pct']:.1f}%</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Prior-session-only {escape(leader_discovery['ranker_contract'])} microstructure ranker;
    untouched evidence starts {escape(leader_discovery['ranker_forward_start'])}.
    Stressed lower 95% bound {leader_discovery['ranker_stress_lower95_pct']:+.3f}%.
    Accepted: <b>{'YES' if leader_discovery['ranker_accepted'] else 'NO'}</b>;
    real orders: <b>NO</b>.
  </div>"""

    migration_diagnostic_class = (
        "gain"
        if leader_discovery["rank_migration_diagnostic_avg_pnl"] > 0
        else "loss"
    )
    migration_forward_class = (
        "gain" if leader_discovery["rank_migration_forward_avg_pnl"] > 0 else "loss"
    )
    leader_html += f"""
  <div class="summary" style="margin-top:10px">
    <span>Rank migration <span class="big">{escape(leader_discovery['rank_migration_status'])}</span></span>
    <span>Diagnostic <span class="big">{leader_discovery['rank_migration_diagnostic_trades']} trades</span></span>
    <span>Diagnostic P&amp;L <span class="big {migration_diagnostic_class}">{money(leader_discovery['rank_migration_diagnostic_avg_pnl'])}/day</span></span>
    <span>Forward sessions <span class="big">{leader_discovery['rank_migration_forward_sessions']}</span></span>
    <span>Forward trades <span class="big">{leader_discovery['rank_migration_forward_trades']}</span></span>
    <span>Forward P&amp;L <span class="big {migration_forward_class}">{money(leader_discovery['rank_migration_forward_avg_pnl'])}/day</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Candidate-relative migration is frozen through 2026-07-21 and remains diagnostic until untouched evidence exists.
    Diagnostic lower bound {leader_discovery['rank_migration_diagnostic_lower95_pct']:+.3f}%;
    forward lower bound {leader_discovery['rank_migration_forward_lower95_pct']:+.3f}%;
    forward goal sessions {leader_discovery['rank_migration_forward_goal_hit_pct']:.1f}%.
    Accepted: <b>{'YES' if leader_discovery['rank_migration_accepted'] else 'NO'}</b>; real orders: <b>NO</b>.
  </div>
  <div class="summary" style="margin-top:10px">
    <span>Full-universe recorder <span class="big">{escape(leader_discovery['full_rank_recorder_status'])}</span></span>
    <span>Universe <span class="big">{leader_discovery['full_rank_universe']}</span></span>
    <span>Rows/snapshot <span class="big">{leader_discovery['full_rank_rows']}</span></span>
    <span>Rapid climbers <span class="big">{leader_discovery['full_rank_rapid']}</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Latest full-universe rank snapshot: {escape(leader_discovery['full_rank_recorded_at'])}.
    First snapshot: {escape(leader_discovery['full_rank_first_recorded_at'])};
    full-session evidence: <b>{'YES' if leader_discovery['full_rank_session_eligible'] else 'NO (partial session)'}</b>.
    The top 75 and rapid rank climbers are recorded once per minute before they need to cross 5%.
    This recorder emits no Discord alerts and has no broker-order path.
  </div>"""

    full_rank_forward_class = (
        "gain"
        if leader_discovery["full_rank_strategy_forward_avg_pnl"] > 0
        else "loss"
    )
    leader_html += f"""
  <div class="summary" style="margin-top:10px">
    <span>Full-rank judge <span class="big">{escape(leader_discovery['full_rank_strategy_status'])}</span></span>
    <span>Selection sessions <span class="big">{leader_discovery['full_rank_strategy_eligible_sessions']}/{leader_discovery['full_rank_strategy_required_sessions']}</span></span>
    <span>Partial sessions <span class="big">{leader_discovery['full_rank_strategy_partial_sessions']}</span></span>
    <span>Executable states <span class="big">{leader_discovery['full_rank_strategy_rows']}</span></span>
    <span>Forward trades <span class="big">{leader_discovery['full_rank_strategy_forward_trades']}</span></span>
    <span>Forward P&amp;L <span class="big {full_rank_forward_class}">{money(leader_discovery['full_rank_strategy_forward_avg_pnl'])}/day</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Fixed before collection: +1.5%/-1.0%, expected return at least +0.20%, one unleveraged Rs 500,000 slot,
    ten trades maximum, 0.10% additional stress, 12 complete selection sessions, then 20 untouched forward sessions.
    Both raw recorder coverage and executable outcome coverage must pass.
    Accepted: <b>{'YES' if leader_discovery['full_rank_strategy_accepted'] else 'NO'}</b>; real orders: <b>NO</b>.
  </div>"""

    full_rank_paper_class = (
        "gain" if leader_discovery["full_rank_paper_net_pnl"] > 0 else "loss"
    )
    leader_html += f"""
  <div class="summary" style="margin-top:10px">
    <span>Exact paper observer <span class="big">{escape(leader_discovery['full_rank_paper_status'])}</span></span>
    <span>Entries <span class="big">{leader_discovery['full_rank_paper_entries']}</span></span>
    <span>Active <span class="big">{leader_discovery['full_rank_paper_active']}</span></span>
    <span>Closed <span class="big">{leader_discovery['full_rank_paper_closed']}</span></span>
    <span>Winners <span class="big">{leader_discovery['full_rank_paper_wins']}</span></span>
    <span>Net P&amp;L <span class="big {full_rank_paper_class}">{money(leader_discovery['full_rank_paper_net_pnl'])}</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Updated {escape(leader_discovery['full_rank_paper_updated_at'])}. Entry is the displayed ask and every exit is the displayed bid,
    with +1.5%/-1.0%, a 60-minute timeout, and another 0.10% stress charge.
    This labels all buyable rank cohorts for research; alerts: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    full_rank_exact_class = (
        "gain" if leader_discovery["full_rank_exact_forward_avg_pnl"] > 0 else "loss"
    )
    leader_html += f"""
  <div class="summary" style="margin-top:10px">
    <span>Exact-capital judge <span class="big">{escape(leader_discovery['full_rank_exact_status'])}</span></span>
    <span>Selection sessions <span class="big">{leader_discovery['full_rank_exact_sessions']}/{leader_discovery['full_rank_exact_required']}</span></span>
    <span>Partial sessions <span class="big">{leader_discovery['full_rank_exact_partial']}</span></span>
    <span>Labels/closed <span class="big">{leader_discovery['full_rank_exact_labels']}/{leader_discovery['full_rank_exact_closed']}</span></span>
    <span>Frozen selector <span class="big">{escape(leader_discovery['full_rank_exact_selector'])}</span></span>
    <span>Forward <span class="big">{leader_discovery['full_rank_exact_forward_sessions']} sessions / {leader_discovery['full_rank_exact_forward_trades']} trades</span></span>
    <span>Forward P&amp;L <span class="big {full_rank_exact_class}">{money(leader_discovery['full_rank_exact_forward_avg_pnl'])}/day</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    One reusable unleveraged Rs 500,000 slot, exact entry/exit timestamps, and ten trades maximum.
    Four causal selectors were fixed before the first complete session. The first 12 complete sessions may freeze one selector;
    acceptance still requires 20 untouched forward sessions. Accepted: <b>{'YES' if leader_discovery['full_rank_exact_accepted'] else 'NO'}</b>;
    real orders: <b>NO</b>.
  </div>"""

    live_paper_class = (
        "gain" if leader_discovery["live_paper_net_pnl"] > 0 else "loss"
    )
    leader_html += f"""
  <div class="summary" style="margin-top:10px">
    <span>Live shadow <span class="big">{escape(leader_discovery['live_paper_status'])}</span></span>
    <span>Predictions <span class="big">{leader_discovery['live_paper_predictions']}</span></span>
    <span>Max score <span class="big">{leader_discovery['live_paper_max_score']:+.3f}%</span></span>
    <span>Paper trades <span class="big">{leader_discovery['live_paper_closed']}/{leader_discovery['live_paper_trades']}</span></span>
    <span>Net P&amp;L <span class="big {live_paper_class}">{money(leader_discovery['live_paper_net_pnl'])}</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Timestamped live paper decisions for {escape(leader_discovery['live_paper_date'])};
    model trained only through {escape(leader_discovery['live_paper_model_through'])}.
    Predictions are recorded before outcomes, use observed asks and bids, and cannot send alerts or broker orders.
  </div>"""

    reclaim_class = (
        "gain" if leader_discovery["reclaim_stress_avg_pnl"] > 0 else "loss"
    )
    leader_html += f"""
  <div class="summary" style="margin-top:10px">
    <span>Pullback/reclaim <span class="big">{escape(leader_discovery['reclaim_status'])}</span></span>
    <span>Sessions <span class="big">{leader_discovery['reclaim_sessions']}</span></span>
    <span>Trades <span class="big">{leader_discovery['reclaim_trades']}</span></span>
    <span>Stressed P&amp;L <span class="big {reclaim_class}">{money(leader_discovery['reclaim_stress_avg_pnl'])}/day</span></span>
    <span>Positive days <span class="big">{leader_discovery['reclaim_positive_session_pct']:.1f}%</span></span>
    <span>Goal days <span class="big">{leader_discovery['reclaim_goal_hit_pct']:.1f}%</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Existing ignition watch followed by a healthy pullback and reclaim, ranked only by information visible in each minute.
    Stressed lower 95% bound {leader_discovery['reclaim_stress_lower95_pct']:+.3f}%.
    Accepted: <b>{'YES' if leader_discovery['reclaim_accepted'] else 'NO'}</b>;
    real orders: <b>NO</b>.
  </div>"""

    backfill_html = f"""
  <h2>Broad five-minute research history</h2>
  <div class="summary">
    <span>Eligible <span class="big">{broad_backfill['cached']}/{broad_backfill['mapped']}</span></span>
    <span>Coverage <span class="big">{broad_backfill['coverage_pct']:.1f}%</span></span>
    <span>Physical files <span class="big">{broad_backfill['present']}</span></span>
    <span>Rejected <span class="big">{broad_backfill['rejected']}</span></span>
    <span>Remaining <span class="big">{broad_backfill['remaining']}</span></span>
    <span>Estimated size <span class="big">{broad_backfill['estimated_total_gb']:.1f} GB</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Exact five-minute range {escape(broad_backfill['start'])} to {escape(broad_backfill['end'])};
    live universe {broad_backfill['universe']} symbols, {broad_backfill['api_calls']:,} estimated API calls remaining.
    Latest rank checkpoint: <b>{broad_rank['latest_milestone'] or 'none'}</b>;
    next: <b>{broad_rank['next_milestone'] or 'complete'}</b>.
    State: <b>{'DIAGNOSTIC ONLY' if broad_rank['diagnostic_only'] else 'FULL COVERAGE'}</b>;
    historical Rs 10,000 gate: <b>{'PASS' if broad_rank['historical_goal_gate_passed'] else 'NO'}</b>.
    Acquisition and checkpoints cannot enable paper or real orders.
  </div>"""

    runner_classifier_class = "gain" if runner_classifier["stress_final_avg_pnl"] > 0 else "loss"
    runner_classifier_html = f"""
  <h2>Eventual-runner classifier</h2>
  <div class="summary">
    <span>Status <span class="big">{'EVALUATED' if runner_classifier['available'] else 'PENDING'}</span></span>
    <span>Stress final <span class="big {runner_classifier_class}">{money(runner_classifier['stress_final_avg_pnl'])}/day</span></span>
    <span>Lower 95% <span class="big {runner_classifier_class}">{money(runner_classifier['stress_final_lower95_pnl'])}/day</span></span>
    <span>Goal days <span class="big">{runner_classifier['stress_final_goal_hit_pct']:.1f}%</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Prior-only 10% runner label; 60-minute +1.5%/-1.0% contract; {escape(runner_classifier['capital_reuse'])}.
    Evidence: <b>{escape(runner_classifier['evidence_scope'])}</b>.
    Signal window: <b>{escape(runner_classifier['signal_window'])}</b>.
    Selection gate: <b>{'PASS' if runner_classifier['selection_gate_passed'] else 'NO'}</b>;
    untouched Rs 10,000 contract: <b>{'PASS' if runner_classifier['final_goal_contract_passed'] else 'NO'}</b>;
    paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    direct_classifier_class = "gain" if direct_classifier["stress_final_avg_pnl"] > 0 else "loss"
    direct_classifier_html = f"""
  <h2>Profit-aligned classifier</h2>
  <div class="summary">
    <span>Status <span class="big">{'EVALUATED' if direct_classifier['available'] else 'PENDING'}</span></span>
    <span>Stress final <span class="big {direct_classifier_class}">{money(direct_classifier['stress_final_avg_pnl'])}/day</span></span>
    <span>Lower 95% <span class="big {direct_classifier_class}">{money(direct_classifier['stress_final_lower95_pnl'])}/day</span></span>
    <span>Goal days <span class="big">{direct_classifier['stress_final_goal_hit_pct']:.1f}%</span></span>
  </div>
  <div class="summary">
    <span>Clairvoyant bound <span class="big">{money(direct_classifier['opportunity_bound_final_avg_pnl'])}/day</span></span>
    <span>Bound lower 95% <span class="big">{money(direct_classifier['opportunity_bound_final_lower95_pnl'])}/day</span></span>
    <span>Bound goal days <span class="big">{direct_classifier['opportunity_bound_goal_hit_pct']:.1f}%</span></span>
    <span>Bound slots <span class="big">{direct_classifier['opportunity_bound_slots']}</span></span>
  </div>
  <div class="updated" style="margin-top:6px">
    Prior-only target-before-stop label; identical 60-minute +1.5%/-1.0% execution contract.
    The bound uses future outcomes and ignores overlap, so it is diagnosis only and can never promote trading.
    Evidence: <b>{escape(direct_classifier['evidence_scope'])}</b>.
    Signal window: <b>{escape(direct_classifier['signal_window'])}</b>.
    Selection gate: <b>{'PASS' if direct_classifier['selection_gate_passed'] else 'NO'}</b>;
    untouched Rs 10,000 contract: <b>{'PASS' if direct_classifier['final_goal_contract_passed'] else 'NO'}</b>;
    paper promotion: <b>NO</b>; real orders: <b>NO</b>.
  </div>"""

    tot_cls = "gain" if tot_pnl >= 0 else "loss"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">
<title>tr-assistant status</title>
<style>
  body {{ background:#0f1115; color:#e6e6e6; font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:24px; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  h2 {{ font-size:15px; margin:28px 0 8px; color:#9aa4b2; text-transform:uppercase; letter-spacing:.5px; }}
  .updated {{ color:#6b7280; font-size:13px; margin-bottom:8px; }}
  table {{ border-collapse:collapse; width:100%; font-size:14px; }}
  th,td {{ padding:8px 12px; text-align:left; border-bottom:1px solid #20242c; }}
  th {{ color:#9aa4b2; font-weight:600; font-size:12px; text-transform:uppercase; }}
  td.num,th.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  .sym {{ font-weight:600; }}
  .gain {{ color:#4ade80; }}
  .loss {{ color:#f87171; }}
  .muted {{ color:#6b7280; font-style:italic; }}
  .badge {{ font-size:10px; padding:2px 6px; border-radius:4px; margin-left:6px; font-weight:700; vertical-align:middle; }}
  .badge.red {{ background:#7f1d1d; color:#fecaca; }}
  .badge.orange {{ background:#7c2d12; color:#fed7aa; }}
  .badge.grey {{ background:#374151; color:#cbd5e1; }}
  tr.paused td {{ color:#6b7280; }}
  .bar {{ background:#20242c; border-radius:6px; height:18px; width:320px; display:inline-block; vertical-align:middle; overflow:hidden; }}
  .bar-fill {{ background:#4ade80; height:100%; }}
  .summary {{ background:#171a21; border-radius:8px; padding:14px 18px; display:inline-block; margin-top:6px; }}
  .summary span {{ margin-right:24px; }}
  .big {{ font-size:18px; font-weight:700; }}
</style></head>
<body>
  <h1>tr-assistant — status</h1>
  <div class="updated">updated {now} · auto-refreshes every 60s</div>

  <h2>Real positions (your money)</h2>
  <div class="summary">
    <span>Invested <span class="big">{money(tot_inv)}</span></span>
    <span>Now <span class="big">{money(tot_val)}</span></span>
    <span>P&amp;L <span class="big {tot_cls}">{money(tot_pnl)} ({tot_pct:+.1f}%)</span></span>
    <span>Open <span class="big">{len(positions)}</span></span>
  </div>
  <table>
    <tr><th>Symbol</th><th class="num">Qty</th><th class="num">Buy</th><th class="num">Now</th>
        <th class="num">Invested</th><th class="num">P&amp;L %</th><th class="num">P&amp;L &#8377;</th><th class="num">Stop</th></tr>
    {pos_rows}
  </table>

  <h2>Paper lanes (measurement only — no real money)</h2>
  <table>
    <tr><th>Lane</th><th class="num">Closed</th><th class="num">Active</th><th class="num">Pending</th>
        <th class="num">Win rate</th><th class="num">Avg/trade</th><th class="num">On &#8377;capital</th></tr>
    {lane_rows}
  </table>
{ranked_html}
{ranked_replay_html}
{evidence_html}
{event_shadow_html}
{transfer_html}
{exit_contract_html}
{micro_html}
{ignition_html}
{leader_html}
{runner_classifier_html}
{direct_classifier_html}
{backfill_html}
{gate_html}
{watch_html}
  <div class="updated" style="margin-top:18px">
    Numbers read live from results/Data journals + kite_tracked_positions.json. "On &#8377;capital" = booked P&amp;L &#247; deployable capital (the real account number, not a sum of per-trade %s). "since" = journal start date — if it jumps, the lane was reset/re-backfilled. Prices from last Kite tick (may be stale outside market hours).
  </div>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/", "/index.html"):
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = render().encode("utf-8")
        except Exception as e:
            body = f"<pre>dashboard error: {escape(str(e))}</pre>".encode("utf-8")
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # quiet


def main():
    server = HTTPServer((HOST, PORT), Handler)
    print(f"tr-assistant dashboard at http://{HOST}:{PORT}  (Ctrl-C to stop)")
    if HOST == "127.0.0.1":
        print("Local only. From your laptop:  ssh -L "
              f"{PORT}:localhost:{PORT} <user>@<vm-ip>  then open http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
