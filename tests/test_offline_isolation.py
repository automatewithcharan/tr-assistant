"""Subprocess and runtime-boundary regressions for offline test execution."""

from __future__ import annotations

import multiprocessing
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from pkscreener.offline_test_guard import OfflineNetworkAttempt, assert_runtime_path


def test_subprocess_sanitizes_credentials_and_blocks_network(offline_subprocess_env, tmp_path):
    code = """
import os, socket, requests
from pkscreener.classes.KiteOrderManager import _get_secret
assert _get_secret('DISCORD_WEBHOOK_URL', '') == ''
assert _get_secret('KITE_ACCESS_TOKEN', '') == ''
for name, call in [('socket', lambda: socket.create_connection(('example.invalid', 443))), ('requests', lambda: requests.post('https://example.invalid'))]:
    try:
        call()
    except BaseException as exc:
        print(name, type(exc).__name__)
    else:
        raise AssertionError(name + ' was not blocked')
"""
    env = offline_subprocess_env({
        "DISCORD_WEBHOOK_URL": "https://production.invalid/webhook",
        "KITE_ACCESS_TOKEN": "pretend-real-token",
    })
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "socket OfflineNetworkAttempt" in result.stdout
    assert "requests OfflineNetworkAttempt" in result.stdout


def test_sibling_runtime_paths_are_rejected(tmp_path):
    sibling_path = tmp_path.parent / "sibling-runtime" / "kite_tracked_positions.json"
    with pytest.raises(RuntimeError, match="outside PKSCREENER_TEST_ROOT"):
        assert_runtime_path(sibling_path)
    assert assert_runtime_path(tmp_path / "runtime.json").parent == tmp_path


def test_socket_escape_exception_bypasses_except_exception_and_teardown_runs(tmp_path):
    marker = tmp_path / "teardown-marker"
    try:
        try:
            socket.create_connection(("example.invalid", 443))
        except Exception:
            pytest.fail("OfflineNetworkAttempt must not be swallowed by except Exception")
    except OfflineNetworkAttempt:
        marker.write_text("handled by test", encoding="utf-8")
    assert marker.exists()


def _child_exit() -> None:
    return


def test_descendant_process_exits_cleanly():
    child = multiprocessing.Process(target=_child_exit)
    child.start()
    child.join(timeout=5)
    assert child.exitcode == 0 and not child.is_alive()


def test_offline_exception_is_not_process_control_flow():
    assert not issubclass(OfflineNetworkAttempt, (KeyboardInterrupt, SystemExit, GeneratorExit))
    assert os.environ.get("PKSCREENER_TEST_OFFLINE") == "1"


def test_numpy_style_lscpu_probe_is_trusted_local_read_only(tmp_path):
    result = subprocess.run("lscpu", capture_output=True, text=True)
    assert result.returncode == 0
    assert "Architecture:" in result.stdout
    assert not (tmp_path / "offline_network_attempts.jsonl").exists()


def test_numpy_and_scipy_import_under_offline_boundary():
    import numpy  # noqa: F401
    import scipy  # noqa: F401


def test_joblib_deterministic_cpu_limit_avoids_physical_core_subprocess(monkeypatch):
    import joblib
    import joblib.externals.loky.backend.context as loky_context

    calls = []
    original = subprocess.Popen

    def observe(args, *popen_args, **popen_kwargs):
        calls.append(args)
        assert not (isinstance(args, (list, tuple)) and args and args[0] == "cat")
        return original(args, *popen_args, **popen_kwargs)

    monkeypatch.setattr(subprocess, "Popen", observe)
    loky_context.physical_cores_cache = None
    assert joblib.cpu_count(only_physical_cores=True) == 1
    assert calls == []


@pytest.mark.parametrize("args", [
    ["nc", "127.0.0.1", "1"], ["curl", "https://example.invalid"],
    ["wget", "https://example.invalid"], ["rsync", "x", "y"],
    ["ssh", "example.invalid"], ["/tmp/unknown-offline-tool"],
])
def test_non_allowlisted_subprocesses_are_denied(args):
    with pytest.raises(OfflineNetworkAttempt):
        subprocess.Popen(args)


def test_lscpu_variants_are_denied(tmp_path):
    fake = tmp_path / "lscpu"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o700)
    with pytest.raises(OfflineNetworkAttempt):
        subprocess.Popen([str(fake)])
    with pytest.raises(OfflineNetworkAttempt):
        subprocess.Popen("lscpu", shell=True)
    with pytest.raises(OfflineNetworkAttempt):
        subprocess.Popen("lscpu --json")
