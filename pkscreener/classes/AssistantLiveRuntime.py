"""
Simple local supervisor for the live trading assistant workflow.

This runtime keeps the user-facing flow intentionally small:
- one start command
- one stop command
- one status command

Under the hood it launches and watches the three long-running pieces:
- official Kite ticker stream
- live signal monitor
- Discord trade bot

It stores runtime state in SQLite so the current session, process ids,
restart counts, and recent events survive restarts and are easy to inspect.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PKDevTools.classes import Archiver
from PKDevTools.classes.OutputControls import OutputControls


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _runtime_env(repo_root: Path) -> Dict[str, str]:
    """Return an environment that can import this checkout from detached launches."""
    env = os.environ.copy()
    repo_path = str(repo_root)
    existing_pythonpath = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = (
        repo_path
        if not existing_pythonpath
        else f"{repo_path}{os.pathsep}{existing_pythonpath}"
    )
    return env


@dataclass
class AssistantLiveSettings:
    universe: str = "nifty50"
    account_name: str = "main"
    budget_amount: float = 10000.0
    risk_percent: float = 1.0
    min_confidence: float = 40.0
    min_trade_value: float = 1000000.0
    min_volume_ratio: float = 0.5
    max_workers: int = 6
    signal_interval: int = 30
    signal_limit: int = 0
    alert_channels: str = "discord"
    ticker_limit: int = 0
    ticker_save_interval: float = 0.5
    prewarm: bool = False
    prewarm_workers: int = 4
    strong_buy_only: bool = False
    max_daily_loss_amount: float = 0.0
    max_consecutive_losses: int = 0
    autopilot_enabled: bool = False
    autopilot_interval: int = 60

    def normalized(self) -> "AssistantLiveSettings":
        return AssistantLiveSettings(
            universe=str(self.universe or "nifty50").strip().lower(),
            account_name=str(self.account_name or "main").strip().lower().replace(" ", "_"),
            budget_amount=float(self.budget_amount or 10000.0),
            risk_percent=float(self.risk_percent or 1.0),
            min_confidence=float(self.min_confidence or 40.0),
            min_trade_value=float(self.min_trade_value or 1000000.0),
            min_volume_ratio=float(self.min_volume_ratio or 1.0),
            max_workers=max(1, int(self.max_workers or 1)),
            signal_interval=max(10, int(self.signal_interval or 30)),
            signal_limit=max(0, int(self.signal_limit or 0)),
            alert_channels=str(self.alert_channels or "discord").strip().lower(),
            ticker_limit=max(0, int(self.ticker_limit or 0)),
            ticker_save_interval=max(0.2, float(self.ticker_save_interval or 0.5)),
            prewarm=bool(self.prewarm),
            prewarm_workers=max(1, int(self.prewarm_workers or 1)),
            strong_buy_only=bool(self.strong_buy_only),
            max_daily_loss_amount=max(0.0, float(self.max_daily_loss_amount or 0.0)),
            max_consecutive_losses=max(0, int(self.max_consecutive_losses or 0)),
            autopilot_enabled=bool(self.autopilot_enabled),
            autopilot_interval=max(10, int(self.autopilot_interval or 60)),
        )


class AssistantRuntimeStore:
    """SQLite-backed runtime state for the local live supervisor."""

    def __init__(self) -> None:
        self.output_dir = Path(Archiver.get_user_data_dir())
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.output_dir / "assistant_logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.output_dir / "assistant_runtime.db"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path))
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    stopped_at TEXT,
                    last_heartbeat TEXT,
                    supervisor_pid INTEGER,
                    universe TEXT,
                    account_name TEXT,
                    budget_amount REAL,
                    risk_percent REAL,
                    min_confidence REAL,
                    min_trade_value REAL,
                    min_volume_ratio REAL,
                    max_workers INTEGER,
                    signal_interval INTEGER,
                    signal_limit INTEGER,
                    alert_channels TEXT,
                    ticker_limit INTEGER,
                    ticker_save_interval REAL,
                    prewarm INTEGER,
                    prewarm_workers INTEGER,
                    settings_json TEXT,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS processes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    pid INTEGER,
                    status TEXT NOT NULL,
                    command TEXT,
                    log_path TEXT,
                    started_at TEXT,
                    stopped_at TEXT,
                    restart_count INTEGER DEFAULT 0,
                    last_exit_code INTEGER,
                    last_seen TEXT,
                    UNIQUE(session_id, name)
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id INTEGER,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL
                );
                """
            )

    def create_session(self, settings: AssistantLiveSettings) -> int:
        payload = asdict(settings.normalized())
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO sessions (
                    status, started_at, universe, account_name, budget_amount,
                    risk_percent, min_confidence, min_trade_value, min_volume_ratio,
                    max_workers, signal_interval, signal_limit, alert_channels,
                    ticker_limit, ticker_save_interval, prewarm, prewarm_workers,
                    settings_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "starting",
                    _now_str(),
                    payload["universe"],
                    payload["account_name"],
                    payload["budget_amount"],
                    payload["risk_percent"],
                    payload["min_confidence"],
                    payload["min_trade_value"],
                    payload["min_volume_ratio"],
                    payload["max_workers"],
                    payload["signal_interval"],
                    payload["signal_limit"],
                    payload["alert_channels"],
                    payload["ticker_limit"],
                    payload["ticker_save_interval"],
                    1 if payload["prewarm"] else 0,
                    payload["prewarm_workers"],
                    json.dumps(payload),
                ),
            )
            session_id = int(cursor.lastrowid)
        self.add_event(session_id, "info", "session", "Created live session.")
        return session_id

    def get_session(self, session_id: int) -> Optional[Dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?",
                (int(session_id),),
            ).fetchone()
        return dict(row) if row else None

    def get_active_session(self) -> Optional[Dict]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM sessions
                WHERE status IN ('starting', 'running', 'stopping')
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        return dict(row) if row else None

    def get_latest_session(self) -> Optional[Dict]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def mark_session_running(self, session_id: int, supervisor_pid: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, supervisor_pid = ?, last_heartbeat = ?
                WHERE id = ?
                """,
                ("running", int(supervisor_pid), _now_str(), int(session_id)),
            )
        self.add_event(session_id, "info", "supervisor", f"Supervisor started with pid {supervisor_pid}.")

    def heartbeat(self, session_id: int, note: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET last_heartbeat = ?, notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                (_now_str(), note or None, int(session_id)),
            )

    def mark_session_stopped(self, session_id: int, note: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, stopped_at = ?, last_heartbeat = ?, notes = COALESCE(?, notes)
                WHERE id = ?
                """,
                ("stopped", _now_str(), _now_str(), note or None, int(session_id)),
            )
        if note:
            self.add_event(session_id, "info", "session", note)

    def mark_session_failed(self, session_id: int, note: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE sessions
                SET status = ?, stopped_at = ?, last_heartbeat = ?, notes = ?
                WHERE id = ?
                """,
                ("failed", _now_str(), _now_str(), note, int(session_id)),
            )
        self.add_event(session_id, "error", "session", note)

    def upsert_process(
        self,
        session_id: int,
        name: str,
        pid: int,
        status: str,
        command: str,
        log_path: str,
        restart_count: int = 0,
        exit_code: Optional[int] = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO processes (
                    session_id, name, pid, status, command, log_path, started_at,
                    restart_count, last_exit_code, last_seen
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, name)
                DO UPDATE SET
                    pid = excluded.pid,
                    status = excluded.status,
                    command = excluded.command,
                    log_path = excluded.log_path,
                    started_at = COALESCE(processes.started_at, excluded.started_at),
                    stopped_at = NULL,
                    restart_count = excluded.restart_count,
                    last_exit_code = excluded.last_exit_code,
                    last_seen = excluded.last_seen
                """,
                (
                    int(session_id),
                    str(name),
                    int(pid),
                    str(status),
                    str(command),
                    str(log_path),
                    _now_str(),
                    int(restart_count),
                    exit_code,
                    _now_str(),
                ),
            )

    def mark_process_stopped(self, session_id: int, name: str, exit_code: Optional[int]) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE processes
                SET status = ?, stopped_at = ?, last_exit_code = ?, last_seen = ?
                WHERE session_id = ? AND name = ?
                """,
                ("stopped", _now_str(), exit_code, _now_str(), int(session_id), str(name)),
            )

    def list_processes(self, session_id: int) -> List[Dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM processes WHERE session_id = ? ORDER BY name",
                (int(session_id),),
            ).fetchall()
        return [dict(row) for row in rows]

    def add_event(self, session_id: Optional[int], level: str, source: str, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events (session_id, created_at, level, source, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(session_id) if session_id not in [None, ""] else None,
                    _now_str(),
                    str(level),
                    str(source),
                    str(message),
                ),
            )

    def recent_events(self, session_id: Optional[int], limit: int = 10) -> List[Dict]:
        query = "SELECT * FROM events WHERE session_id = ? ORDER BY id DESC LIMIT ?"
        params = (int(session_id), int(limit))
        if session_id is None:
            query = "SELECT * FROM events ORDER BY id DESC LIMIT ?"
            params = (int(limit),)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [dict(row) for row in rows]


class AssistantLiveSupervisor:
    """Background supervisor for live market worker processes."""

    def __init__(self, config_manager, session_id: int) -> None:
        self.config_manager = config_manager
        self.session_id = int(session_id)
        self.store = AssistantRuntimeStore()
        session = self.store.get_session(self.session_id)
        if session is None:
            raise RuntimeError(f"Live session {self.session_id} was not found.")
        payload = json.loads(session["settings_json"])
        self.settings = AssistantLiveSettings(**payload).normalized()
        self.repo_root = Path(__file__).resolve().parents[2]
        self.processes: Dict[str, Dict] = {}
        self.stop_requested = False
        self._code_commit: Optional[str] = self._read_git_head()
        self._code_check_counter: int = 0

    def run(self) -> None:
        self.store.mark_session_running(self.session_id, os.getpid())
        try:
            if self.settings.prewarm:
                self._run_prewarm()
            self._start_required_processes()
            self.store.add_event(self.session_id, "info", "supervisor", "Live assistant is running.")

            while not self.stop_requested:
                self._heartbeat()
                self._restart_dead_processes()
                self._code_check_counter += 1
                if self._code_check_counter >= 6:  # every ~30 s
                    self._code_check_counter = 0
                    self._check_for_code_update()
                time.sleep(5)
        except KeyboardInterrupt:
            self.store.add_event(self.session_id, "warn", "supervisor", "Supervisor interrupted.")
        except Exception as exc:
            self.store.mark_session_failed(self.session_id, f"Supervisor failed: {exc}")
            raise
        finally:
            self._stop_children()
            if (self.store.get_session(self.session_id) or {}).get("status") not in ["failed", "stopped"]:
                self.store.mark_session_stopped(self.session_id, "Live assistant stopped.")

    def _run_prewarm(self) -> None:
        command = [
            sys.executable,
            "-m",
            "pkscreener.pkscreenercli",
            "--kiteprewarm",
            self.settings.universe,
            "--kiteprewarmworkers",
            str(self.settings.prewarm_workers),
        ]
        self.store.add_event(self.session_id, "info", "prewarm", "Starting historical prewarm before live session.")
        subprocess.run(command, cwd=str(self.repo_root), env=_runtime_env(self.repo_root), check=False)

    def _start_required_processes(self) -> None:
        from pkscreener.classes.KiteConnectManager import KiteConnectSessionManager

        mgr = KiteConnectSessionManager()
        report = mgr.ensure_fresh_session()
        if report.get("error"):
            raise RuntimeError(
                "Official Kite session is not usable for live mode. "
                f"Fix this first: {report.get('error')}"
            )
        self._launch_process("ticker", self._ticker_command())
        self._launch_process("monitor", self._monitor_command())
        self._launch_process("discord", self._discord_command())
        if self.settings.autopilot_enabled:
            self._launch_process("autopilot", self._autopilot_command())

    def _ticker_command(self) -> List[str]:
        command = [
            sys.executable,
            "-m",
            "pkscreener.pkscreenercli",
            "--kitetickerstream",
            self.settings.universe,
            "--kitetickersaveinterval",
            str(self.settings.ticker_save_interval),
        ]
        if self.settings.ticker_limit > 0:
            command.extend(["--kitetickerlimit", str(self.settings.ticker_limit)])
        return command

    def _monitor_command(self) -> List[str]:
        command = [
            sys.executable,
            "-m",
            "pkscreener.pkscreenercli",
            "--signalmonitor",
            self.settings.universe,
            "--signalmonitorlivekite",
            "--signalmonitoralerts",
            # When autopilot is active, it handles Discord alerts with scoring.
            # Don't let the raw signal monitor also push to Discord (duplicates).
            "none" if self.settings.autopilot_enabled else self.settings.alert_channels,
            "--signalmonitorbudget",
            str(self.settings.budget_amount),
            "--signalmonitorriskpct",
            str(self.settings.risk_percent),
            "--signalmonitorminconfidence",
            str(self.settings.min_confidence),
            "--signalmonitorminliquidity",
            str(self.settings.min_trade_value),
            "--signalmonitorminvolratio",
            str(self.settings.min_volume_ratio),
            "--signalmonitorworkers",
            str(self.settings.max_workers),
            "-c",
            str(self.settings.signal_interval),
            "-a",
            "Y",
        ]
        # Always pass --signalmonitorlimit so 0 means "no limit" rather than defaulting to 50
        command.extend(["--signalmonitorlimit", str(max(0, int(self.settings.signal_limit or 0)))])
        if self.settings.strong_buy_only:
            command.append("--signalmonitorstrongbuyonly")
        if self.settings.account_name:
            command.extend(["--signalmonitoraccount", str(self.settings.account_name)])
        if self.settings.max_daily_loss_amount > 0:
            command.extend(["--signalmonitormaxdailyloss", str(self.settings.max_daily_loss_amount)])
        if self.settings.max_consecutive_losses > 0:
            command.extend(["--signalmonitormaxconsecutivelosses", str(self.settings.max_consecutive_losses)])
        return command

    def _discord_command(self) -> List[str]:
        return [
            sys.executable,
            "-m",
            "pkscreener.pkscreenercli",
            "--discordtradebot",
            "--discordtradeaccount",
            self.settings.account_name,
            "--discordtradeuniverse",
            self.settings.universe,
        ]

    def _autopilot_command(self) -> List[str]:
        command = [
            sys.executable,
            "-m",
            "pkscreener.pkscreenercli",
            "--autopilot",
            self.settings.universe,
            "--autopilotaccount",
            self.settings.account_name,
            "--autopilotminconfidence",
            str(self.settings.min_confidence),
            "--autopilotalerts",
            self.settings.alert_channels,
            "-c",
            str(self.settings.autopilot_interval),
            "-a",
            "Y",
        ]
        if self.settings.max_daily_loss_amount > 0:
            command.extend(["--autopilotmaxdailyloss", str(self.settings.max_daily_loss_amount)])
        if self.settings.max_consecutive_losses > 0:
            command.extend(["--autopilotmaxconsecutivelosses", str(self.settings.max_consecutive_losses)])
        return command

    def _launch_process(self, name: str, command: List[str], restart_count: int = 0) -> None:
        log_path = self.store.logs_dir / f"session_{self.session_id}_{name}.log"
        log_handle = open(log_path, "a", encoding="utf-8")
        log_handle.write(f"\n[{_now_str()}] starting {name}: {' '.join(command)}\n")
        log_handle.flush()

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        process = subprocess.Popen(
            command,
            cwd=str(self.repo_root),
            env=_runtime_env(self.repo_root),
            stdout=log_handle,
            stderr=log_handle,
            creationflags=creationflags,
        )
        self.processes[name] = {
            "process": process,
            "log_handle": log_handle,
            "command": command,
            "restart_count": restart_count,
            "log_path": str(log_path),
        }
        self.store.upsert_process(
            self.session_id,
            name,
            process.pid,
            "running",
            " ".join(command),
            str(log_path),
            restart_count=restart_count,
        )
        self.store.add_event(
            self.session_id,
            "info",
            name,
            f"Started {name} with pid {process.pid}.",
        )

    def _heartbeat(self) -> None:
        self.store.heartbeat(self.session_id, note=f"Universe={self.settings.universe}")
        for name, metadata in self.processes.items():
            process = metadata["process"]
            status = "running" if process.poll() is None else "stopped"
            self.store.upsert_process(
                self.session_id,
                name,
                process.pid,
                status,
                " ".join(metadata["command"]),
                metadata["log_path"],
                restart_count=metadata["restart_count"],
                exit_code=process.poll(),
            )

    def _read_git_head(self) -> Optional[str]:
        """Return the current git HEAD commit hash, or None if not in a git repo."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(self.repo_root),
                capture_output=True,
                text=True,
                timeout=5,
            )
            return result.stdout.strip() if result.returncode == 0 else None
        except Exception:
            return None

    def _check_for_code_update(self) -> None:
        """Restart all child processes when a new git commit is detected.

        Also re-execs the supervisor itself so it picks up the new code too.
        This solves the chicken-and-egg problem: the old supervisor can't run
        new-code features until it restarts itself.
        """
        current = self._read_git_head()
        if current and current != self._code_commit:
            prev_short = self._code_commit[:7] if self._code_commit else "unknown"
            self.store.add_event(
                self.session_id,
                "info",
                "supervisor",
                f"Code updated ({prev_short} → {current[:7]}). Restarting all workers + supervisor.",
            )
            # Stop all children gracefully first
            self._stop_children()
            # Re-exec the supervisor process itself with the same arguments.
            # os.execv replaces this process in-place — the PID changes but
            # systemd/watchdog won't notice because it's a clean exec.
            try:
                os.environ.update(_runtime_env(self.repo_root))
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception:
                # Fallback: just restart children with new code
                self._code_commit = current
                self._start_required_processes()

    def _restart_dead_processes(self) -> None:
        for name, metadata in list(self.processes.items()):
            process = metadata["process"]
            exit_code = process.poll()
            if exit_code is None:
                continue

            metadata["log_handle"].write(f"[{_now_str()}] {name} exited with code {exit_code}\n")
            metadata["log_handle"].flush()
            metadata["log_handle"].close()
            self.store.mark_process_stopped(self.session_id, name, exit_code)
            self.store.add_event(
                self.session_id,
                "warn",
                name,
                f"{name} exited with code {exit_code}. Restarting...",
            )
            restart_count = int(metadata.get("restart_count", 0)) + 1
            time.sleep(2)
            self._launch_process(name, metadata["command"], restart_count=restart_count)

    def _stop_children(self) -> None:
        for name, metadata in self.processes.items():
            process = metadata["process"]
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
            except Exception:
                pass
            try:
                metadata["log_handle"].write(f"[{_now_str()}] stopping {name}\n")
                metadata["log_handle"].flush()
                metadata["log_handle"].close()
            except Exception:
                pass
            self.store.mark_process_stopped(self.session_id, name, process.poll())


