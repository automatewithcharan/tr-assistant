import sys
from pathlib import Path
from types import SimpleNamespace

from pkscreener.classes.AssistantLiveRuntime import (
    AssistantLiveSupervisor,
    _env_truthy,
)


class EventStore:
    def __init__(self):
        self.events = []

    def add_event(self, *args):
        self.events.append(args)


def test_research_and_dashboard_changes_do_not_restart_live_workers():
    paths = [
        "scripts/assess_full_rank_capital_allocation.py",
        "scripts/dashboard.py",
        "tests/test_dashboard_evidence.py",
        "docs/intraday_daily_goal_progress.md",
    ]
    assert not AssistantLiveSupervisor._requires_live_worker_restart(paths)


def test_live_package_or_dependency_changes_restart_live_workers():
    for path in (
        "pkscreener/classes/SignalMonitor.py",
        "pkscreener/pkscreenercli.py",
        "requirements.txt",
        "pkscreener.ini",
    ):
        assert AssistantLiveSupervisor._requires_live_worker_restart([path])


def test_unknown_diff_restarts_conservatively():
    assert AssistantLiveSupervisor._requires_live_worker_restart(None)


def test_non_live_commit_advances_head_without_stopping_children():
    supervisor = object.__new__(AssistantLiveSupervisor)
    supervisor._code_commit = "a" * 40
    supervisor.session_id = 1
    supervisor.store = EventStore()
    supervisor._read_git_head = lambda: "b" * 40
    supervisor._changed_paths = lambda previous, current: ["scripts/dashboard.py"]
    supervisor._stop_children = lambda: (_ for _ in ()).throw(
        AssertionError("children were stopped")
    )

    supervisor._check_for_code_update()

    assert supervisor._code_commit == "b" * 40
    assert "kept running" in supervisor.store.events[-1][-1]


def test_standalone_evidence_command_uses_runtime_python_and_poll(monkeypatch):
    supervisor = object.__new__(AssistantLiveSupervisor)
    supervisor.repo_root = Path("/repo")
    supervisor.settings = SimpleNamespace(universe="all")
    monkeypatch.setenv(
        "SIGNALMONITOR_STANDALONE_EVIDENCE_POLL_SECONDS",
        "3",
    )

    command = supervisor._evidence_command()

    assert command == [
        sys.executable,
        "/repo/scripts/run_full_rank_evidence_recorder.py",
        "--universe",
        "all",
        "--poll-seconds",
        "3.0",
    ]


def test_runtime_truthy_parser(monkeypatch):
    monkeypatch.setenv("SIGNALMONITOR_STANDALONE_EVIDENCE", "yes")
    assert _env_truthy("SIGNALMONITOR_STANDALONE_EVIDENCE") is True
    monkeypatch.setenv("SIGNALMONITOR_STANDALONE_EVIDENCE", "0")
    assert _env_truthy("SIGNALMONITOR_STANDALONE_EVIDENCE") is False
