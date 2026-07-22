#!/usr/bin/env python3
"""Operational readiness check for the paper-only intraday evidence pipeline."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
IST = ZoneInfo("Asia/Kolkata")
TRUTHY = {"1", "true", "yes", "y", "on"}


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


def parse_ist(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)
    except ValueError:
        return None


def is_truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in TRUTHY


def check(name: str, passed: bool, detail: str, critical: bool = True) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "critical": critical, "detail": detail}


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
    checks.append(check("trade_service_active", service_active, "trade-assistant.service active" if service_active else "trade-assistant.service inactive"))
    checks.append(check("start_timer_enabled", start_timer_enabled, "08:45 IST weekday start enabled" if start_timer_enabled else "trade-assistant.timer not enabled"))
    checks.append(check("prewarm_timer_enabled", prewarm_timer_enabled, "08:30 IST weekday prewarm enabled" if prewarm_timer_enabled else "prewarm timer not enabled"))
    checks.append(check("disk_headroom", disk_free_gb >= 10.0 and disk_used_pct < 85.0, f"{disk_free_gb:.1f} GB free, {disk_used_pct:.1f}% used"))

    required_files = [
        "pkscreener/classes/FullUniverseRankPaperObserver.py",
        "pkscreener/classes/FullUniverseRankContractMatrixObserver.py",
        "pkscreener/classes/FullUniverseRankEntryTimingObserver.py",
        "scripts/assess_exact_full_rank_paper.py",
        "scripts/assess_full_rank_contract_matrix.py",
        "scripts/assess_full_rank_entry_timing.py",
        "scripts/assess_full_rank_feature_gates.py",
        "scripts/project_intraday_capital_growth.py",
        "scripts/live_evidence_eod.sh",
    ]
    missing = [value for value in required_files if not (root / value).is_file()]
    checks.append(check("required_code_present", not missing, "all evidence components present" if not missing else "missing: " + ", ".join(missing)))

    eod_script = (root / "scripts/live_evidence_eod.sh").read_text(errors="ignore") if (root / "scripts/live_evidence_eod.sh").exists() else ""
    required_eod_calls = [
        "run_exact_full_rank_strategy",
        "run_full_rank_contract_matrix",
        "run_full_rank_entry_timing",
        "run_full_rank_feature_gates",
        "run_intraday_capital_growth",
    ]
    absent_calls = [value for value in required_eod_calls if eod_script.count(value) < 2]
    checks.append(check("eod_judges_wired", not absent_calls, "all exact judges run after close" if not absent_calls else "missing calls: " + ", ".join(absent_calls)))
    cron_ok = "5 10 * * 1-5" in cron_text and "scripts/live_evidence_eod.sh" in cron_text
    checks.append(check("eod_schedule_installed", cron_ok, "15:35 IST weekday EOD judge installed" if cron_ok else "EOD evidence cron missing"))

    ticker = load_json(data / "kiteticker_all_status_latest.json")
    ticker_at = parse_ist(ticker.get("last_tick_at") or ticker.get("last_save_at"))
    ticker_age = (now - ticker_at).total_seconds() if ticker_at else float("inf")
    ticker_ok = bool(ticker.get("connected")) and ticker_age <= 180 and int(ticker.get("snapshot_symbols") or 0) >= 2500
    checks.append(check("live_ticks_fresh", ticker_ok, f"connected={bool(ticker.get('connected'))}, symbols={int(ticker.get('snapshot_symbols') or 0)}, age={ticker_age:.0f}s"))

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
        and float(recorder.get("interval_seconds") or 0) <= 60
        and recorder_safe
    )
    checks.append(check("rank_recorder_fresh", recorder_ok, f"universe={int(recorder.get('universe_size') or 0)}, rows={int(recorder.get('rows_this_snapshot') or 0)}, age={recorder_age:.0f}s, paper_only={recorder_safe}"))

    observers = {
        "exact_ask_bid_observer": ("full_universe_rank_paper/status.json", "recording_exact_ask_to_bid_paper_outcomes", None, None),
        "exit_contract_matrix": ("full_universe_rank_contract_matrix/status.json", "recording_exact_exit_contract_matrix", "contracts", 9),
        "entry_timing_matrix": ("full_universe_rank_entry_timing/status.json", "recording_exact_entry_timing_matrix", "variants", 6),
    }
    for name, (relative, expected_status, count_key, expected_count) in observers.items():
        status = load_json(data / relative)
        updated = parse_ist(status.get("updated_at"))
        age = (now - updated).total_seconds() if updated else float("inf")
        safe = bool(status.get("paper_only")) and not bool(status.get("alerts_allowed")) and not bool(status.get("real_orders_allowed"))
        count_ok = count_key is None or int(status.get(count_key) or 0) == expected_count
        passed = status.get("status") == expected_status and str(status.get("date")) == now.strftime("%Y-%m-%d") and age <= 180 and safe and count_ok
        count_detail = f", {count_key}={status.get(count_key)}" if count_key else ""
        checks.append(check(name, passed, f"age={age:.0f}s, paper_only={safe}{count_detail}"))

    source_contracts = (root / "pkscreener/classes/FullUniverseRankContractMatrixObserver.py").read_text(errors="ignore")
    source_variants = (root / "pkscreener/classes/FullUniverseRankEntryTimingObserver.py").read_text(errors="ignore")
    source_gates = (root / "scripts/assess_full_rank_feature_gates.py").read_text(errors="ignore")
    source_selectors = (root / "scripts/assess_exact_full_rank_paper.py").read_text(errors="ignore")
    matrix_ok = (
        source_contracts.count('"t') >= 9
        and source_variants.count('"mode"') == 6
        and source_gates.count('": "') >= 9
        and source_selectors.count('": "') >= 4
    )
    checks.append(check("predeclared_search_space", matrix_ok, "9 exit contracts, 6 timing variants, 9 feature gates, 4 selectors declared" if matrix_ok else "one or more predeclared matrices are incomplete"))

    failures = [item for item in checks if item["critical"] and not item["passed"]]
    return {
        "status": "ready_for_next_full_session" if not failures else "operational_issues_detected",
        "checked_at": now.isoformat(timespec="seconds"),
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
