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


class RunningEventStore(EventStore):
    def mark_session_running(self, *_args):
        return None

    def get_session(self, _session_id):
        return {"status": "stopped"}


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


def _code_update_supervisor(*, enabled, changed_paths=None):
    supervisor = object.__new__(AssistantLiveSupervisor)
    supervisor._code_commit = "a" * 40
    supervisor._code_update_restart_enabled = enabled
    supervisor.session_id = 1
    supervisor.store = EventStore()
    supervisor.repo_root = Path("/repo")
    supervisor._read_git_head = lambda: "b" * 40
    supervisor._changed_paths = lambda previous, current: (
        ["pkscreener/classes/SignalMonitor.py"]
        if changed_paths is None
        else changed_paths
    )
    supervisor._stop_children = lambda: None
    supervisor._start_required_processes = lambda: None
    return supervisor


def test_code_update_restart_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ASSISTANT_ENABLE_CODE_UPDATE_RESTART", raising=False)
    assert _env_truthy("ASSISTANT_ENABLE_CODE_UPDATE_RESTART", "0") is False


def test_startup_log_states_code_update_restart_mode():
    supervisor = object.__new__(AssistantLiveSupervisor)
    supervisor.session_id = 1
    supervisor.store = RunningEventStore()
    supervisor.settings = SimpleNamespace(prewarm=False, universe="all")
    supervisor.stop_requested = True
    supervisor._code_update_restart_enabled = False
    supervisor._install_signal_handlers = lambda: None
    supervisor._start_required_processes = lambda: None
    supervisor._stop_children = lambda: None

    supervisor.run()

    messages = [event[-1] for event in supervisor.store.events]
    assert "Code-update auto-restart: disabled" in messages


def test_live_head_change_disabled_keeps_children_and_does_not_exec(monkeypatch):
    supervisor = _code_update_supervisor(enabled=False)
    stopped = []
    exec_calls = []
    supervisor._stop_children = lambda: stopped.append(True)
    monkeypatch.setattr("os.execv", lambda *args: exec_calls.append(args))

    supervisor._check_for_code_update()

    assert stopped == []
    assert exec_calls == []
    assert supervisor._code_commit == "b" * 40
    assert "auto-restart disabled" in supervisor.store.events[-1][-1]
    assert "workers kept running" in supervisor.store.events[-1][-1]


def test_live_head_change_enabled_preserves_coordinated_restart(monkeypatch):
    supervisor = _code_update_supervisor(enabled=True)
    actions = []
    supervisor._stop_children = lambda: actions.append("stop")

    def record_exec(*_args):
        actions.append("execv")
        raise OSError("simulated exec failure")

    monkeypatch.setattr("os.execv", record_exec)
    monkeypatch.setattr(
        "pkscreener.classes.AssistantLiveRuntime._runtime_env",
        lambda _repo_root: {},
    )
    supervisor._start_required_processes = lambda: actions.append("fallback-start")

    supervisor._check_for_code_update()

    assert actions == ["stop", "execv", "fallback-start"]
    assert supervisor._code_commit == "b" * 40
    assert "Restarting all workers + supervisor" in supervisor.store.events[-1][-1]


def test_non_live_change_remains_non_restarting_when_opted_in(monkeypatch):
    supervisor = _code_update_supervisor(
        enabled=True,
        changed_paths=["scripts/dashboard.py"],
    )
    stopped = []
    exec_calls = []
    supervisor._stop_children = lambda: stopped.append(True)
    monkeypatch.setattr("os.execv", lambda *args: exec_calls.append(args))

    supervisor._check_for_code_update()

    assert stopped == []
    assert exec_calls == []
    assert supervisor._code_commit == "b" * 40
    assert "Non-live code updated" in supervisor.store.events[-1][-1]


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