class AssistantLiveLauncher:
    """User-facing helpers for start/stop/status commands."""

    def __init__(self) -> None:
        self.store = AssistantRuntimeStore()
        self.repo_root = Path(__file__).resolve().parents[2]

    def _is_pid_running(self, pid: Optional[int]) -> bool:
        pid = _safe_int(pid)
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                output = str(result.stdout or "").strip()
                if not output or "No tasks are running" in output or "INFO:" in output:
                    return False
                return f'"{pid}"' in output or f",{pid}," in output or output.startswith('"python')
            except Exception:
                return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        except Exception:
            return True
        return True

    def _stop_pid_tree(self, pid: int) -> None:
        if pid <= 0:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
            )
            return
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass

    def start(self, settings: AssistantLiveSettings) -> int:
        settings = settings.normalized()
        active = self.store.get_active_session()
        if active:
            active_pid = _safe_int(active.get("supervisor_pid"))
            if self._is_pid_running(active_pid):
                raise RuntimeError(
                    f"A live session is already running for {active.get('universe')} (session {active.get('id')}, pid {active_pid}). "
                    "Stop it first with --assistantstop."
                )
            self.store.mark_session_failed(
                int(active["id"]),
                "Marked as failed because the supervisor process was no longer running.",
            )

        session_id = self.store.create_session(settings)
        command = [
            sys.executable,
            "-m",
            "pkscreener.pkscreenercli",
            "--assistantinternalsupervisor",
            "--assistantsessionid",
            str(session_id),
        ]
        supervisor_log = self.store.logs_dir / f"session_{session_id}_supervisor.log"
        with open(supervisor_log, "a", encoding="utf-8") as handle:
            handle.write(f"\n[{_now_str()}] launching supervisor: {' '.join(command)}\n")
            handle.flush()

        creationflags = 0
        if os.name == "nt":
            creationflags = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )

        log_handle = open(supervisor_log, "a", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=str(self.repo_root),
            env=_runtime_env(self.repo_root),
            stdout=log_handle,
            stderr=log_handle,
            creationflags=creationflags,
            start_new_session=(os.name != "nt"),
        )
        time.sleep(2)
        if process.poll() is not None:
            log_handle.close()
            self.store.mark_session_failed(session_id, "Supervisor exited immediately. Check the supervisor log.")
            raise RuntimeError(f"Could not start the live supervisor. Check {supervisor_log}")

        self.store.mark_session_running(session_id, process.pid)
        self.store.upsert_process(
            session_id,
            "supervisor",
            process.pid,
            "running",
            " ".join(command),
            str(supervisor_log),
        )
        self.store.add_event(session_id, "info", "launcher", "Detached live supervisor launched.")
        return session_id

    def stop(self) -> Optional[int]:
        active = self.store.get_active_session()
        if not active:
            return None
        session_id = int(active["id"])
        supervisor_pid = _safe_int(active.get("supervisor_pid"))
        self.store.add_event(session_id, "info", "launcher", "Stop requested by user.")
        self._stop_pid_tree(supervisor_pid)
        time.sleep(2)
        for process in self.store.list_processes(session_id):
            self.store.mark_process_stopped(session_id, process["name"], process.get("last_exit_code"))
        self.store.mark_session_stopped(session_id, "Stop requested by user.")
        return session_id

    def status_report(self) -> Dict:
        session = self.store.get_active_session() or self.store.get_latest_session()
        if not session:
            return {"status": "idle", "message": "No live session has been started yet."}

        session_id = int(session["id"])
        supervisor_pid = _safe_int(session.get("supervisor_pid"))
        session["supervisor_running"] = self._is_pid_running(supervisor_pid)

        processes = self.store.list_processes(session_id)
        for process in processes:
            process["is_running_now"] = self._is_pid_running(process.get("pid"))

        events = self.store.recent_events(session_id, limit=8)
        return {
            "status": session.get("status"),
            "session": session,
            "processes": processes,
            "events": events,
            "db_path": str(self.store.db_path),
            "logs_dir": str(self.store.logs_dir),
        }

    def print_status(self) -> None:
        report = self.status_report()
        if report.get("status") == "idle":
            OutputControls().printOutput("  [+] No live session is running right now.")
            OutputControls().printOutput(f"  [+] Runtime DB: {report.get('db_path', self.store.db_path)}")
            return

        session = report["session"]
        OutputControls().printOutput("  [+] Live assistant status")
        OutputControls().printOutput(
            f"  [+] Session {session['id']} | Status: {session['status']} | Universe: {session['universe']} | Account: {session['account_name']}"
        )
        OutputControls().printOutput(
            f"  [+] Started: {session.get('started_at', '')} | Last heartbeat: {session.get('last_heartbeat', '')}"
        )
        OutputControls().printOutput(
            f"  [+] Supervisor PID: {session.get('supervisor_pid', '')} | Running now: {'yes' if session.get('supervisor_running') else 'no'}"
        )
        OutputControls().printOutput(
            f"  [+] Alerts: {session.get('alert_channels', '')} | Budget: {session.get('budget_amount', 0)} | Risk %: {session.get('risk_percent', 0)}"
        )
        OutputControls().printOutput(f"  [+] Logs: {report['logs_dir']}")
        OutputControls().printOutput(f"  [+] Runtime DB: {report['db_path']}")

        processes = report.get("processes", [])
        if processes:
            OutputControls().printOutput("  [+] Processes")
            for process in processes:
                OutputControls().printOutput(
                    f"     - {process['name']}: pid={process.get('pid', '')}, status={process.get('status', '')}, running_now={'yes' if process.get('is_running_now') else 'no'}, restarts={process.get('restart_count', 0)}"
                )
                OutputControls().printOutput(f"       log: {process.get('log_path', '')}")

        events = report.get("events", [])
        if events:
            OutputControls().printOutput("  [+] Recent events")
            for event in reversed(events):
                OutputControls().printOutput(
                    f"     - [{event['created_at']}] {event['source']} ({event['level']}): {event['message']}"
                )
